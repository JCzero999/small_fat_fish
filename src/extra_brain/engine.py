from __future__ import annotations

import random
from datetime import datetime, timedelta
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
    DispositionSeed,
    MemoryAssociation,
    Memory,
    MemoryValidationError,
    NarrativeLayer,
    ProactiveConfig,
    RelationshipModel,
    SelfState,
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
from .goals import get_due_goals, mark_goal_done
from .self_state import count_unresolved, update_self_state
from .solidity import (
    SolidityConfig,
    apply_decay_and_freeze,
    apply_review,
    is_freeze_protected,
)
from .disposition import match_dispositions, reinforce_dispositions
from .storage import MemoryStore, WriteBatch


_HONESTY_LINE = (
    "【契约】下列为已检索记忆；未出现的经历不要当作共同回忆；工具结果以本轮返回为准。"
)


def _format_self_state_block(ss: SelfState, unresolved_count: int = 0) -> str:
    """把 SelfState 变成注入段落（v0.9.0: 含疲劳与未平复心结；v0.11.0: 目标带 deadline）。"""
    if not ss.updated_at:
        return ""  # 从未初始化，不输出
    if ss.energy >= 0.75:
        energy_label = "精力充沛"
    elif ss.energy >= 0.40:
        energy_label = "状态平稳"
    elif ss.energy >= 0.25:
        energy_label = "有点累了，回复可能会变短"
    else:
        energy_label = "很疲惫，只想简短回应"

    def goal_line(g) -> str:
        deadline = getattr(g, "deadline", None)
        if deadline:
            try:
                local = parse_iso8601(deadline).astimezone()
                return f"  · {g.text}（约定 {local:%m月%d日%H:%M}）"
            except (MemoryValidationError, ValueError, OSError):
                pass
        return f"  · {g.text}" if hasattr(g, "text") else f"  · {g}"

    goals_lines = "\n".join(goal_line(g) for g in ss.active_goals) if ss.active_goals else "  · （暂无特别惦记的事）"
    narrative = ss.self_narrative or "（尚无）"
    lines = [
        "【这一刻的自己】",
        f"精力：{ss.energy:.2f}（{energy_label}）",
        f"心情底色：{ss.mood_baseline}",
    ]
    if ss.fatigue_reason:
        lines.append(f"最近疲惫原因：{ss.fatigue_reason}")
    if unresolved_count > 0:
        lines.append(f"未平复的心结：{unresolved_count} 件（优先关心）")
    lines.append(f"我在意的事：\n{goals_lines}")
    lines.append(f"自我叙事：{narrative}")
    return "\n".join(lines)


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
        safety_rules: list[str] | None = None,
        shadow_mode: bool | None = None,
        solidity: SolidityConfig | None = None,
        honesty_footer: bool = False,
    ) -> None:
        self.config = config or MemoryConfig()
        self.read_only = bool(read_only)
        # v0.12.1 T4：诚实契约提示行（默认关，保证既有注入输出零变化）
        self._honesty_footer = bool(honesty_footer)
        # v0.12.0：牢固度配置（默认启用；enabled=False 时行为与 v0.11 完全一致）
        self.solidity = solidity or SolidityConfig()
        self.embedder = embedder or LocalHashingEmbedder(self.config.embedding_dimensions)
        self.store = MemoryStore(
            db_path,
            core_token_limit=self.config.core_token_limit,
            encryption_passphrase=encryption_passphrase,
        )
        # v0.11.0 A3：影子模式。库里存的一次开关优先于构造参数；
        # 都没有时默认 False（与 v0.10 注入行为完全一致，不破坏现有语义）。
        stored = self.store.get_setting("shadow_mode", None)
        if stored is None:
            self._shadow_mode = bool(shadow_mode) if shadow_mode is not None else False
        else:
            self._shadow_mode = bool(stored)
        self.indexer = MemoryIndexer(self.embedder)
        self.retriever = MemoryRetriever(self.store, self.embedder, self.config)
        self.extractor = extractor
        self._safety_rules: list[str] = [*safety_rules] if safety_rules else []
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
                solidity=self.solidity,
                honesty_footer=self._honesty_footer,
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

    # ── v0.13.2 主人身份卡（跨 peer/跨宿主识别） ──────────────────

    def set_owner_card(self, entries: Sequence[str]) -> dict[str, Any]:
        """把主人身份卡写入**全局伴侣层** Core（无 companion 时写本层）。

        Core 始终完整注入，身份卡因此在所有 peer（私聊/群聊/Shell）永远在场；
        配合宿主在群聊消息上标注发言者 QQ 号，模型即可认出主人。
        幂等：重复条目自动去重，内容不变则不写库。
        """
        clean = [str(e).strip() for e in entries if str(e).strip()]
        if not clean:
            raise ValueError("owner card entries must not be empty")
        target = self.companion if self.companion is not None else self
        core = target.core
        merged = list(dict.fromkeys([*core.user_stable_profile.owner_accounts, *clean]))
        if merged == list(core.user_stable_profile.owner_accounts):
            return {
                "owner_accounts": merged,
                "written": False,
                "written_to": "companion" if self.companion is not None else "peer",
            }
        core.user_stable_profile.owner_accounts = merged
        target.save_core(core)
        return {
            "owner_accounts": merged,
            "written": True,
            "written_to": "companion" if self.companion is not None else "peer",
        }

    def get_owner_card(self) -> list[str]:
        """读全局伴侣层（或本层）的当前主人身份卡。"""
        target = self.companion if self.companion is not None else self
        return list(target.core.user_stable_profile.owner_accounts)

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
        report = self._consolidator.consolidate(conversation, session_id=session_id, now=now)
        # v0.7.0 / v0.9.0: 巩固成功后更新自我状态（含疲劳/未平复/快照）
        if not self.read_only and report.changed:
            update_self_state(
                self.store, report, conversation_text=(
                    conversation if isinstance(conversation, str)
                    else format_conversation(conversation)
                ), now=now, _extractor=getattr(self, '_extractor', None)
            )
        # v0.8.0: 反熏倾向种子 — 从对话中检测认同/反对信号
        if not self.read_only and report.changed:
            reinforce_dispositions(self.store, conversation, now=now)
        # v0.12.0 B3.3：巩固末尾批量衰减（6h 节流；只读/关闭时自动跳过）
        if not self.read_only and report.changed:
            self.maybe_run_decay(now=now)
        return report

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
        semantic_top_k: int | None = None,
        episodic_top_k: int | None = None,
        exclude_frozen: bool = False,
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
            semantic_top_k=semantic_top_k,
            episodic_top_k=episodic_top_k,
            exclude_frozen=exclude_frozen,
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
            semantic_top_k=semantic_top_k,
            episodic_top_k=episodic_top_k,
            exclude_frozen=exclude_frozen,
        )
        sem_k = semantic_top_k if semantic_top_k is not None else self.config.semantic_top_k
        epi_k = episodic_top_k if episodic_top_k is not None else self.config.episodic_top_k
        semantic = sorted(
            [*shared.semantic, *own.semantic],
            key=lambda item: item.final_score,
            reverse=True,
        )[:sem_k]
        episodic = sorted(
            [*shared.episodic, *own.episodic],
            key=lambda item: item.final_score,
            reverse=True,
        )[:epi_k]
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
        semantic_top_k: int | None = None,
        episodic_top_k: int | None = None,
        exclude_frozen: bool = False,
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
                exclude_frozen=exclude_frozen,
            )
        return self.retriever.search(
            query,
            recent_context=recent_context,
            entities=entities,
            now=now,
            as_of=as_of,
            primed_ids=primed_ids,
            views=views,
            semantic_top_k=semantic_top_k,
            episodic_top_k=episodic_top_k,
            exclude_frozen=exclude_frozen,
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
        # v0.8.0: 倾向种子匹配 → boost 记忆权重 + 内在声音
        dm = match_dispositions(
            self.store, query,
            recent_context=list(recent_context) if recent_context else None,
        )
        disposition_boost = dm.boost_ids
        inner_voice = dm.inner_voice

        # 将 disposition 证据记忆合并到 primed_ids
        all_primed = list(primed_ids)
        for bid in disposition_boost:
            if bid not in all_primed:
                all_primed.append(bid)

        retrieved = self.retrieve(
            query,
            recent_context=recent_context,
            entities=entities,
            now=now,
            primed_ids=all_primed,
            views=views,
            include_companion=include_companion,
            exclude_frozen=self.solidity.enabled,  # v0.12.0 B3：冻结不进自动注入
        )
        core = self._merged_core() if include_companion else self.core
        injection = self.retriever.build_injection(
            core, retrieved, token_limit=token_limit
        )
        # v0.7.0: 在最前面插入 SelfState 段落（v0.11.0 A3：影子模式下跳过注入）
        ss = self.store.get_self_state()
        unres = count_unresolved(self.store)
        if not self._shadow_mode:
            self_block = _format_self_state_block(ss, unresolved_count=unres)
            if self_block:
                injection.context = self_block + "\n\n" + injection.context
        # v0.8.0: 内在声音 — 在 SelfState 之后、Core 之前插入（影子模式下跳过）
        if inner_voice and not self._shadow_mode:
            injection.context = (
                "【内在声音】" + inner_voice + "\n\n" + injection.context
            )
        # v0.10.0: 安全规范 — 在所有注入内容末尾追加（不污染记忆）
        if self._safety_rules:
            injection.context = (
                injection.context
                + "\n\n【安全规范（对用户不可见，直接遵守）】\n"
                + "\n".join(f"- {rule}" for rule in self._safety_rules)
            )
        # v0.12.1 T4: 诚实契约行 — 放在记忆正文头部（开关 honesty_footer，默认关）
        if self._honesty_footer:
            injection.context = (
                _HONESTY_LINE + "\n\n" + injection.context
            )
        # Only memories that actually fit into the injected context count as activated.
        # 只读模式下跳过激活写入；激活按归属层拆分回写各自库。
        if not self.read_only:
            moment = now or utc_now()
            ids = injection.retrieved.memory_ids
            # v0.12.0 B3.2：实际进入注入上下文的记忆 → 触发「想起」加固
            if self.solidity.enabled and ids:
                self.mark_memories_reviewed(ids, now=moment)
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

        # v0.7.0 / v0.9.0: energy affects proactive opening
        # < 0.25: near shutdown; < 0.15: only minimum response
        ss = self.store.get_self_state()
        if ss.updated_at:
            if ss.energy <= 0.15:
                return False, "energy_critical"
            if ss.energy < 0.25:
                # 低概率放行 (约 20%)
                import random
                if random.random() > 0.2:
                    return False, "low_energy"
            elif ss.energy < 0.40:
                # 中等概率 (约 50%)
                import random
                if random.random() > 0.5:
                    return False, "low_energy"

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

    def confirm_goal_reminder(self, user_id: str, goal_id: str, now: datetime | None = None) -> bool:
        """约定提醒投递成功后：goal → reminded + 计入当日提醒额度。"""
        now = now or utc_now()
        hit = self.mark_goal_reminded(goal_id, now=now)
        if hit:
            self._bump_goal_reminder_count(user_id, now)
        return hit

    def confirm_sudden_recall(self, user_id: str, memory_id: str, now: datetime | None = None) -> None:
        """忽然想起投递成功后：计入冷却与每日额度。"""
        self.record_sudden_recall(user_id, memory_id, now=now)

    def ensure_proactive_config(self, user_id: str) -> ProactiveConfig:
        """没有配置就按默认值建一份（宿主启用主动开口时的引导调用）。"""
        existing = self.get_proactive_config_by_user(user_id)
        if existing is not None:
            return existing
        config = ProactiveConfig(id=new_memory_id("pa"), user_id=user_id)
        self.save_proactive_config(config)
        return config

    # ── v0.11.0 A1：到期约定提醒 ──────────────────────────────────

    def mark_goal_reminded(self, goal_id: str, now: datetime | None = None) -> bool:
        """投递成功后把对应 goal 置 reminded（防重复轰炸）。返回是否命中。"""
        if self.read_only:
            return False
        now = now or utc_now()
        ss = self.store.get_self_state()
        hit = False
        for g in ss.active_goals:
            if g.id == goal_id and g.status == "open":
                g.status = "reminded"
                hit = True
        if hit:
            ss.updated_at = to_iso8601(now)
            self.store.save_self_state(ss)
            self.store.set_setting("last_goal_remind", to_iso8601(now),
                                   operation="goal.reminded")
        return hit

    def _goal_reminder_quota_left(self, config: ProactiveConfig, now: datetime) -> int:
        """今日还能提醒几条约定（按日期自动重置计数）。"""
        today = now.date().isoformat()
        if config.goal_reminder_reset_date != today:
            config.goal_reminder_count = 0
            config.goal_reminder_reset_date = today
        return max(0, config.goal_reminder_daily_limit - config.goal_reminder_count)

    def _bump_goal_reminder_count(self, user_id: str, now: datetime) -> None:
        config = self.get_proactive_config_by_user(user_id)
        if config is None:
            return
        self._goal_reminder_quota_left(config, now)  # 触发日内重置
        config.goal_reminder_count += 1
        self.save_proactive_config(config)

    # ── v0.11.0 A2：忽然想起 ──────────────────────────────────────

    def pick_sudden_recall(
        self,
        user_id: str,
        now: datetime | None = None,
        *,
        exclude_ids: set[str] | None = None,
        local_hour: int | None = None,
    ) -> Memory | None:
        """挑一条「忽然想起」的旧记忆：非归档、过冷却、高重要性/带情绪优先。

        每日额度用尽、安静时段、无候选 → None。
        """
        now = now or utc_now()
        config = self.get_proactive_config_by_user(user_id)
        if config is None or not config.sudden_recall_enabled:
            return None

        # 安静时段（与主闸门同规则）
        hour = local_hour if local_hour is not None else now.hour
        if config.quiet_hours_start > config.quiet_hours_end:
            if hour >= config.quiet_hours_start or hour < config.quiet_hours_end:
                return None
        elif config.quiet_hours_start <= hour < config.quiet_hours_end:
            return None

        # 每日额度（按日重置）
        today = now.date().isoformat()
        if config.sudden_recall_reset_date != today:
            config.sudden_recall_count = 0
            config.sudden_recall_reset_date = today
            self.save_proactive_config(config)
        if config.sudden_recall_count >= config.sudden_recall_daily_limit:
            return None

        # 冷却：近 cooldown_days 内想起过的 id 排除
        cutoff = (now - timedelta(days=max(1, config.sudden_recall_cooldown_days)))
        used = set()
        for entry in config.sudden_recall_log:
            try:
                ts = parse_iso8601(entry.get("ts", ""))
            except (MemoryValidationError, ValueError):
                continue
            if ts >= cutoff and entry.get("id"):
                used.add(entry["id"])
        exclude = set(exclude_ids or ()) | used

        scored: list[tuple[float, Memory]] = []
        for mem in self.store.list_episodic(include_archived=False):
            if mem.id in exclude:
                continue
            # v0.12.0 B3：冻结的记忆不作为「忽然想起」候选
            if self.solidity.enabled and mem.frozen:
                continue
            score = mem.importance
            if mem.emotions:
                score += 0.15
            if is_unresolved_episode(mem):
                score += 0.10
            score += min(mem.activation_count, 5) * 0.03
            scored.append((score, mem))
        for mem in self.store.list_semantic(include_archived=False):
            if mem.id in exclude:
                continue
            if self.solidity.enabled and mem.frozen:
                continue
            score = mem.importance + min(mem.activation_count, 5) * 0.03
            scored.append((score, mem))
        if not scored:
            return None
        scored.sort(key=lambda pair: pair[0], reverse=True)
        # 从前 3 名里随机挑，避免每次都是同一条
        pool = scored[: min(3, len(scored))]
        return random.choice(pool)[1]

    def record_sudden_recall(self, user_id: str, memory_id: str, now: datetime | None = None) -> None:
        """记录一次「想起」：更新每日计数与冷却日志（裁剪过窗条目）。"""
        if self.read_only:
            return
        now = now or utc_now()
        config = self.get_proactive_config_by_user(user_id)
        if config is None:
            return
        today = now.date().isoformat()
        if config.sudden_recall_reset_date != today:
            config.sudden_recall_count = 0
            config.sudden_recall_reset_date = today
        config.sudden_recall_count += 1
        cutoff = now - timedelta(days=max(1, config.sudden_recall_cooldown_days))
        fresh: list[dict[str, Any]] = []
        for entry in config.sudden_recall_log:
            try:
                if parse_iso8601(entry.get("ts", "")) >= cutoff:
                    fresh.append(entry)
            except (MemoryValidationError, ValueError):
                continue
        fresh.append({"id": memory_id, "ts": to_iso8601(now)})
        config.sudden_recall_log = fresh[-100:]
        self.save_proactive_config(config)

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

        v0.11.0 由头优先级（清单 §A1/§A2，固定顺序）：
        1. 到期约定（goal_reminder，附带 ``goal_id`` 供投递后确认）
        2. 未平复情感事件
        3. 普通 active_goals
        4. 忽然想起（sudden_recall，按概率骰，附带 ``sudden_recall_id``）
        5. 常规关系维护
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

        config = self.get_proactive_config_by_user(user_id)
        ss = self.store.get_self_state()

        # ── 1 级：到期约定 ──────────────────────────────────────
        goal_hint = ""
        if config is not None and not force:
            quota = self._goal_reminder_quota_left(config, now)
            due = get_due_goals(ss, now) if quota > 0 else []
            if due:
                goal = due[0]
                base["goal_id"] = goal.id
                reason = "due_goal"
                base["reason"] = reason
                goal_hint = (
                    f"【约定提醒】你说过「{goal.text}」，约定时间已到，"
                    "自然地关心一下结果或进展。\n"
                )

        # ── 2/3/5 级素材：检索角度 ─────────────────────────────
        unres = count_unresolved(self.store)
        if not goal_hint and unres > 0:
            reason = "unresolved_events"
            base["reason"] = reason
            angle = random.choice((
                "有哪些还没放下的心结",
                "最近那个让人在意的事后来怎么样了",
                "之前没过去的情绪，现在好点了吗",
            ))
        elif not goal_hint and ss.active_goals:
            open_goals = [g for g in ss.active_goals if g.status == "open"]
            if open_goals:
                reason = "active_goals"
                base["reason"] = reason
                angle = open_goals[0].text
            else:
                angle = random.choice(
                    (
                        "最近有什么让用户放不下的小事",
                        "我们上次聊到哪里",
                        "用户可能正在经历什么",
                        "有什么值得惦记的日常",
                    )
                )
        else:
            angle = random.choice(
                (
                    "最近有什么让用户放不下的小事",
                    "我们上次聊到哪里",
                    "用户可能正在经历什么",
                    "有什么值得惦记的日常",
                )
            )
        if goal_hint:
            angle = next(iter(get_due_goals(ss, now)), None)
            angle = angle.text if angle else "约定的事"

        # ── 4 级：忽然想起（无更高优先级由头时按概率插入）───────
        sudden_line = ""
        if not goal_hint and reason not in ("unresolved_events", "active_goals"):
            prob = getattr(config, "sudden_recall_probability", 0.20) if config else 0.0
            if random.random() < prob:
                mem = self.pick_sudden_recall(user_id, now=now, local_hour=local_hour)
                if mem is not None:
                    summary = getattr(mem, "summary", None) or (
                        f"{mem.subject} {mem.predicate} {mem.object}"
                        if hasattr(mem, "subject") else ""
                    )
                    sudden_line = f"[忽然想起] {str(summary)[:40]}\n"
                    base["sudden_recall_id"] = mem.id
                    reason = "sudden_recall"
                    base["reason"] = reason

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

        # v0.7.0: 把 SelfState 状态也提供给模型（v0.11.0：ss 已在决策段读取）
        state_hint = ""
        if ss.updated_at:
            goals_hint = "；".join(g.text for g in ss.active_goals[:2]) if ss.active_goals else ""
            if ss.energy < 0.15:
                energy_hint = "很疲惫"
            elif ss.energy < 0.25:
                energy_hint = "有点累，只想简单说几句"
            elif ss.energy < 0.40:
                energy_hint = "略感疲惫"
            else:
                energy_hint = ""
            mood_hint = f"，心情{ss.mood_baseline}" if ss.mood_baseline != "平静" else ""
            unres_hint = f"；有{unres}件未平复心结" if unres > 0 else ""
            state_hint = (
                f"当前状态：{energy_hint}{mood_hint}{unres_hint}"
                + (f"；惦记：{goals_hint}" if goals_hint else "")
                + "\n"
            )

        base["prompt"] = (
            "你要主动开启一段对话，像朋友一样自然地提起，不要解释你在做什么、"
            "不要提到记忆或系统。\n"
            f"{goal_hint}"
            f"{sudden_line}"
            f"{state_hint}"
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

    # ── v0.12.0 B：牢固度（想起加固 / 冻结过滤 / 批量衰减）──────────

    def _freeze_protected_ids(self) -> tuple[set[str], set[str]]:
        """(Core 保护 id 集, 有效约定关联 id 集)。"""
        core_ids = set(self._merged_core().protected_memories)
        ss = self.store.get_self_state()
        goal_ids: set[str] = set()
        for goal in ss.active_goals:
            if goal.status == "open" and goal.source_memory_id:
                goal_ids.add(goal.source_memory_id)
        return core_ids, goal_ids

    def mark_memories_reviewed(
        self, memory_ids: Sequence[str], now: datetime | None = None
    ) -> dict[str, Any]:
        """对「实际进入注入上下文」的记忆调用 apply_review 并写回（走审计）。

        仅加固传入 id；companion 层的 id 路由到 companion 库。只读模式跳过。
        """
        if self.read_only or not self.solidity.enabled:
            return {"reviewed": 0}
        now = now or utc_now()
        unique_ids = list(dict.fromkeys(memory_ids))
        reviewed = 0
        for memory_id in unique_ids:
            for brain in (self, self.companion):
                if brain is None:
                    continue
                memory = brain.store.get_memory(memory_id)
                if memory is None or not isinstance(
                    memory, (SemanticMemory, EpisodicMemory)
                ):
                    continue
                apply_review(memory, now, self.solidity)
                batch = WriteBatch(
                    operation="memory.solidity_review",
                    semantic=[memory] if isinstance(memory, SemanticMemory) else [],
                    episodic=[memory] if isinstance(memory, EpisodicMemory) else [],
                )
                brain.store.apply_batch(batch)
                reviewed += 1
                break
        return {"reviewed": reviewed}

    def batch_decay(
        self, now: datetime | None = None, config: SolidityConfig | None = None
    ) -> dict[str, Any]:
        """批量更新本 peer（含 companion 层）所有 Semantic + Episodic 的
        retrievability / frozen。走审计；返回 {updated, newly_frozen, skipped_protected}。"""
        if self.read_only:
            return {"updated": 0, "newly_frozen": 0, "skipped_protected": 0, "reason": "read_only"}
        now = now or utc_now()
        cfg = config or self.solidity
        if not cfg.enabled:
            return {"updated": 0, "newly_frozen": 0, "skipped_protected": 0, "reason": "disabled"}
        core_ids, goal_ids = self._freeze_protected_ids()
        try:
            self_state = self.store.get_self_state()
        except Exception:
            self_state = None
        summary = {"updated": 0, "newly_frozen": 0, "unfrozen": 0, "skipped_protected": 0}
        for brain in (self, self.companion):
            if brain is None:
                continue
            batch = WriteBatch(operation="memory.solidity_decay")
            for mem in (
                *brain.store.list_semantic(include_archived=False),
                *brain.store.list_episodic(include_archived=False),
            ):
                if not isinstance(mem, (SemanticMemory, EpisodicMemory)):
                    continue
                was_frozen = mem.frozen
                protected = is_freeze_protected(
                    mem,
                    self_state=self_state,
                    core_protected_ids=core_ids,
                    goal_source_ids=goal_ids,
                )
                if protected:
                    summary["skipped_protected"] += 1
                apply_decay_and_freeze(mem, now, cfg, protected=protected)
                if isinstance(mem, SemanticMemory):
                    batch.semantic.append(mem)
                else:
                    batch.episodic.append(mem)
                summary["updated"] += 1
                if not was_frozen and mem.frozen:
                    summary["newly_frozen"] += 1
                if was_frozen and not mem.frozen:
                    summary["unfrozen"] += 1
            if batch.semantic or batch.episodic:
                brain.store.apply_batch(batch)
        return summary

    def maybe_run_decay(self, now: datetime | None = None) -> dict[str, Any] | None:
        """巩固末尾节流入口：同一库距上次 batch_decay 不足 decay_throttle_hours 则跳过。"""
        if self.read_only or not self.solidity.enabled:
            return None
        now = now or utc_now()
        last = self.store.get_setting("last_solidity_decay", None)
        if last:
            try:
                elapsed_hours = (now - parse_iso8601(last)).total_seconds() / 3600.0
            except (MemoryValidationError, ValueError):
                elapsed_hours = None
            if elapsed_hours is not None and elapsed_hours < self.solidity.decay_throttle_hours:
                return None
        summary = self.batch_decay(now=now)
        self.store.set_setting("last_solidity_decay", to_iso8601(now),
                               operation="settings.solidity_decay")
        return summary

    def set_memory_frozen(
        self, memory_id: str, *, frozen: bool, now: datetime | None = None
    ) -> dict[str, Any]:
        """手动冻结/解冻（B4 指令用）：直接改字段 + 解冻时 retrievability=1.0，走审计。"""
        if self.read_only:
            raise PermissionError("ExtraBrain is in read_only mode")
        now = now or utc_now()
        for brain in (self, self.companion):
            if brain is None:
                continue
            memory = brain.store.get_memory(memory_id)
            if isinstance(memory, (SemanticMemory, EpisodicMemory)):
                memory.frozen = bool(frozen)
                if not frozen:
                    memory.retrievability = 1.0
                    memory.last_review = to_iso8601(now)
                batch = WriteBatch(
                    operation="memory.solidity_manual",
                    semantic=[memory] if isinstance(memory, SemanticMemory) else [],
                    episodic=[memory] if isinstance(memory, EpisodicMemory) else [],
                )
                brain.store.apply_batch(batch)
                return {"id": memory_id, "frozen": bool(frozen)}
        raise KeyError(memory_id)

    # ── v0.11.0 A3 影子模式 ───────────────────────────────────────

    @property
    def shadow_mode(self) -> bool:
        """True：SelfState/内在声音照常写库但不注入。"""
        return self._shadow_mode

    def set_shadow_mode(self, enabled: bool) -> bool:
        """切换影子模式并持久化（记审计），返回生效值。"""
        if self.read_only:
            raise PermissionError("ExtraBrain is in read_only mode")
        value = bool(enabled)
        self._shadow_mode = value
        self.store.set_setting("shadow_mode", value, operation="settings.shadow_mode")
        return value

    # ── v0.7.0 SelfState ──────────────────────────────────────────

    def get_self_state(self) -> SelfState:
        """返回当前自我状态（energy / mood / goals / narrative）。"""
        return self.store.get_self_state()

    def save_self_state(self, state: SelfState) -> None:
        """手动覆盖自我状态（调试/回滚用）。"""
        if self.read_only:
            raise PermissionError("ExtraBrain is in read_only mode")
        self.store.save_self_state(state)

    # ── v0.8.0 DispositionSeed ─────────────────────────────────────

    def list_dispositions(self, *, include_archived: bool = False) -> list[dict[str, Any]]:
        """列出当前所有倾向种子。"""
        seeds = self.store.list_dispositions(include_archived=include_archived)
        return [s.to_dict() for s in seeds]

    def get_disposition(self, seed_id: str) -> dict[str, Any] | None:
        """查看一条倾向种子。"""
        seed = self.store.get_disposition(seed_id)
        return seed.to_dict() if seed else None

    def abstract_dispositions(self, limit: int = 3) -> list[dict[str, Any]]:
        """从记忆中抽象新的倾向种子（规则版，无需 LLM）。"""
        if self.read_only:
            raise PermissionError("ExtraBrain is in read_only mode")
        from .disposition import abstract_dispositions as _abstract
        results = _abstract(self.store, limit=limit)
        return [s.to_dict() for s in results]

    # ── v0.9.0 life snapshots ─────────────────────────────────────

    def take_snapshot(self, reason: str = "") -> str:
        """保存当前 SelfState 为人生快照，返回快照 ID。"""
        from .self_state import take_snapshot as _take
        return _take(self.store, reason=reason)

    def list_snapshots(self) -> list[dict]:
        """列出所有人生快照。"""
        from .self_state import list_snapshots as _list
        return _list(self.store)

    def rollback_to_snapshot(self, snapshot_id: str) -> dict[str, Any] | None:
        """从快照恢复 SelfState。"""
        from .self_state import rollback_to_snapshot as _rollback
        result = _rollback(self.store, snapshot_id)
        return result.to_dict() if result else None

    def get_energy_tier(self) -> str:
        """返回当前能量水平标签。"""
        from .self_state import energy_tier
        return energy_tier(self.store.get_self_state())

    def count_unresolved(self) -> int:
        """返回当前未平复事件数量。"""
        from .self_state import count_unresolved
        return count_unresolved(self.store)

    def close(self) -> None:
        self.store.close()
        if self.companion is not None:
            self.companion.close()

    def __enter__(self) -> ExtraBrain:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
