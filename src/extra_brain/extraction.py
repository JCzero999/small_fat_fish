from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .models import (
    MemoryValidationError,
    _choice,
    _mapping,
    _number,
    _string,
    _string_list,
    parse_iso8601,
)


DEEP_EXTRACTION_PROMPT = """你是一个极度严谨的长期记忆整理官。你的唯一任务是从对话中提取对「持续陪伴关系」真正有长期价值的信息。

对话内容：
{conversation}

输出要求：
- 只输出合法 JSON，不要任何解释或 markdown
- 严格遵守下面的结构
- 如果没有值得记录的内容，对应数组必须为空
- 重要性评分必须保守：只有真正影响关系 continuity 的才给高分

{
  "core_candidate_updates": {
    "user_profile_changes": [],
    "relationship_changes": [],
    "new_or_updated_threads": [],
    "should_promote_to_core": []
  },
  "semantic_facts": [
    {
      "type": "fact|preference|relationship|goal|trait",
      "subject": "",
      "predicate": "",
      "object": "",
      "detail": "",
      "importance": 0.0,
      "emotion_valence": "positive|neutral|negative|mixed",
      "emotion_intensity": 0.0,
      "confidence": 0.0
    }
  ],
  "episodic_events": [
    {
      "summary": "",
      "key_moments": [],
      "emotions": [],
      "importance": 0.0,
      "emotion_state": "open|acknowledged|attended|resolved|dormant"
    }
  ],
  "emotion_updates": [
    {
      "episodic_id": "",
      "to_state": "acknowledged|attended|resolved|dormant"
    }
  ],
  "style_observations": [
    {
      "observation": "",
      "category": "expression|preference|interaction_habit|boundary",
      "importance": 0.0
    }
  ],
  "conflicts_detected": []
}

提取铁律：
1. 情绪强度 + 关系意义 > 信息量
2. 一次性闲聊和技术细节默认不记录，除非用户明确赋予高情绪价值
3. 用户前后矛盾时必须在 conflicts_detected 中标注
4. importance ≥ 0.8 的条目需要有明确理由支撑（在 detail 或 summary 中体现）
5. 线程宁合勿分：同一主题（如「准备面试」）只保留一个线程，新信息更新已有线程，不要重复新建
6. 情感事件状态机（emotion_state）：新提取的情绪事件默认 "open"（用户表达了情绪但本轮尚未被回应/安抚）；
   若对话显示用户的情绪已经被接住、被回应 → "acknowledged"；被认真讨论/陪伴 → "attended"；
   用户明确表示想通了/释怀/翻篇 → "resolved"；提及旧事但情绪已淡、只是顺带 → "dormant"。
   判断不了就写 "open"——未平复的情绪不应衰减，会被强制优先回忆（反漠视设计）。
7. emotion_updates：当本轮对话推进了「已有长期记忆」中某条情节记忆（kind=episodic）的情感状态时，
   输出 {"episodic_id":"已有情节记忆完整ID","to_state":"acknowledged|attended|resolved|dormant"}。
   只允许向前推进（open→acknowledged→attended→resolved，或任意→dormant），禁止编造 ID，状态不明确时省略。

数组元素的精确格式（用于消除原规范中的歧义）：
- user_profile_changes: {"field":"preferred_name|key_traits|core_preferences|important_life_facts","value":"字符串","operation":"set|add|remove"}
  preferred_name 仅允许 set/remove；其余字段仅允许 add/remove。
- relationship_changes: {"field":"name|persona_core|relationship_definition|speaking_style","value":"字符串"}
- new_or_updated_threads: {"id":"已有线程ID（新建时省略）","summary":"字符串","status":"open|watching|closed","last_mentioned":"ISO8601（可省略）"}
  关于线程 id：只能填写「已有长期记忆」中真实存在的线程 id，绝对禁止自己编造 id；
  如果对话主题已经存在于某个已有线程，必须复用该线程的 id 来更新它（只写一个条目）；
  只有全新主题才省略 id 新建线程。
- should_promote_to_core: 已有记忆的完整 ID 字符串数组；不要编造 ID。
- conflicts_detected: {"semantic_id":"已有语义记忆完整ID","reason":"冲突原因"}。

已有长期记忆（含语义记忆、情节记忆，以及 kind=active_thread 的已有线程；
仅用于识别更新、晋升、线程复用和冲突；没有则为空数组）：
{existing_memories}
"""


@dataclass(frozen=True, slots=True)
class ConversationMessage:
    role: str
    content: str


@dataclass(frozen=True, slots=True)
class ProfileChange:
    field: str
    value: str
    operation: str


@dataclass(frozen=True, slots=True)
class RelationshipChange:
    field: str
    value: str


@dataclass(frozen=True, slots=True)
class ThreadChange:
    summary: str
    status: str
    id: str | None = None
    last_mentioned: str | None = None


@dataclass(frozen=True, slots=True)
class CoreCandidateUpdates:
    user_profile_changes: list[ProfileChange] = field(default_factory=list)
    relationship_changes: list[RelationshipChange] = field(default_factory=list)
    new_or_updated_threads: list[ThreadChange] = field(default_factory=list)
    should_promote_to_core: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class SemanticCandidate:
    type: str
    subject: str
    predicate: str
    object: str
    detail: str
    importance: float
    emotion_valence: str
    emotion_intensity: float
    confidence: float


@dataclass(frozen=True, slots=True)
class EpisodicCandidate:
    summary: str
    key_moments: list[str]
    emotions: list[str]
    importance: float
    emotion_state: str = "open"


@dataclass(frozen=True, slots=True)
class EmotionUpdate:
    """对已有情节记忆的情感状态推进（v0.2.0 状态机的模型侧输入）。"""

    episodic_id: str
    to_state: str


@dataclass(frozen=True, slots=True)
class StyleCandidate:
    observation: str
    category: str
    importance: float


@dataclass(frozen=True, slots=True)
class ConflictDetection:
    semantic_id: str
    reason: str


@dataclass(frozen=True, slots=True)
class ExtractionResult:
    core_candidate_updates: CoreCandidateUpdates = field(default_factory=CoreCandidateUpdates)
    semantic_facts: list[SemanticCandidate] = field(default_factory=list)
    episodic_events: list[EpisodicCandidate] = field(default_factory=list)
    emotion_updates: list[EmotionUpdate] = field(default_factory=list)
    style_observations: list[StyleCandidate] = field(default_factory=list)
    conflicts_detected: list[ConflictDetection] = field(default_factory=list)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ExtractionResult:
        data = _mapping(value, "extraction")
        _keys(
            data,
            "extraction",
            {
                "core_candidate_updates",
                "semantic_facts",
                "episodic_events",
                "style_observations",
                "conflicts_detected",
            },
            {"emotion_updates"},
        )
        core = _mapping(data["core_candidate_updates"], "core_candidate_updates")
        _keys(
            core,
            "core_candidate_updates",
            {
                "user_profile_changes",
                "relationship_changes",
                "new_or_updated_threads",
                "should_promote_to_core",
            },
        )

        profile_changes = []
        for index, item in enumerate(_array(core["user_profile_changes"], "user_profile_changes")):
            entry = _mapping(item, f"user_profile_changes[{index}]")
            _keys(entry, f"user_profile_changes[{index}]", {"field", "value", "operation"})
            field_name = _choice(
                entry["field"],
                "profile.field",
                ("preferred_name", "key_traits", "core_preferences", "important_life_facts"),
            )
            operation = _choice(entry["operation"], "profile.operation", ("set", "add", "remove"))
            if field_name == "preferred_name" and operation not in ("set", "remove"):
                raise MemoryValidationError("preferred_name only supports set/remove")
            if field_name != "preferred_name" and operation not in ("add", "remove"):
                raise MemoryValidationError("profile list fields only support add/remove")
            profile_changes.append(
                ProfileChange(field_name, _string(entry["value"], "profile.value"), operation)
            )

        relationship_changes = []
        for index, item in enumerate(_array(core["relationship_changes"], "relationship_changes")):
            entry = _mapping(item, f"relationship_changes[{index}]")
            _keys(entry, f"relationship_changes[{index}]", {"field", "value"})
            relationship_changes.append(
                RelationshipChange(
                    _choice(
                        entry["field"],
                        "relationship.field",
                        ("name", "persona_core", "relationship_definition", "speaking_style"),
                    ),
                    _string(entry["value"], "relationship.value"),
                )
            )

        threads = []
        for index, item in enumerate(_array(core["new_or_updated_threads"], "new_or_updated_threads")):
            entry = _mapping(item, f"new_or_updated_threads[{index}]")
            _keys(
                entry,
                f"new_or_updated_threads[{index}]",
                {"summary", "status"},
                {"id", "last_mentioned"},
            )
            thread_id = entry.get("id")
            if thread_id is not None:
                thread_id = _string(thread_id, "thread.id", allow_empty=True) or None
            last_mentioned = entry.get("last_mentioned")
            if last_mentioned is not None:
                last_mentioned = _string(
                    last_mentioned, "thread.last_mentioned", allow_empty=True
                ) or None
                if last_mentioned is not None:
                    parse_iso8601(last_mentioned)
            threads.append(
                ThreadChange(
                    summary=_string(entry["summary"], "thread.summary"),
                    status=_choice(entry["status"], "thread.status", ("open", "watching", "closed")),
                    id=thread_id,
                    last_mentioned=last_mentioned,
                )
            )

        facts = []
        for index, item in enumerate(_array(data["semantic_facts"], "semantic_facts")):
            entry = _mapping(item, f"semantic_facts[{index}]")
            names = {
                "type",
                "subject",
                "predicate",
                "object",
                "detail",
                "importance",
                "emotion_valence",
                "emotion_intensity",
                "confidence",
            }
            _keys(entry, f"semantic_facts[{index}]", names)
            facts.append(SemanticCandidate(**{name: entry[name] for name in names}))
            # Construct through the persisted model's validators without manufacturing storage metadata.
            _choice(facts[-1].type, "semantic.type", ("fact", "preference", "relationship", "goal", "trait"))
            _string(facts[-1].subject, "semantic.subject")
            _string(facts[-1].predicate, "semantic.predicate")
            _string(facts[-1].object, "semantic.object")
            _string(facts[-1].detail, "semantic.detail")
            _number(facts[-1].importance, "semantic.importance")
            _choice(facts[-1].emotion_valence, "semantic.emotion_valence", ("positive", "neutral", "negative", "mixed"))
            _number(facts[-1].emotion_intensity, "semantic.emotion_intensity")
            _number(facts[-1].confidence, "semantic.confidence")

        events = []
        for index, item in enumerate(_array(data["episodic_events"], "episodic_events")):
            entry = _mapping(item, f"episodic_events[{index}]")
            _keys(
                entry,
                f"episodic_events[{index}]",
                {"summary", "key_moments", "emotions", "importance"},
                {"emotion_state"},
            )
            events.append(
                EpisodicCandidate(
                    summary=_string(entry["summary"], "episodic.summary"),
                    key_moments=_string_list(entry["key_moments"], "episodic.key_moments"),
                    emotions=_string_list(entry["emotions"], "episodic.emotions"),
                    importance=_number(entry["importance"], "episodic.importance"),
                    emotion_state=_choice(
                        entry.get("emotion_state", "open"),
                        "episodic.emotion_state",
                        ("open", "acknowledged", "attended", "resolved", "dormant"),
                    ),
                )
            )

        emotion_updates = []
        raw_updates = data.get("emotion_updates", [])
        for index, item in enumerate(_array(raw_updates, "emotion_updates")):
            entry = _mapping(item, f"emotion_updates[{index}]")
            _keys(entry, f"emotion_updates[{index}]", {"episodic_id", "to_state"})
            emotion_updates.append(
                EmotionUpdate(
                    episodic_id=_string(entry["episodic_id"], "emotion_update.episodic_id"),
                    to_state=_choice(
                        entry["to_state"],
                        "emotion_update.to_state",
                        ("acknowledged", "attended", "resolved", "dormant"),
                    ),
                )
            )

        styles = []
        for index, item in enumerate(_array(data["style_observations"], "style_observations")):
            entry = _mapping(item, f"style_observations[{index}]")
            _keys(entry, f"style_observations[{index}]", {"observation", "category", "importance"})
            styles.append(
                StyleCandidate(
                    observation=_string(entry["observation"], "style.observation"),
                    category=_choice(
                        entry["category"],
                        "style.category",
                        ("expression", "preference", "interaction_habit", "boundary"),
                    ),
                    importance=_number(entry["importance"], "style.importance"),
                )
            )

        conflicts = []
        for index, item in enumerate(_array(data["conflicts_detected"], "conflicts_detected")):
            entry = _mapping(item, f"conflicts_detected[{index}]")
            _keys(entry, f"conflicts_detected[{index}]", {"semantic_id", "reason"})
            conflicts.append(
                ConflictDetection(
                    semantic_id=_string(entry["semantic_id"], "conflict.semantic_id"),
                    reason=_string(entry["reason"], "conflict.reason"),
                )
            )

        return cls(
            core_candidate_updates=CoreCandidateUpdates(
                user_profile_changes=profile_changes,
                relationship_changes=relationship_changes,
                new_or_updated_threads=threads,
                should_promote_to_core=_string_list(
                    core["should_promote_to_core"], "should_promote_to_core"
                ),
            ),
            semantic_facts=facts,
            episodic_events=events,
            emotion_updates=emotion_updates,
            style_observations=styles,
            conflicts_detected=conflicts,
        )


class MemoryExtractor(Protocol):
    def extract(self, prompt: str) -> ExtractionResult: ...


class ExtractionError(RuntimeError):
    pass


class OpenAICompatibleExtractor:
    """Minimal OpenAI-compatible chat client; works with DeepSeek without a vendor SDK."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str = "deepseek-chat",
        base_url: str = "https://api.deepseek.com",
        timeout_seconds: float = 60.0,
    ) -> None:
        if not api_key.strip():
            raise ValueError("api_key is required")
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    @classmethod
    def from_env(cls) -> OpenAICompatibleExtractor:
        api_key = os.environ.get("DEEPSEEK_API_KEY", "")
        if not api_key:
            raise ExtractionError("DEEPSEEK_API_KEY is not set")
        return cls(
            api_key=api_key,
            model=os.environ.get("DEEPSEEK_MODEL", "deepseek-chat"),
            base_url=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        )

    def extract(self, prompt: str) -> ExtractionResult:
        request = Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(
                {
                    "model": self.model,
                    "messages": [{"role": "user", "content": prompt}],
                    "response_format": {"type": "json_object"},
                    "temperature": 0.1,
                    "stream": False,
                }
            ).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
            content = payload["choices"][0]["message"]["content"]
            if not isinstance(content, str):
                raise ExtractionError("extractor response content is not a string")
            parsed = json.loads(content)
        except HTTPError as exc:
            raise ExtractionError(f"extractor HTTP request failed with status {exc.code}") from exc
        except (URLError, TimeoutError) as exc:
            raise ExtractionError(f"extractor request failed: {exc}") from exc
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise ExtractionError("extractor returned an invalid JSON response") from exc
        return ExtractionResult.from_dict(parsed)


def build_extraction_prompt(conversation: str, existing_memories: Sequence[Mapping[str, Any]]) -> str:
    conversation = _string(conversation, "conversation")
    serialized_existing = json.dumps(
        list(existing_memories), ensure_ascii=False, separators=(",", ":")
    )
    return DEEP_EXTRACTION_PROMPT.replace("{conversation}", conversation).replace(
        "{existing_memories}", serialized_existing
    )


def format_conversation(messages: Sequence[ConversationMessage | Mapping[str, str]]) -> str:
    lines = []
    for index, message in enumerate(messages):
        if isinstance(message, ConversationMessage):
            role, content = message.role, message.content
        else:
            data = _mapping(message, f"messages[{index}]")
            role = _string(data.get("role"), f"messages[{index}].role")
            content = _string(data.get("content"), f"messages[{index}].content")
        lines.append(f"{_string(role, 'message.role')}: {_string(content, 'message.content')}")
    if not lines:
        raise MemoryValidationError("conversation must contain at least one message")
    return "\n".join(lines)


def _array(value: Any, name: str) -> list[Any]:
    if not isinstance(value, list):
        raise MemoryValidationError(f"{name} must be an array")
    return value


def _keys(
    data: Mapping[str, Any], name: str, required: set[str], optional: set[str] | None = None
) -> None:
    optional = optional or set()
    missing = required - data.keys()
    unknown = data.keys() - required - optional
    if missing:
        raise MemoryValidationError(f"{name} is missing fields: {', '.join(sorted(missing))}")
    if unknown:
        raise MemoryValidationError(f"{name} has unknown fields: {', '.join(sorted(unknown))}")
