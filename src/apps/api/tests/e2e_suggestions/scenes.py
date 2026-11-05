"""Setup shared by the round-5 proof: a thread in a named state, served by the unmodified GET.

``Scene`` is one creator's phone on one seeded thread. The state helpers (a native-editor Save, a
render that finished, a staged render that was denied or lapsed) drive the product's own entry
points: the editor-commit route through the ASGI app, ``decide_approval``, ``submit_turn``. The
only stand-ins are the render worker's completion write and the render dispatch (a recorder), the
same two the rest of this directory uses.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

import httpx

from app.database import AsyncSessionLocal, sync_session
from app.models import CreationThread, CreatorAgentApproval, CreatorAgentSession, Job
from tests.e2e_suggestions import threads
from tests.e2e_suggestions.driver import Phone, Tap
from tests.e2e_suggestions.threads import SIGNALS, TALKING, Scenario
from tests.tasks import kria_pinned_suggestion_harness as h

CASSETTES = Path(__file__).resolve().parent / "cassettes"


def prefs(key: str) -> list[dict[str, Any]]:
    path = CASSETTES / f"{key.replace('/', '_')}.json"
    return json.loads(path.read_text(encoding="utf-8"))["prefs"]


@dataclass
class Scene:
    key: str
    profile: str
    scn: Scenario
    seeded: h.Seeded
    phone: Phone
    world: Any

    async def twin(self) -> Scene:
        """A new thread of the same scenario and profile, with its own phone."""
        return build(self.world, self.key, self.profile)


def build(  # noqa: ANN001
    world, key: str, profile: str, *, brief: tuple = (), scn: Scenario | None = None
) -> Scene:
    """Seed a thread (the footage, the render, the plan, the signals) and give it a phone. A
    Talking take also gets the speech-cleanup analysis its filler card is built from. ``scn``
    replaces the scenario ``key`` names (the cassette is still ``key``'s)."""
    scn = scn or threads.SCENARIOS[key]
    seeded = threads.seed_thread(scn, profile)
    if profile == TALKING:
        threads.seed_cleanup_analysis(seeded, language=scn.language)
    for batch in brief:
        from tests.tasks import kria_pinned_kinds_harness as k  # noqa: PLC0415

        k.write_brief(seeded, *batch)
    world.refreshes.clear()
    return Scene(key, profile, scn, seeded, Phone(seeded, world, prefs(key), scn.language), world)


def card_of(out, kind: str):  # noqa: ANN001, ANN201
    return next((c for c in out.suggestions if c.kind == kind), None)


def kinds_of(out) -> list[str]:  # noqa: ANN001
    return [c.kind for c in out.suggestions]


def untouched(tap: Tap) -> bool:
    """A refused tap wrote nothing a creator could see: no draft, approval, brief or job row."""
    return tap.new_drafts == 0 and tap.before == tap.after


def is_refused(tap: Tap, reason: str | None = None) -> bool:
    signal = tap.refused or {}
    return bool(
        signal.get("outcome") == "refused"
        and (reason is None or signal.get("reason") == reason)
        and untouched(tap)
    )


def executed(tap: Tap) -> bool:
    return bool(tap.planned and tap.refused is None)


# ── A native-editor Save and a finished render ───────────────────────────────────────


def _stamp() -> str:
    # The wire format every render stamp uses: naive UTC with a literal "Z".
    return datetime.now(UTC).replace(tzinfo=None).isoformat() + "Z"


def finish_render(seeded: h.Seeded) -> None:
    """What the render worker writes when the Save's render lands (the one stand-in of a Save)."""
    h.edit_variant(
        seeded, lambda v: v.update(render_status="ready", render_finished_at=_stamp(), ok=True)
    )


def baseline(seeded: h.Seeded) -> str:
    from app.routes.generative_jobs import variant_render_baseline  # noqa: PLC0415

    with sync_session() as db:
        return variant_render_baseline(db.get(Job, seeded.job_id).assembly_plan["variants"][0])


async def route_save(seeded: h.Seeded, **sections: Any) -> str:
    """The creator's Save: ``POST /plan-items/{item}/variants/{variant}/editor-commit`` through the
    real ASGI app (only the creator's identity is overridden, and the render kick is not sent to a
    broker), then the render worker's completion write. Returns the generation the Save minted."""
    from app.auth import get_current_user  # noqa: PLC0415
    from app.main import app  # noqa: PLC0415

    with sync_session() as db:
        variant = db.get(Job, seeded.job_id).assembly_plan["variants"][0]
    body = {**(sections or {"caption_meta": {"size_px": 96}})}
    body["base_generation"] = baseline(seeded)
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=seeded.user_id)
    try:
        # Stand-ins: the render kick (no broker), and where the sources live (a timeline Save keeps
        # only durable ones and checks each exists: the seeded clips sit under users/e2e, and there
        # is no bucket to ask).
        with (
            mock.patch("app.routes.plan_items.enqueue_editor_commit_render"),
            mock.patch("app.routes.generative_jobs._durable_sources_prefix", lambda _job: "users/"),
            mock.patch("app.routes.generative_jobs.storage.object_exists", return_value=True),
        ):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://phone"
            ) as client:
                response = await client.post(
                    f"/plan-items/{seeded.item_id}/variants/{variant['variant_id']}/editor-commit",
                    json=body,
                )
    finally:
        app.dependency_overrides.pop(get_current_user, None)
    assert response.status_code == 200, (response.status_code, response.text[:400])
    finish_render(seeded)
    return str(response.json()["generation"])


def pointer(seeded: h.Seeded) -> str | None:
    with sync_session() as db:
        return db.get(CreatorAgentSession, seeded.session_id).target_generation_id


def agent_reads_video(seeded: h.Seeded) -> str:
    """What a Kria render's completion does for the agent: the session's pointer follows the
    saved video (the only writer of the pointer besides an editor-op tap)."""
    generation = baseline(seeded)
    with sync_session() as db:
        session = db.get(CreatorAgentSession, seeded.session_id, with_for_update=True)
        session.target_generation_id = generation
        db.commit()
    return generation


def payload_of(draft: dict[str, Any] | None) -> dict[str, Any]:
    return ((draft or {}).get("snap") or {}).get("editor_payload") or {}


# ── A staged render that is not approved ─────────────────────────────────────────────


def pending_approval(seeded: h.Seeded) -> CreatorAgentApproval:
    with sync_session() as db:
        return db.execute(
            h.select(CreatorAgentApproval).where(
                CreatorAgentApproval.thread_id == seeded.thread_id,
                CreatorAgentApproval.status == "pending",
            )
        ).scalar_one()


def approval_statuses(seeded: h.Seeded) -> list[str]:
    with sync_session() as db:
        return list(
            db.execute(
                h.select(CreatorAgentApproval.status)
                .where(CreatorAgentApproval.thread_id == seeded.thread_id)
                .order_by(CreatorAgentApproval.created_at)
            ).scalars()
        )


async def decide(seeded: h.Seeded, decision: str):  # noqa: ANN201
    """The creator's answer to the staged approval: the real ``decide_approval``."""
    from app.kria.api_schemas import ApprovalDecisionBody  # noqa: PLC0415
    from app.kria.runtime import approval_fingerprint, decide_approval  # noqa: PLC0415

    approval = pending_approval(seeded)
    with sync_session() as db:
        revision = db.get(CreationThread, seeded.thread_id).revision
    async with AsyncSessionLocal() as db:
        return await decide_approval(
            db,
            thread_id=seeded.thread_id,
            approval_id=approval.id,
            creator_id=seeded.user_id,
            decision=decision,
            body=ApprovalDecisionBody(
                expected_thread_revision=revision,
                expected_draft_revision=approval.draft_revision,
                expected_approval_fingerprint=approval_fingerprint(approval),
            ),
        )


async def cancel_awaiting_turn(seeded: h.Seeded, turn_id: str) -> None:
    """The creator cancels the turn that is waiting for their approval."""
    import uuid  # noqa: PLC0415

    from app.kria.runtime import cancel_turn  # noqa: PLC0415

    with sync_session() as db:
        revision = db.get(CreationThread, seeded.thread_id).revision
    async with AsyncSessionLocal() as db:
        await cancel_turn(
            db,
            thread_id=seeded.thread_id,
            turn_id=uuid.UUID(turn_id),
            creator_id=seeded.user_id,
            expected_thread_revision=revision,
        )


def sweep_expired_approvals() -> None:
    """The beat task that expires approvals nobody decided (no creator in the loop)."""
    from app.tasks.kria_runtime import _expire_pending_approvals  # noqa: PLC0415

    _expire_pending_approvals()


def session_moves_on(seeded: h.Seeded) -> None:
    """The agent read a newer video than the one the staged render was planned on."""
    with sync_session() as db:
        session = db.get(CreatorAgentSession, seeded.session_id, with_for_update=True)
        session.target_generation_id = "generation-9"
        db.commit()


def lapse_approval(seeded: h.Seeded) -> None:
    """A day of nobody deciding: the pending approval's time ran out."""
    with sync_session() as db:
        approval = db.execute(
            h.select(CreatorAgentApproval).where(
                CreatorAgentApproval.thread_id == seeded.thread_id,
                CreatorAgentApproval.status == "pending",
            )
        ).scalar_one()
        approval.expires_at = datetime.now(UTC) - timedelta(minutes=5)
        db.commit()


__all__ = [
    "SIGNALS",
    "TALKING",
    "Scene",
    "agent_reads_video",
    "approval_statuses",
    "baseline",
    "build",
    "card_of",
    "cancel_awaiting_turn",
    "decide",
    "executed",
    "finish_render",
    "is_refused",
    "kinds_of",
    "lapse_approval",
    "payload_of",
    "pending_approval",
    "pointer",
    "prefs",
    "route_save",
    "session_moves_on",
    "sweep_expired_approvals",
    "untouched",
]
