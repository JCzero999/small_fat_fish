from __future__ import annotations

import copy
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from .config import MemoryConfig
from .embeddings import MemoryIndexer, VectorEntry
from .extraction import (
    ExtractionResult,
    MemoryExtractor,
    SemanticCandidate,
    build_extraction_prompt,
)
from .models import (
    ActiveThread,
    CoreMemory,
    CoreMemoryLimitError,
    EpisodicMemory,
    SemanticMemory,
    StyleMemory,
    is_unresolved_episode,
    new_memory_id,
    parse_iso8601,
    to_iso8601,
    utc_now,
)
from .storage import MemoryStore, WriteBatch


@dataclass(slots=True)
class ConsolidationReport:
    transaction_id: str
    semantic_written: int = 0
    episodic_written: int = 0
    style_written: int = 0
    archived: int = 0
    promoted: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    core_pruned: list[str] = field(default_factory=list)
    emotion_updated: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(
            self.semantic_written
            or self.episodic_written
            or self.style_written
            or self.archived
            or self.promoted
            or self.conflicts
            or self.core_pruned
            or self.emotion_updated
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "transaction_id": self.transaction_id,
            "semantic_written": self.semantic_written,
            "episodic_written": self.episodic_written,
            "style_written": self.style_written,
            "archived": self.archived,
            "promoted": list(self.promoted),
            "conflicts": list(self.conflicts),
            "core_pruned": list(self.core_pruned),
            "emotion_updated": list(self.emotion_updated),
            "warnings": list(self.warnings),
        }


# 情感事件五态状态机（v0.2.0）：正向链 open→acknowledged→attended→resolved；
# 任意状态可 dormant（旧事淡出）；dormant 被重新提起可回到 open/acknowledged。
_EMOTION_FORWARD = ("open", "acknowledged", "attended", "resolved")


def _emotion_transition_ok(current: str, target: str) -> bool:
    if target == "dormant":
        return current != "dormant"
    if current == "dormant":
        return target in ("open", "acknowledged")
    try:
        return _EMOTION_FORWARD.index(target) == _EMOTION_FORWARD.index(current) + 1
    except ValueError:
        return False


def calculate_decay(
    *,
    importance: float,
    last_activated: str,
    activation_count: int,
    protected: bool,
    now: datetime,
    decay_lambda: float,
) -> float:
    if protected:
        return 1.0
    elapsed_days = max(0.0, (now - parse_iso8601(last_activated)).total_seconds() / 86400)
    activation_bonus = 1.0 + 0.1 * math.log1p(activation_count)
    score = importance * math.exp(-decay_lambda * elapsed_days) * activation_bonus
    return max(0.0, min(1.0, score))


class Consolidator:
    def __init__(
        self,
        store: MemoryStore,
        extractor: MemoryExtractor,
        *,
        indexer: MemoryIndexer,
        config: MemoryConfig,
    ) -> None:
        self.store = store
        self.extractor = extractor
        self.indexer = indexer
        self.config = config

    def consolidate(
        self,
        conversation: str,
        *,
        session_id: str | None = None,
        now: datetime | None = None,
    ) -> ConsolidationReport:
        now = now or utc_now()
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        now = now.astimezone(timezone.utc)
        now_iso = to_iso8601(now)
        transaction_id = uuid4().hex
        report = ConsolidationReport(transaction_id)

        existing_semantic = self.store.list_semantic()
        existing_episodic = self.store.list_episodic()
        prompt_memories = self._prompt_memories(existing_semantic, existing_episodic)
        extraction = self.extractor.extract(build_extraction_prompt(conversation, prompt_memories))

        semantic = {memory.id: copy.deepcopy(memory) for memory in existing_semantic}
        episodic = {memory.id: copy.deepcopy(memory) for memory in existing_episodic}
        original_semantic = {memory_id: memory.to_dict() for memory_id, memory in semantic.items()}
        original_episodic = {memory_id: memory.to_dict() for memory_id, memory in episodic.items()}
        touched_semantic: set[str] = set()
        touched_episodic: set[str] = set()
        semantic_ids_from_batch: list[str] = []
        implicit_conflicts: set[str] = set()
        superseded: list[tuple[str, str | None]] = []

        for candidate in extraction.episodic_events:
            if candidate.importance < self.config.min_importance:
                report.warnings.append(f"discarded low-importance episode: {candidate.summary[:40]}")
                continue
            memory_id = new_memory_id("ep", now)
            episodic[memory_id] = EpisodicMemory(
                id=memory_id,
                timestamp=now_iso,
                summary=candidate.summary,
                key_moments=candidate.key_moments,
                emotions=candidate.emotions,
                importance=candidate.importance,
                related_semantic_ids=[],
                participants=["user", "agent"],
                raw_reference=session_id,
                last_activated=now_iso,
                emotion_state=candidate.emotion_state,
            )
            touched_episodic.add(memory_id)

        for candidate in extraction.semantic_facts:
            if candidate.importance < self.config.min_importance:
                report.warnings.append(
                    f"discarded low-importance fact: {candidate.subject} {candidate.predicate}"
                )
                continue
            incoming = SemanticMemory(
                id=new_memory_id("sem", now),
                type=candidate.type,
                subject=candidate.subject,
                predicate=candidate.predicate,
                object=candidate.object,
                detail=candidate.detail,
                importance=candidate.importance,
                emotion_valence=candidate.emotion_valence,
                emotion_intensity=candidate.emotion_intensity,
                confidence=candidate.confidence,
                source_episodic_ids=list(touched_episodic),
                created_at=now_iso,
                last_activated=now_iso,
            )

            duplicate = next(
                (
                    memory
                    for memory in semantic.values()
                    if _normalized(memory.subject) == _normalized(incoming.subject)
                    and _normalized(memory.predicate) == _normalized(incoming.predicate)
                    and _normalized(memory.object) == _normalized(incoming.object)
                ),
                None,
            )
            if duplicate is not None:
                merged = _merge_semantic(duplicate, incoming, now_iso)
                semantic[duplicate.id] = merged
                touched_semantic.add(duplicate.id)
                semantic_ids_from_batch.append(duplicate.id)
                continue

            if incoming.type in ("fact", "relationship"):
                for old in semantic.values():
                    if (
                        old.id == incoming.id
                        or old.type != incoming.type
                        or _normalized(old.subject) != _normalized(incoming.subject)
                        or _normalized(old.predicate) != _normalized(incoming.predicate)
                        or _normalized(old.object) == _normalized(incoming.object)
                    ):
                        continue
                    if incoming.confidence >= old.confidence:
                        old.confidence = round(old.confidence * 0.5, 6)
                        old.conflict_flag = True
                        touched_semantic.add(old.id)
                        implicit_conflicts.add(old.id)
                        report.conflicts.append(old.id)
                        superseded.append((old.object, incoming.object))
                    else:
                        incoming.confidence = round(incoming.confidence * 0.5, 6)
                        incoming.conflict_flag = True
                        report.conflicts.append(incoming.id)

            semantic[incoming.id] = incoming
            touched_semantic.add(incoming.id)
            semantic_ids_from_batch.append(incoming.id)

        for conflict in extraction.conflicts_detected:
            target = semantic.get(conflict.semantic_id)
            if target is None:
                report.warnings.append(f"unknown conflict memory: {conflict.semantic_id}")
                continue
            if target.id not in implicit_conflicts:
                target.confidence = round(target.confidence * 0.5, 6)
                target.conflict_flag = True
                touched_semantic.add(target.id)
                report.conflicts.append(target.id)
                replacement = self._replacement_object(semantic, original_semantic, target)
                superseded.append((target.object, replacement))

        # 情感事件状态机（v0.2.0）：只允许向前推进或进入 dormant；非法转换拒绝并记警告。
        for update in extraction.emotion_updates:
            target = episodic.get(update.episodic_id)
            if target is None:
                report.warnings.append(f"unknown emotion episode: {update.episodic_id}")
                continue
            current = target.emotion_state or "open"
            if not _emotion_transition_ok(current, update.to_state):
                report.warnings.append(
                    f"rejected emotion transition {current} -> {update.to_state} "
                    f"for {update.episodic_id}"
                )
                continue
            if current != update.to_state:
                target.emotion_state = update.to_state
                touched_episodic.add(target.id)
                report.emotion_updated.append(target.id)

        for memory_id in touched_episodic:
            if memory_id not in original_episodic:
                episodic[memory_id].related_semantic_ids = list(dict.fromkeys(semantic_ids_from_batch))

        core = self._apply_core_candidates(self.store.get_core(), extraction, now)
        core = self._prune_core_conflicts(core, superseded, report)
        promoted = self._promote(
            core,
            extraction.core_candidate_updates.should_promote_to_core,
            semantic,
            episodic,
            touched_semantic,
            touched_episodic,
            report,
        )
        report.promoted.extend(promoted)

        archived = self._apply_decay_and_archival(
            core,
            semantic,
            episodic,
            touched_semantic,
            touched_episodic,
            original_semantic,
            original_episodic,
            now,
        )
        report.archived = archived

        styles = self._merge_styles(extraction, now, report)
        vectors = self._build_vectors(
            semantic, episodic, touched_semantic, touched_episodic
        )
        report.semantic_written = len(touched_semantic)
        report.episodic_written = len(touched_episodic)
        report.style_written = len(styles)

        batch = WriteBatch(
            core=core,
            semantic=[semantic[memory_id] for memory_id in sorted(touched_semantic)],
            episodic=[episodic[memory_id] for memory_id in sorted(touched_episodic)],
            style=styles,
            vectors=vectors,
            operation="consolidate",
            transaction_id=transaction_id,
            audit_details={
                "session_id": session_id,
                "report": report.to_dict(),
                "empty_extraction": not (
                    extraction.semantic_facts
                    or extraction.episodic_events
                    or extraction.style_observations
                ),
            },
        )
        self.store.apply_batch(batch)
        return report

    def _prompt_memories(
        self,
        semantic: list[SemanticMemory],
        episodic: list[EpisodicMemory],
    ) -> list[dict[str, Any]]:
        # Keep extraction context bounded just like retrieval context.
        semantic = sorted(semantic, key=lambda item: item.importance, reverse=True)[:100]
        episodic = sorted(episodic, key=lambda item: item.timestamp, reverse=True)[:50]
        memories: list[dict[str, Any]] = [memory.to_dict() for memory in (*semantic, *episodic)]
        # 把已有线程也交给模型，否则模型看不到旧线程 id，只能每天新建导致碎片化。
        memories.extend(
            {
                "kind": "active_thread",
                "id": thread.id,
                "summary": thread.summary,
                "status": thread.status,
            }
            for thread in self.store.get_core().active_threads
        )
        return memories

    def _apply_core_candidates(
        self, core: CoreMemory, extraction: ExtractionResult, now: datetime
    ) -> CoreMemory:
        core = copy.deepcopy(core)
        core.last_updated = to_iso8601(now)
        changes = extraction.core_candidate_updates
        for change in changes.user_profile_changes:
            profile = core.user_stable_profile
            if change.field == "preferred_name":
                profile.preferred_name = change.value if change.operation == "set" else ""
                continue
            values = getattr(profile, change.field)
            if change.operation == "add" and change.value not in values:
                values.append(change.value)
            elif change.operation == "remove":
                values[:] = [value for value in values if value != change.value]

        for change in changes.relationship_changes:
            setattr(core.identity, change.field, change.value)

        for change in changes.new_or_updated_threads:
            thread = next(
                (item for item in core.active_threads if item.id == change.id), None
            )
            last_mentioned = change.last_mentioned or to_iso8601(now)
            if thread is None:
                # 只有我们自己在 existing_memories 里给出的 id 才是可信的；
                # 模型偶尔会伪造新 id，此时一律改用系统生成的 id，避免污染 Core。
                core.active_threads.append(
                    ActiveThread(
                        id=new_memory_id("thread", now),
                        summary=change.summary,
                        status=change.status,
                        last_mentioned=last_mentioned,
                    )
                )
            else:
                thread.summary = change.summary
                thread.status = change.status
                thread.last_mentioned = last_mentioned

        # Reconstruct nested models so direct attribute updates receive the same validation.
        validated = CoreMemory.from_dict(core.to_dict())
        validated.validate_size(self.config.core_token_limit)
        return validated

    def _replacement_object(
        self,
        semantic: dict[str, SemanticMemory],
        original_semantic: dict[str, dict[str, Any]],
        target: SemanticMemory,
    ) -> str | None:
        """A non-conflicted memory created in this batch, same subject and type, is the new value."""
        candidates = [
            memory
            for memory in semantic.values()
            if memory.id not in original_semantic
            and not memory.conflict_flag
            and memory.type == target.type
            and _normalized(memory.subject) == _normalized(target.subject)
        ]
        return candidates[0].object if len(candidates) == 1 else None

    def _prune_core_conflicts(
        self,
        core: CoreMemory,
        superseded: list[tuple[str, str | None]],
        report: ConsolidationReport,
    ) -> CoreMemory:
        if not superseded:
            return core
        profile = core.user_stable_profile
        for field_name in ("key_traits", "core_preferences", "important_life_facts"):
            values = getattr(profile, field_name)
            kept: list[str] = []
            for value in values:
                if any(
                    _overlap_ratio(value, old_object) >= 0.5
                    and (replacement is None or _overlap_ratio(value, replacement) < 0.5)
                    for old_object, replacement in superseded
                ):
                    report.core_pruned.append(value)
                else:
                    kept.append(value)
            setattr(profile, field_name, kept)
        return core

    def _promote(
        self,
        core: CoreMemory,
        requested_ids: list[str],
        semantic: dict[str, SemanticMemory],
        episodic: dict[str, EpisodicMemory],
        touched_semantic: set[str],
        touched_episodic: set[str],
        report: ConsolidationReport,
    ) -> list[str]:
        automatic = [
            memory.id
            for memory in semantic.values()
            if memory.importance >= self.config.promotion_importance
            and memory.activation_count >= self.config.promotion_activations
            and memory.type == "relationship"
        ]
        promoted = []
        for memory_id in dict.fromkeys([*requested_ids, *automatic]):
            if memory_id in core.protected_memories:
                continue
            memory = semantic.get(memory_id) or episodic.get(memory_id)
            if memory is None:
                report.warnings.append(f"unknown promotion memory: {memory_id}")
                continue
            candidate_ids = [*core.protected_memories, memory_id]
            previous_ids = core.protected_memories
            core.protected_memories = candidate_ids
            try:
                core.validate_size(self.config.core_token_limit)
            except CoreMemoryLimitError:
                core.protected_memories = previous_ids
                report.warnings.append(f"Core budget prevented promotion: {memory_id}")
                continue
            memory.protected = True
            memory.decay_score = 1.0
            if isinstance(memory, SemanticMemory):
                touched_semantic.add(memory_id)
            else:
                touched_episodic.add(memory_id)
            promoted.append(memory_id)
        return promoted

    def _apply_decay_and_archival(
        self,
        core: CoreMemory,
        semantic: dict[str, SemanticMemory],
        episodic: dict[str, EpisodicMemory],
        touched_semantic: set[str],
        touched_episodic: set[str],
        original_semantic: dict[str, dict[str, Any]],
        original_episodic: dict[str, dict[str, Any]],
        now: datetime,
    ) -> int:
        archived_count = 0
        collections = (
            (semantic, touched_semantic, original_semantic),
            (episodic, touched_episodic, original_episodic),
        )
        for memories, touched, originals in collections:
            for memory_id, memory in memories.items():
                if memory_id in core.protected_memories:
                    memory.protected = True
                last_activated = memory.last_activated or (
                    memory.timestamp if isinstance(memory, EpisodicMemory) else None
                )
                if last_activated is None:
                    raise ValueError(f"memory has no activation timestamp: {memory_id}")
                memory.decay_score = self._calculate_retention_score(
                    memory, now
                )
                # 反漠视（v0.2.0）：未平复的情感事件不衰减——评分保底温层，永不自动归档。
                unresolved = is_unresolved_episode(memory)
                if unresolved:
                    memory.decay_score = max(memory.decay_score, 0.60)
                days_inactive = max(
                    0.0,
                    (now - parse_iso8601(last_activated)).total_seconds() / 86400,
                )
                memory.retention_tier = self._retention_tier(memory.decay_score)
                memory.protected = memory_id in core.protected_memories
                if (
                    not memory.protected
                    and not unresolved
                    and memory.retention_tier == 4
                    and days_inactive >= self.config.archive_after_days
                ):
                    memory.archived = True
                    memory.decay_score = 0.0
                before = originals.get(memory_id)
                if before is None or before != memory.to_dict():
                    touched.add(memory_id)
                if before is not None and not before["archived"] and memory.archived:
                    archived_count += 1
        return archived_count

    def _calculate_retention_score(
        self, memory: Memory, now: datetime
    ) -> float:
        """七因子保留度评分：ACT-R 激活 + 重要度 + 情绪 + 关联 + 叙事 + 锚定 + 间隔 − 冗余

        采用 mnemovault 七因子公式，score = 0.5 + gain·(Σwᵢxᵢ − bias)
        """
        if memory.protected:
            return 1.0

        last_activated = memory.last_activated or (
            memory.timestamp if isinstance(memory, EpisodicMemory) else None
        )
        if last_activated is None:
            last_activated = memory.created_at
        elapsed_days = max(
            0.0, (now - parse_iso8601(last_activated)).total_seconds() / 86400
        )

        # 1. 激活 (ACT-R): ln Σ (t_now − t_j)^(−0.5)，频率与近因统一
        activation_count = max(memory.activation_count, 1)
        activation = math.log1p(activation_count) / (1.0 + math.log1p(elapsed_days))

        # 2. 重要度
        importance = memory.importance

        # 3. 情绪强度
        emotion = abs(memory.emotion_intensity) if hasattr(memory, "emotion_intensity") else 0.0

        # 4. 关联度：基于已关联的语义/情节记忆数量
        association = min(
            1.0, len(memory.source_episodic_ids) / 5.0
        ) if hasattr(memory, "source_episodic_ids") else 0.0

        # 5. 叙事角色：简化为 importance × 0.5（叙事层记忆更高）
        narrative = 0.5

        # 6. 锚定：protected 记忆视为用户锚定
        anchor = 1.0 if memory.protected else 0.0

        # 7. 间隔效应：时间越长衰减越大
        interval = math.exp(-0.06 * elapsed_days)

        # 冗余：同一主题下重复记忆越多，分数越低（简化：基于来源情节数）
        redundancy = min(1.0, len(memory.source_episodic_ids) / 10.0) if hasattr(memory, "source_episodic_ids") else 0.0

        factors = {
            "activation": activation,
            "importance": importance,
            "emotion": emotion,
            "association": association,
            "narrative": narrative,
            "anchor": anchor,
            "interval": interval,
        }
        weights = {
            "activation": 0.20,
            "importance": 0.20,
            "emotion": 0.15,
            "association": 0.10,
            "narrative": 0.10,
            "anchor": 0.10,
            "interval": 0.15,
        }

        weighted_sum = sum(weights[k] * factors[k] for k in weights)
        bias = 0.30
        gain = 0.5

        raw_score = 0.5 + gain * (weighted_sum - bias)
        raw_score = max(0.0, min(1.0, raw_score))

        # 陈旧包络：30 天内不折损，730 天到地板
        if elapsed_days > 30:
            envelope = 0.15 + 0.85 * max(
                0.0, min(1.0, 1.0 - (elapsed_days - 30) / 700)
            )
            raw_score *= envelope

        return max(0.0, min(1.0, raw_score))

    def _retention_tier(self, score: float) -> int:
        """六档处置：
        P5 保持 ≥0.75（热层，完整向量 + 残差）
        P4 压缩 ≥0.55（温层，向量降为 PQ32）
        P3 摘要化 ≥0.35（冷层，只留槽位 + digest）
        P2 归档 ≥0.18（归档层）
        P1 墓碑 <0.18（只留 id + digest + 理由）
        P0 物理删除：永不自动执行，仅手动 --yes 审批
        """
        if score >= 0.75:
            return 0  # P5 保持
        if score >= 0.55:
            return 1  # P4 压缩
        if score >= 0.35:
            return 2  # P3 摘要化
        if score >= 0.18:
            return 3  # P2 归档
        return 4  # P1 墓碑

    def _merge_styles(
        self, extraction: ExtractionResult, now: datetime, report: ConsolidationReport
    ) -> list[StyleMemory]:
        styles = {(_normalized(item.category), _normalized(item.observation)): item for item in self.store.list_style()}
        touched: dict[str, StyleMemory] = {}
        for candidate in extraction.style_observations:
            if candidate.importance < self.config.min_importance:
                report.warnings.append(f"discarded low-importance style: {candidate.observation[:40]}")
                continue
            key = (_normalized(candidate.category), _normalized(candidate.observation))
            if key in styles:
                style = copy.deepcopy(styles[key])
                style.importance = max(style.importance, candidate.importance)
                style.last_observed = to_iso8601(now)
            else:
                style = StyleMemory(
                    id=new_memory_id("style", now),
                    observation=candidate.observation,
                    category=candidate.category,
                    importance=candidate.importance,
                    examples=[],
                    last_observed=to_iso8601(now),
                )
            styles[key] = style
            touched[style.id] = style
        return list(touched.values())

    def _build_vectors(
        self,
        semantic: dict[str, SemanticMemory],
        episodic: dict[str, EpisodicMemory],
        touched_semantic: set[str],
        touched_episodic: set[str],
    ) -> dict[str, VectorEntry]:
        entries: dict[str, VectorEntry] = {}
        for memory_id in touched_semantic:
            memory = semantic[memory_id]
            entries[memory_id] = self.indexer.entry_for(
                memory_id, "semantic", memory.embedding_text()
            )
        for memory_id in touched_episodic:
            memory = episodic[memory_id]
            entries[memory_id] = self.indexer.entry_for(
                memory_id, "episodic", memory.embedding_text()
            )
        return entries


def _merge_semantic(
    old: SemanticMemory, incoming: SemanticMemory, now_iso: str
) -> SemanticMemory:
    merged = copy.deepcopy(old)
    merged.detail = incoming.detail
    merged.importance = max(old.importance, incoming.importance)
    merged.confidence = max(old.confidence, incoming.confidence)
    merged.emotion_intensity = max(old.emotion_intensity, incoming.emotion_intensity)
    if old.emotion_valence != incoming.emotion_valence:
        merged.emotion_valence = "mixed"
    merged.source_episodic_ids = list(
        dict.fromkeys([*old.source_episodic_ids, *incoming.source_episodic_ids])
    )
    merged.last_activated = now_iso
    merged.conflict_flag = old.conflict_flag and incoming.conflict_flag
    merged.archived = False
    return merged


def _normalized(value: str) -> str:
    return " ".join(value.casefold().split())


def _token_set(text: str) -> set[str]:
    """CJK character bigrams plus whole ASCII words, for paraphrase-tolerant overlap."""

    lowered = text.casefold()
    tokens: set[str] = set(re.findall(r"[a-z0-9]+", lowered))
    for run in re.findall(r"[一-鿿]+", lowered):
        if len(run) == 1:
            tokens.add(run)
        else:
            tokens.update(run[index : index + 2] for index in range(len(run) - 1))
    return tokens


def _overlap_ratio(text: str, needle: str) -> float:
    needle_tokens = _token_set(needle)
    if not needle_tokens:
        return 0.0
    return len(needle_tokens & _token_set(text)) / len(needle_tokens)
