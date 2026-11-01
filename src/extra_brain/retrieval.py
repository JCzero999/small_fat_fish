from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, Sequence

from .config import MemoryConfig
from .embeddings import EmbeddingProvider, cosine_similarity
from .models import (
    CoreMemory,
    CoreMemoryLimitError,
    EpisodicMemory,
    Memory,
    MEMORY_VIEWS,
    SemanticMemory,
    estimate_tokens,
    is_unresolved_episode,
    parse_iso8601,
    utc_now,
)
from .storage import MemoryStore


@dataclass(frozen=True, slots=True)
class ScoredMemory:
    memory: Memory
    semantic_similarity: float
    importance: float
    recency_boost: float
    emotion_match: float
    final_score: float

    def to_dict(self) -> dict:
        return {
            "memory": self.memory.to_dict(),
            "scores": {
                "semantic_similarity": round(self.semantic_similarity, 6),
                "importance": round(self.importance, 6),
                "recency_boost": round(self.recency_boost, 6),
                "emotion_match": round(self.emotion_match, 6),
                "final_score": round(self.final_score, 6),
            },
        }


@dataclass(slots=True)
class RetrievalResult:
    semantic: list[ScoredMemory] = field(default_factory=list)
    episodic: list[ScoredMemory] = field(default_factory=list)
    path: str = "standard"  # retrieval path label

    @property
    def memory_ids(self) -> list[str]:
        return [item.memory.id for item in (*self.semantic, *self.episodic)]

    def to_dict(self) -> dict:
        return {
            "semantic": [item.to_dict() for item in self.semantic],
            "episodic": [item.to_dict() for item in self.episodic],
            "path": self.path,
        }


@dataclass(slots=True)
class Injection:
    context: str
    core: CoreMemory
    retrieved: RetrievalResult
    estimated_tokens: int
    token_limit: int


class MemoryRetriever:
    def __init__(
        self,
        store: MemoryStore,
        embedder: EmbeddingProvider,
        config: MemoryConfig,
    ) -> None:
        self.store = store
        self.embedder = embedder
        self.config = config

    def search(
        self,
        query: str,
        *,
        recent_context: Sequence[str] = (),
        entities: Iterable[str] = (),
        now: datetime | None = None,
        semantic_top_k: int | None = None,
        episodic_top_k: int | None = None,
        as_of: str | None = None,
        primed_ids: Iterable[str] = (),
        views: Iterable[str] = (),
    ) -> RetrievalResult:
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must not be empty")
        now = now or utc_now()
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        recent_context = list(recent_context)[-5:]
        query_text = "\n".join([*recent_context, query])
        query_vector = self.embedder.embed(query_text)
        query_emotion = detect_emotion(query_text)
        normalized_entities = [entity.casefold() for entity in entities if entity.strip()]
        normalized_views = [view for view in views if view]
        unknown_views = [view for view in normalized_views if view not in MEMORY_VIEWS]
        if unknown_views:
            raise ValueError(
                f"unknown views: {', '.join(unknown_views)}; allowed: {', '.join(MEMORY_VIEWS)}"
            )
        views_set = set(normalized_views)
        as_of_dt = None
        if as_of is not None:
            as_of_dt = parse_iso8601(as_of)
            if as_of_dt.tzinfo is None:
                raise ValueError("as_of must be timezone-aware")

        semantic_vectors = self.store.get_vectors(
            "semantic",
            model=self.embedder.model_name,
            dimensions=self.embedder.dimensions,
        )
        episodic_vectors = self.store.get_vectors(
            "episodic",
            model=self.embedder.model_name,
            dimensions=self.embedder.dimensions,
        )
        semantic_memories = [
            memory
            for memory in self.store.list_semantic()
            if memory.id in semantic_vectors
            and self._eligible(memory, normalized_entities, as_of_dt, views_set)
        ]
        episodic_memories = [
            memory
            for memory in self.store.list_episodic()
            if memory.id in episodic_vectors
            and self._eligible(memory, normalized_entities, as_of_dt, views_set)
        ]

        primed = frozenset(
            memory_id for memory_id in primed_ids if isinstance(memory_id, str)
        )
        return RetrievalResult(
            semantic=self._rank_type(
                semantic_memories,
                semantic_vectors,
                query_vector,
                query_emotion,
                now,
                semantic_top_k
                if semantic_top_k is not None
                else self.config.semantic_top_k,
                primed_ids=primed,
            ),
            episodic=self._rank_type(
                episodic_memories,
                episodic_vectors,
                query_vector,
                query_emotion,
                now,
                episodic_top_k
                if episodic_top_k is not None
                else self.config.episodic_top_k,
                primed_ids=primed,
            ),
        )

    def _eligible(
        self,
        memory: Memory,
        entities: list[str],
        as_of: datetime | None = None,
        views: set[str] | None = None,
    ) -> bool:
        if memory.archived or memory.importance < self.config.min_importance:
            return False
        if isinstance(memory, SemanticMemory) and memory.conflict_flag and memory.confidence < 0.25:
            return False
        # 多视角过滤（v0.4.0 遗留）：非空 views 集合按记忆写入时算好的视角过滤。
        if views and (getattr(memory, "view", "") or "") not in views:
            return False
        # 双时态过滤：only include memory valid at `as_of`
        if as_of is not None:
            if isinstance(memory, SemanticMemory):
                if memory.valid_from is not None and as_of < parse_iso8601(memory.valid_from):
                    return False
                if memory.valid_to is not None and as_of >= parse_iso8601(memory.valid_to):
                    return False
            elif isinstance(memory, EpisodicMemory):
                if memory.valid_from is not None and as_of < parse_iso8601(memory.valid_from):
                    return False
                if memory.valid_to is not None and as_of >= parse_iso8601(memory.valid_to):
                    return False
        if entities:
            text = memory.embedding_text().casefold()
            return any(entity in text for entity in entities)
        return True

    def search_four_paths(
        self,
        query: str,
        *,
        recent_context: Sequence[str] = (),
        entities: Iterable[str] = (),
        now: datetime | None = None,
        path: str | None = None,
        as_of: str | None = None,
        primed_ids: Iterable[str] = (),
        views: Iterable[str] = (),
    ) -> RetrievalResult:
        """四路召回（v0.3.0）：

        1) Path A - 语义相似：对 query 与语义/情节向量做余弦相似度，取 top-k。
        2) Path B - 关键词实体：对 query 提取实体/关键词，硬过滤相关记忆。
        3) Path C - 情绪共鸣：query 情绪标签与记忆情绪匹配，得分更高。
        4) Path D - 时间点快照：使用 as_of 查询某个时间点的记忆状态（双时态）。

        当 ``path`` 为 None 时，自动混合所有路径的得分；否则仅使用指定路径。
        """
        now = now or utc_now()
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        primed_ids = frozenset(
            memory_id for memory_id in primed_ids if isinstance(memory_id, str)
        )

        # Path A: 语义检索（标准检索）
        standard = self.search(
            query,
            recent_context=recent_context,
            entities=entities,
            now=now,
            as_of=as_of,
            primed_ids=primed_ids,
            views=views,
        )
        if path == "A":
            standard.path = "A"
            return standard

        # Path B: 关键词/实体硬过滤检索（只使用实体，不看语义相似度）
        entities_only = self._entity_search(
            query,
            recent_context=recent_context,
            entities=entities,
            now=now,
            as_of=as_of,
            primed_ids=primed_ids,
            views=views,
        )
        if path == "B":
            entities_only.path = "B"
            return entities_only

        # Path C: 情绪共鸣检索（以情绪匹配为主导）
        emotion_first = self._emotion_search(
            query,
            recent_context=recent_context,
            entities=entities,
            now=now,
            as_of=as_of,
            primed_ids=primed_ids,
            views=views,
        )
        if path == "C":
            emotion_first.path = "C"
            return emotion_first

        # Path D: 时间点快照（as_of 翻车，返回空结果表示该时间点无数据）
        snapshot = self._snapshot_search(query, now=now, as_of=as_of, views=views)
        if path == "D":
            snapshot.path = "D"
            return snapshot

        # 默认：四路融合，按 final_score 合并去重
        merged: dict[str, ScoredMemory] = {}
        for item in (*standard.semantic, *standard.episodic):
            merged[item.memory.id] = item
        for item in (*entities_only.semantic, *entities_only.episodic):
            if item.memory.id not in merged or item.final_score > merged[item.memory.id].final_score:
                merged[item.memory.id] = item
        for item in (*emotion_first.semantic, *emotion_first.episodic):
            if item.memory.id not in merged or item.final_score > merged[item.memory.id].final_score:
                merged[item.memory.id] = item
        for item in (*snapshot.semantic, *snapshot.episodic):
            if item.memory.id not in merged or item.final_score > merged[item.memory.id].final_score:
                merged[item.memory.id] = item

        semantic = sorted(
            [item for item in merged.values() if isinstance(item.memory, SemanticMemory)],
            key=lambda item: item.final_score,
            reverse=True,
        )[: self.config.semantic_top_k]
        episodic = sorted(
            [item for item in merged.values() if isinstance(item.memory, EpisodicMemory)],
            key=lambda item: item.final_score,
            reverse=True,
        )[: self.config.episodic_top_k]

        result = RetrievalResult(semantic=semantic, episodic=episodic)
        result.path = "ABCD"
        return result

    def _entity_search(
        self,
        query: str,
        *,
        recent_context: Sequence[str] = (),
        entities: Iterable[str] = (),
        now: datetime | None = None,
        as_of: str | None = None,
        primed_ids: Iterable[str] = (),
        views: Iterable[str] = (),
    ) -> RetrievalResult:
        """Path B：只使用实体关键词硬过滤，不计算语义相似度分数（similarity 设为 0）。"""
        if not entities:
            return RetrievalResult(path="B")
        result = self.search(
            query,
            recent_context=recent_context,
            entities=list(entities),
            now=now,
            as_of=as_of,
            primed_ids=primed_ids,
            views=views,
        )
        # 重新计算分数：只保留 importance + recency + emotion，similarity=0
        primed_set = set(primed_ids)

        def _recompute(item: ScoredMemory) -> float:
            score = (
                0.25 * item.importance
                + 0.20 * item.recency_boost
                + 0.15 * item.emotion_match
            )
            if item.memory.id in primed_set:
                score = min(1.0, score + 0.08)
            return score

        result.semantic = [
            ScoredMemory(
                memory=item.memory,
                semantic_similarity=0.0,
                importance=item.importance,
                recency_boost=item.recency_boost,
                emotion_match=item.emotion_match,
                final_score=_recompute(item),
            )
            for item in result.semantic
        ]
        result.episodic = [
            ScoredMemory(
                memory=item.memory,
                semantic_similarity=0.0,
                importance=item.importance,
                recency_boost=item.recency_boost,
                emotion_match=item.emotion_match,
                final_score=_recompute(item),
            )
            for item in result.episodic
        ]
        result.path = "B"
        return result

    def _emotion_search(
        self,
        query: str,
        *,
        recent_context: Sequence[str] = (),
        entities: Iterable[str] = (),
        now: datetime | None = None,
        as_of: str | None = None,
        primed_ids: Iterable[str] = (),
        views: Iterable[str] = (),
    ) -> RetrievalResult:
        """Path C：以情绪共鸣为主导，语义相似度权重降低，情绪匹配权重提高。"""
        result = self.search(
            query,
            recent_context=recent_context,
            entities=list(entities),
            now=now,
            as_of=as_of,
            primed_ids=primed_ids,
            views=views,
        )
        query_emotion = detect_emotion("\n".join([*recent_context, query]))
        primed_set = set(primed_ids)

        def _recompute(item: ScoredMemory) -> float:
            score = (
                0.30 * item.semantic_similarity
                + 0.25 * item.importance
                + 0.20 * item.recency_boost
                + 0.25 * item.emotion_match
            )
            if item.memory.id in primed_set:
                score = min(1.0, score + 0.08)
            return score

        result.semantic = [
            ScoredMemory(
                memory=item.memory,
                semantic_similarity=item.semantic_similarity,
                importance=item.importance,
                recency_boost=item.recency_boost,
                emotion_match=item.emotion_match,
                final_score=_recompute(item),
            )
            for item in result.semantic
            if query_emotion is None or item.emotion_match > 0.3
        ]
        result.episodic = [
            ScoredMemory(
                memory=item.memory,
                semantic_similarity=item.semantic_similarity,
                importance=item.importance,
                recency_boost=item.recency_boost,
                emotion_match=item.emotion_match,
                final_score=_recompute(item),
            )
            for item in result.episodic
            if query_emotion is None or item.emotion_match > 0.3
        ]
        result.path = "C"
        return result

    def _snapshot_search(
        self,
        query: str,
        *,
        now: datetime | None = None,
        as_of: str | None = None,
        views: Iterable[str] = (),
    ) -> RetrievalResult:
        """Path D：返回 as_of 时间点生效的记忆状态，不参与检索排序，仅作时间快照。"""
        if as_of is None:
            return RetrievalResult(path="D")
        # as_of 检索会通过 _eligible 过滤掉未生效或已过期的记忆
        return self.search(
            query,
            now=now,
            as_of=as_of,
            views=views,
        )

    def _rank_type(
        self,
        memories: list[Memory],
        vectors: dict[str, tuple[float, ...]],
        query_vector: Sequence[float],
        query_emotion: str | None,
        now: datetime,
        top_k: int,
        primed_ids: frozenset[str] | set[str] = frozenset(),
    ) -> list[ScoredMemory]:
        if isinstance(top_k, bool) or not isinstance(top_k, int) or top_k < 0:
            raise ValueError("top_k must be a non-negative integer")
        candidates = []
        for memory in memories:
            similarity = cosine_similarity(query_vector, vectors[memory.id])
            candidates.append((similarity, memory))
        candidates.sort(key=lambda item: item[0], reverse=True)
        # 启动效应候选保送：工作记忆里的条目即使相似度排名靠后，也保留进候选池。
        if primed_ids:
            kept = [
                item
                for index, item in enumerate(candidates)
                if index < self.config.retrieval_candidate_limit or item[1].id in primed_ids
            ]
            candidates = kept
        else:
            candidates = candidates[: self.config.retrieval_candidate_limit]

        scored = []
        for similarity, memory in candidates:
            last_activated = memory.last_activated or (
                memory.timestamp if isinstance(memory, EpisodicMemory) else None
            )
            if last_activated is None:
                continue
            age_days = max(
                0.0, (now - parse_iso8601(last_activated)).total_seconds() / 86400
            )
            recency = math.exp(
                -math.log(2) * age_days / self.config.recency_half_life_days
            )
            emotion = emotion_match(memory, query_emotion)
            final_score = (
                0.40 * similarity
                + 0.25 * memory.importance
                + 0.20 * recency
                + 0.15 * emotion
            )
            # 反漠视（v0.2.0）：未平复的情感事件强制优先回忆，加 0.10 权重并豁免分数门槛。
            unresolved = is_unresolved_episode(memory)
            if unresolved:
                final_score = min(1.0, final_score + 0.10)
            # 启动效应（v0.2.1 遗留）：工作记忆（上一轮刚触及）的条目加 0.08。
            if memory.id in primed_ids:
                final_score = min(1.0, final_score + 0.08)
            if final_score < self.config.min_retrieval_score and not unresolved:
                continue
            scored.append(
                ScoredMemory(
                    memory=memory,
                    semantic_similarity=similarity,
                    importance=memory.importance,
                    recency_boost=recency,
                    emotion_match=emotion,
                    final_score=final_score,
                )
            )
        scored.sort(key=lambda item: (item.final_score, item.memory.id), reverse=True)
        return scored[:top_k]

    def build_injection(
        self,
        core: CoreMemory,
        retrieved: RetrievalResult,
        *,
        token_limit: int | None = None,
    ) -> Injection:
        token_limit = token_limit if token_limit is not None else self.config.injection_token_limit
        if isinstance(token_limit, bool) or not isinstance(token_limit, int) or token_limit <= 0:
            raise ValueError("token_limit must be a positive integer")
        core_block = (
            "# 长期记忆（Core，始终完整保留）\n"
            + json.dumps(core.to_dict(), ensure_ascii=False, separators=(",", ":"))
        )
        core_tokens = estimate_tokens(core_block)
        if core_tokens > token_limit:
            raise CoreMemoryLimitError(
                f"complete Core Memory needs {core_tokens} estimated tokens; "
                f"injection limit is {token_limit}"
            )

        blocks: list[tuple[float, str, ScoredMemory]] = []
        for item in retrieved.semantic:
            memory = item.memory
            assert isinstance(memory, SemanticMemory)
            text = (
                f"[{memory.id}] {memory.type}: {memory.subject} / {memory.predicate} / "
                f"{memory.object}\n{memory.detail}\n"
                f"confidence={memory.confidence:.2f}, importance={memory.importance:.2f}"
            )
            blocks.append((item.final_score, text, item))
        for item in retrieved.episodic:
            memory = item.memory
            assert isinstance(memory, EpisodicMemory)
            moments = "\n".join(f"- {moment}" for moment in memory.key_moments)
            text = (
                f"[{memory.id}] {memory.timestamp}: {memory.summary}\n"
                f"关键原话/时刻：\n{moments or '- （无）'}\n"
                f"情绪：{', '.join(memory.emotions) or '未标注'}"
            )
            blocks.append((item.final_score, text, item))

        blocks.sort(key=lambda block: block[0], reverse=True)
        selected_semantic: list[ScoredMemory] = []
        selected_episodic: list[ScoredMemory] = []
        context = core_block
        used_tokens = core_tokens
        semantic_section_added = False
        episodic_section_added = False
        for _, text, item in blocks:
            is_semantic = isinstance(item.memory, SemanticMemory)
            if is_semantic:
                heading = "" if semantic_section_added else "\n\n# 相关语义记忆\n"
            else:
                heading = "" if episodic_section_added else "\n\n# 相关情节记忆\n"
            block = heading + ("\n\n" if not heading else "") + text
            block_tokens = estimate_tokens(block)
            if used_tokens + block_tokens > token_limit:
                continue
            context += block
            used_tokens += block_tokens
            if is_semantic:
                semantic_section_added = True
                selected_semantic.append(item)
            else:
                episodic_section_added = True
                selected_episodic.append(item)

        selected = RetrievalResult(selected_semantic, selected_episodic)
        return Injection(context, core, selected, used_tokens, token_limit)


_POSITIVE_EMOTIONS = ("开心", "高兴", "喜欢", "感动", "幸福", "安心", "谢谢", "happy", "love", "glad")
_NEGATIVE_EMOTIONS = ("难过", "伤心", "焦虑", "害怕", "生气", "孤独", "失望", "痛苦", "sad", "angry", "afraid", "lonely")


def detect_emotion(text: str) -> str | None:
    lowered = text.casefold()
    positive = any(word in lowered for word in _POSITIVE_EMOTIONS)
    negative = any(word in lowered for word in _NEGATIVE_EMOTIONS)
    if positive and negative:
        return "mixed"
    if positive:
        return "positive"
    if negative:
        return "negative"
    return None


def emotion_match(memory: Memory, query_emotion: str | None) -> float:
    if query_emotion is None:
        return 0.5
    if isinstance(memory, EpisodicMemory):
        emotions = " ".join(memory.emotions).casefold()
        memory_emotion = detect_emotion(emotions)
        if memory_emotion is None:
            memory_emotion = "mixed" if len(memory.emotions) > 1 else "neutral"
    else:
        memory_emotion = memory.emotion_valence
    if memory_emotion == query_emotion:
        return 1.0
    if memory_emotion == "mixed" or query_emotion == "mixed":
        return 0.6
    if memory_emotion == "neutral":
        return 0.3
    return 0.0
