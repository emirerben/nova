"""Request-scoped persistence for provider-marked Gemini thought summaries.

The publisher is deliberately separate from creation-thread events: a live
summary must not advance a chat revision while an accepted turn is running.
Only the model client's ``part.thought`` path calls an attempt sink. Neither
prompts, signatures, nor summary text are written to diagnostic logs.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from threading import Lock

import structlog
from sqlalchemy import update

from app.database import sync_session
from app.models import ThoughtSummary

log = structlog.get_logger()
_MAX_TEXT = 12_000
_current_publisher: ContextVar[ThoughtSummaryPublisher | None] = ContextVar(
    "current_thought_summary_publisher", default=None
)


class ThoughtSummaryPublisher:
    """One authenticated client request, with fenced model attempts.

    Each attempt gets its own closure. If a timed-out provider thread produces
    another chunk after the runtime starts a retry, its older closure is stale
    and cannot write into the new attempt's row.
    """

    def __init__(
        self,
        *,
        creator_id: uuid.UUID,
        client_request_id: str,
        thread_id: uuid.UUID | None = None,
        plan_item_id: uuid.UUID | None = None,
    ) -> None:
        if (thread_id is None) == (plan_item_id is None):
            raise ValueError("exactly one thought-summary subject is required")
        if not client_request_id or len(client_request_id) > 160:
            raise ValueError("invalid thought-summary request id")
        self.creator_id = creator_id
        self.client_request_id = client_request_id
        self.thread_id = thread_id
        self.plan_item_id = plan_item_id
        self._lock = Lock()
        self._generation = 0
        self._row_id: uuid.UUID | None = None
        self._text = ""
        self._closed = False
        self._last_write_at = 0.0
        self._attempt_started_at: datetime | None = None

    def begin_attempt(self) -> Callable[[str], None]:
        with self._lock:
            if self._generation == 0:
                self._discard_prior_request_locked()
            self._discard_current_locked()
            self._generation += 1
            generation = self._generation
            self._text = ""
            self._row_id = None
            self._closed = False
            self._last_write_at = 0.0
            self._attempt_started_at = datetime.now(UTC)

        def publish(chunk: str) -> None:
            self._append(generation, chunk)

        return publish

    def _append(self, generation: int, chunk: str) -> None:
        if not isinstance(chunk, str) or not chunk:
            return
        with self._lock:
            if generation != self._generation or self._closed:
                return
            remaining = _MAX_TEXT - len(self._text)
            if remaining <= 0:
                return
            next_text = self._text + chunk[:remaining]
            if not next_text.strip():
                self._text = next_text
                return
            self._text = next_text
            # The client polls once per second. Avoid a database commit for
            # every provider token while still publishing the first text fast.
            now = time.monotonic()
            if self._row_id is not None and now - self._last_write_at < 0.25:
                return
            try:
                with sync_session() as db:
                    if self._row_id is None:
                        row = ThoughtSummary(
                            creator_id=self.creator_id,
                            thread_id=self.thread_id,
                            plan_item_id=self.plan_item_id,
                            client_request_id=self.client_request_id,
                            status="streaming",
                            text=next_text,
                            started_at=self._attempt_started_at,
                        )
                        db.add(row)
                        db.flush()
                        pending_row_id = row.id
                    else:
                        db.execute(
                            update(ThoughtSummary)
                            .where(
                                ThoughtSummary.id == self._row_id,
                                ThoughtSummary.status == "streaming",
                            )
                            .values(text=next_text)
                        )
                    db.commit()
                    if self._row_id is None:
                        self._row_id = pending_row_id
                    self._last_write_at = now
            except Exception:  # noqa: BLE001 - a display failure cannot fail the model call
                log.warning("thought_summary_persist_failed")

    def complete(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if self._row_id is None or not self._text.strip():
                return
            try:
                with sync_session() as db:
                    db.execute(
                        update(ThoughtSummary)
                        .where(
                            ThoughtSummary.id == self._row_id, ThoughtSummary.status == "streaming"
                        )
                        .values(status="completed", text=self._text, completed_at=datetime.now(UTC))
                    )
                    db.commit()
            except Exception:  # noqa: BLE001 - a display failure cannot fail the reply
                log.warning("thought_summary_complete_failed")

    def fail(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._discard_current_locked()

    def _discard_current_locked(self) -> None:
        if self._row_id is None:
            return
        try:
            with sync_session() as db:
                db.execute(
                    update(ThoughtSummary)
                    .where(ThoughtSummary.id == self._row_id, ThoughtSummary.status == "streaming")
                    .values(status="failed", text="")
                )
                db.commit()
        except Exception:  # noqa: BLE001 - an unavailable summary store cannot fail a retry
            log.warning("thought_summary_discard_failed")
        self._row_id = None

    def _discard_prior_request_locked(self) -> None:
        """Fence unfinished rows left by an earlier worker for this request."""
        try:
            with sync_session() as db:
                db.execute(
                    update(ThoughtSummary)
                    .where(
                        ThoughtSummary.creator_id == self.creator_id,
                        ThoughtSummary.client_request_id == self.client_request_id,
                        ThoughtSummary.thread_id == self.thread_id,
                        ThoughtSummary.plan_item_id == self.plan_item_id,
                        ThoughtSummary.status == "streaming",
                    )
                    .values(status="failed", text="")
                )
                db.commit()
        except Exception:  # noqa: BLE001 - preserve the model call if display storage fails
            log.warning("thought_summary_prior_discard_failed")


@contextmanager
def bind_thought_publisher(publisher: ThoughtSummaryPublisher | None) -> Iterator[None]:
    token = _current_publisher.set(publisher)
    try:
        yield
    finally:
        _current_publisher.reset(token)


def current_thought_publisher() -> ThoughtSummaryPublisher | None:
    return _current_publisher.get()


def publisher_for_creation(
    *, creator_id: uuid.UUID, thread_id: uuid.UUID, client_request_id: str
) -> ThoughtSummaryPublisher | None:
    from app.config import settings  # noqa: PLC0415

    if not settings.thought_summaries_enabled:
        return None
    return ThoughtSummaryPublisher(
        creator_id=creator_id, thread_id=thread_id, client_request_id=client_request_id
    )


def publisher_for_slide_post(
    *, creator_id: uuid.UUID, plan_item_id: uuid.UUID, client_request_id: str | None
) -> ThoughtSummaryPublisher | None:
    from app.config import settings  # noqa: PLC0415

    if not settings.thought_summaries_enabled or not client_request_id:
        return None
    return ThoughtSummaryPublisher(
        creator_id=creator_id, plan_item_id=plan_item_id, client_request_id=client_request_id
    )
