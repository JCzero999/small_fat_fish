from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping
from uuid import uuid4


class MemoryValidationError(ValueError):
    """Raised when memory data does not conform to the public schema."""


class CoreMemoryLimitError(MemoryValidationError):
    """Raised when a Core Memory update would exceed its token budget."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def to_iso8601(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def parse_iso8601(value: str) -> datetime:
    text = _string(value, "datetime")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MemoryValidationError(f"invalid ISO8601 datetime: {text!r}") from exc
    if parsed.tzinfo is None:
        raise MemoryValidationError("ISO8601 datetime must include a timezone")
    return parsed.astimezone(timezone.utc)


def new_memory_id(prefix: str, now: datetime | None = None) -> str:
    moment = now or utc_now()
    return f"{prefix}_{moment:%Y%m%d}_{uuid4().hex[:12]}"


def estimate_tokens(value: Any) -> int:
    """A conservative, dependency-free estimate for mixed Chinese/English JSON."""

    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    units = sum(1.0 if ord(character) > 127 else 0.25 for character in value)
    return max(1, math.ceil(units))


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise MemoryValidationError(f"{name} must be an object")
    return value


def _string(value: Any, name: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise MemoryValidationError(f"{name} must be a string")
    result = value.strip()
    if not allow_empty and not result:
        raise MemoryValidationError(f"{name} must not be empty")
    return result


def _string_list(value: Any, name: str) -> list[str]:
    if not isinstance(value, list):
        raise MemoryValidationError(f"{name} must be an array")
    return [_string(item, f"{name}[]") for item in value]


def _number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise MemoryValidationError(f"{name} must be a finite number")
    result = float(value)
    if not 0.0 <= result <= 1.0:
        raise MemoryValidationError(f"{name} must be between 0 and 1")
    return result


def _integer(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise MemoryValidationError(f"{name} must be a non-negative integer")
    return value


def _boolean(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise MemoryValidationError(f"{name} must be a boolean")
    return value


def _choice(value: Any, name: str, choices: Iterable[str]) -> str:
    result = _string(value, name)
    if result not in choices:
        raise MemoryValidationError(f"{name} must be one of {', '.join(choices)}")
    return result


def _datetime_field(value: Any, name: str) -> str:
    return to_iso8601(parse_iso8601(_string(value, name)))


@dataclass(slots=True)
class Identity:
    name: str = "额外大脑"
    persona_core: str = "真诚、克制、可靠，重视用户的情绪与持续关系。"
    relationship_definition: str = "陪伴型 AI 与用户"
    speaking_style: str = "自然、温暖、简洁，不刻意讨好。"

    def __post_init__(self) -> None:
        self.name = _string(self.name, "identity.name")
        self.persona_core = _string(self.persona_core, "identity.persona_core")
        if len(self.persona_core) > 150:
            raise MemoryValidationError("identity.persona_core must not exceed 150 characters")
        self.relationship_definition = _string(
            self.relationship_definition, "identity.relationship_definition"
        )
        self.speaking_style = _string(self.speaking_style, "identity.speaking_style")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Identity:
        data = _mapping(data, "identity")
        defaults = cls()
        return cls(
            name=data.get("name", defaults.name),
            persona_core=data.get("persona_core", defaults.persona_core),
            relationship_definition=data.get(
                "relationship_definition", defaults.relationship_definition
            ),
            speaking_style=data.get("speaking_style", defaults.speaking_style),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "persona_core": self.persona_core,
            "relationship_definition": self.relationship_definition,
            "speaking_style": self.speaking_style,
        }


@dataclass(slots=True)
class UserStableProfile:
    preferred_name: str = ""
    key_traits: list[str] = field(default_factory=list)
    core_preferences: list[str] = field(default_factory=list)
    important_life_facts: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.preferred_name = _string(
            self.preferred_name, "user_stable_profile.preferred_name", allow_empty=True
        )
        self.key_traits = _string_list(self.key_traits, "user_stable_profile.key_traits")
        self.core_preferences = _string_list(
            self.core_preferences, "user_stable_profile.core_preferences"
        )
        self.important_life_facts = _string_list(
            self.important_life_facts, "user_stable_profile.important_life_facts"
        )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> UserStableProfile:
        data = _mapping(data, "user_stable_profile")
        return cls(
            preferred_name=data.get("preferred_name", ""),
            key_traits=data.get("key_traits", []),
            core_preferences=data.get("core_preferences", []),
            important_life_facts=data.get("important_life_facts", []),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "preferred_name": self.preferred_name,
            "key_traits": list(self.key_traits),
            "core_preferences": list(self.core_preferences),
            "important_life_facts": list(self.important_life_facts),
        }


@dataclass(slots=True)
class ActiveThread:
    id: str
    summary: str
    status: str
    last_mentioned: str

    def __post_init__(self) -> None:
        self.id = _string(self.id, "active_threads[].id")
        self.summary = _string(self.summary, "active_threads[].summary")
        self.status = _choice(
            self.status, "active_threads[].status", ("open", "watching", "closed")
        )
        self.last_mentioned = _datetime_field(
            self.last_mentioned, "active_threads[].last_mentioned"
        )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ActiveThread:
        data = _mapping(data, "active_threads[]")
        return cls(
            id=data["id"],
            summary=data["summary"],
            status=data["status"],
            last_mentioned=data["last_mentioned"],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "summary": self.summary,
            "status": self.status,
            "last_mentioned": self.last_mentioned,
        }


@dataclass(slots=True)
class CoreMemory:
    version: str = "1.0"
    last_updated: str = field(default_factory=lambda: to_iso8601(utc_now()))
    identity: Identity = field(default_factory=Identity)
    user_stable_profile: UserStableProfile = field(default_factory=UserStableProfile)
    active_threads: list[ActiveThread] = field(default_factory=list)
    protected_memories: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.version = _string(self.version, "version")
        self.last_updated = _datetime_field(self.last_updated, "last_updated")
        self.protected_memories = list(
            dict.fromkeys(_string_list(self.protected_memories, "protected_memories"))
        )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> CoreMemory:
        data = _mapping(data, "core")
        return cls(
            version=data.get("version", "1.0"),
            last_updated=data.get("last_updated", to_iso8601(utc_now())),
            identity=Identity.from_dict(data.get("identity", {})),
            user_stable_profile=UserStableProfile.from_dict(
                data.get("user_stable_profile", {})
            ),
            active_threads=[
                ActiveThread.from_dict(item) for item in data.get("active_threads", [])
            ],
            protected_memories=data.get("protected_memories", []),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "last_updated": self.last_updated,
            "identity": self.identity.to_dict(),
            "user_stable_profile": self.user_stable_profile.to_dict(),
            "active_threads": [thread.to_dict() for thread in self.active_threads],
            "protected_memories": list(self.protected_memories),
        }

    @property
    def estimated_tokens(self) -> int:
        return estimate_tokens(self.to_dict())

    def validate_size(self, token_limit: int) -> None:
        if self.estimated_tokens > token_limit:
            raise CoreMemoryLimitError(
                f"Core Memory needs {self.estimated_tokens} estimated tokens; "
                f"limit is {token_limit}"
            )


@dataclass(slots=True)
class SemanticMemory:
    id: str
    type: str
    subject: str
    predicate: str
    object: str
    detail: str
    importance: float
    emotion_valence: str
    emotion_intensity: float
    confidence: float
    source_episodic_ids: list[str]
    created_at: str
    last_activated: str
    activation_count: int = 0
    decay_score: float = 1.0
    conflict_flag: bool = False
    protected: bool = False
    archived: bool = False

    def __post_init__(self) -> None:
        self.id = _string(self.id, "semantic.id")
        self.type = _choice(
            self.type, "semantic.type", ("fact", "preference", "relationship", "goal", "trait")
        )
        self.subject = _string(self.subject, "semantic.subject")
        self.predicate = _string(self.predicate, "semantic.predicate")
        self.object = _string(self.object, "semantic.object")
        self.detail = _string(self.detail, "semantic.detail")
        self.importance = _number(self.importance, "semantic.importance")
        self.emotion_valence = _choice(
            self.emotion_valence,
            "semantic.emotion_valence",
            ("positive", "neutral", "negative", "mixed"),
        )
        self.emotion_intensity = _number(
            self.emotion_intensity, "semantic.emotion_intensity"
        )
        self.confidence = _number(self.confidence, "semantic.confidence")
        self.source_episodic_ids = list(
            dict.fromkeys(_string_list(self.source_episodic_ids, "source_episodic_ids"))
        )
        self.created_at = _datetime_field(self.created_at, "semantic.created_at")
        self.last_activated = _datetime_field(self.last_activated, "semantic.last_activated")
        self.activation_count = _integer(self.activation_count, "semantic.activation_count")
        self.decay_score = _number(self.decay_score, "semantic.decay_score")
        self.conflict_flag = _boolean(self.conflict_flag, "semantic.conflict_flag")
        self.protected = _boolean(self.protected, "semantic.protected")
        self.archived = _boolean(self.archived, "semantic.archived")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> SemanticMemory:
        data = _mapping(data, "semantic")
        names = (
            "id",
            "type",
            "subject",
            "predicate",
            "object",
            "detail",
            "importance",
            "emotion_valence",
            "emotion_intensity",
            "confidence",
            "source_episodic_ids",
            "created_at",
            "last_activated",
        )
        return cls(
            **{name: data[name] for name in names},
            activation_count=data.get("activation_count", 0),
            decay_score=data.get("decay_score", 1.0),
            conflict_flag=data.get("conflict_flag", False),
            protected=data.get("protected", False),
            archived=data.get("archived", False),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "subject": self.subject,
            "predicate": self.predicate,
            "object": self.object,
            "detail": self.detail,
            "importance": self.importance,
            "emotion_valence": self.emotion_valence,
            "emotion_intensity": self.emotion_intensity,
            "confidence": self.confidence,
            "source_episodic_ids": list(self.source_episodic_ids),
            "created_at": self.created_at,
            "last_activated": self.last_activated,
            "activation_count": self.activation_count,
            "decay_score": self.decay_score,
            "conflict_flag": self.conflict_flag,
            "protected": self.protected,
            "archived": self.archived,
        }

    def embedding_text(self) -> str:
        return " ".join((self.subject, self.predicate, self.object, self.detail))


@dataclass(slots=True)
class EpisodicMemory:
    id: str
    timestamp: str
    summary: str
    key_moments: list[str]
    emotions: list[str]
    importance: float
    related_semantic_ids: list[str]
    participants: list[str]
    raw_reference: str | None = None
    decay_score: float = 1.0
    protected: bool = False
    last_activated: str | None = None
    activation_count: int = 0
    archived: bool = False

    def __post_init__(self) -> None:
        self.id = _string(self.id, "episodic.id")
        self.timestamp = _datetime_field(self.timestamp, "episodic.timestamp")
        self.summary = _string(self.summary, "episodic.summary")
        self.key_moments = _string_list(self.key_moments, "episodic.key_moments")
        self.emotions = _string_list(self.emotions, "episodic.emotions")
        self.importance = _number(self.importance, "episodic.importance")
        self.related_semantic_ids = list(
            dict.fromkeys(_string_list(self.related_semantic_ids, "related_semantic_ids"))
        )
        self.participants = _string_list(self.participants, "episodic.participants")
        if self.raw_reference is not None:
            self.raw_reference = _string(self.raw_reference, "episodic.raw_reference")
        self.decay_score = _number(self.decay_score, "episodic.decay_score")
        self.protected = _boolean(self.protected, "episodic.protected")
        if self.last_activated is not None:
            self.last_activated = _datetime_field(
                self.last_activated, "episodic.last_activated"
            )
        self.activation_count = _integer(self.activation_count, "episodic.activation_count")
        self.archived = _boolean(self.archived, "episodic.archived")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> EpisodicMemory:
        data = _mapping(data, "episodic")
        names = (
            "id",
            "timestamp",
            "summary",
            "key_moments",
            "emotions",
            "importance",
            "related_semantic_ids",
            "participants",
            "raw_reference",
            "decay_score",
            "protected",
            "last_activated",
            "activation_count",
            "archived",
        )
        required = names[:8]
        return cls(
            **{name: data[name] for name in required},
            raw_reference=data.get("raw_reference"),
            decay_score=data.get("decay_score", 1.0),
            protected=data.get("protected", False),
            last_activated=data.get("last_activated"),
            activation_count=data.get("activation_count", 0),
            archived=data.get("archived", False),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "timestamp": self.timestamp,
            "summary": self.summary,
            "key_moments": list(self.key_moments),
            "emotions": list(self.emotions),
            "importance": self.importance,
            "related_semantic_ids": list(self.related_semantic_ids),
            "participants": list(self.participants),
            "raw_reference": self.raw_reference,
            "decay_score": self.decay_score,
            "protected": self.protected,
            "last_activated": self.last_activated,
            "activation_count": self.activation_count,
            "archived": self.archived,
        }

    def embedding_text(self) -> str:
        return " ".join((self.summary, *self.key_moments, *self.emotions))


@dataclass(slots=True)
class StyleMemory:
    id: str
    observation: str
    category: str
    importance: float
    examples: list[str]
    last_observed: str

    def __post_init__(self) -> None:
        self.id = _string(self.id, "style.id")
        self.observation = _string(self.observation, "style.observation")
        self.category = _choice(
            self.category,
            "style.category",
            ("expression", "preference", "interaction_habit", "boundary"),
        )
        self.importance = _number(self.importance, "style.importance")
        self.examples = _string_list(self.examples, "style.examples")
        self.last_observed = _datetime_field(self.last_observed, "style.last_observed")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> StyleMemory:
        data = _mapping(data, "style")
        names = ("id", "observation", "category", "importance", "examples", "last_observed")
        return cls(**{name: data[name] for name in names})

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "observation": self.observation,
            "category": self.category,
            "importance": self.importance,
            "examples": list(self.examples),
            "last_observed": self.last_observed,
        }


Memory = SemanticMemory | EpisodicMemory
