"""Replay-style harness for pinned suggestion taps (see ``test_kria_pinned_suggestion``).

A fixture here is a real thread on real Postgres: a ready editor target, the creator's clips, a
served set of suggestion cards, a tap, and the real ``submit_turn`` -> ``run_kria_turn`` path.
Nothing the seam owns is mocked: the copilot parser, the draft compiler, receipts, the registry
builders and witnesses and the row-locked commit are all real. What is replaced is everything that
would reach a model or a broker (the copilot, Main Creator, the brief extractor, Celery, the
suggester queue) and three inputs that depend on deployment state rather than on the seam: the
editor capability map (it follows flags and source freshness), the sound-effects catalog and the
creator manifest.

``serve`` stands in for the serve lane that does not exist yet: it builds cards with the real
candidate engine and the real registry builders and witnesses, and writes them to the ring in
the shape ``pinned.ring_suggestions`` defines. The seam under test only reads that ring.
"""

from __future__ import annotations

import asyncio
import copy
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

from sqlalchemy import func, select

from app.config import settings
from app.database import AsyncSessionLocal, sync_session
from app.kria import planner
from app.kria.api_schemas import SubmitTurnBody
from app.kria.runtime import submit_turn
from app.models import (
    CreationThread,
    CreationThreadEvent,
    CreativeBriefVersion,
    CreatorAgentApproval,
    CreatorAgentExecution,
    CreatorAgentSession,
    CreatorAgentTurn,
    CreatorEditDraft,
    Job,
    PlanItem,
)
from app.services import suggestion_chips as sc
from app.services.suggestion_chips import RING_KEY
from app.services.suggestions import pinned, registry
from app.services.suggestions.context import snapshot_digest
from app.services.suggestions.models import (
    ChipAnchorOut,
    EditorOpsAction,
    Suggestion,
    SuggestionDigest,
    SuggestionSet,
    suggestion_id,
)
from app.tasks.kria_runtime import run_kria_turn
from tests.kria.test_creative_brief import _MANIFEST
from tests.kria.test_runtime_postgres_integration import _seed_runtime_project

# A capability map with every lane the five pinned ops touch open. The real derivation closes them
# unless several deployment flags are on and the sources are fresh; it is not what is under test.
CAPABILITIES = {
    "text_elements": True,
    "timeline": True,
    "clips": {"transitions": {"editable": True}},
    "music_operations": {"remove": True},
    "mix": True,
    "sfx": True,
    "caption_meta": {"editable": True},
    "caption_cues": {"editable": True},
}
SFX_CATALOG = [
    {
        "id": "fx-whoosh",
        "name": "Whoosh",
        "label": "Whoosh",
        "category": "swoosh",
        "duration_s": 0.6,
    }
]
MUSIC_ID = "00000000-0000-0000-0000-000000000001"
HOOK = {
    "id": "hook",
    "text": "Old matcha hook",
    "start_s": 0.0,
    "end_s": 2.0,
    "role": "generative_intro",
    "font_family": "Playfair Display",
    "size_px": 72,
    "color": "#FFFFFF",
    "effect": "static",
    "alignment": "center",
    "position": "middle",
}
CLIP_PATHS = ["users/test/a.mp4", "users/test/b.mp4", "users/test/c.mp4"]
MEDIA_IDS = ["clip-a", "clip-b", "clip-c"]


def ready_variant() -> dict[str, Any]:
    """A rendered montage: three cut-joined clips, a music track, captions switched off."""
    return {
        "variant_id": "original_text",
        "resolved_archetype": "montage",
        "render_status": "ready",
        "render_generation_id": "generation-1",
        "render_finished_at": "2026-09-07T08:00:00Z",
        "video_path": "generative-jobs/test/output.mp4",
        "base_video_path": "generative-jobs/test/base.mp4",
        "mix": 0.5,
        "music_track_id": MUSIC_ID,
        "captions_enabled": False,
        "text_elements": [copy.deepcopy(HOOK)],
        "caption_cues": [{"id": "cue-1", "text": "Kriya", "start_s": 0.0, "end_s": 1.0}],
        "ai_timeline": {
            "beat_grid": [],
            "slots": [
                {
                    "slot_id": f"slot-{index}",
                    "clip_index": index,
                    "media_id": MEDIA_IDS[index],
                    "source_gcs_path": CLIP_PATHS[index],
                    "source_duration_s": 8.0,
                    "in_s": 0.0,
                    "duration_beats": None,
                    "duration_s": 2.0,
                    "removed": False,
                    "transition_after": "cut",
                }
                for index in range(3)
            ],
        },
    }


def _assignments() -> list[dict[str, Any]]:
    speech = {"has_speech": True, "to_camera": False, "segments": [{"start_s": 0, "end_s": 1}]}
    return [
        {
            "media_id": media_id,
            "kind": "video",
            "source_filename": f"{media_id}.mov",
            "gcs_path": CLIP_PATHS[index],
            "duration_s": 8.0,
            "analysis": {
                "duration_s": 8.0,
                "understanding": {"subject": f"clip {index + 1}", "speech": speech},
            },
        }
        for index, media_id in enumerate(MEDIA_IDS)
    ]


@dataclass
class Seeded:
    user_id: uuid.UUID
    thread_id: uuid.UUID
    session_id: uuid.UUID
    item_id: uuid.UUID
    job_id: uuid.UUID
    cards: dict[str, Suggestion] = field(default_factory=dict)
    set_id: str = ""


def seed_ready_thread(variant: dict[str, Any] | None = None) -> Seeded:
    """A thread whose current render is ready, editable, and followed by no open question."""
    user_id, thread_id, session_id = _seed_runtime_project()
    variant = variant or ready_variant()
    job_id = uuid.uuid4()
    with sync_session() as db:
        session = db.get(CreatorAgentSession, session_id, with_for_update=True)
        item = db.get(PlanItem, session.plan_item_id, with_for_update=True)
        db.add(
            Job(
                id=job_id,
                user_id=user_id,
                status="variants_ready",
                mode="generative",
                raw_storage_path="",
                selected_platforms=["tiktok"],
                content_plan_item_id=item.id,
                content_plan_ownership_epoch=0,
                all_candidates={"clip_paths": list(CLIP_PATHS)},
                assembly_plan={"variants": [variant]},
            )
        )
        db.flush()
        item.current_job_id = job_id
        item.clip_assignments = _assignments()
        session.target_job_id = job_id
        session.target_variant_id = variant["variant_id"]
        session.target_generation_id = variant["render_generation_id"]
        session.manifest_hash = "b" * 64
        thread = db.get(CreationThread, thread_id, with_for_update=True)
        db.add_all(
            [
                CreationThreadEvent(
                    thread_id=thread_id,
                    sequence=2,
                    revision=3,
                    role="user",
                    event_type="user_message",
                    content="Make me a matcha video",
                    payload={"turn_status": "completed"},
                ),
                CreationThreadEvent(
                    thread_id=thread_id,
                    sequence=3,
                    revision=4,
                    role="assistant",
                    event_type="assistant_review",
                    content="Your video is ready.",
                    payload={"turn_value": "review"},
                ),
            ]
        )
        thread.revision = 4
        thread.updated_at = datetime.now(UTC)
        db.commit()
    return Seeded(user_id, thread_id, session_id, item.id, job_id)


def install_world(monkeypatch) -> SimpleNamespace:  # noqa: ANN001
    """Flags on, capabilities fixed, and every model or broker call poisoned or recorded."""
    for name, value in {
        "kria_runtime_v2_enabled": True,
        "kria_suggestion_chips_enabled": True,
        "kria_suggestion_pinned_taps_enabled": True,
        "kria_creative_brief_enabled": True,
        "kria_brief_binding_enabled": True,
        "kria_editor_state_turns_enabled": True,
        "main_creator_agent_enabled": True,
        "kria_clip_understanding_enabled": False,
        "clip_intents_enabled": False,
    }.items():
        monkeypatch.setattr(settings, name, value)
    monkeypatch.setattr(
        "app.services.kria_editor_ops._editor_capabilities", lambda *_: CAPABILITIES
    )
    manifest = _MANIFEST.model_copy(update={"item_id": str(uuid.uuid4())})
    resolver = AsyncMock(return_value=(manifest, []))
    monkeypatch.setattr(planner, "resolve_item_creator_context", resolver)
    monkeypatch.setattr("app.services.creator_sessions.resolve_item_creator_context", resolver)
    monkeypatch.setattr(
        planner, "_copilot_clip_context", AsyncMock(return_value={"sfx_catalog": SFX_CATALOG})
    )

    def poisoned(name: str):  # noqa: ANN202
        def _raise(*_a, **_k):  # noqa: ANN002, ANN003
            raise AssertionError(f"{name}: a pinned tap must never reach a model or the planner")

        return _raise

    legacy_planner = AsyncMock(side_effect=poisoned("legacy planner"))
    monkeypatch.setattr("app.tasks.kria_runtime._plan_with_live_agent", legacy_planner)
    monkeypatch.setattr(planner, "run_copilot_turn", AsyncMock(side_effect=poisoned("copilot")))
    monkeypatch.setattr(planner, "MainCreatorAgent", poisoned("Main Creator"))
    monkeypatch.setattr(planner, "BriefExtractorAgent", poisoned("brief extractor"))
    monkeypatch.setattr(planner, "default_client", poisoned("model client"))
    world = SimpleNamespace(legacy_planner=legacy_planner, refreshes=[], published=[], extracted=[])
    monkeypatch.setattr(
        "app.services.suggestion_serve.enqueue_suggester",
        lambda thread_id, **kwargs: world.refreshes.append((thread_id, kwargs)) or True,
    )
    monkeypatch.setattr(
        run_kria_turn, "apply_async", lambda *a, **k: world.published.append((a, k)), raising=False
    )
    from app.tasks.kria_runtime import extract_kria_brief

    monkeypatch.setattr(
        extract_kria_brief,
        "apply_async",
        lambda *a, **k: world.extracted.append((a, k)),
        raising=False,
    )
    monkeypatch.setattr("app.tasks.kria_runtime.nudge_kria_observer", lambda *a: None)
    return world


def legacy_planner_answers(monkeypatch, text: str = "The planner answered."):  # noqa: ANN001, ANN201
    """Let the legacy planner exist again, answering with a plain reply, and record its calls."""
    from app.kria.contracts import KriaTurnPlan
    from app.kria.planner import PlannedKriaTurn

    answer = AsyncMock(
        return_value=PlannedKriaTurn(
            plan=KriaTurnPlan(mode="respond", turn_value="decision", response=text),
            manifest_hash="b" * 64,
            context_hash="a" * 64,
        )
    )
    monkeypatch.setattr("app.tasks.kria_runtime._plan_with_live_agent", answer)
    return answer


# ── Serving: the serve lane's stand-in ───────────────────────────────────────────────


async def serve(seeded: Seeded, *, protocol: int | None = None) -> SuggestionSet:
    """Build the cards the thread would be offered right now and store them in the ring."""
    async with AsyncSessionLocal() as db:
        thread = await db.get(CreationThread, seeded.thread_id)
        ctx = await sc.load_suggestion_context(db, thread, None, protocol)
    assert ctx.state == "ready", f"the seeded thread must serve ready cards, got {ctx.state}"
    plan = sc.build_candidates(ctx)
    base = snapshot_digest(ctx.snapshot)
    suggestions: list[Suggestion] = []
    for cand in plan.levers:
        kind = registry.get(str(cand.kind))
        if kind is None:
            continue
        action = kind.build_action(ctx, [cand.target], None)
        if action is None or not kind.witness(action, ctx).ok:
            continue
        suggestions.append(
            Suggestion(
                id=suggestion_id(str(cand.kind), [cand.target.key], base),
                kind=str(cand.kind),
                title=cand.fallback_label,
                prompt=cand.message,
                rank=len(suggestions) + 1,
                action=action,
                witness_tier="editor",
            )
        )
    sset = SuggestionSet(
        set_id=f"sg_{uuid.uuid4().hex[:16]}",
        digest=SuggestionDigest(media_digest=ctx.media_digest, state="ready"),
        state="ready",
        suggestions=suggestions,
    )
    write_ring(seeded.thread_id, sset)
    seeded.cards = {s.kind: s for s in suggestions}
    seeded.set_id = sset.set_id
    return sset


def write_ring(thread_id: uuid.UUID, sset: SuggestionSet, *, extra: dict | None = None) -> None:
    """Append a v2 entry to the ring the way the serve lane will (revision-neutral)."""
    with sync_session() as db:
        thread = db.get(CreationThread, thread_id, with_for_update=True)
        state = dict(thread.state or {})
        entry = {
            "set_id": sset.set_id,
            "state": sset.state,
            "digest": sset.digest.value(),
            "media_digest": sset.digest.media_digest,
            "final": True,
            "chips": [],
            "suggestions": pinned.ring_suggestions(sset),
            **(extra or {}),
        }
        state[RING_KEY] = [*(state.get(RING_KEY) or []), entry][-sc.RING_SIZE :]
        thread.state = state
        db.commit()


def card(
    kind: str,
    prompt: str,
    ops: list[dict[str, Any]],
    *,
    base_digest: str = "x" * 32,
    media_id: str | None = None,
    set_prefix: str = "ss_",
) -> Suggestion:
    return Suggestion(
        id=f"{set_prefix}{uuid.uuid4().hex[:12]}",
        kind=kind,
        title=prompt[:40],
        prompt=prompt,
        rank=1,
        anchor=ChipAnchorOut(media_id=media_id) if media_id else ChipAnchorOut(),
        action=EditorOpsAction(ops=ops, base_digest=base_digest),
        witness_tier="editor",
    )


def put_cards(seeded: Seeded, *cards: Suggestion) -> str:
    set_id = f"sg_{uuid.uuid4().hex[:16]}"
    ranked = [c.model_copy(update={"rank": i + 1}) for i, c in enumerate(cards)]
    write_ring(
        seeded.thread_id,
        SuggestionSet(
            set_id=set_id,
            digest=SuggestionDigest(state="ready"),
            state="ready",
            suggestions=ranked,
        ),
    )
    seeded.set_id = set_id
    seeded.cards.update({c.kind: c for c in ranked})
    return set_id


# ── Driving a turn ───────────────────────────────────────────────────────────────────


async def current_digest(seeded: Seeded, editor_state: dict | None = None) -> str:
    """The digest a card built right now would carry as its ``base_digest``."""
    from app.kria.api_schemas import EditorStateIn

    async with AsyncSessionLocal() as db:
        item = await db.get(PlanItem, seeded.item_id)
        target = await planner._load_editor_target(
            db,
            thread_id=seeded.thread_id,
            item=item,
            **(
                {"editor_state": EditorStateIn.model_validate(editor_state)} if editor_state else {}
            ),
        )
    return snapshot_digest(target.snapshot)


async def media_digest_of(seeded: Seeded) -> str:
    from app.kria.brief_binding import snapshot_media
    from app.services.creative_copy_decisions import media_digest

    async with AsyncSessionLocal() as db:
        return media_digest(snapshot_media(await db.get(PlanItem, seeded.item_id)))


def install_dedupe_kind(monkeypatch, *, flag: str | None = None) -> None:  # noqa: ANN001
    """A test stand-in for the duplicates lane's kind: ``remove_clip`` addressed by media id.

    The registered ``duplicates`` kind is a stub that never offers. This one builds the
    index-addressed op the way the real one will, so the seam's rebuild has something to run.
    """
    from app.services.suggestions.builders import editor_ops_witness
    from app.services.suggestions.context import SuggestionContext, slots
    from app.services.suggestions.models import SuggestionKind

    def available(ctx: SuggestionContext) -> bool:
        return len(slots(ctx)) >= 2

    def build(ctx, targets, _copy):  # noqa: ANN001, ANN202
        rows = slots(ctx)
        wanted = targets[0].media_id
        index = next((i for i, row in enumerate(rows) if row.get("media_id") == wanted), None)
        if index is None:
            return None
        return EditorOpsAction(
            ops=[{"op": "remove_clip", "slot_index": index}],
            base_digest=snapshot_digest(ctx.snapshot),
        )

    kind = SuggestionKind(
        kind="duplicates",
        states=frozenset({"ready"}),
        required_signals=(),
        availability=available,
        already_present=lambda ctx: False,
        build_action=build,
        witness=editor_ops_witness,
        surfaces=frozenset({"phone"}),
        flag=flag,
    )
    real = registry.get
    monkeypatch.setattr(registry, "get", lambda name: kind if name == "duplicates" else real(name))


def after_planning(monkeypatch, mutate) -> None:  # noqa: ANN001
    """Run ``mutate`` right after the pinned plan is built and before the row-locked commit."""
    from app.tasks import kria_runtime

    real = kria_runtime._plan_pinned_turn

    async def planned_then_moved(*args, **kwargs):  # noqa: ANN002, ANN003
        planned = await real(*args, **kwargs)
        mutate()
        return planned

    monkeypatch.setattr(kria_runtime, "_plan_pinned_turn", planned_then_moved)


def published_turns(world) -> list[str]:  # noqa: ANN001
    """The turn ids the runtime asked Celery to run (requeues and promoted successors)."""
    return [str(kwargs["args"][0]) for _args, kwargs in world.published]


def world_refreshes(world) -> list[str]:  # noqa: ANN001
    return [kwargs["trigger"] for _thread, kwargs in world.refreshes]


def draft_execution_manifest(seeded: Seeded) -> str | None:
    with sync_session() as db:
        return db.scalar(
            select(CreatorAgentExecution.expected_manifest_hash)
            .where(
                CreatorAgentExecution.target_thread_id == seeded.thread_id,
                CreatorAgentExecution.tool_name == "draft.apply_editor_ops",
            )
            .order_by(CreatorAgentExecution.created_at.desc())
            .limit(1)
        )


def revision(thread_id: uuid.UUID) -> int:
    with sync_session() as db:
        return int(db.get(CreationThread, thread_id).revision)


async def submit(
    seeded: Seeded,
    message: str,
    *,
    tap: dict[str, str] | None = None,
    event_id: str | None = None,
    expected_revision: int | None = None,
    editor_state: dict | None = None,
):  # noqa: ANN201
    body = SubmitTurnBody(
        message=message,
        client_event_id=event_id or f"tap-{uuid.uuid4().hex}",
        expected_thread_revision=revision(seeded.thread_id)
        if expected_revision is None
        else expected_revision,
        **({"suggestion": tap} if tap is not None else {}),
        **({"editor_state": editor_state} if editor_state is not None else {}),
    )
    async with AsyncSessionLocal() as db:
        return await submit_turn(
            db, thread_id=seeded.thread_id, creator_id=seeded.user_id, body=body
        )


async def tap(seeded: Seeded, kind: str, **kwargs):  # noqa: ANN003, ANN201
    chosen = seeded.cards[kind]
    return await submit(
        seeded,
        kwargs.pop("message", chosen.prompt),
        tap=kwargs.pop("tap", {"set_id": seeded.set_id, "chip_id": chosen.id}),
        **kwargs,
    )


async def run(turn_id: str) -> dict[str, str]:
    return await asyncio.to_thread(run_kria_turn.run, turn_id)


# ── Reading what happened ────────────────────────────────────────────────────────────


def drafts(thread_id: uuid.UUID) -> list[dict[str, Any]]:
    with sync_session() as db:
        rows = (
            db.execute(
                select(CreatorEditDraft)
                .where(CreatorEditDraft.thread_id == thread_id)
                .order_by(CreatorEditDraft.draft_revision)
            )
            .scalars()
            .all()
        )
        return [
            {
                "id": r.id,
                "rev": r.draft_revision,
                "parent": r.parent_draft_id,
                "head": r.is_head,
                "snap": r.snapshot_json,
                "gen": r.base_generation_id,
            }
            for r in rows
        ]


def events(thread_id: uuid.UUID, *types: str) -> list[CreationThreadEvent]:
    with sync_session() as db:
        query = select(CreationThreadEvent).where(CreationThreadEvent.thread_id == thread_id)
        if types:
            query = query.where(CreationThreadEvent.event_type.in_(types))
        rows = list(db.execute(query.order_by(CreationThreadEvent.sequence)).scalars().all())
        for row in rows:
            db.expunge(row)
        return rows


def briefs(thread_id: uuid.UUID) -> list[CreativeBriefVersion]:
    with sync_session() as db:
        rows = list(
            db.execute(
                select(CreativeBriefVersion)
                .where(CreativeBriefVersion.thread_id == thread_id)
                .order_by(CreativeBriefVersion.version)
            )
            .scalars()
            .all()
        )
        for row in rows:
            db.expunge(row)
        return rows


def turn_row(turn_id: str) -> CreatorAgentTurn:
    with sync_session() as db:
        row = db.get(CreatorAgentTurn, uuid.UUID(turn_id))
        db.refresh(row)
        db.expunge(row)
        return row


def counts(seeded: Seeded) -> dict[str, int]:
    """Row counts that a refused or replayed tap must leave exactly as they were."""
    with sync_session() as db:
        return {
            "drafts": db.scalar(
                select(func.count())
                .select_from(CreatorEditDraft)
                .where(CreatorEditDraft.thread_id == seeded.thread_id)
            ),
            "briefs": db.scalar(
                select(func.count())
                .select_from(CreativeBriefVersion)
                .where(CreativeBriefVersion.thread_id == seeded.thread_id)
            ),
            "approvals": db.scalar(
                select(func.count())
                .select_from(CreatorAgentApproval)
                .where(CreatorAgentApproval.thread_id == seeded.thread_id)
            ),
            "executions": db.scalar(
                select(func.count())
                .select_from(CreatorAgentExecution)
                .where(CreatorAgentExecution.target_thread_id == seeded.thread_id)
            ),
            "jobs": db.scalar(
                select(func.count()).select_from(Job).where(Job.user_id == seeded.user_id)
            ),
        }


def job_variant(seeded: Seeded) -> dict[str, Any]:
    with sync_session() as db:
        return copy.deepcopy(db.get(Job, seeded.job_id).assembly_plan["variants"][0])


def edit_variant(seeded: Seeded, patch) -> None:  # noqa: ANN001
    """Change the saved render underneath the thread (what an editor Save does)."""
    with sync_session() as db:
        job = db.get(Job, seeded.job_id, with_for_update=True)
        plan = copy.deepcopy(job.assembly_plan)
        patch(plan["variants"][0])
        job.assembly_plan = plan
        db.commit()
