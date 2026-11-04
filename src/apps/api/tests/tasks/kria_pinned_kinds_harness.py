"""Fixtures for the v2 kinds through the pinned seam (see ``test_kria_pinned_kinds``).

Extends ``kria_pinned_suggestion_harness`` (a real thread on real Postgres, the real ``submit_turn``
-> ``run_kria_turn`` path, every model and broker poisoned). What this adds:

* ``serve_cards`` builds cards with the REAL v2 assembly (``assemble_suggestions``: the registry's
  ``build_action`` and ``witness``) from an agent answer, and stores them in the ring in the shape
  the serve lane writes. The seam under test only reads that ring.
* ``install_inputs`` stands in for the serve lane's shared loader
  (``suggestion_serve.with_transcripts`` adds every kind's own inputs). Both the serve here and the
  seam's ``ready_context`` call it, so a rebuild at the tap sees what the serve saw, exactly like
  production once the loader exists. It is built from the kinds' own typed builders
  (``hook_inputs``, ``loader_input``, ``load_evidence``) and reads the live thread, so it moves when
  the edit moves.
"""

from __future__ import annotations

import copy
import dataclasses
import types
import uuid
from typing import Any

from sqlalchemy import select

from app.config import settings
from app.database import AsyncSessionLocal, sync_session
from app.models import CreationThread, PlanItem
from app.services import suggestion_chips as sc
from app.services.suggestions.models import RankedPick, SuggestionAgentOutput, SuggestionSet
from tests.tasks import kria_pinned_suggestion_harness as base

FLAGS = {
    "kria_suggestion_hook_copy_enabled": True,
    "kria_suggestion_duplicates_enabled": True,
    "kria_suggestion_mute_clip_enabled": True,
    "kria_suggestion_filler_words_enabled": True,
    "kria_suggestion_signals_enabled": True,
}


def enable_kinds(monkeypatch) -> None:  # noqa: ANN001
    for name, value in FLAGS.items():
        monkeypatch.setattr(settings, name, value)


# ── The footage the cards read ───────────────────────────────────────────────────────


def ready_variant_without_title() -> dict[str, Any]:
    """The harness's rendered montage with no opening title (a hook card needs room for one)."""
    variant = base.ready_variant()
    variant["text_elements"] = []
    return variant


def set_analysis(seeded: base.Seeded, media_id: str, patch: dict[str, Any]) -> None:
    """Merge ``patch`` into one clip's stored ``analysis`` (signals, understanding)."""
    with sync_session() as db:
        item = db.get(PlanItem, seeded.item_id, with_for_update=True)
        rows = copy.deepcopy(item.clip_assignments)
        for row in rows:
            if row["media_id"] == media_id:
                row["analysis"] = {**(row.get("analysis") or {}), **patch}
        item.clip_assignments = rows
        db.commit()


def set_item(seeded: base.Seeded, **fields: Any) -> None:
    with sync_session() as db:
        item = db.get(PlanItem, seeded.item_id, with_for_update=True)
        for key, value in fields.items():
            setattr(item, key, value)
        db.commit()


def understood(seeded: base.Seeded, media_id: str, **understanding: Any) -> None:
    """Give a clip a complete understanding record (the base fixture's clips all speak)."""
    base_understanding = {"subject": f"{media_id} subject", "summary": "a short clip"}
    set_analysis(seeded, media_id, {"understanding": {**base_understanding, **understanding}})


# ── The shared loader's stand-in ─────────────────────────────────────────────────────


def install_inputs(  # noqa: ANN001
    monkeypatch, *, filler_evidence=None, needed=(), stale_copy_states=False
) -> None:
    """Make ``suggestion_serve.with_transcripts`` add every kind's inputs, from the live thread.

    ``stale_copy_states`` makes the hook inputs miss a creative-copy decision (a planner read that
    started before it landed), which is the race the in-lock consent gate exists for."""
    from app.kria import planner
    from app.kria.brief import load_latest_brief
    from app.services import suggestion_serve as serve
    from app.services.clip_understanding import clip_record
    from app.services.creative_copy_decisions import fold_creative_copy
    from app.services.suggestions.kinds import filler_words, hook, mute_clip

    real = serve.with_transcripts

    async def with_inputs(db, thread, ctx):  # noqa: ANN001, ANN202
        ctx = await real(db, thread, ctx)
        item = await db.get(PlanItem, thread.active_plan_item_id)
        brief = await load_latest_brief(db, thread.id)
        events = await planner._load_thread_events(db, thread.id, release=False)
        states = fold_creative_copy(
            events, dependency_digest=ctx.media_digest, include_suggestions=True
        )
        records = [
            clip_record(a.get("analysis"), kind="video")
            for a in item.clip_assignments
            if isinstance(a, dict)
        ]
        # The clip rows keep their completeness marks (the mute kind refuses an unmarked clip).
        marks = {r["media_id"]: r for r in sc._clip_rows(item)}
        ctx = dataclasses.replace(
            ctx,
            clips=[
                {
                    **c,
                    "_empty": marks[c["media_id"]]["_empty"],
                    "_incomplete": marks[c["media_id"]]["_incomplete"],
                }
                if c.get("media_id") in marks
                else c
                for c in ctx.clips
            ],
        )
        inputs = {
            **ctx.kind_inputs,
            "hook": hook.hook_inputs(
                brief=brief, copy_states={} if stale_copy_states else states, records=records
            ),
            "mute_clip": mute_clip.loader_input(brief),
            "duplicates": {"needed": sorted(needed), "capture_times": {}, "exposure": {}},
        }
        if filler_evidence is not None:
            inputs["filler_words"] = await filler_words.load_evidence(db, item)
        return dataclasses.replace(ctx, kind_inputs=inputs)

    monkeypatch.setattr(serve, "with_transcripts", with_inputs)


def stub_filler_analysis(monkeypatch, *, analysis) -> None:  # noqa: ANN001
    """The cleanup analysis row the filler evidence reads (the engine run is the kind lane's)."""
    from app.services import speech_cleanup_preflight as preflight
    from app.services.suggestions.kinds import filler_words

    async def current(_db, _item_id):  # noqa: ANN001, ANN202
        return analysis

    monkeypatch.setattr(preflight, "current_analysis_async", current)
    monkeypatch.setattr(preflight, "current_analysis_sync", lambda _db, _id, **_k: analysis)
    monkeypatch.setattr(
        filler_words, "_fingerprint", lambda _item: analysis.source_policy_fingerprint
    )


def analysis_row(**over: Any) -> types.SimpleNamespace:
    row = dict(
        id=uuid.uuid4(),
        source_policy_fingerprint="policy-fp-1",
        status="ready",
        decision=None,
        category_counts={"filler_sounds": 4, "long_pauses": 1, "retakes": 0},
        estimated_removed_ms=1760,
        source_storage_path="uploads/u1/clip.mp4",
        engine_version=None,
        analysis_payload=None,
    )
    row.update(over)
    return types.SimpleNamespace(**row)


# ── Serving v2 cards ─────────────────────────────────────────────────────────────────


async def serve_cards(
    seeded: base.Seeded,
    *picks: RankedPick,
    protocol: int | None = None,
) -> SuggestionSet:
    """The cards the thread is offered right now, built by the real registry, stored in the ring."""
    from app.services import suggestion_serve as serve

    async with AsyncSessionLocal() as db:
        thread = await db.get(CreationThread, seeded.thread_id)
        ctx = await sc.load_suggestion_context(db, thread, None, protocol)
        assert ctx.state == "ready", f"the seeded thread must serve ready cards, got {ctx.state}"
        ctx = await serve.with_transcripts(db, thread, ctx)
    plan = sc.build_candidates(ctx)
    output = SuggestionAgentOutput(ranked=list(picks)) if picks else None
    sset, drops = sc.assemble_suggestions(ctx, plan, output)
    assert sset is not None and sset.suggestions, f"nothing was offered: {drops}"
    base.write_ring(seeded.thread_id, sset)
    seeded.cards = {s.kind: s for s in sset.suggestions}
    seeded.set_id = sset.set_id
    return sset


def pick(kind: str, *, targets: tuple[str, ...] = (), copy_text: str | None = None, rank: int = 1):
    return RankedPick(kind=kind, target_ids=list(targets), why="", copy=copy_text, rank=rank)


def thread_state(thread_id: uuid.UUID) -> dict[str, Any]:
    with sync_session() as db:
        return copy.deepcopy(db.get(CreationThread, thread_id).state or {})


def ring_entry(thread_id: uuid.UUID, set_id: str) -> dict[str, Any] | None:
    ring = thread_state(thread_id).get(sc.RING_KEY) or []
    return next((e for e in ring if e.get("set_id") == set_id), None)


def item_row(seeded: base.Seeded) -> dict[str, Any]:
    with sync_session() as db:
        item = db.execute(select(PlanItem).where(PlanItem.id == seeded.item_id)).scalar_one()
        return {
            "speech_cleanup_enabled": bool(item.speech_cleanup_enabled),
            "user_edited": bool(item.user_edited),
            "edit_format": item.edit_format,
        }


def write_brief(seeded: base.Seeded, *updates) -> None:  # noqa: ANN002
    """The creator stated requirements: a typed brief version, as an earlier turn would write."""
    from app.kria.brief import apply_updates, load_latest_brief_sync

    with sync_session() as db:
        prior = load_latest_brief_sync(db, seeded.thread_id)
        merged = apply_updates(prior, tuple(updates), source_turn_id=None)
        db.add(
            base.CreativeBriefVersion(
                thread_id=seeded.thread_id,
                version=(prior.version if prior else 0) + 1,
                requirements=[r.model_dump(mode="json") for r in merged.requirements],
                source_turn_id=None,
            )
        )
        db.commit()


# ── Duplicates: real recorded measurements ───────────────────────────────────────────


def dup_case() -> dict[str, Any]:
    import json
    from pathlib import Path

    path = (
        Path(__file__).resolve().parents[1]
        / "fixtures"
        / "agent_evals"
        / "suggestion_kind_dupes"
        / "d01_two_takes_sharper.json"
    )
    return json.loads(path.read_text(encoding="utf-8"))


def seed_duplicate_pair(seeded: base.Seeded) -> None:
    """clip-a and clip-b are two takes of one shot (b is sharper); clip-c is another shot.

    The hashes are the near-duplicate lane's recorded measurements (d01), stored the way the
    signal writes them. No clip speaks, so the pair may be taken apart.
    """
    from app.services.clip_signals import near_dup_hash

    clips = {c["media_id"]: c for c in dup_case()["clips"]}
    for media_id, recorded in zip(base.MEDIA_IDS, ("m1", "m2", "m3"), strict=True):
        record = clips[recorded]
        set_analysis(
            seeded,
            media_id,
            {
                "duration_s": 8.0,
                "understanding": {
                    "subject": f"{media_id} subject",
                    "speech": {"has_speech": False},
                },
                "signals": {
                    near_dup_hash.NAME: {
                        "v": near_dup_hash.VERSION,
                        "data": {
                            "hashes": record["hashes"],
                            "sharpness": record["sharpness"],
                            "mean_luma": record["mean_luma"],
                        },
                    }
                },
            },
        )


async def get_suggestions(seeded: base.Seeded):  # noqa: ANN201
    """The real ``GET /creation-threads/{id}/suggestions`` for this thread (Redis down: the
    deterministic set; the cards' ids and the set id are the ones the serve would store)."""
    from types import SimpleNamespace

    from starlette.responses import Response

    from app.routes import creation_threads as ct

    async with AsyncSessionLocal() as db:
        return await ct.thread_suggestions(
            SimpleNamespace(headers={}),
            str(seeded.thread_id),
            SimpleNamespace(id=seeded.user_id),
            db,
            Response(),
            None,
        )


# ── The thread's own plan (what a restating tap re-drafts) ───────────────────────────


def seed_strategy_draft(seeded: base.Seeded) -> uuid.UUID:
    """The strategy draft the current video was made from: the head of the thread's drafts."""
    from app.kria.drafts import KriaDraftDocument, canonical_snapshot
    from app.models import CreatorEditDraft

    from app.agents._schemas.creator_agent import CreativeStrategy

    strategy = CreativeStrategy(rationale="A matcha ritual in three beats.").model_dump(
        mode="json", exclude_none=True
    )
    document = KriaDraftDocument(
        kind="strategy",
        intent="A matcha ritual in three beats.",
        edit_format="montage",
        strategy=strategy,
        changes=[],
    )
    snapshot, digest = canonical_snapshot(document)
    with sync_session() as db:
        draft = CreatorEditDraft(
            creator_id=seeded.user_id,
            thread_id=seeded.thread_id,
            item_id=seeded.item_id,
            variant_key="original_text",
            base_job_id=seeded.job_id,
            base_generation_id="generation-1",
            draft_revision=0,
            snapshot_json=snapshot,
            snapshot_hash=digest,
            is_head=True,
        )
        db.add(draft)
        db.commit()
        return draft.id


def noisy_audio(seeded: base.Seeded, media_id: str) -> dict[str, Any]:
    """Give a clip the REAL ``audio_quality`` measurement of a committed wind recording.

    The clip also gets a complete understanding without speech, so the mute kind may consider it.
    """
    from pathlib import Path

    from app.services.clip_signals import audio_quality
    from app.services.clip_signals.registry import SignalInputs

    path = (
        Path(__file__).resolve().parents[1] / "fixtures" / "clip_signals" / "audio" / "wind_00.m4a"
    )
    record = audio_quality.compute(
        SignalInputs(media_id=media_id, kind="video", local_path=str(path))
    )
    assert record is not None and record.get("noisy") is True, record
    set_analysis(
        seeded,
        media_id,
        {
            "duration_s": 8.0,
            "understanding": {"subject": "a street", "speech": {"has_speech": False}},
            "signals": {
                audio_quality.SPEC.name: {"v": audio_quality.SPEC.version, "data": dict(record)}
            },
        },
    )
    return dict(record)


# ── Filler words: a Talking item with a measured cleanup analysis ────────────────────

CLEANUP_FLAGS = {
    "silence_cut_enabled": True,
    "subtitled_archetype_enabled": True,
    "edit_format_talking_head_enabled": True,
    "narrated_self_narration_enabled": True,
    "speech_cleanup_mode": "opt_in",
    "speech_cleanup_preflight_mode": "off",
}


def filler_world(monkeypatch, *, analysis=None):  # noqa: ANN001, ANN201
    """The cleanup switches on, a ready analysis with measurable fillers, and the shared loader's
    stand-in reading it. ``analysis`` replaces the default row (the kind lane's own cafe numbers)."""
    for name, value in CLEANUP_FLAGS.items():
        monkeypatch.setattr(settings, name, value)
    row = analysis or analysis_row()
    stub_filler_analysis(monkeypatch, analysis=row)
    install_inputs(monkeypatch, filler_evidence=True)
    return row


def seed_talking_item(seeded: base.Seeded) -> None:
    """One take on a subtitled item: the shape speech cleanup can cut (the capability the PATCH
    route checks)."""
    set_item(
        seeded,
        edit_format="subtitled",
        clip_gcs_paths=["gs://b/take.mp4"],
        speech_cleanup_enabled=False,
        user_edited=False,
    )


def queued_analyses(seeded: base.Seeded) -> list[str]:
    from app.models import SpeechCleanupAnalysis

    with sync_session() as db:
        rows = db.execute(
            select(SpeechCleanupAnalysis).where(
                SpeechCleanupAnalysis.plan_item_id == seeded.item_id
            )
        ).scalars()
        return [r.status for r in rows]
