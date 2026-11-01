from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
from dataclasses import dataclass, field
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


class OpenAICompatibleEmbedder:
    """v0.5.0: 真实 embedding Provider 适配器。

    通过 OpenAI 兼容的 /embeddings 接口调用宿主 embedding 服务，
    本地哈希向量作为 fallback。需要设置 EMBEDDING_API_BASE 和
    EMBEDDING_API_KEY 环境变量。
    """

    def __init__(
        self,
        *,
        api_base: str | None = None,
        api_key: str | None = None,
        model: str = "text-embedding-v3",
        dimensions: int = 384,
        fallback: EmbeddingProvider | None = None,
    ) -> None:
        import urllib.request
        import urllib.error

        self._api_base = api_base or os.environ.get("EMBEDDING_API_BASE", "")
        self._api_key = api_key or os.environ.get("EMBEDDING_API_KEY", "")
        self._model = model
        self._dimensions = dimensions
        self._fallback = fallback or LocalHashingEmbedder(dimensions)
        self._urllib = (urllib.request, urllib.error)

    @property
    def model_name(self) -> str:
        return f"openai-compatible:{self._model}"

    @property
    def dimensions(self) -> int:
        return self._dimensions

    def embed(self, text: str) -> tuple[float, ...]:
        if not isinstance(text, str):
            raise TypeError("text must be a string")
        if not self._api_base:
            return self._fallback.embed(text)
        try:
            payload = json.dumps(
                {"input": text, "model": self._model, "encoding_format": "float"}
            ).encode("utf-8")
            req = self._urllib[0].Request(
                self._api_base.rstrip("/") + "/embeddings",
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self._api_key}",
                },
                method="POST",
            )
            with self._urllib[0].urlopen(req, timeout=30) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            data = body["data"][0]["embedding"]
            values = tuple(float(v) for v in data)
            if len(values) != self._dimensions:
                # 维度不匹配时降级到 fallback
                return self._fallback.embed(text)
            return values
        except Exception:
            # 任何网络/解析错误都降级到本地哈希向量
            return self._fallback.embed(text)


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


# =====================================================================
# v0.5.0 HNSW 向量索引（纯 Python 标准库实现）
# =====================================================================


class HNSWNode:
    """HNSW 图节点，包含向量和邻接表（各层）。"""

    __slots__ = ("id", "vector", "layers", "level")

    def __init__(self, node_id: str, vector: Sequence[float], max_level: int) -> None:
        self.id = node_id
        self.vector = tuple(float(v) for v in vector)
        # layers[i] 是第 i 层的邻居 ID 集合
        self.layers: list[set[str]] = [set() for _ in range(max_level + 1)]
        self.level = max_level


class HNSWIndex:
    """v0.5.0 HNSW（分层导航小世界图）向量索引。

    当 cell < 2000 时使用精确扫描，超过自动切 HNSW（M=16 / efConstruction=200 / efSearch=64）。
    层高走 id 哈希保证可复现。
    """

    def __init__(
        self,
        *,
        dimensions: int,
        M: int = 16,
        ef_construction: int = 200,
        ef_search: int = 64,
        m_l: float = 1.0 / math.log(2.0),
    ) -> None:
        self.dimensions = dimensions
        self.M = M
        self.ef_construction = ef_construction
        self.ef_search = ef_search
        self.m_l = m_l
        self.nodes: dict[str, HNSWNode] = {}
        self.entry_point: str | None = None
        self.max_level = 0

    def _random_level(self, node_id: str) -> int:
        """基于 node_id 哈希的层高，保证可复现。"""
        digest = hashlib.sha256(node_id.encode("utf-8")).digest()
        r = int.from_bytes(digest[:8], "big") / (1 << 64)
        level = int(-math.log(1.0 - r) * self.m_l)
        return max(0, level)

    def _distance(self, a: Sequence[float], b: Sequence[float]) -> float:
        return 1.0 - cosine_similarity(a, b)

    def _search_layer(
        self,
        query: Sequence[float],
        entry_id: str,
        ef: int,
        layer: int,
    ) -> list[tuple[float, str]]:
        """单层 best-first 搜索，返回按距离升序排列的 top-ef 候选 (dist, id)。"""
        visited: set[str] = {entry_id}
        entry_dist = self._distance(query, self.nodes[entry_id].vector)
        candidates: list[tuple[float, str]] = [(entry_dist, entry_id)]
        results: list[tuple[float, str]] = [(entry_dist, entry_id)]
        index = 0
        while index < len(candidates):
            dist, cid = candidates[index]
            index += 1
            if len(results) >= ef and dist > results[-1][0]:
                break
            for nid in self.nodes[cid].layers[layer]:
                if nid in visited:
                    continue
                visited.add(nid)
                ndist = self._distance(query, self.nodes[nid].vector)
                if len(results) < ef or ndist < results[-1][0]:
                    insert_at = len(results)
                    for i, (d, _) in enumerate(results):
                        if ndist < d:
                            insert_at = i
                            break
                    results.insert(insert_at, (ndist, nid))
                    if len(results) > ef:
                        results.pop()
                    insert_c = len(candidates)
                    for i, (d, _) in enumerate(candidates):
                        if ndist < d:
                            insert_c = i
                            break
                    candidates.insert(insert_c, (ndist, nid))
        return results

    def _greedy_descent(
        self, query: Sequence[float], entry_id: str, layer: int
    ) -> tuple[str, float]:
        """在 layer 上贪婪下降到局部最近点，用于跨层导航。"""
        ep = entry_id
        ep_dist = self._distance(query, self.nodes[ep].vector)
        improved = True
        while improved:
            improved = False
            for nb in self.nodes[ep].layers[layer]:
                d = self._distance(query, self.nodes[nb].vector)
                if d < ep_dist:
                    ep, ep_dist = nb, d
                    improved = True
        return ep, ep_dist

    def add(self, node_id: str, vector: Sequence[float]) -> None:
        if node_id in self.nodes:
            return
        level = self._random_level(node_id)
        node = HNSWNode(node_id, vector, level)
        self.nodes[node_id] = node

        if self.entry_point is None:
            self.entry_point = node_id
            self.max_level = level
            return

        # 从顶层逐层贪心下降到新节点的最高层
        ep = self.entry_point
        for layer in range(self.max_level, level, -1):
            ep, _ = self._greedy_descent(vector, ep, layer)

        # 在 0..min(level, max_level) 层建立连接
        for layer in range(min(level, self.max_level), -1, -1):
            found = self._search_layer(vector, ep, self.ef_construction, layer)
            selected = [nid for _, nid in found if nid != node_id][: self.M]
            node.layers[layer].update(selected)
            for nid in selected:
                other = self.nodes[nid]
                other.layers[layer].add(node_id)
                # 容量控制：邻居超过 M*2 时保留最近的 M*2 个
                if len(other.layers[layer]) > self.M * 2:
                    ranked = sorted(
                        (
                            self._distance(other.vector, self.nodes[x].vector),
                            x,
                        )
                        for x in other.layers[layer]
                    )
                    keep = {x for _, x in ranked[: self.M * 2]}
                    other.layers[layer] = keep
            if found:
                ep = found[0][1] if found[0][1] != node_id else (
                    found[1][1] if len(found) > 1 else ep
                )

        if level > self.max_level:
            self.entry_point = node_id
            self.max_level = level

    def search(self, query: Sequence[float], k: int = 5) -> list[tuple[str, float]]:
        """返回 (node_id, similarity) 按相似度降序排列的 top-k。"""
        if not self.nodes:
            return []
        if len(self.nodes) <= k:
            # 小数据集直接精确扫描
            scored = [
                (nid, cosine_similarity(query, node.vector))
                for nid, node in self.nodes.items()
            ]
            scored.sort(key=lambda x: x[1], reverse=True)
            return scored[:k]

        if self.entry_point is None:
            return []

        ep = self.entry_point
        for layer in range(self.max_level, 0, -1):
            ep, _ = self._greedy_descent(query, ep, layer)

        candidates = self._search_layer(query, ep, max(self.ef_search, k), 0)
        results = [(nid, 1.0 - dist) for dist, nid in candidates]
        results.sort(key=lambda x: x[1], reverse=True)
        return results[:k]

    def __len__(self) -> int:
        return len(self.nodes)


class VectorIndex:
    """v0.5.0 向量索引容器：小数据集精确扫描，大数据集自动切 HNSW。

    cell < 2000 时精确扫描，超过自动切 HNSW（M=16 / efConstruction=200 / efSearch=64）。
    """

    def __init__(
        self,
        *,
        dimensions: int,
        hnsw_threshold: int = 2000,
    ) -> None:
        self.dimensions = dimensions
        self.hnsw_threshold = hnsw_threshold
        self._vectors: dict[str, tuple[float, ...]] = {}
        self._hnsw: HNSWIndex | None = None
        self._use_hnsw = False

    def add(self, memory_id: str, vector: Sequence[float]) -> None:
        values = tuple(float(v) for v in vector)
        self._vectors[memory_id] = values
        if self._use_hnsw:
            assert self._hnsw is not None
            self._hnsw.add(memory_id, values)
        elif len(self._vectors) >= self.hnsw_threshold:
            self._migrate_to_hnsw()

    def _migrate_to_hnsw(self) -> None:
        self._hnsw = HNSWIndex(dimensions=self.dimensions)
        for nid, vec in self._vectors.items():
            self._hnsw.add(nid, vec)
        self._use_hnsw = True

    def search(self, query: Sequence[float], k: int = 5) -> list[tuple[str, float]]:
        if not self._vectors:
            return []
        if not self._use_hnsw:
            scored = [
                (nid, cosine_similarity(query, vec))
                for nid, vec in self._vectors.items()
            ]
            scored.sort(key=lambda x: x[1], reverse=True)
            return scored[:k]
        assert self._hnsw is not None
        return self._hnsw.search(query, k)

    def __len__(self) -> int:
        return len(self._vectors)


def build_vector_index(
    entries: dict[str, VectorEntry],
    *,
    hnsw_threshold: int = 2000,
) -> VectorIndex:
    """从 VectorEntry 字典构建 VectorIndex。"""
    if not entries:
        return VectorIndex(dimensions=384, hnsw_threshold=hnsw_threshold)
    first = next(iter(entries.values()))
    index = VectorIndex(dimensions=first.dimensions, hnsw_threshold=hnsw_threshold)
    for nid, entry in entries.items():
        index.add(nid, entry.values)
    return index
