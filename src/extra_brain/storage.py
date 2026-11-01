from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from uuid import uuid4

from .embeddings import VectorEntry
from .models import (
    CoreMemory,
    EpisodicMemory,
    Memory,
    SemanticMemory,
    StyleMemory,
    to_iso8601,
    utc_now,
)


@dataclass(slots=True)
class WriteBatch:
    core: CoreMemory | None = None
    semantic: list[SemanticMemory] = field(default_factory=list)
    episodic: list[EpisodicMemory] = field(default_factory=list)
    style: list[StyleMemory] = field(default_factory=list)
    vectors: dict[str, VectorEntry] = field(default_factory=dict)
    operation: str = "write_batch"
    transaction_id: str = field(default_factory=lambda: uuid4().hex)
    audit_details: dict[str, Any] | None = None


class MemoryStore:
    """SQLite persistence. A batch, its vectors, and its audit records commit together."""

    def __init__(self, path: str | Path, *, core_token_limit: int = 1200) -> None:
        self.path = str(path)
        self.core_token_limit = core_token_limit
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA busy_timeout = 5000")
        if self.path != ":memory:":
            self._connection.execute("PRAGMA journal_mode = WAL")
        self._initialize()

    def _initialize(self) -> None:
        with self._connection:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS core_memory (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    data TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS semantic_memories (
                    id TEXT PRIMARY KEY,
                    subject TEXT NOT NULL,
                    predicate TEXT NOT NULL,
                    importance REAL NOT NULL,
                    confidence REAL NOT NULL,
                    decay_score REAL NOT NULL,
                    protected INTEGER NOT NULL,
                    archived INTEGER NOT NULL,
                    last_activated TEXT NOT NULL,
                    data TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_semantic_retrieval
                    ON semantic_memories(archived, decay_score, importance);
                CREATE INDEX IF NOT EXISTS idx_semantic_conflict
                    ON semantic_memories(subject, predicate);

                CREATE TABLE IF NOT EXISTS episodic_memories (
                    id TEXT PRIMARY KEY,
                    timestamp TEXT NOT NULL,
                    importance REAL NOT NULL,
                    decay_score REAL NOT NULL,
                    protected INTEGER NOT NULL,
                    archived INTEGER NOT NULL,
                    last_activated TEXT,
                    data TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_episodic_retrieval
                    ON episodic_memories(archived, decay_score, importance);

                CREATE TABLE IF NOT EXISTS style_memories (
                    id TEXT PRIMARY KEY,
                    category TEXT NOT NULL,
                    importance REAL NOT NULL,
                    last_observed TEXT NOT NULL,
                    data TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS relationship_models (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    relationship_type TEXT NOT NULL,
                    closeness REAL NOT NULL,
                    trust REAL NOT NULL,
                    familiarity REAL NOT NULL,
                    last_updated TEXT NOT NULL,
                    interaction_count INTEGER NOT NULL,
                    positive_interactions INTEGER NOT NULL,
                    negative_interactions INTEGER NOT NULL,
                    notes TEXT NOT NULL,
                    created_at TEXT,
                    data TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_relationship_user
                    ON relationship_models(user_id);

                CREATE TABLE IF NOT EXISTS user_schemas (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    schema_type TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    last_updated TEXT NOT NULL,
                    observation_count INTEGER NOT NULL,
                    created_at TEXT,
                    data TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_user_schema_user
                    ON user_schemas(user_id);

                CREATE TABLE IF NOT EXISTS narrative_layers (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    theme TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    last_updated TEXT NOT NULL,
                    created_at TEXT,
                    data TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_narrative_user
                    ON narrative_layers(user_id);

                CREATE TABLE IF NOT EXISTS memory_associations (
                    id TEXT PRIMARY KEY,
                    source_memory_id TEXT NOT NULL,
                    target_memory_id TEXT NOT NULL,
                    strength REAL NOT NULL,
                    co_activation_count INTEGER NOT NULL,
                    last_co_activated TEXT,
                    created_at TEXT,
                    data TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_association_source
                    ON memory_associations(source_memory_id);
                CREATE INDEX IF NOT EXISTS idx_association_target
                    ON memory_associations(target_memory_id);

                CREATE TABLE IF NOT EXISTS proactive_configs (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    enabled INTEGER NOT NULL,
                    min_interval_minutes INTEGER NOT NULL,
                    daily_limit INTEGER NOT NULL,
                    silence_threshold_minutes INTEGER NOT NULL,
                    quiet_hours_start INTEGER NOT NULL,
                    quiet_hours_end INTEGER NOT NULL,
                    last_proactive_at TEXT,
                    daily_count INTEGER NOT NULL,
                    daily_reset_date TEXT,
                    created_at TEXT,
                    data TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_proactive_user
                    ON proactive_configs(user_id);

                CREATE TABLE IF NOT EXISTS memory_vectors (
                    memory_id TEXT PRIMARY KEY,
                    memory_type TEXT NOT NULL,
                    model TEXT NOT NULL,
                    dimensions INTEGER NOT NULL,
                    values_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_vector_lookup
                    ON memory_vectors(memory_type, model, dimensions);

                CREATE TABLE IF NOT EXISTS operation_log (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    transaction_id TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    before_json TEXT,
                    after_json TEXT,
                    details_json TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_operation_entity
                    ON operation_log(entity_type, entity_id, sequence);
                """
            )

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self) -> MemoryStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def get_core(self) -> CoreMemory:
        with self._lock:
            row = self._connection.execute(
                "SELECT data FROM core_memory WHERE singleton = 1"
            ).fetchone()
        return CoreMemory.from_dict(json.loads(row["data"])) if row else CoreMemory()

    def save_core(self, core: CoreMemory) -> None:
        self.apply_batch(WriteBatch(core=core, operation="core.write"))

    def get_semantic(self, memory_id: str) -> SemanticMemory | None:
        row = self._connection.execute(
            "SELECT data FROM semantic_memories WHERE id = ?", (memory_id,)
        ).fetchone()
        return SemanticMemory.from_dict(json.loads(row["data"])) if row else None

    def get_episodic(self, memory_id: str) -> EpisodicMemory | None:
        row = self._connection.execute(
            "SELECT data FROM episodic_memories WHERE id = ?", (memory_id,)
        ).fetchone()
        return EpisodicMemory.from_dict(json.loads(row["data"])) if row else None

    def get_memory(self, memory_id: str) -> Memory | None:
        return self.get_semantic(memory_id) or self.get_episodic(memory_id)

    def save_semantic(self, memory: SemanticMemory, *, vector: VectorEntry | None = None) -> None:
        vectors = {memory.id: vector} if vector else {}
        self.apply_batch(
            WriteBatch(semantic=[memory], vectors=vectors, operation="semantic.write")
        )

    def save_episodic(self, memory: EpisodicMemory, *, vector: VectorEntry | None = None) -> None:
        vectors = {memory.id: vector} if vector else {}
        self.apply_batch(
            WriteBatch(episodic=[memory], vectors=vectors, operation="episodic.write")
        )

    def save_style(self, memory: StyleMemory) -> None:
        self.apply_batch(WriteBatch(style=[memory], operation="style.write"))

    # --- v0.4.0 陪伴层数据模型存储 ---

    def save_relationship(self, model: RelationshipModel) -> None:
        self.apply_batch(
            WriteBatch(
                operation="relationship.write",
                transaction_id=uuid4().hex,
                audit_details={"model": model.to_dict()},
            )
        )
        with self._lock, self._connection:
            data = _to_json(model.to_dict())
            before = self._connection.execute(
                "SELECT data FROM relationship_models WHERE id = ?", (model.id,)
            ).fetchone()
            self._connection.execute(
                """
                INSERT INTO relationship_models(
                    id, user_id, relationship_type, closeness, trust, familiarity,
                    last_updated, interaction_count, positive_interactions,
                    negative_interactions, notes, created_at, data
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    user_id = excluded.user_id,
                    relationship_type = excluded.relationship_type,
                    closeness = excluded.closeness,
                    trust = excluded.trust,
                    familiarity = excluded.familiarity,
                    last_updated = excluded.last_updated,
                    interaction_count = excluded.interaction_count,
                    positive_interactions = excluded.positive_interactions,
                    negative_interactions = excluded.negative_interactions,
                    notes = excluded.notes,
                    created_at = excluded.created_at,
                    data = excluded.data
                """,
                (
                    model.id,
                    model.user_id,
                    model.relationship_type,
                    model.closeness,
                    model.trust,
                    model.familiarity,
                    model.last_updated,
                    model.interaction_count,
                    model.positive_interactions,
                    model.negative_interactions,
                    model.notes,
                    model.created_at,
                    data,
                ),
            )
            self._audit(
                WriteBatch(operation="relationship.write", transaction_id=uuid4().hex),
                "relationship",
                model.id,
                before["data"] if before else None,
                data,
            )

    def get_relationship(self, model_id: str) -> RelationshipModel | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT data FROM relationship_models WHERE id = ?", (model_id,)
            ).fetchone()
        return RelationshipModel.from_dict(json.loads(row["data"])) if row else None

    def get_relationship_by_user(self, user_id: str) -> RelationshipModel | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT data FROM relationship_models WHERE user_id = ?", (user_id,)
            ).fetchone()
        return RelationshipModel.from_dict(json.loads(row["data"])) if row else None

    def list_relationships(self) -> list[RelationshipModel]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT data FROM relationship_models ORDER BY id"
            ).fetchall()
        return [RelationshipModel.from_dict(json.loads(row["data"])) for row in rows]

    def save_user_schema(self, schema: UserSchema) -> None:
        self.apply_batch(
            WriteBatch(
                operation="user_schema.write",
                transaction_id=uuid4().hex,
                audit_details={"schema": schema.to_dict()},
            )
        )
        with self._lock, self._connection:
            data = _to_json(schema.to_dict())
            before = self._connection.execute(
                "SELECT data FROM user_schemas WHERE id = ?", (schema.id,)
            ).fetchone()
            self._connection.execute(
                """
                INSERT INTO user_schemas(
                    id, user_id, schema_type, traits, emotional_patterns,
                    preferred_topics, avoided_topics, confidence,
                    last_updated, observation_count, created_at, data
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    user_id = excluded.user_id,
                    schema_type = excluded.schema_type,
                    traits = excluded.traits,
                    emotional_patterns = excluded.emotional_patterns,
                    preferred_topics = excluded.preferred_topics,
                    avoided_topics = excluded.avoided_topics,
                    confidence = excluded.confidence,
                    last_updated = excluded.last_updated,
                    observation_count = excluded.observation_count,
                    created_at = excluded.created_at,
                    data = excluded.data
                """,
                (
                    schema.id,
                    schema.user_id,
                    schema.schema_type,
                    json.dumps(schema.traits, ensure_ascii=False),
                    json.dumps(schema.emotional_patterns, ensure_ascii=False),
                    json.dumps(schema.preferred_topics, ensure_ascii=False),
                    json.dumps(schema.avoided_topics, ensure_ascii=False),
                    schema.confidence,
                    schema.last_updated,
                    schema.observation_count,
                    schema.created_at,
                    data,
                ),
            )
            self._audit(
                WriteBatch(operation="user_schema.write", transaction_id=uuid4().hex),
                "user_schema",
                schema.id,
                before["data"] if before else None,
                data,
            )

    def get_user_schema(self, schema_id: str) -> UserSchema | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT data FROM user_schemas WHERE id = ?", (schema_id,)
            ).fetchone()
        return UserSchema.from_dict(json.loads(row["data"])) if row else None

    def get_user_schema_by_user(self, user_id: str) -> UserSchema | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT data FROM user_schemas WHERE user_id = ?", (user_id,)
            ).fetchone()
        return UserSchema.from_dict(json.loads(row["data"])) if row else None

    def list_user_schemas(self) -> list[UserSchema]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT data FROM user_schemas ORDER BY id"
            ).fetchall()
        return [UserSchema.from_dict(json.loads(row["data"])) for row in rows]

    def save_narrative(self, narrative: NarrativeLayer) -> None:
        self.apply_batch(
            WriteBatch(
                operation="narrative.write",
                transaction_id=uuid4().hex,
                audit_details={"narrative": narrative.to_dict()},
            )
        )
        with self._lock, self._connection:
            data = _to_json(narrative.to_dict())
            before = self._connection.execute(
                "SELECT data FROM narrative_layers WHERE id = ?", (narrative.id,)
            ).fetchone()
            self._connection.execute(
                """
                INSERT INTO narrative_layers(
                    id, user_id, phase, theme, event_ids, summary,
                    last_updated, created_at, data
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    user_id = excluded.user_id,
                    phase = excluded.phase,
                    theme = excluded.theme,
                    event_ids = excluded.event_ids,
                    summary = excluded.summary,
                    last_updated = excluded.last_updated,
                    created_at = excluded.created_at,
                    data = excluded.data
                """,
                (
                    narrative.id,
                    narrative.user_id,
                    narrative.phase,
                    narrative.theme,
                    json.dumps(narrative.event_ids, ensure_ascii=False),
                    narrative.summary,
                    narrative.last_updated,
                    narrative.created_at,
                    data,
                ),
            )
            self._audit(
                WriteBatch(operation="narrative.write", transaction_id=uuid4().hex),
                "narrative",
                narrative.id,
                before["data"] if before else None,
                data,
            )

    def get_narrative(self, narrative_id: str) -> NarrativeLayer | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT data FROM narrative_layers WHERE id = ?", (narrative_id,)
            ).fetchone()
        return NarrativeLayer.from_dict(json.loads(row["data"])) if row else None

    def get_narrative_by_user(self, user_id: str) -> NarrativeLayer | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT data FROM narrative_layers WHERE user_id = ?", (user_id,)
            ).fetchone()
        return NarrativeLayer.from_dict(json.loads(row["data"])) if row else None

    def list_narratives(self) -> list[NarrativeLayer]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT data FROM narrative_layers ORDER BY id"
            ).fetchall()
        return [NarrativeLayer.from_dict(json.loads(row["data"])) for row in rows]

    def save_association(self, association: MemoryAssociation) -> None:
        self.apply_batch(
            WriteBatch(
                operation="association.write",
                transaction_id=uuid4().hex,
                audit_details={"association": association.to_dict()},
            )
        )
        with self._lock, self._connection:
            data = _to_json(association.to_dict())
            before = self._connection.execute(
                "SELECT data FROM memory_associations WHERE id = ?", (association.id,)
            ).fetchone()
            self._connection.execute(
                """
                INSERT INTO memory_associations(
                    id, source_memory_id, target_memory_id, strength,
                    co_activation_count, last_co_activated, created_at, data
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    source_memory_id = excluded.source_memory_id,
                    target_memory_id = excluded.target_memory_id,
                    strength = excluded.strength,
                    co_activation_count = excluded.co_activation_count,
                    last_co_activated = excluded.last_co_activated,
                    created_at = excluded.created_at,
                    data = excluded.data
                """,
                (
                    association.id,
                    association.source_memory_id,
                    association.target_memory_id,
                    association.strength,
                    association.co_activation_count,
                    association.last_co_activated,
                    association.created_at,
                    data,
                ),
            )
            self._audit(
                WriteBatch(operation="association.write", transaction_id=uuid4().hex),
                "association",
                association.id,
                before["data"] if before else None,
                data,
            )

    def get_association(self, association_id: str) -> MemoryAssociation | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT data FROM memory_associations WHERE id = ?", (association_id,)
            ).fetchone()
        return MemoryAssociation.from_dict(json.loads(row["data"])) if row else None

    def list_associations_for_memory(self, memory_id: str) -> list[MemoryAssociation]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT data FROM memory_associations
                WHERE source_memory_id = ? OR target_memory_id = ?
                ORDER BY strength DESC
                """,
                (memory_id, memory_id),
            ).fetchall()
        return [MemoryAssociation.from_dict(json.loads(row["data"])) for row in rows]

    def save_proactive_config(self, config: ProactiveConfig) -> None:
        self.apply_batch(
            WriteBatch(
                operation="proactive.write",
                transaction_id=uuid4().hex,
                audit_details={"config": config.to_dict()},
            )
        )
        with self._lock, self._connection:
            data = _to_json(config.to_dict())
            before = self._connection.execute(
                "SELECT data FROM proactive_configs WHERE id = ?", (config.id,)
            ).fetchone()
            self._connection.execute(
                """
                INSERT INTO proactive_configs(
                    id, user_id, enabled, min_interval_minutes, daily_limit,
                    silence_threshold_minutes, quiet_hours_start, quiet_hours_end,
                    last_proactive_at, daily_count, daily_reset_date, created_at, data
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    user_id = excluded.user_id,
                    enabled = excluded.enabled,
                    min_interval_minutes = excluded.min_interval_minutes,
                    daily_limit = excluded.daily_limit,
                    silence_threshold_minutes = excluded.silence_threshold_minutes,
                    quiet_hours_start = excluded.quiet_hours_start,
                    quiet_hours_end = excluded.quiet_hours_end,
                    last_proactive_at = excluded.last_proactive_at,
                    daily_count = excluded.daily_count,
                    daily_reset_date = excluded.daily_reset_date,
                    created_at = excluded.created_at,
                    data = excluded.data
                """,
                (
                    config.id,
                    config.user_id,
                    int(config.enabled),
                    config.min_interval_minutes,
                    config.daily_limit,
                    config.silence_threshold_minutes,
                    config.quiet_hours_start,
                    config.quiet_hours_end,
                    config.last_proactive_at,
                    config.daily_count,
                    config.daily_reset_date,
                    config.created_at,
                    data,
                ),
            )
            self._audit(
                WriteBatch(operation="proactive.write", transaction_id=uuid4().hex),
                "proactive",
                config.id,
                before["data"] if before else None,
                data,
            )

    def get_proactive_config(self, config_id: str) -> ProactiveConfig | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT data FROM proactive_configs WHERE id = ?", (config_id,)
            ).fetchone()
        return ProactiveConfig.from_dict(json.loads(row["data"])) if row else None

    def get_proactive_config_by_user(self, user_id: str) -> ProactiveConfig | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT data FROM proactive_configs WHERE user_id = ?", (user_id,)
            ).fetchone()
        return ProactiveConfig.from_dict(json.loads(row["data"])) if row else None

    def list_proactive_configs(self) -> list[ProactiveConfig]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT data FROM proactive_configs ORDER BY id"
            ).fetchall()
        return [ProactiveConfig.from_dict(json.loads(row["data"])) for row in rows]

    def list_semantic(self, *, include_archived: bool = False) -> list[SemanticMemory]:
        query = "SELECT data FROM semantic_memories"
        if not include_archived:
            query += " WHERE archived = 0"
        query += " ORDER BY id"
        with self._lock:
            rows = self._connection.execute(query).fetchall()
        memories = [SemanticMemory.from_dict(json.loads(row["data"])) for row in rows]
        return sorted(memories, key=lambda item: (item.created_at, item.id))

    def list_episodic(self, *, include_archived: bool = False) -> list[EpisodicMemory]:
        query = "SELECT data FROM episodic_memories"
        if not include_archived:
            query += " WHERE archived = 0"
        query += " ORDER BY id"
        with self._lock:
            rows = self._connection.execute(query).fetchall()
        memories = [EpisodicMemory.from_dict(json.loads(row["data"])) for row in rows]
        return sorted(memories, key=lambda item: (item.timestamp, item.id))

    def list_style(self) -> list[StyleMemory]:
        with self._lock:
            rows = self._connection.execute("SELECT data FROM style_memories ORDER BY id").fetchall()
        return [StyleMemory.from_dict(json.loads(row["data"])) for row in rows]

    def get_vectors(
        self, memory_type: str, *, model: str, dimensions: int
    ) -> dict[str, tuple[float, ...]]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT memory_id, values_json FROM memory_vectors
                WHERE memory_type = ? AND model = ? AND dimensions = ?
                """,
                (memory_type, model, dimensions),
            ).fetchall()
        return {
            row["memory_id"]: tuple(float(value) for value in json.loads(row["values_json"]))
            for row in rows
        }

    def apply_batch(self, batch: WriteBatch) -> None:
        if batch.core is not None:
            batch.core.validate_size(self.core_token_limit)
        with self._lock, self._connection:
            if batch.core is not None:
                before_row = self._connection.execute(
                    "SELECT data FROM core_memory WHERE singleton = 1"
                ).fetchone()
                data = _to_json(batch.core.to_dict())
                self._connection.execute(
                    """
                    INSERT INTO core_memory(singleton, data) VALUES (1, ?)
                    ON CONFLICT(singleton) DO UPDATE SET data = excluded.data
                    """,
                    (data,),
                )
                self._audit(
                    batch,
                    "core",
                    "core",
                    before_row["data"] if before_row else None,
                    data,
                )

            for memory in batch.semantic:
                self._write_semantic(batch, memory)
            for memory in batch.episodic:
                self._write_episodic(batch, memory)
            for memory in batch.style:
                self._write_style(batch, memory)
            for entry in batch.vectors.values():
                self._write_vector(entry)

            if batch.audit_details is not None:
                self._audit(
                    batch,
                    "consolidation",
                    batch.transaction_id,
                    None,
                    None,
                    _to_json(batch.audit_details),
                )

    def _write_semantic(self, batch: WriteBatch, memory: SemanticMemory) -> None:
        before = self._connection.execute(
            "SELECT data FROM semantic_memories WHERE id = ?", (memory.id,)
        ).fetchone()
        data = _to_json(memory.to_dict())
        self._connection.execute(
            """
            INSERT INTO semantic_memories(
                id, subject, predicate, importance, confidence, decay_score,
                protected, archived, last_activated, data
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                subject = excluded.subject,
                predicate = excluded.predicate,
                importance = excluded.importance,
                confidence = excluded.confidence,
                decay_score = excluded.decay_score,
                protected = excluded.protected,
                archived = excluded.archived,
                last_activated = excluded.last_activated,
                data = excluded.data
            """,
            (
                memory.id,
                memory.subject,
                memory.predicate,
                memory.importance,
                memory.confidence,
                memory.decay_score,
                int(memory.protected),
                int(memory.archived),
                memory.last_activated,
                data,
            ),
        )
        self._audit(batch, "semantic", memory.id, before["data"] if before else None, data)

    def _write_episodic(self, batch: WriteBatch, memory: EpisodicMemory) -> None:
        before = self._connection.execute(
            "SELECT data FROM episodic_memories WHERE id = ?", (memory.id,)
        ).fetchone()
        data = _to_json(memory.to_dict())
        self._connection.execute(
            """
            INSERT INTO episodic_memories(
                id, timestamp, importance, decay_score, protected, archived,
                last_activated, data
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                timestamp = excluded.timestamp,
                importance = excluded.importance,
                decay_score = excluded.decay_score,
                protected = excluded.protected,
                archived = excluded.archived,
                last_activated = excluded.last_activated,
                data = excluded.data
            """,
            (
                memory.id,
                memory.timestamp,
                memory.importance,
                memory.decay_score,
                int(memory.protected),
                int(memory.archived),
                memory.last_activated,
                data,
            ),
        )
        self._audit(batch, "episodic", memory.id, before["data"] if before else None, data)

    def _write_style(self, batch: WriteBatch, memory: StyleMemory) -> None:
        before = self._connection.execute(
            "SELECT data FROM style_memories WHERE id = ?", (memory.id,)
        ).fetchone()
        data = _to_json(memory.to_dict())
        self._connection.execute(
            """
            INSERT INTO style_memories(id, category, importance, last_observed, data)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                category = excluded.category,
                importance = excluded.importance,
                last_observed = excluded.last_observed,
                data = excluded.data
            """,
            (memory.id, memory.category, memory.importance, memory.last_observed, data),
        )
        self._audit(batch, "style", memory.id, before["data"] if before else None, data)

    def _write_vector(self, entry: VectorEntry) -> None:
        self._connection.execute(
            """
            INSERT INTO memory_vectors(memory_id, memory_type, model, dimensions, values_json)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(memory_id) DO UPDATE SET
                memory_type = excluded.memory_type,
                model = excluded.model,
                dimensions = excluded.dimensions,
                values_json = excluded.values_json
            """,
            (
                entry.memory_id,
                entry.memory_type,
                entry.model,
                entry.dimensions,
                json.dumps(entry.values, separators=(",", ":")),
            ),
        )

    def _audit(
        self,
        batch: WriteBatch,
        entity_type: str,
        entity_id: str,
        before_json: str | None,
        after_json: str | None,
        details_json: str | None = None,
    ) -> None:
        operation = batch.operation
        if before_json is None and after_json is not None:
            operation += ".create"
        elif before_json is not None and after_json is not None:
            operation += ".update"
        self._connection.execute(
            """
            INSERT INTO operation_log(
                timestamp, transaction_id, operation, entity_type, entity_id,
                before_json, after_json, details_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                to_iso8601(utc_now()),
                batch.transaction_id,
                operation,
                entity_type,
                entity_id,
                before_json,
                after_json,
                details_json,
            ),
        )

    def record_activations(self, memory_ids: Iterable[str], *, now: datetime) -> None:
        ids = list(dict.fromkeys(memory_ids))
        if not ids:
            return
        batch = WriteBatch(operation="memory.activate")
        for memory_id in ids:
            memory = self.get_memory(memory_id)
            if memory is None or memory.archived:
                continue
            memory.activation_count += 1
            memory.last_activated = to_iso8601(now)
            if memory.protected:
                memory.decay_score = 1.0
            if isinstance(memory, SemanticMemory):
                batch.semantic.append(memory)
            else:
                batch.episodic.append(memory)
        self.apply_batch(batch)

    def set_protected(self, memory_id: str, protected: bool = True) -> None:
        memory = self.get_memory(memory_id)
        if memory is None:
            raise KeyError(memory_id)
        core = self.get_core()
        protected_ids = [item for item in core.protected_memories if item != memory_id]
        if protected:
            protected_ids.append(memory_id)
        core.protected_memories = protected_ids
        core.last_updated = to_iso8601(utc_now())
        core.validate_size(self.core_token_limit)
        memory.protected = protected
        if protected:
            memory.decay_score = 1.0
        batch = WriteBatch(core=core, operation="memory.protect")
        if isinstance(memory, SemanticMemory):
            batch.semantic.append(memory)
        else:
            batch.episodic.append(memory)
        self.apply_batch(batch)

    def list_audit(self, *, limit: int = 100) -> list[dict[str, Any]]:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError("limit must be a positive integer")
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT * FROM operation_log ORDER BY sequence DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            {
                "sequence": row["sequence"],
                "timestamp": row["timestamp"],
                "transaction_id": row["transaction_id"],
                "operation": row["operation"],
                "entity_type": row["entity_type"],
                "entity_id": row["entity_id"],
                "before": json.loads(row["before_json"]) if row["before_json"] else None,
                "after": json.loads(row["after_json"]) if row["after_json"] else None,
                "details": json.loads(row["details_json"]) if row["details_json"] else None,
            }
            for row in rows
        ]

    def storage_stats(self) -> dict[str, Any]:
        """返回数据库分层统计：各类型记忆数量、保留度档位分布、归档比例等。"""
        with self._lock:
            semantic_total = self._connection.execute(
                "SELECT COUNT(*) FROM semantic_memories"
            ).fetchone()[0]
            semantic_archived = self._connection.execute(
                "SELECT COUNT(*) FROM semantic_memories WHERE archived = 1"
            ).fetchone()[0]
            episodic_total = self._connection.execute(
                "SELECT COUNT(*) FROM episodic_memories"
            ).fetchone()[0]
            episodic_archived = self._connection.execute(
                "SELECT COUNT(*) FROM episodic_memories WHERE archived = 1"
            ).fetchone()[0]
            style_total = self._connection.execute(
                "SELECT COUNT(*) FROM style_memories"
            ).fetchone()[0]
            vector_count = self._connection.execute(
                "SELECT COUNT(*) FROM memory_vectors"
            ).fetchone()[0]

        # 分层统计（基于 retention_tier，从内存中读取更准确）
        semantic_tiers: dict[int, int] = {}
        for memory in self.list_semantic(include_archived=True):
            semantic_tiers[memory.retention_tier] = (
                semantic_tiers.get(memory.retention_tier, 0) + 1
            )
        episodic_tiers: dict[int, int] = {}
        for memory in self.list_episodic(include_archived=True):
            episodic_tiers[memory.retention_tier] = (
                episodic_tiers.get(memory.retention_tier, 0) + 1
            )

        total_memories = semantic_total + episodic_total + style_total
        total_archived = semantic_archived + episodic_archived
        db_size_bytes = 0
        try:
            db_size_bytes = Path(self.path).stat().st_size
        except OSError:
            pass

        return {
            "total_memories": total_memories,
            "total_archived": total_archived,
            "archive_ratio": round(total_archived / max(total_memories, 1), 4),
            "semantic": {
                "total": semantic_total,
                "archived": semantic_archived,
                "archive_ratio": round(semantic_archived / max(semantic_total, 1), 4),
                "tiers": {str(k): v for k, v in sorted(semantic_tiers.items())},
            },
            "episodic": {
                "total": episodic_total,
                "archived": episodic_archived,
                "archive_ratio": round(episodic_archived / max(episodic_total, 1), 4),
                "tiers": {str(k): v for k, v in sorted(episodic_tiers.items())},
            },
            "style": {"total": style_total},
            "vectors": {"total": vector_count},
            "db_size_bytes": db_size_bytes,
        }


def _to_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
