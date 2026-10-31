from __future__ import annotations

from collections import deque
from datetime import datetime
from typing import Callable, Sequence
from uuid import uuid4

from .consolidation import ConsolidationReport
from .engine import ExtraBrain
from .extraction import ConversationMessage
from .models import utc_now
from .retrieval import Injection


class SessionTracker:
    """Accumulates turns and consolidates on a message-count threshold or on demand.

    The optional ``clock`` callable supplies "now", so a script can advance simulated
    days without waiting in real time.

    SensoryRing (感觉记忆): holds the last ``sensory_turns`` turns (user+assistant pairs)
    in a fixed-size ring buffer, never persisted to disk. Used during injection to give
    priority to recently mentioned content.

    WorkingSet (工作记忆): tracks memory IDs touched during the current turn, capped at
    ``working_set_cap`` entries. Cleared at the end of each turn. Items in the working
    set receive a recency boost during retrieval.
    """

    def __init__(
        self,
        brain: ExtraBrain,
        *,
        consolidate_every: int = 8,
        clock: Callable[[], datetime] | None = None,
        sensory_turns: int = 2,
        working_set_cap: int = 16,
    ) -> None:
        if (
            isinstance(consolidate_every, bool)
            or not isinstance(consolidate_every, int)
            or consolidate_every <= 0
        ):
            raise ValueError("consolidate_every must be a positive integer")
        if (
            isinstance(sensory_turns, bool)
            or not isinstance(sensory_turns, int)
            or sensory_turns <= 0
        ):
            raise ValueError("sensory_turns must be a positive integer")
        if (
            isinstance(working_set_cap, bool)
            or not isinstance(working_set_cap, int)
            or working_set_cap <= 0
        ):
            raise ValueError("working_set_cap must be a positive integer")
        self.brain = brain
        self.consolidate_every = consolidate_every
        self.clock = clock or utc_now
        self.session_id: str | None = None
        self.messages: list[ConversationMessage] = []
        # 感觉记忆：最近 sensory_turns 轮对话，不落盘
        self.sensory_ring: deque[ConversationMessage] = deque(maxlen=sensory_turns * 2)
        self.sensory_turns = sensory_turns
        # 工作记忆：本轮已触及的记忆 ID 集合，上限 working_set_cap，turn 结束清空
        self.working_set: set[str] = set()
        self.working_set_cap = working_set_cap

    def start_session(self, session_id: str | None = None) -> str:
        self.session_id = session_id or f"session_{uuid4().hex[:8]}"
        self.messages = []
        self.sensory_ring.clear()
        self.working_set.clear()
        return self.session_id

    @property
    def pending_count(self) -> int:
        return len(self.messages)

    def add(
        self, role: str, content: str, *, auto_consolidate: bool = True
    ) -> ConsolidationReport | None:
        self.messages.append(ConversationMessage(role=role, content=content))
        # 感觉记忆：每轮对话（user+assistant 成对）进入感觉记忆环
        self.sensory_ring.append(ConversationMessage(role=role, content=content))
        if auto_consolidate and len(self.messages) >= self.consolidate_every:
            return self.consolidate()
        return None

    def consolidate(self, *, now: datetime | None = None) -> ConsolidationReport | None:
        if not self.messages:
            return None
        if self.session_id is None:
            self.start_session()
        report = self.brain.consolidate(
            self.messages,
            session_id=self.session_id,
            now=now if now is not None else self.clock(),
        )
        self.messages = []
        self.working_set.clear()
        return report

    def inject(
        self,
        query: str,
        *,
        recent_context: Sequence[str] = (),
        entities: Sequence[str] = (),
        token_limit: int | None = None,
        now: datetime | None = None,
    ) -> Injection:
        # 工作记忆：将本轮检索到的记忆 ID 标记入 working_set
        injection = self.brain.build_injection(
            query,
            recent_context=recent_context,
            entities=entities,
            token_limit=token_limit,
            now=now if now is not None else self.clock(),
        )
        for mem_id in injection.retrieved.memory_ids:
            if len(self.working_set) >= self.working_set_cap:
                # 移除最早加入的一个（FIFO），保持集合大小不超过上限
                self.working_set.discard(next(iter(self.working_set)))
            self.working_set.add(mem_id)
        return injection

    def working_set_clear(self) -> None:
        """手动清空工作记忆集合（通常在 turn 结束时由 consolidate() 自动调用）。"""
        self.working_set.clear()
