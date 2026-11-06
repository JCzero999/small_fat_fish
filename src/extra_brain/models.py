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


MEMORY_VIEWS = ("fact", "feeling", "bond", "story")


def derive_view(
    memory_type: str | None,
    emotion_valence: str | None = None,
    emotion_intensity: float | None = None,
    is_episodic: bool = False,
) -> str:
    """多角度视角（v0.4.0 遗留）：写入时确定性算好，不依赖模型输出。

    - story   ：情节记忆（事件本身）
    - bond    ：关系类语义（type=relationship）
    - feeling ：带显著情绪的语义（效价非 neutral 且 intensity ≥ 0.45）
    - fact    ：其余事实/偏好/目标/特质
    """
    if is_episodic:
        return "story"
    if memory_type == "relationship":
        return "bond"
    if (
        emotion_valence in ("positive", "negative", "mixed")
        and isinstance(emotion_intensity, (int, float))
        and not isinstance(emotion_intensity, bool)
        and emotion_intensity >= 0.45
    ):
        return "feeling"
    return "fact"


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
    # v0.13.2 主人身份卡：跨 peer/跨宿主识别用户（"主人QQ:1975824324|称呼:JCzero|关系:父女"）。
    # 始终随 Core 注入——群聊里模型据此 + 发言者标注认出主人，不依赖检索命中。
    owner_accounts: list[str] = field(default_factory=list)

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
        self.owner_accounts = _string_list(
            self.owner_accounts, "user_stable_profile.owner_accounts"
        )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> UserStableProfile:
        data = _mapping(data, "user_stable_profile")
        return cls(
            preferred_name=data.get("preferred_name", ""),
            key_traits=data.get("key_traits", []),
            core_preferences=data.get("core_preferences", []),
            important_life_facts=data.get("important_life_facts", []),
            owner_accounts=data.get("owner_accounts", []),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "preferred_name": self.preferred_name,
            "key_traits": list(self.key_traits),
            "core_preferences": list(self.core_preferences),
            "important_life_facts": list(self.important_life_facts),
            "owner_accounts": list(self.owner_accounts),
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
    # --- 人格锚版本（v0.2.0） ---
    persona_anchor_version: str | None = None       # 当前人格锚版本 ID
    persona_anchor_history: list[dict[str, Any]] = field(default_factory=list)  # 历史版本，支持 rollback

    def __post_init__(self) -> None:
        self.version = _string(self.version, "version")
        self.last_updated = _datetime_field(self.last_updated, "last_updated")
        self.protected_memories = list(
            dict.fromkeys(_string_list(self.protected_memories, "protected_memories"))
        )
        if self.persona_anchor_version is not None:
            self.persona_anchor_version = _string(
                self.persona_anchor_version, "core.persona_anchor_version"
            )
        self.persona_anchor_history = list(self.persona_anchor_history)

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
            persona_anchor_version=data.get("persona_anchor_version"),
            persona_anchor_history=data.get("persona_anchor_history", []),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "last_updated": self.last_updated,
            "identity": self.identity.to_dict(),
            "user_stable_profile": self.user_stable_profile.to_dict(),
            "active_threads": [thread.to_dict() for thread in self.active_threads],
            "protected_memories": list(self.protected_memories),
            "persona_anchor_version": self.persona_anchor_version,
            "persona_anchor_history": list(self.persona_anchor_history),
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


def _validate_solidity(obj: Any) -> None:
    """v0.12.0：牢固度字段校验（Semantic/Episodic 共用）。"""
    if isinstance(obj.stability, bool) or not isinstance(obj.stability, (int, float)) \
            or not math.isfinite(obj.stability) or obj.stability <= 0:
        raise MemoryValidationError("solidity.stability must be a positive finite number")
    obj.stability = float(obj.stability)
    obj.retrievability = _number(obj.retrievability, "solidity.retrievability")
    if obj.last_review is not None:
        obj.last_review = _datetime_field(obj.last_review, "solidity.last_review")
    obj.review_count = _integer(obj.review_count, "solidity.review_count")
    obj.frozen = _boolean(obj.frozen, "solidity.frozen")


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
    # --- 双时态版本链（v0.2.0） ---
    valid_from: str | None = None      # 生效开始时间（ISO8601），None 表示创建时立即生效
    valid_to: str | None = None        # 生效结束时间（ISO8601），None 表示持续有效
    supersedes: str | None = None      # 被取代的记忆 ID，形成版本链
    # --- 六档保留度（v0.2.2） ---
    retention_tier: int = 0            # 0=热/P5, 1=温/P4, 2=冷/P3, 3=归档/P2, 4=墓碑/P1
    # --- 多角度视角（v0.4.0 遗留，写入时确定性推导） ---
    view: str = ""                     # fact | feeling | bond | story；空=按类型/情绪自动补标
    # --- v0.12.0 牢固度（FSRS 风格）：想起就牢、久不提就淡 ---
    stability: float = 1.0             # 稳定度（天量级，越高衰减越慢）
    retrievability: float = 1.0        # 当前可提取度 [0,1]
    last_review: str | None = None     # 上次「想起」时间（ISO8601）
    review_count: int = 0
    frozen: bool = False               # 低于阈值后冻结（不入自动注入/忽然想起）

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
        if self.valid_from is not None:
            self.valid_from = _datetime_field(self.valid_from, "semantic.valid_from")
        if self.valid_to is not None:
            self.valid_to = _datetime_field(self.valid_to, "semantic.valid_to")
        if self.supersedes is not None:
            self.supersedes = _string(self.supersedes, "semantic.supersedes")
        self.retention_tier = _integer(self.retention_tier, "semantic.retention_tier")
        if not self.view:
            self.view = derive_view(
                self.type, self.emotion_valence, self.emotion_intensity
            )
        else:
            self.view = _choice(self.view, "semantic.view", MEMORY_VIEWS)
        self._apply_solidity_fields()

    def _apply_solidity_fields(self) -> None:
        """v0.12.0：牢固度字段校验（Semantic/Episodic 共用逻辑）。"""
        _validate_solidity(self)

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
            valid_from=data.get("valid_from"),
            valid_to=data.get("valid_to"),
            supersedes=data.get("supersedes"),
            retention_tier=data.get("retention_tier", 0),
            view=data.get("view", ""),
            stability=data.get("stability", 1.0),
            retrievability=data.get("retrievability", 1.0),
            last_review=data.get("last_review"),
            review_count=data.get("review_count", 0),
            frozen=data.get("frozen", False),
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
            "valid_from": self.valid_from,
            "valid_to": self.valid_to,
            "supersedes": self.supersedes,
            "retention_tier": self.retention_tier,
            "view": self.view,
            "stability": self.stability,
            "retrievability": self.retrievability,
            "last_review": self.last_review,
            "review_count": self.review_count,
            "frozen": self.frozen,
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
    # --- 双时态版本链（v0.2.0） ---
    valid_from: str | None = None
    valid_to: str | None = None
    supersedes: str | None = None
    # --- 情感事件五态状态机（v0.2.0） ---
    emotion_state: str | None = None  # open | acknowledged | attended | resolved | dormant
    # --- 六档保留度（v0.2.2） ---
    retention_tier: int = 0            # 0=热/P5, 1=温/P4, 2=冷/P3, 3=归档/P2, 4=墓碑/P1
    # --- 多角度视角（v0.4.0 遗留）：情节记忆默认 story ---
    view: str = ""                     # 空=自动补标；显式值须属于 MEMORY_VIEWS
    # --- v0.12.0 牢固度（FSRS 风格） ---
    stability: float = 1.0
    retrievability: float = 1.0
    last_review: str | None = None
    review_count: int = 0
    frozen: bool = False

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
        if self.valid_from is not None:
            self.valid_from = _datetime_field(self.valid_from, "episodic.valid_from")
        if self.valid_to is not None:
            self.valid_to = _datetime_field(self.valid_to, "episodic.valid_to")
        if self.supersedes is not None:
            self.supersedes = _string(self.supersedes, "episodic.supersedes")
        if self.emotion_state is not None:
            self.emotion_state = _choice(
                self.emotion_state,
                "episodic.emotion_state",
                ("open", "acknowledged", "attended", "resolved", "dormant"),
            )
        self.retention_tier = _integer(self.retention_tier, "episodic.retention_tier")
        if not self.view:
            self.view = derive_view(None, is_episodic=True)
        else:
            self.view = _choice(self.view, "episodic.view", MEMORY_VIEWS)
        self._apply_solidity_fields()

    def _apply_solidity_fields(self) -> None:
        """v0.12.0：牢固度字段校验。"""
        _validate_solidity(self)

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
            valid_from=data.get("valid_from"),
            valid_to=data.get("valid_to"),
            supersedes=data.get("supersedes"),
            emotion_state=data.get("emotion_state"),
            retention_tier=data.get("retention_tier", 0),
            view=data.get("view", ""),
            stability=data.get("stability", 1.0),
            retrievability=data.get("retrievability", 1.0),
            last_review=data.get("last_review"),
            review_count=data.get("review_count", 0),
            frozen=data.get("frozen", False),
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
            "valid_from": self.valid_from,
            "valid_to": self.valid_to,
            "supersedes": self.supersedes,
            "emotion_state": self.emotion_state,
            "retention_tier": self.retention_tier,
            "view": self.view,
            "stability": self.stability,
            "retrievability": self.retrievability,
            "last_review": self.last_review,
            "review_count": self.review_count,
            "frozen": self.frozen,
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


def is_unresolved_episode(memory: Memory) -> bool:
    """未平复的情感事件：显式处于 open/acknowledged/attended 且确实带情绪标签。

    反漠视设计（v0.2.0）：这类记忆不自动归档、检索时强制优先回忆。
    旧库数据 emotion_state 为 None 时维持原行为（可正常归档），只有状态机
    明确标记为「尚未处理完」的事件才享受保护。
    """
    return (
        isinstance(memory, EpisodicMemory)
        and memory.emotion_state in ("open", "acknowledged", "attended")
        and bool(memory.emotions)
    )


def is_emotion_protected(memory: Memory) -> bool:
    """情感记忆保护（v0.4.0 规划项，对应 mnemovault flags.bit6）。

    高情绪显著性的记忆在保留度计算中遗忘曲线放慢 3 倍、保底温层（P4），
    不会被淡忘到冷/归档/墓碑——用户对助手留下强烈感受的事，助手不该忘。

    判定（以本库可用的情绪字段映射 PAD 阈值）：
    - 语义记忆：emotion_intensity ≥ 0.65（≈唤起阈值），或
      效价非 neutral 且 emotion_intensity ≥ 0.45（≈|valence|×强度显著）
    - 情节记忆：带情绪标签且 importance ≥ 0.75（本库无逐事件强度字段）
    """
    intensity = getattr(memory, "emotion_intensity", None)
    if isinstance(intensity, (int, float)):
        valence = getattr(memory, "emotion_valence", "neutral")
        return intensity >= 0.65 or (
            valence in ("positive", "negative", "mixed") and intensity >= 0.45
        )
    if isinstance(memory, EpisodicMemory):
        return bool(memory.emotions) and memory.importance >= 0.75
    return False


@dataclass(slots=True)
class RelationshipModel:
    """v0.4.0 关系模型（kind=5）：持久化的人际关系状态，重启后恢复。

    慢环不落盘 = 重启就忘，此项修复让关系模型可跨会话持久化。
    """

    id: str
    user_id: str
    relationship_type: str  # 如 "陪伴型", "父女", "朋友", "导师"
    closeness: float  # 0.0~1.0，亲近度
    trust: float  # 0.0~1.0，信任度
    familiarity: float  # 0.0~1.0，熟悉度
    last_updated: str
    interaction_count: int = 0
    positive_interactions: int = 0
    negative_interactions: int = 0
    notes: str = ""
    created_at: str | None = None

    def __post_init__(self) -> None:
        self.id = _string(self.id, "relationship.id")
        self.user_id = _string(self.user_id, "relationship.user_id")
        self.relationship_type = _string(
            self.relationship_type, "relationship.relationship_type"
        )
        self.closeness = _number(self.closeness, "relationship.closeness")
        self.trust = _number(self.trust, "relationship.trust")
        self.familiarity = _number(self.familiarity, "relationship.familiarity")
        self.last_updated = _datetime_field(
            self.last_updated, "relationship.last_updated"
        )
        self.interaction_count = _integer(
            self.interaction_count, "relationship.interaction_count"
        )
        self.positive_interactions = _integer(
            self.positive_interactions, "relationship.positive_interactions"
        )
        self.negative_interactions = _integer(
            self.negative_interactions, "relationship.negative_interactions"
        )
        self.notes = _string(self.notes, "relationship.notes", allow_empty=True)
        if self.created_at is not None:
            self.created_at = _datetime_field(
                self.created_at, "relationship.created_at"
            )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> RelationshipModel:
        data = _mapping(data, "relationship")
        return cls(
            id=data["id"],
            user_id=data["user_id"],
            relationship_type=data["relationship_type"],
            closeness=data["closeness"],
            trust=data["trust"],
            familiarity=data["familiarity"],
            last_updated=data["last_updated"],
            interaction_count=data.get("interaction_count", 0),
            positive_interactions=data.get("positive_interactions", 0),
            negative_interactions=data.get("negative_interactions", 0),
            notes=data.get("notes", ""),
            created_at=data.get("created_at"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "relationship_type": self.relationship_type,
            "closeness": self.closeness,
            "trust": self.trust,
            "familiarity": self.familiarity,
            "last_updated": self.last_updated,
            "interaction_count": self.interaction_count,
            "positive_interactions": self.positive_interactions,
            "negative_interactions": self.negative_interactions,
            "notes": self.notes,
            "created_at": self.created_at,
        }

    @property
    def estimated_tokens(self) -> int:
        return estimate_tokens(self.to_dict())


@dataclass(slots=True)
class UserSchema:
    """v0.4.0 离线用户图式（kind=3）：把散落的情感反应抽象成稳定画像。

    唯一真正的「学习」，把散落的情感反应抽象成「这个人是什么样」。
    """

    id: str
    user_id: str
    schema_type: str  # 如 "情感型", "理性型", "混合型", "内向型", "外向型"
    traits: list[str]  # 抽象出的稳定特质
    emotional_patterns: list[str]  # 情感反应模式
    preferred_topics: list[str]  # 偏好话题
    avoided_topics: list[str]  # 回避话题
    confidence: float  # 0.0~1.0，图式置信度
    last_updated: str
    observation_count: int = 0
    created_at: str | None = None

    def __post_init__(self) -> None:
        self.id = _string(self.id, "user_schema.id")
        self.user_id = _string(self.user_id, "user_schema.user_id")
        self.schema_type = _string(self.schema_type, "user_schema.schema_type")
        self.traits = _string_list(self.traits, "user_schema.traits")
        self.emotional_patterns = _string_list(
            self.emotional_patterns, "user_schema.emotional_patterns"
        )
        self.preferred_topics = _string_list(
            self.preferred_topics, "user_schema.preferred_topics"
        )
        self.avoided_topics = _string_list(
            self.avoided_topics, "user_schema.avoided_topics"
        )
        self.confidence = _number(self.confidence, "user_schema.confidence")
        self.last_updated = _datetime_field(
            self.last_updated, "user_schema.last_updated"
        )
        self.observation_count = _integer(
            self.observation_count, "user_schema.observation_count"
        )
        if self.created_at is not None:
            self.created_at = _datetime_field(
                self.created_at, "user_schema.created_at"
            )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> UserSchema:
        data = _mapping(data, "user_schema")
        return cls(
            id=data["id"],
            user_id=data["user_id"],
            schema_type=data["schema_type"],
            traits=data.get("traits", []),
            emotional_patterns=data.get("emotional_patterns", []),
            preferred_topics=data.get("preferred_topics", []),
            avoided_topics=data.get("avoided_topics", []),
            confidence=data["confidence"],
            last_updated=data["last_updated"],
            observation_count=data.get("observation_count", 0),
            created_at=data.get("created_at"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "schema_type": self.schema_type,
            "traits": list(self.traits),
            "emotional_patterns": list(self.emotional_patterns),
            "preferred_topics": list(self.preferred_topics),
            "avoided_topics": list(self.avoided_topics),
            "confidence": self.confidence,
            "last_updated": self.last_updated,
            "observation_count": self.observation_count,
            "created_at": self.created_at,
        }

    @property
    def estimated_tokens(self) -> int:
        return estimate_tokens(self.to_dict())


@dataclass(slots=True)
class NarrativeLayer:
    """v0.4.0 叙事层（kind=6）：把情感事件串成「我们那段日子」。

    包括亲近期/磨合期/摩擦期等阶段标记，+ 主题 + 事件引用。
    能回答「我们是什么关系」，而不仅是「他喜欢什么」。
    v0.9.0: 增加 unresolved_threads。
    """

    id: str
    user_id: str
    phase: str  # "亲近期" | "磨合期" | "摩擦期" | "稳定期" | "冷淡期"
    theme: str  # 叙事主题，如 "共同成长", "克服困难", "日常陪伴"
    event_ids: list[str]  # 引用的情节记忆 ID
    summary: str  # 叙事摘要
    last_updated: str
    unresolved_threads: list[str] = field(default_factory=list)  # v0.9.0: 未平复心结/目标
    created_at: str | None = None

    def __post_init__(self) -> None:
        self.id = _string(self.id, "narrative.id")
        self.user_id = _string(self.user_id, "narrative.user_id")
        self.phase = _choice(
            self.phase,
            "narrative.phase",
            ("亲近期", "磨合期", "摩擦期", "稳定期", "冷淡期"),
        )
        self.theme = _string(self.theme, "narrative.theme")
        self.event_ids = _string_list(self.event_ids, "narrative.event_ids")
        self.unresolved_threads = _string_list(
            self.unresolved_threads, "narrative.unresolved_threads"
        )
        self.summary = _string(self.summary, "narrative.summary")
        self.last_updated = _datetime_field(
            self.last_updated, "narrative.last_updated"
        )
        if self.created_at is not None:
            self.created_at = _datetime_field(
                self.created_at, "narrative.created_at"
            )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> NarrativeLayer:
        data = _mapping(data, "narrative")
        return cls(
            id=data["id"],
            user_id=data["user_id"],
            phase=data["phase"],
            theme=data["theme"],
            event_ids=data.get("event_ids", []),
            unresolved_threads=data.get("unresolved_threads", []),
            summary=data["summary"],
            last_updated=data["last_updated"],
            created_at=data.get("created_at"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "phase": self.phase,
            "theme": self.theme,
            "event_ids": list(self.event_ids),
            "unresolved_threads": list(self.unresolved_threads),
            "summary": self.summary,
            "last_updated": self.last_updated,
            "created_at": self.created_at,
        }

    @property
    def estimated_tokens(self) -> int:
        return estimate_tokens(self.to_dict())


@dataclass(slots=True)
class MemoryAssociation:
    """v0.4.0 记忆联想边（ASSOC）：Hebbian 共激活强化。

    同轮被一起想起的记忆之间长 ASSOC 边；/memory associate <id>
    打印 2 跳联想链。
    """

    id: str
    source_memory_id: str
    target_memory_id: str
    strength: float  # 0.0~1.0，联想强度
    co_activation_count: int = 0  # 共同被检索的次数
    last_co_activated: str | None = None
    created_at: str | None = None

    def __post_init__(self) -> None:
        self.id = _string(self.id, "association.id")
        self.source_memory_id = _string(
            self.source_memory_id, "association.source_memory_id"
        )
        self.target_memory_id = _string(
            self.target_memory_id, "association.target_memory_id"
        )
        self.strength = _number(self.strength, "association.strength")
        self.co_activation_count = _integer(
            self.co_activation_count, "association.co_activation_count"
        )
        if self.last_co_activated is not None:
            self.last_co_activated = _datetime_field(
                self.last_co_activated, "association.last_co_activated"
            )
        if self.created_at is not None:
            self.created_at = _datetime_field(
                self.created_at, "association.created_at"
            )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> MemoryAssociation:
        data = _mapping(data, "association")
        return cls(
            id=data["id"],
            source_memory_id=data["source_memory_id"],
            target_memory_id=data["target_memory_id"],
            strength=data["strength"],
            co_activation_count=data.get("co_activation_count", 0),
            last_co_activated=data.get("last_co_activated"),
            created_at=data.get("created_at"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source_memory_id": self.source_memory_id,
            "target_memory_id": self.target_memory_id,
            "strength": self.strength,
            "co_activation_count": self.co_activation_count,
            "last_co_activated": self.last_co_activated,
            "created_at": self.created_at,
        }

    @property
    def estimated_tokens(self) -> int:
        return estimate_tokens(self.to_dict())


# ── v0.11.0 ActiveGoal（A1）──────────────────────────────────────

GOAL_STATUSES = ("open", "reminded", "done", "expired")


@dataclass(slots=True)
class ActiveGoal:
    """v0.11.0：结构化约定/目标，支持 deadline 与到期提醒状态机。

    兼容旧库：纯字符串目标等价于 ``ActiveGoal(text=s)``（open、无 deadline）。
    """

    text: str
    deadline: str | None = None          # ISO8601（带时区），无则 None
    created_at: str = ""
    source_memory_id: str | None = None
    status: str = "open"                 # open | reminded | done | expired
    id: str = ""

    def __post_init__(self) -> None:
        self.text = _string(self.text, "goal.text")[:80]
        self.status = _choice(self.status, "goal.status", GOAL_STATUSES)
        if self.deadline is not None and self.deadline != "":
            self.deadline = to_iso8601(parse_iso8601(self.deadline))
        else:
            self.deadline = None
        if not self.id:
            self.id = new_memory_id("goal")
        else:
            self.id = _string(self.id, "goal.id")
        self.created_at = _string(self.created_at, "goal.created_at", allow_empty=True)
        if self.source_memory_id is not None:
            self.source_memory_id = _string(
                self.source_memory_id, "goal.source_memory_id"
            )

    # ── 兼容旧行为：与字符串相等、str() 即 text ──
    def __str__(self) -> str:
        return self.text

    def __eq__(self, other: object) -> bool:
        if isinstance(other, ActiveGoal):
            return self.text == other.text
        if isinstance(other, str):
            return self.text == other
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self.text)

    @property
    def plain(self) -> bool:
        """无 deadline 且仍 open 的目标可降级为纯字符串序列化（兼容旧数据格式）。"""
        return self.deadline is None and self.status == "open"

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | str) -> ActiveGoal:
        if isinstance(data, str):
            return cls(text=data)
        data = _mapping(data, "goal")
        return cls(
            text=data["text"],
            deadline=data.get("deadline"),
            created_at=data.get("created_at", ""),
            source_memory_id=data.get("source_memory_id"),
            status=data.get("status", "open"),
            id=data.get("id", ""),
        )

    def to_dict(self, *, compact: bool = True) -> Any:
        if compact and self.plain:
            return self.text
        return {
            "id": self.id,
            "text": self.text,
            "deadline": self.deadline,
            "created_at": self.created_at,
            "source_memory_id": self.source_memory_id,
            "status": self.status,
        }

    def is_due(self, now: datetime) -> bool:
        return (
            self.status == "open"
            and self.deadline is not None
            and parse_iso8601(self.deadline) <= now
        )

    @property
    def deadline_dt(self) -> datetime | None:
        return parse_iso8601(self.deadline) if self.deadline else None


def normalize_goal(value: Any) -> ActiveGoal:
    """str / dict / ActiveGoal 统一转为 ActiveGoal（旧库兼容入口）。"""
    if isinstance(value, ActiveGoal):
        return value
    if isinstance(value, str):
        return ActiveGoal(text=value)
    if isinstance(value, Mapping):
        return ActiveGoal.from_dict(value)
    raise MemoryValidationError("goal must be a string, object, or ActiveGoal")


@dataclass(slots=True)
class ProactiveConfig:
    """v0.4.0 主动开口配置：频率/静默/每日上限/由头选择。

    有由头才生成给模型的提示，台词由模型自己说。
    v0.11.0 新增：约定提醒额度（A1）与「忽然想起」（A2）配置与计数器。
    """

    id: str
    user_id: str
    enabled: bool = True
    min_interval_minutes: int = 60  # 最小开口间隔（分钟）
    daily_limit: int = 5  # 每日开口上限
    silence_threshold_minutes: int = 120  # 静默阈值（分钟），超过则可开口
    quiet_hours_start: int = 22  # 安静时段开始（小时）
    quiet_hours_end: int = 8  # 安静时段结束（小时）
    last_proactive_at: str | None = None
    daily_count: int = 0
    daily_reset_date: str | None = None
    last_umo: str | None = None  # 宿主会话路由（主动开口的发送目标），由适配器更新
    created_at: str | None = None
    # ── v0.11.0 A1：到期约定提醒 ──
    goal_reminder_daily_limit: int = 1
    goal_reminder_count: int = 0
    goal_reminder_reset_date: str | None = None
    # ── v0.11.0 A2：忽然想起 ──
    sudden_recall_enabled: bool = True
    sudden_recall_daily_limit: int = 2
    sudden_recall_cooldown_days: int = 7
    sudden_recall_probability: float = 0.20
    sudden_recall_count: int = 0
    sudden_recall_reset_date: str | None = None
    sudden_recall_log: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.id = _string(self.id, "proactive.id")
        self.user_id = _string(self.user_id, "proactive.user_id")
        self.enabled = _boolean(self.enabled, "proactive.enabled")
        self.min_interval_minutes = _integer(
            self.min_interval_minutes, "proactive.min_interval_minutes"
        )
        self.daily_limit = _integer(
            self.daily_limit, "proactive.daily_limit"
        )
        self.silence_threshold_minutes = _integer(
            self.silence_threshold_minutes, "proactive.silence_threshold_minutes"
        )
        self.quiet_hours_start = _integer(
            self.quiet_hours_start, "proactive.quiet_hours_start"
        )
        self.quiet_hours_end = _integer(
            self.quiet_hours_end, "proactive.quiet_hours_end"
        )
        self.daily_count = _integer(
            self.daily_count, "proactive.daily_count"
        )
        if self.last_proactive_at is not None:
            self.last_proactive_at = _datetime_field(
                self.last_proactive_at, "proactive.last_proactive_at"
            )
        if self.daily_reset_date is not None:
            self.daily_reset_date = _string(
                self.daily_reset_date, "proactive.daily_reset_date"
            )
        if self.last_umo is not None:
            self.last_umo = _string(self.last_umo, "proactive.last_umo")
        if self.created_at is not None:
            self.created_at = _datetime_field(
                self.created_at, "proactive.created_at"
            )
        # ── v0.11.0 A1 ──
        self.goal_reminder_daily_limit = _integer(
            self.goal_reminder_daily_limit, "proactive.goal_reminder_daily_limit"
        )
        self.goal_reminder_count = _integer(
            self.goal_reminder_count, "proactive.goal_reminder_count"
        )
        if self.goal_reminder_reset_date is not None:
            self.goal_reminder_reset_date = _string(
                self.goal_reminder_reset_date, "proactive.goal_reminder_reset_date"
            )
        # ── v0.11.0 A2 ──
        self.sudden_recall_enabled = _boolean(
            self.sudden_recall_enabled, "proactive.sudden_recall_enabled"
        )
        self.sudden_recall_daily_limit = _integer(
            self.sudden_recall_daily_limit, "proactive.sudden_recall_daily_limit"
        )
        self.sudden_recall_cooldown_days = _integer(
            self.sudden_recall_cooldown_days, "proactive.sudden_recall_cooldown_days"
        )
        self.sudden_recall_probability = _number(
            self.sudden_recall_probability, "proactive.sudden_recall_probability"
        )
        self.sudden_recall_count = _integer(
            self.sudden_recall_count, "proactive.sudden_recall_count"
        )
        if self.sudden_recall_reset_date is not None:
            self.sudden_recall_reset_date = _string(
                self.sudden_recall_reset_date, "proactive.sudden_recall_reset_date"
            )
        if not isinstance(self.sudden_recall_log, list):
            raise MemoryValidationError("proactive.sudden_recall_log must be an array")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ProactiveConfig:
        data = _mapping(data, "proactive")
        return cls(
            id=data["id"],
            user_id=data["user_id"],
            enabled=data.get("enabled", True),
            min_interval_minutes=data.get("min_interval_minutes", 60),
            daily_limit=data.get("daily_limit", 5),
            silence_threshold_minutes=data.get("silence_threshold_minutes", 120),
            quiet_hours_start=data.get("quiet_hours_start", 22),
            quiet_hours_end=data.get("quiet_hours_end", 8),
            last_proactive_at=data.get("last_proactive_at"),
            daily_count=data.get("daily_count", 0),
            daily_reset_date=data.get("daily_reset_date"),
            last_umo=data.get("last_umo"),
            created_at=data.get("created_at"),
            goal_reminder_daily_limit=data.get("goal_reminder_daily_limit", 1),
            goal_reminder_count=data.get("goal_reminder_count", 0),
            goal_reminder_reset_date=data.get("goal_reminder_reset_date"),
            sudden_recall_enabled=data.get("sudden_recall_enabled", True),
            sudden_recall_daily_limit=data.get("sudden_recall_daily_limit", 2),
            sudden_recall_cooldown_days=data.get("sudden_recall_cooldown_days", 7),
            sudden_recall_probability=data.get("sudden_recall_probability", 0.20),
            sudden_recall_count=data.get("sudden_recall_count", 0),
            sudden_recall_reset_date=data.get("sudden_recall_reset_date"),
            sudden_recall_log=list(data.get("sudden_recall_log", [])),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "enabled": self.enabled,
            "min_interval_minutes": self.min_interval_minutes,
            "daily_limit": self.daily_limit,
            "silence_threshold_minutes": self.silence_threshold_minutes,
            "quiet_hours_start": self.quiet_hours_start,
            "quiet_hours_end": self.quiet_hours_end,
            "last_proactive_at": self.last_proactive_at,
            "daily_count": self.daily_count,
            "daily_reset_date": self.daily_reset_date,
            "last_umo": self.last_umo,
            "created_at": self.created_at,
            "goal_reminder_daily_limit": self.goal_reminder_daily_limit,
            "goal_reminder_count": self.goal_reminder_count,
            "goal_reminder_reset_date": self.goal_reminder_reset_date,
            "sudden_recall_enabled": self.sudden_recall_enabled,
            "sudden_recall_daily_limit": self.sudden_recall_daily_limit,
            "sudden_recall_cooldown_days": self.sudden_recall_cooldown_days,
            "sudden_recall_probability": self.sudden_recall_probability,
            "sudden_recall_count": self.sudden_recall_count,
            "sudden_recall_reset_date": self.sudden_recall_reset_date,
            "sudden_recall_log": list(self.sudden_recall_log),
        }

    @property
    def estimated_tokens(self) -> int:
        return estimate_tokens(self.to_dict())


# ── v0.7.0 SelfState ──────────────────────────────────────────────


@dataclass(slots=True)
class SelfState:
    """可持久、可演化的「这一刻的自己」。

    存储为 separate table（peer 级单行），由每次 consolidate 后更新。
    注入时在 Core 之前优先输出，主动开口决策也读它。
    """

    energy: float = 0.85                     # 0.0–1.0
    mood_baseline: str = "平静"              # 当前底色情绪
    active_goals: list[ActiveGoal] = field(default_factory=list)  # ≤5（v0.11.0 结构化）
    self_narrative: str = ""                 # ≤80 字
    last_significant_event_id: str | None = None
    fatigue_reason: str | None = None        # v0.9.0: 最近 energy 下降的原因
    updated_at: str = ""
    version: int = 1

    _ENERGY_MIN: float = field(default=0.15, init=False, repr=False)
    _ENERGY_MAX: float = field(default=1.0, init=False, repr=False)

    def __post_init__(self) -> None:
        self.energy = float(self.energy)
        if self.energy != self.energy:  # NaN check
            raise MemoryValidationError("self_state.energy must be a valid number")
        self.energy = max(self._ENERGY_MIN, min(self._ENERGY_MAX, self.energy))
        self.mood_baseline = _string(self.mood_baseline, "self_state.mood_baseline")
        # v0.11.0：兼容旧库 list[str] —— 元素自动升级为 ActiveGoal(text=...)
        self.active_goals = [
            normalize_goal(item) for item in self.active_goals
        ][:5]
        self.self_narrative = _string(
            self.self_narrative, "self_state.self_narrative", allow_empty=True
        )
        if len(self.self_narrative) > 80:
            raise MemoryValidationError("self_state.self_narrative must not exceed 80 characters")
        self.last_significant_event_id = (
            _string(self.last_significant_event_id, "self_state.last_significant_event_id")
            if self.last_significant_event_id is not None
            else None
        )
        self.fatigue_reason = (
            _string(self.fatigue_reason, "self_state.fatigue_reason", allow_empty=True)
            if self.fatigue_reason is not None
            else None
        )
        self.updated_at = _string(self.updated_at, "self_state.updated_at", allow_empty=True)
        self.version = int(self.version)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> SelfState:
        data = _mapping(data, "self_state")
        return cls(
            energy=data.get("energy", 0.85),
            mood_baseline=data.get("mood_baseline", "平静"),
            active_goals=list(data.get("active_goals", [])),
            self_narrative=data.get("self_narrative", ""),
            last_significant_event_id=data.get("last_significant_event_id"),
            fatigue_reason=data.get("fatigue_reason"),
            updated_at=data.get("updated_at", ""),
            version=data.get("version", 1),
        )

    def to_dict(self) -> dict[str, Any]:
        # v0.11.0：无 deadline 的 open 目标序列化为纯字符串（旧格式兼容）
        return {
            "energy": self.energy,
            "mood_baseline": self.mood_baseline,
            "active_goals": [goal.to_dict(compact=True) for goal in self.active_goals],
            "self_narrative": self.self_narrative,
            "last_significant_event_id": self.last_significant_event_id,
            "fatigue_reason": self.fatigue_reason,
            "updated_at": self.updated_at,
            "version": self.version,
        }


# ── v0.8.0 DispositionSeed ────────────────────────────────────────


@dataclass(slots=True)
class DispositionSeed:
    """倾向种子：「种子 → 现行 → 再熏习」闭环的基础单元。

    存储为 separate table，每 peer 上限 15 条。
    """

    id: str
    statement: str                     # 「面对压力时倾向于先倾诉再行动」
    strength: float = 0.5             # 0.0–1.0
    evidence_ids: list[str] = field(default_factory=list)
    last_activated: str | None = None
    activation_count: int = 0
    created_at: str = ""
    updated_at: str = ""
    archived: bool = False

    def __post_init__(self) -> None:
        self.id = _string(self.id, "disposition.id")
        if len(self.id) > 64:
            raise MemoryValidationError("disposition.id must not exceed 64 characters")
        self.statement = _string(self.statement, "disposition.statement")
        if not self.statement:
            raise MemoryValidationError("disposition.statement must not be empty")
        if len(self.statement) > 200:
            raise MemoryValidationError("disposition.statement must not exceed 200 characters")
        self.strength = float(self.strength)
        if not (0.0 <= self.strength <= 1.0):
            raise MemoryValidationError("disposition.strength must be in [0, 1]")
        self.evidence_ids = _string_list(self.evidence_ids, "disposition.evidence_ids")
        self.last_activated = (
            _string(self.last_activated, "disposition.last_activated")
            if self.last_activated is not None
            else None
        )
        self.activation_count = int(self.activation_count)
        self.created_at = _string(self.created_at, "disposition.created_at", allow_empty=True)
        self.updated_at = _string(self.updated_at, "disposition.updated_at", allow_empty=True)
        self.archived = bool(self.archived)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> DispositionSeed:
        data = _mapping(data, "disposition")
        return cls(
            id=data["id"],
            statement=data["statement"],
            strength=data.get("strength", 0.5),
            evidence_ids=list(data.get("evidence_ids", [])),
            last_activated=data.get("last_activated"),
            activation_count=data.get("activation_count", 0),
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
            archived=data.get("archived", False),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "statement": self.statement,
            "strength": self.strength,
            "evidence_ids": list(self.evidence_ids),
            "last_activated": self.last_activated,
            "activation_count": self.activation_count,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "archived": self.archived,
        }
