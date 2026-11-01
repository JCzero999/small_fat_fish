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
    """Public facade for storage, consolidation, retrieval, and prompt injection."""

    def __init__(
        self,
        db_path: str | Path = "extra_brain.db",
        *,
        extractor: MemoryExtractor | None = None,
        embedder: EmbeddingProvider | None = None,
        config: MemoryConfig | None = None,
        read_only: bool = False,
        encryption_passphrase: str | None = None,
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
            )
        return self.retriever.search(
            query,
            recent_context=recent_context,
            entities=entities,
            now=now,
            as_of=as_of,
            primed_ids=primed_ids,
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
    ) -> Injection:
        retrieved = self.retrieve(
            query,
            recent_context=recent_context,
            entities=entities,
            now=now,
            primed_ids=primed_ids,
        )
        injection = self.retriever.build_injection(
            self.core, retrieved, token_limit=token_limit
        )
        # Only memories that actually fit into the injected context count as activated.
        # 只读模式下跳过激活写入。
        if not self.read_only:
            self.store.record_activations(
                injection.retrieved.memory_ids, now=now or utc_now()
            )
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

    def close(self) -> None:
        self.store.close()

    def __enter__(self) -> ExtraBrain:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
