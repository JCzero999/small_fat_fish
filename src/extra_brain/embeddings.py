from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from typing import Protocol, Sequence


class EmbeddingProvider(Protocol):
    @property
    def model_name(self) -> str: ...

    @property
    def dimensions(self) -> int: ...

    def embed(self, text: str) -> Sequence[float]: ...


@dataclass(frozen=True, slots=True)
class VectorEntry:
    memory_id: str
    memory_type: str
    model: str
    values: tuple[float, ...]

    @property
    def dimensions(self) -> int:
        return len(self.values)


class LocalHashingEmbedder:
    """Deterministic multilingual feature hashing with no network or model download."""

    _word_pattern = re.compile(r"[a-zA-Z0-9_]+|[㐀-鿿]")

    def __init__(self, dimensions: int = 384) -> None:
        if isinstance(dimensions, bool) or not isinstance(dimensions, int) or dimensions < 16:
            raise ValueError("dimensions must be an integer of at least 16")
        self._dimensions = dimensions

    @property
    def model_name(self) -> str:
        return "local-hashing-cjk-v1"

    @property
    def dimensions(self) -> int:
        return self._dimensions

    def embed(self, text: str) -> tuple[float, ...]:
        if not isinstance(text, str):
            raise TypeError("text must be a string")
        tokens = self._word_pattern.findall(text.lower())
        features: list[str] = []
        features.extend(f"tok:{token}" for token in tokens)

        cjk_runs = re.findall(r"[㐀-鿿]+", text.lower())
        for run in cjk_runs:
            features.extend(f"cjk2:{run[index:index + 2]}" for index in range(len(run) - 1))

        latin_words = re.findall(r"[a-zA-Z0-9_]+", text.lower())
        features.extend(
            f"word2:{latin_words[index]}_{latin_words[index + 1]}"
            for index in range(len(latin_words) - 1)
        )

        vector = [0.0] * self._dimensions
        for feature in features:
            digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
            bucket = int.from_bytes(digest[:4], "big") % self._dimensions
            sign = 1.0 if digest[4] & 1 else -1.0
            vector[bucket] += sign

        norm = math.sqrt(sum(value * value for value in vector))
        if norm:
            vector = [value / norm for value in vector]
        return tuple(vector)


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right):
        raise ValueError("vector dimensions do not match")
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if not left_norm or not right_norm:
        return 0.0
    # Feature hashing uses signed features, so normalize from [-1, 1] to [0, 1].
    return max(0.0, min(1.0, (dot / (left_norm * right_norm) + 1.0) / 2.0))


class MemoryIndexer:
    def __init__(self, provider: EmbeddingProvider) -> None:
        self.provider = provider

    def entry_for(self, memory_id: str, memory_type: str, text: str) -> VectorEntry:
        values = tuple(float(value) for value in self.provider.embed(text))
        if len(values) != self.provider.dimensions:
            raise ValueError(
                f"embedding provider returned {len(values)} dimensions; "
                f"expected {self.provider.dimensions}"
            )
        if not all(math.isfinite(value) for value in values):
            raise ValueError("embedding provider returned a non-finite value")
        return VectorEntry(memory_id, memory_type, self.provider.model_name, values)
