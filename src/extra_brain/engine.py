from __future__ import annotations

import random
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from .config import MemoryConfig
from .consolidation import ConsolidationReport, Consolidator
from .embeddings import EmbeddingProvider, LocalHashingEmbedder, MemoryIndexer
from .extraction import (
    ConversationMessage,
    ExtractionError,
    MemoryExtractor,
    OpenAICompatibleExtractor,
    format_conversation,
)
from .models import (
    CoreMemory,
    MemoryAssociation,
    Memory,
    NarrativeLayer,
    ProactiveConfig,
    RelationshipModel,
    SemanticMemory,
    EpisodicMemory,
    UserSchema,
    is_unresolved_episode,
    new_memory_id,
    parse_iso8601,
    to_iso8601,
    utc_now,
)
from .retrieval import Injection, MemoryRetriever, RetrievalResult
from .storage import MemoryStore, WriteBatch


class ExtraBrain:
    """Public facade for storage, consolidation, retrieval, and prompt injection.

    ``companion_db`` 可选：指向一个**全局伴侣库**（persona/称呼/说话风格/跨场景稳定事实）。
    每个 peer 仍保留自己的情景库（事件、场景专属记忆），但检索与注入会把两层合并——
    私聊里定下的人设和偏好，群聊里同样「记得」，像真人一样不分窗口。
    """

    def __init__(
        self,
        db_path: str | Path = "extra_brain.db",
        *,
        extractor: MemoryExtractor | None = None,
        embedder: EmbeddingProvider | None = None,
        config: MemoryConfig | None = None,
        read_only: bool = False,
        encryption_passphrase: str | None = None,
        companion_db: str | Path | None = None,
    ) -> None:
        self.config = config or MemoryConfig()
        self.read_only = bool(read_only)
        self.embedder = embedder or LocalHashingEmbedder(self.config.embedding_dimensions)
        self.store = MemoryStore(
            db_path,
            core_token_limit=self.config.core_token_limit,
            encryption_passphrase=encryption_passphrase,
        )
        self.indexer = MemoryIndexer(self.embedder)
        self.retriever = MemoryRetriever(self.store, self.embedder, self.config)
        self.extractor = extractor
        self._consolidator = (
            Consolidator(
                self.store,
                extractor,
                indexer=self.indexer,
                config=self.config,
            )
            if extractor is not None
            else None
        )
        # 全局伴侣层：复用同一 embedder/config；不带 extractor（情景巩固只写 peer 库）。
        # 自引用防护：companion 与自身同一路径时不挂载（否则无限递归），
        # 这同时允许把 _global 当普通 peer 直接 consolidate 全局层。
        self.companion: ExtraBrain | None = None
        if companion_db is not None and str(Path(companion_db)) != str(Path(db_path)):
            self.companion = ExtraBrain(
                companion_db,
                embedder=self.embedder,
                config=self.config,
                read_only=self.read_only,
                encryption_passphrase=encryption_passphrase,
            )

    @classmethod
    def from_env(
        cls,
        db_path: str | Path = "extra_brain.db",
        *,
        config: MemoryConfig | None = None,
    ) -> ExtraBrain:
        return cls(db_path, extractor=OpenAICompatibleExtractor.from_env(), config=config)

    @property
    def core(self) -> CoreMemory:
        return self.store.get_core()

    def save_core(self, core: CoreMemory) -> None:
        self.store.save_core(core)

    def storage_stats(self) -> dict[str, Any]:
        """返回数据库分层统计（保留度档位、归档比例等）。"""
        return self.store.storage_stats()

    def forget(self, memory_id: str, *, hard: bool = False, approved: bool = False) -> dict[str, Any]:
        """v0.5.0 遗忘链路：默认归档+墓碑（P1），hard=True 且 approved=True 才物理删除（P0）。"""
        if self.read_only:
            raise PermissionError("ExtraBrain is in read_only mode")
        if hard:
            return self.store.hard_delete(memory_id, approved=approved)
        return self.store.tombstone(memory_id)

    def consolidate(
        self,
        conversation: str | Sequence[ConversationMessage | Mapping[str, str]],
        *,
        session_id: str | None = None,
        now: datetime | None = None,
    ) -> ConsolidationReport:
        if self.read_only:
            raise PermissionError("ExtraBrain is in read_only mode; consolidation is not allowed")
        if self._consolidator is None:
            raise ExtractionError(
                "no extractor configured; provide MemoryExtractor or use ExtraBrain.from_env()"
            )
        if not isinstance(conversation, str):
            conversation = format_conversation(conversation)
        return self._consolidator.consolidate(conversation, session_id=session_id, now=now)

    def _merged_core(self) -> CoreMemory:
        """合并两层 Core：全局层主导人设/画像，本层保留活跃线程（情景）。"""
        import copy

        if self.companion is None:
            return self.core
        base = copy.deepcopy(self.companion.core)
        peer = self.core
        base.active_threads = peer.active_threads
        base.protected_memories = list(
            dict.fromkeys([*base.protected_memories, *peer.protected_memories])
        )
        profile = base.user_stable_profile
        for field_name in ("key_traits", "core_preferences", "important_life_facts"):
            values = getattr(profile, field_name)
            values.extend(
                value
                for value in getattr(peer.user_stable_profile, field_name)
                if value not in values
            )
        return base

    def retrieve(
        self,
        query: str,
        *,
        recent_context: Sequence[str] = (),
        entities: Sequence[str] = (),
        now: datetime | None = None,
        as_of: str | None = None,
        path: str | None = None,
        primed_ids: Sequence[str] = (),
        views: Sequence[str] = (),
        include_companion: bool = True,
    ) -> RetrievalResult:
        own = self._retrieve_one(
            query,
            recent_context=recent_context,
            entities=entities,
            now=now,
            as_of=as_of,
            path=path,
            primed_ids=primed_ids,
            views=views,
        )
        if not include_companion or self.companion is None:
            return own
        shared = self.companion._retrieve_one(
            query,
            recent_context=recent_context,
            entities=entities,
            now=now,
            as_of=as_of,
            path=path,
            primed_ids=primed_ids,
            views=views,
        )
        semantic = sorted(
            [*shared.semantic, *own.semantic],
            key=lambda item: item.final_score,
            reverse=True,
        )[: self.config.semantic_top_k]
        episodic = sorted(
            [*shared.episodic, *own.episodic],
            key=lambda item: item.final_score,
            reverse=True,
        )[: self.config.episodic_top_k]
        merged = RetrievalResult(semantic=semantic, episodic=episodic)
        merged.path = f"{own.path}+companion" if shared.path == own.path else "companion"
        return merged

    def _retrieve_one(
        self,
        query: str,
        *,
        recent_context: Sequence[str] = (),
        entities: Sequence[str] = (),
        now: datetime | None = None,
        as_of: str | None = None,
        path: str | None = None,
        primed_ids: Sequence[str] = (),
        views: Sequence[str] = (),
    ) -> RetrievalResult:
        if path is not None:
            return self.retriever.search_four_paths(
                query,
                recent_context=recent_context,
                entities=entities,
                now=now,
                path=path,
                as_of=as_of,
                primed_ids=primed_ids,
                views=views,
            )
        return self.retriever.search(
            query,
            recent_context=recent_context,
            entities=entities,
            now=now,
            as_of=as_of,
            primed_ids=primed_ids,
            views=views,
        )

    def build_injection(
        self,
        query: str,
        *,
        recent_context: Sequence[str] = (),
        entities: Sequence[str] = (),
        token_limit: int | None = None,
        now: datetime | None = None,
        primed_ids: Sequence[str] = (),
        views: Sequence[str] = (),
        include_companion: bool = True,
    ) -> Injection:
        retrieved = self.retrieve(
            query,
            recent_context=recent_context,
            entities=entities,
            now=now,
            primed_ids=primed_ids,
            views=views,
            include_companion=include_companion,
        )
        core = self._merged_core() if include_companion else self.core
        injection = self.retriever.build_injection(
            core, retrieved, token_limit=token_limit
        )
        # Only memories that actually fit into the injected context count as activated.
        # 只读模式下跳过激活写入；激活按归属层拆分回写各自库。
        if not self.read_only:
            moment = now or utc_now()
            ids = injection.retrieved.memory_ids
            if self.companion is not None and include_companion:
                companion_ids = {
                    memory.id
                    for memory in (
                        *(item.memory for item in retrieved.semantic),
                        *(item.memory for item in retrieved.episodic),
                    )
                    if self.companion.store.get_memory(memory.id) is not None
                }
                peer_ids = [memory_id for memory_id in ids if memory_id not in companion_ids]
                if peer_ids:
                    self.store.record_activations(peer_ids, now=moment)
                if companion_ids:
                    self.companion.store.record_activations(
                        [memory_id for memory_id in ids if memory_id in companion_ids],
                        now=moment,
                    )
            else:
                self.store.record_activations(ids, now=moment)
        return injection

    def reindex(self) -> int:
        entries = {}
        for memory in self.store.list_semantic(include_archived=True):
            entries[memory.id] = self.indexer.entry_for(
                memory.id, "semantic", memory.embedding_text()
            )
        for memory in self.store.list_episodic(include_archived=True):
            entries[memory.id] = self.indexer.entry_for(
                memory.id, "episodic", memory.embedding_text()
            )
        self.store.apply_batch(
            WriteBatch(vectors=entries, operation="index.rebuild", audit_details={"count": len(entries)})
        )
        return len(entries)

    # --- v0.3.0 遗留：崩溃安全重建 ---

    def rebuild_from_log(self) -> dict[str, Any]:
        """从 operation_log 的 after 快照重建记忆表 + 向量派生层。

        晶格是派生层，坏了重放，不是数据丢失。整批重建在一个事务里完成。
        """
        if self.read_only:
            raise PermissionError("ExtraBrain is in read_only mode")
        restored = self.store.rebuild_from_log()
        restored["vectors_rebuilt"] = self.reindex()
        return restored

    def list_pending_consolidations(self) -> list[dict[str, Any]]:
        """崩溃后待重放的影子输入（pending/failed）。"""
        return self.store.list_pending_sessions()

    def replay_pending(self, *, limit: int = 10) -> list[dict[str, Any]]:
        """重放未完成的巩固输入（需要已配置 extractor）。成功的标 done，失败的记 note。"""
        if self.read_only:
            raise PermissionError("ExtraBrain is in read_only mode")
        if self._consolidator is None:
            raise ExtractionError("no extractor configured; cannot replay pending")
        results: list[dict[str, Any]] = []
        for entry in self.store.list_pending_sessions()[:limit]:
            try:
                report = self._consolidator.consolidate(
                    entry["conversation"],
                    session_id=entry["session_id"],
                    shadow_log_id=entry["id"],
                )
                results.append(
                    {"log_id": entry["id"], "ok": True, "report": report.to_dict()}
                )
            except Exception as exc:
                results.append({"log_id": entry["id"], "ok": False, "error": str(exc)[:200]})
        return results

    # --- v0.4.0 关系模型 ---

    def save_relationship(self, model: RelationshipModel) -> None:
        if self.read_only:
            raise PermissionError("ExtraBrain is in read_only mode")
        self.store.save_relationship(model)

    def get_relationship(self, model_id: str) -> RelationshipModel | None:
        return self.store.get_relationship(model_id)

    def get_relationship_by_user(self, user_id: str) -> RelationshipModel | None:
        return self.store.get_relationship_by_user(user_id)

    def list_relationships(self) -> list[RelationshipModel]:
        return self.store.list_relationships()

    # --- v0.4.0 用户图式 ---

    def save_user_schema(self, schema: UserSchema) -> None:
        if self.read_only:
            raise PermissionError("ExtraBrain is in read_only mode")
        self.store.save_user_schema(schema)

    def get_user_schema(self, schema_id: str) -> UserSchema | None:
        return self.store.get_user_schema(schema_id)

    def get_user_schema_by_user(self, user_id: str) -> UserSchema | None:
        return self.store.get_user_schema_by_user(user_id)

    def list_user_schemas(self) -> list[UserSchema]:
        return self.store.list_user_schemas()

    # --- v0.4.0 叙事层 ---

    def save_narrative(self, narrative: NarrativeLayer) -> None:
        if self.read_only:
            raise PermissionError("ExtraBrain is in read_only mode")
        self.store.save_narrative(narrative)

    def get_narrative(self, narrative_id: str) -> NarrativeLayer | None:
        return self.store.get_narrative(narrative_id)

    def get_narrative_by_user(self, user_id: str) -> NarrativeLayer | None:
        return self.store.get_narrative_by_user(user_id)

    def list_narratives(self) -> list[NarrativeLayer]:
        return self.store.list_narratives()

    # --- v0.4.0 记忆联想 ---

    def save_association(self, association: MemoryAssociation) -> None:
        if self.read_only:
            raise PermissionError("ExtraBrain is in read_only mode")
        self.store.save_association(association)

    def get_association(self, association_id: str) -> MemoryAssociation | None:
        return self.store.get_association(association_id)

    def list_associations_for_memory(self, memory_id: str) -> list[MemoryAssociation]:
        return self.store.list_associations_for_memory(memory_id)

    # --- v0.4.0 主动开口 ---

    def save_proactive_config(self, config: ProactiveConfig) -> None:
        if self.read_only:
            raise PermissionError("ExtraBrain is in read_only mode")
        self.store.save_proactive_config(config)

    def get_proactive_config(self, config_id: str) -> ProactiveConfig | None:
        return self.store.get_proactive_config(config_id)

    def get_proactive_config_by_user(self, user_id: str) -> ProactiveConfig | None:
        return self.store.get_proactive_config_by_user(user_id)

    def list_proactive_configs(self) -> list[ProactiveConfig]:
        return self.store.list_proactive_configs()

    def should_proactive_open(
        self, user_id: str, now: datetime | None = None, *, local_hour: int | None = None
    ) -> tuple[bool, str]:
        """v0.4.0 主动开口决策：是否应该主动向用户开口。

        返回 (should_open, reason)。规则：
        1. 未启用 → 不开口
        2. 安静时段 → 不开口
        3. 每日已达上限 → 不开口
        4. 距离上次开口未达最小间隔 → 不开口
        5. 静默未达阈值 → 不开口
        6. 否则 → 可开口，reason 说明由头

        ``local_hour``（0-23）显式指定用户本地小时——跨时区宿主用它覆盖
        墙钟判断，保证安静时段检查按用户当地时间执行。
        """
        now = now or utc_now()
        config = self.get_proactive_config_by_user(user_id)
        if config is None or not config.enabled:
            return False, "proactive_not_configured_or_disabled"

        # 安静时段检查
        if local_hour is not None and 0 <= int(local_hour) <= 23:
            hour = int(local_hour)
        else:
            hour = now.hour
        if config.quiet_hours_start > config.quiet_hours_end:
            # 跨午夜，如 22~8
            if hour >= config.quiet_hours_start or hour < config.quiet_hours_end:
                return False, "quiet_hours"
        else:
            if config.quiet_hours_start <= hour < config.quiet_hours_end:
                return False, "quiet_hours"

        # 每日重置
        today = now.date().isoformat()
        if config.daily_reset_date != today:
            config.daily_count = 0
            config.daily_reset_date = today

        # 每日上限
        if config.daily_count >= config.daily_limit:
            return False, "daily_limit_reached"

        # 最小间隔
        if config.last_proactive_at is not None:
            last = parse_iso8601(config.last_proactive_at)
            elapsed_min = (now - last).total_seconds() / 60
            if elapsed_min < config.min_interval_minutes:
                return False, "min_interval_not_reached"

        # 静默阈值：如果最近有对话，静默时间不足则不开口
        # 这里简化：如果 last_proactive_at 存在且静默时间不足，不开口
        if config.last_proactive_at is not None:
            last = parse_iso8601(config.last_proactive_at)
            silence_min = (now - last).total_seconds() / 60
            if silence_min < config.silence_threshold_minutes:
                return False, "silence_threshold_not_reached"

        return True, "eligible_for_proactive_open"

    def record_proactive_open(
        self, user_id: str, now: datetime | None = None, *, umo: str | None = None
    ) -> None:
        """记录一次主动开口，更新计数和时间戳；umo 存回复路由供下次投递。"""
        now = now or utc_now()
        config = self.get_proactive_config_by_user(user_id)
        if config is None:
            return
        today = now.date().isoformat()
        if config.daily_reset_date != today:
            config.daily_count = 0
            config.daily_reset_date = today
        config.daily_count += 1
        config.last_proactive_at = to_iso8601(now)
        if umo is not None:
            config.last_umo = umo
        self.save_proactive_config(config)

    def ensure_proactive_config(self, user_id: str) -> ProactiveConfig:
        """没有配置就按默认值建一份（宿主启用主动开口时的引导调用）。"""
        existing = self.get_proactive_config_by_user(user_id)
        if existing is not None:
            return existing
        config = ProactiveConfig(id=new_memory_id("pa"), user_id=user_id)
        self.save_proactive_config(config)
        return config

    def build_proactive_prompt(
        self,
        user_id: str,
        now: datetime | None = None,
        *,
        force: bool = False,
        local_hour: int | None = None,
    ) -> dict[str, Any]:
        """一步到位：决策 + 由头 + 检索记忆 → 交给宿主模型的开场提示词。

        返回 ``{"should": bool, "reason": str, "user_id": ...}``；
        ``should=True`` 时附带 ``prompt``（台词由模型自己说，库只给由头与素材）。
        ``force=True`` 跳过闸门直接给提示词（宿主测试指令用，仍照常记录）。
        只读安全：内部用 retrieve()，不写激活计数。
        注意：``now`` 应传宿主本地时区的 aware datetime（安静时段按本地小时判断）。
        """
        now = now or utc_now()
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        should, reason = self.should_proactive_open(user_id, now=now, local_hour=local_hour)
        if force:
            should, reason = True, "forced_test"
        base: dict[str, Any] = {"should": should, "reason": reason, "user_id": user_id}
        if not should:
            return base

        angle = random.choice(
            (
                "最近有什么让用户放不下的小事",
                "我们上次聊到哪里",
                "用户可能正在经历什么",
                "有什么值得惦记的日常",
            )
        )
        retrieved = self.retrieve(angle, now=now)
        blocks: list[str] = []
        for item in retrieved.semantic[:3]:
            memory = item.memory
            assert isinstance(memory, SemanticMemory)
            blocks.append(f"- {memory.subject} {memory.predicate} {memory.object}")
        for item in retrieved.episodic[:2]:
            memory = item.memory
            assert isinstance(memory, EpisodicMemory)
            marker = "（情绪还没平复，优先惦记）" if is_unresolved_episode(memory) else ""
            blocks.append(f"- {memory.timestamp[:10]} {memory.summary}{marker}")

        threads = self.core.active_threads[-2:]
        thread_text = "；".join(thread.summary for thread in threads) or "（无）"
        memory_text = "\n".join(blocks) or "（无特定素材，关心一下近况即可）"

        base["prompt"] = (
            "你要主动开启一段对话，像朋友一样自然地提起，不要解释你在做什么、"
            "不要提到记忆或系统。\n"
            f"由头：{reason}\n"
            f"当前正在进行的事：{thread_text}\n"
            f"可以参考的记忆（可选其一，也可以都不用）：\n{memory_text}\n"
            "要求：一句话，不超过40字，口语化、温暖、符合你的人设语气，"
            "不要用 Markdown、不要连续追问。"
        )
        return base

    def sync_persona_to_companion(self, *, keep_threads: bool = False) -> dict[str, Any]:
        """把本层 Core 的人设/称呼/画像推入全局伴侣层（私聊训练成果 → 全场景生效）。"""
        if self.read_only:
            raise PermissionError("ExtraBrain is in read_only mode")
        if self.companion is None:
            raise ValueError("no companion_db configured")
        import copy

        peer = self.core
        target = copy.deepcopy(self.companion.core)
        target.identity = copy.deepcopy(peer.identity)
        target.user_stable_profile = copy.deepcopy(peer.user_stable_profile)
        if keep_threads:
            target.active_threads = copy.deepcopy(peer.active_threads)
        target.last_updated = to_iso8601(utc_now())
        # 版本锚：push 前把当前快照记入人格锚历史（v0.2.0 rollback 链路复用）
        snapshot = {
            "version": f"push_{target.last_updated}",
            "at": target.last_updated,
            "persona_core": target.identity.persona_core,
            "from": str(self.store.path),
        }
        target.persona_anchor_history = [*target.persona_anchor_history[-9:], snapshot]
        target.persona_anchor_version = snapshot["version"]
        self.companion.save_core(target)
        return {"pushed_to": str(self.companion.store.path), "anchor": snapshot["version"]}

    def pull_persona_from_companion(self) -> dict[str, Any]:
        """从全局层拉取人设到本层（新 peer 自动继承），不动本层活跃线程。"""
        if self.read_only:
            raise PermissionError("ExtraBrain is in read_only mode")
        if self.companion is None:
            raise ValueError("no companion_db configured")
        import copy

        shared = self.companion.core
        local = copy.deepcopy(self.core)
        local.identity = copy.deepcopy(shared.identity)
        profile = local.user_stable_profile
        source = shared.user_stable_profile
        if not profile.preferred_name:
            profile.preferred_name = source.preferred_name
        for field_name in ("key_traits", "core_preferences", "important_life_facts"):
            values = getattr(profile, field_name)
            values.extend(v for v in getattr(source, field_name) if v not in values)
        local.last_updated = to_iso8601(utc_now())
        self.save_core(local)
        return {"pulled_from": str(self.companion.store.path)}

    def close(self) -> None:
        self.store.close()
        if self.companion is not None:
            self.companion.close()

    def __enter__(self) -> ExtraBrain:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
