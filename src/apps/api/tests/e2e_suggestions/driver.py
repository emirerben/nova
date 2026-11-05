"""What the iPhone does, driven against the real server paths on real Postgres.

``Phone.serve`` is the creator opening the chat: ``GET /creation-threads/{id}/suggestions`` (the
real route), the worker run it enqueues (the real ``kria_suggester`` task body with the real
``SuggestionAgent``, answered by a cassette), and the GET again. ``Phone.tap`` is the creator
tapping a card: ``submit_turn`` with the card's own prompt and ``{set_id, chip_id}``, then
``run_kria_turn``. Nothing between is mocked except the model, the broker and the Redis behind the
suggestion store (see ``fakes``).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from unittest import mock

from starlette.responses import Response

from app.database import AsyncSessionLocal, sync_session
from app.models import CreatorAgentApproval, PlanItem
from app.services import suggestion_chips as sc
from tests.e2e_suggestions import fakes
from tests.tasks import kria_pinned_kinds_harness as k
from tests.tasks import kria_pinned_suggestion_harness as h

NO_LONGER_FITS = (
    "This suggestion no longer fits your edit. Nothing was changed, and I'm refreshing the ideas."
)
NO_LONGER_FITS_TR = (
    "Bu öneri artık düzenlemene uymuyor. Hiçbir şey değişmedi, önerileri yeniliyorum."
)
TASK_ARGS = {"trigger", "expected_digest", "language", "protocol", "attempt"}


@dataclass
class Tap:
    """Everything a tap left behind, read back from the database after ``run_kria_turn``."""

    card: Any
    set_id: str
    accepted: Any
    result: dict[str, str]
    before: dict[str, int]
    after: dict[str, int]
    reply: str
    chat: str
    pin: dict[str, Any]
    refused: dict[str, Any] | None
    head_before: dict[str, Any] | None
    head_after: dict[str, Any] | None
    receipts: list[dict[str, Any]] = field(default_factory=list)
    applied_event: dict[str, Any] = field(default_factory=dict)

    @property
    def planned(self) -> bool:
        return self.pin.get("outcome") == "planned"

    @property
    def new_drafts(self) -> int:
        return self.after["drafts"] - self.before["drafts"]


def head_of(thread_id) -> dict[str, Any] | None:  # noqa: ANN001
    return next((d for d in h.drafts(thread_id) if d["head"]), None)


def lane_diff(parent: dict[str, Any] | None, new: dict[str, Any] | None) -> dict[str, tuple]:
    """``{lane: (before, after)}`` for every editor lane that differs between two draft heads."""
    before = ((parent or {}).get("snap") or {}).get("editor_payload") or {}
    after = ((new or {}).get("snap") or {}).get("editor_payload") or {}
    return {
        lane: (before.get(lane), after.get(lane))
        for lane in sorted(set(before) | set(after))
        if before.get(lane) != after.get(lane)
    }


def approvals_pending(thread_id) -> int:  # noqa: ANN001
    with sync_session() as db:
        return len(
            db.execute(
                h.select(CreatorAgentApproval).where(
                    CreatorAgentApproval.thread_id == thread_id,
                    CreatorAgentApproval.status == "pending",
                )
            )
            .scalars()
            .all()
        )


def item_flags(seeded: h.Seeded) -> dict[str, Any]:
    with sync_session() as db:
        item = db.get(PlanItem, seeded.item_id)
        return {
            "speech_cleanup_enabled": bool(item.speech_cleanup_enabled),
            "user_edited": bool(item.user_edited),
        }


class Phone:
    """One creator's phone on one thread."""

    def __init__(
        self,
        seeded: h.Seeded,
        world,
        preferences: list[dict[str, Any]],
        language: str = "en",  # noqa: ANN001
    ) -> None:
        self.language = language
        self.seeded = seeded
        self.world = world
        self.client = fakes.CassetteClient(preferences)
        self.worker_runs: list[dict[str, Any]] = []

    # ── Serving ────────────────────────────────────────────────────────────────────

    async def get(self) -> sc.SuggestionSetOut:
        """The real ``GET /creation-threads/{id}/suggestions`` (a worker run may be enqueued)."""
        from app.routes import creation_threads as ct

        async with AsyncSessionLocal() as db:
            return await ct.thread_suggestions(
                SimpleNamespace(headers={}),
                str(self.seeded.thread_id),
                SimpleNamespace(id=self.seeded.user_id),
                db,
                Response(),
                None,
            )

    async def run_worker(self) -> int:
        """Run every suggester job the server enqueued, with the cassette as the model."""
        from app.services import ai_cost_control
        from app.tasks import kria_suggester

        ran = 0
        while self.world.refreshes:
            thread_id, kwargs = self.world.refreshes.pop(0)
            args = {key: value for key, value in kwargs.items() if key in TASK_ARGS}
            with (
                mock.patch("app.agents._model_client.default_client", lambda: self.client),
                mock.patch.object(ai_cost_control, "reserve_paid_call", lambda _request: None),
            ):
                found = await asyncio.to_thread(
                    kria_suggester.kria_suggester.run, thread_id, **args
                )
            self.worker_runs.append({"thread_id": thread_id, **args, "result": found})
            ran += 1
            if ran > 12:  # a busy turn defers itself; the real task gives up after a bounded number
                break
        return ran

    async def serve(self) -> sc.SuggestionSetOut:
        """Open the chat: GET, let the worker answer, GET again, until nothing is pending."""
        out = await self.get()
        for _ in range(4):
            if not await self.run_worker():
                break
            out = await self.get()
        return out

    # ── Tapping ────────────────────────────────────────────────────────────────────

    async def tap(
        self,
        card,  # noqa: ANN001
        out: sc.SuggestionSetOut,
        *,
        message: str | None = None,
        editor_state: dict | None = None,
        chip_id: str | None = None,
    ) -> Tap:
        """Send the card's prompt with its pin, run the turn, read back what it did."""
        seeded = self.seeded
        # Creators tap seconds apart: the freshness debounce and the single-flight locks expire.
        self.world.redis.advance(60)
        before = h.counts(seeded)
        head_before = head_of(seeded.thread_id)
        applied_before = len(h.events(seeded.thread_id, "draft_applied"))
        accepted, _publish = await h.submit(
            seeded,
            message if message is not None else card.prompt,
            tap={"set_id": out.set_id, "chip_id": chip_id or card.id},
            editor_state=editor_state,
        )
        result = await h.run(accepted.turn_id)
        assistant = [e for e in h.events(seeded.thread_id) if e.role == "assistant"][-1]
        chat = [e for e in h.events(seeded.thread_id) if e.event_type == "user_message"][-1]
        applied = h.events(seeded.thread_id, "draft_applied")
        fresh = len(applied) > applied_before
        applied_payload = dict(applied[-1].payload) if fresh else {}
        plan = h.turn_row(accepted.turn_id).plan_json or {}
        pin = (plan.get("diagnostics") or {}).get("suggestion_pin") or {}
        return Tap(
            card=card,
            set_id=out.set_id,
            accepted=accepted,
            result=result,
            before=before,
            after=h.counts(seeded),
            reply=assistant.content or "",
            chat=chat.content or "",
            pin=dict(pin),
            refused=(assistant.payload or {}).get("suggestion_pin"),
            head_before=head_before,
            head_after=head_of(seeded.thread_id),
            receipts=list(applied_payload.get("requirement_receipts") or []),
            applied_event=applied_payload,
        )

    async def undo(self) -> dict[str, Any] | None:
        """The Undo the phone sends for the head draft; returns the head draft it leaves."""
        from app.kria.drafts import undo_draft

        head = head_of(self.seeded.thread_id)
        assert head is not None, "there is no draft to undo"
        async with AsyncSessionLocal() as db:
            await undo_draft(
                db,
                thread_id=self.seeded.thread_id,
                creator_id=self.seeded.user_id,
                expected_revision=head["rev"],
            )
        return head_of(self.seeded.thread_id)

    def ring_entry(self, set_id: str) -> dict[str, Any] | None:
        return k.ring_entry(self.seeded.thread_id, set_id)
