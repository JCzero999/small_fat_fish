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
    SemanticMemory,
    estimate_tokens,
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

    @property
    def memory_ids(self) -> list[str]:
        return [item.memory.id for item in (*self.semantic, *self.episodic)]

    def to_dict(self) -> dict:
        return {
            "semantic": [item.to_dict() for item in self.semantic],
            "episodic": [item.to_dict() for item in self.episodic],
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
            and self._eligible(memory, normalized_entities, as_of_dt)
        ]
        episodic_memories = [
            memory
            for memory in self.store.list_episodic()
            if memory.id in episodic_vectors
            and self._eligible(memory, normalized_entities, as_of_dt)
        ]

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
            ),
        )

    def _eligible(
        self, memory: Memory, entities: list[str], as_of: datetime | None = None
    ) -> bool:
        if memory.archived or memory.importance < self.config.min_importance:
            return False
        if isinstance(memory, SemanticMemory) and memory.conflict_flag and memory.confidence < 0.25:
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

    def _rank_type(
        self,
        memories: list[Memory],
        vectors: dict[str, tuple[float, ...]],
        query_vector: Sequence[float],
        query_emotion: str | None,
        now: datetime,
        top_k: int,
    ) -> list[ScoredMemory]:
        if isinstance(top_k, bool) or not isinstance(top_k, int) or top_k < 0:
            raise ValueError("top_k must be a non-negative integer")
        candidates = []
        for memory in memories:
            similarity = cosine_similarity(query_vector, vectors[memory.id])
            candidates.append((similarity, memory))
        candidates.sort(key=lambda item: item[0], reverse=True)
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
            if final_score < self.config.min_retrieval_score:
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
