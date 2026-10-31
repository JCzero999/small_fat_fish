from __future__ import annotations

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
    """

    def __init__(
        self,
        brain: ExtraBrain,
        *,
        consolidate_every: int = 8,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if (
            isinstance(consolidate_every, bool)
            or not isinstance(consolidate_every, int)
            or consolidate_every <= 0
        ):
            raise ValueError("consolidate_every must be a positive integer")
        self.brain = brain
        self.consolidate_every = consolidate_every
        self.clock = clock or utc_now
        self.session_id: str | None = None
        self.messages: list[ConversationMessage] = []

    def start_session(self, session_id: str | None = None) -> str:
        self.session_id = session_id or f"session_{uuid4().hex[:8]}"
        self.messages = []
        return self.session_id

    @property
    def pending_count(self) -> int:
        return len(self.messages)

    def add(
        self, role: str, content: str, *, auto_consolidate: bool = True
    ) -> ConsolidationReport | None:
        self.messages.append(ConversationMessage(role=role, content=content))
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
        return self.brain.build_injection(
            query,
            recent_context=recent_context,
            entities=entities,
            token_limit=token_limit,
            now=now if now is not None else self.clock(),
        )
