from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Mapping, Sequence

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
from .models import CoreMemory, utc_now
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
    ) -> None:
        self.config = config or MemoryConfig()
        self.embedder = embedder or LocalHashingEmbedder(self.config.embedding_dimensions)
        self.store = MemoryStore(db_path, core_token_limit=self.config.core_token_limit)
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

    def consolidate(
        self,
        conversation: str | Sequence[ConversationMessage | Mapping[str, str]],
        *,
        session_id: str | None = None,
        now: datetime | None = None,
    ) -> ConsolidationReport:
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
    ) -> RetrievalResult:
        return self.retriever.search(
            query,
            recent_context=recent_context,
            entities=entities,
            now=now,
            as_of=as_of,
        )

    def build_injection(
        self,
        query: str,
        *,
        recent_context: Sequence[str] = (),
        entities: Sequence[str] = (),
        token_limit: int | None = None,
        now: datetime | None = None,
    ) -> Injection:
        retrieved = self.retrieve(
            query, recent_context=recent_context, entities=entities, now=now
        )
        injection = self.retriever.build_injection(
            self.core, retrieved, token_limit=token_limit
        )
        # Only memories that actually fit into the injected context count as activated.
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

    def close(self) -> None:
        self.store.close()

    def __enter__(self) -> ExtraBrain:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
