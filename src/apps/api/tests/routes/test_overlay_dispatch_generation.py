"""Overlay/SFX dispatchers join the render-generation supersession model.

Before this fix, `dispatch_set_media_overlays` / `dispatch_set_sound_effects`
enqueued token-less renders and never bumped `render_generation_id`. Every AI
overlay writer funnels through these dispatchers (`apply_suggestions_to_variant`
→ dispatch, zero-click auto-apply → same helper, manual item-page saves →
dispatch), so an AI apply was invisible to the editor's baseline compare:
an editor Save loaded before the apply would full-replace `media_overlays`
with its stale list — silently wiping the AI-placed cards — and the AI render
itself could never be superseded/discarded.

House style of test_editor_commit.py: fake SimpleNamespace jobs, no DB,
render task captured via monkeypatch.
"""

from __future__ import annotations

import types
import uuid

import pytest
from fastapi import HTTPException

import app.routes.generative_jobs as gj

USER_ID = "u123"


def _job(**variant_extra):
    jid = uuid.uuid4()
    variant = {
        "variant_id": "song_text",
        "text_mode": "agent_text",
        "music_track_id": "t1",
        "render_status": "ready",
        "render_finished_at": "2026-07-01T00:00:00Z",
        "video_path": f"generative-jobs/{jid}/variant.mp4",
        "output_url": "https://signed/variant.mp4",
        "base_video_path": f"generative-jobs/{jid}/base_1.mp4",
        **variant_extra,
    }
    return types.SimpleNamespace(
        id=jid,
        assembly_plan={"variants": [variant]},
        all_candidates={"clip_paths": []},
        status="variants_ready",
        mode="content_plan",
    )


def _arm(monkeypatch) -> list[dict]:
    from app.config import settings

    monkeypatch.setattr(settings, "media_overlays_enabled", True, raising=False)
    monkeypatch.setattr(settings, "sound_effects_enabled", True, raising=False)
    # House pattern (test_autoplace_tasks.py): fake jobs aren't ORM instances.
    monkeypatch.setattr("sqlalchemy.orm.attributes.flag_modified", lambda obj, key: None)
    calls: list[dict] = []
    monkeypatch.setattr(
        "app.tasks.generative_build.regenerate_generative_variant",
        types.SimpleNamespace(apply_async=lambda **k: calls.append(k)),
        raising=False,
    )
    return calls


def _arm_reburn(monkeypatch) -> list[dict]:
    calls: list[dict] = []
    monkeypatch.setattr(
        "app.tasks.generative_build.reburn_narrated_captions",
        types.SimpleNamespace(apply_async=lambda **k: calls.append(k)),
        raising=False,
    )
    return calls


def _overlay(id_: str = "ov-1") -> dict:
    return {
        "id": id_,
        "kind": "image",
        "src_gcs_path": f"users/{USER_ID}/plan/item/pool/card.png",
        "position": "center",
        "x_frac": 0.5,
        "y_frac": 0.5,
        "scale": 0.35,
        "start_s": 0.4,
        "end_s": 2.4,
        "z": 0,
    }


def test_media_overlay_dispatch_bumps_generation_and_stamps_task(monkeypatch):
    calls = _arm(monkeypatch)
    job = _job()

    gj.dispatch_set_media_overlays(job, "song_text", overlays_raw=[_overlay()], user_id=USER_ID)

    v = job.assembly_plan["variants"][0]
    assert v["render_status"] == "rendering"
    new_gen = v.get("render_generation_id")
    assert new_gen, "dispatch must bump render_generation_id in the same write"
    assert len(calls) == 1
    assert calls[0]["kwargs"]["render_gen_id"] == new_gen
    assert calls[0]["queue"] == "overlay-jobs"


def test_sound_effects_dispatch_bumps_generation_and_stamps_task(monkeypatch):
    calls = _arm(monkeypatch)
    monkeypatch.setattr(
        gj, "validate_sound_effects_for_user", lambda *, sfx_raw, user_id: list(sfx_raw)
    )
    job = _job()

    gj.dispatch_set_sound_effects(
        job,
        "song_text",
        sfx_raw=[{"id": "sfx-1", "src_gcs_path": "sfx/pop.mp3", "at_s": 1.0}],
        user_id=USER_ID,
        db_for_glossary=None,
    )

    v = job.assembly_plan["variants"][0]
    new_gen = v.get("render_generation_id")
    assert new_gen
    assert len(calls) == 1
    assert calls[0]["kwargs"]["render_gen_id"] == new_gen


def test_media_overlay_dispatch_routes_subtitled_through_caption_reburn(monkeypatch):
    """Plan 010 + R2: caption archetypes get the manual lanes, but a lane save on
    a variant with a caption base must render via the caption reburn + reapply
    chain — the fast pass composites onto the CURRENT video and would silently
    drop an in-flight caption edit it supersedes. The validated lane is persisted
    in the SAME write as the gate/gen (the reburn reads persisted state).

    R1-1: the reburn's start write is token-checked, so the dispatcher returns a
    DEFERRED enqueue thunk (nothing queued yet) — the route commits the gate
    write first, then invokes it."""
    regen_calls = _arm(monkeypatch)
    reburn_calls = _arm_reburn(monkeypatch)
    job = _job(
        variant_id="subtitled",
        text_mode="none",
        resolved_archetype="subtitled",
        music_track_id=None,
    )

    enqueue = gj.dispatch_set_media_overlays(
        job, "subtitled", overlays_raw=[_overlay()], user_id=USER_ID
    )

    v = job.assembly_plan["variants"][0]
    assert v["render_status"] == "rendering"
    assert v["media_overlays"][0]["id"] == "ov-1"  # persisted with the gate write
    assert regen_calls == []
    assert reburn_calls == []  # deferred — nothing enqueued before the caller commits
    assert enqueue is not None
    enqueue()
    assert len(reburn_calls) == 1
    assert reburn_calls[0]["args"] == [str(job.id), "subtitled"]
    assert reburn_calls[0]["kwargs"]["render_gen_id"] == v["render_generation_id"]
    assert reburn_calls[0]["queue"] == "overlay-jobs"


def test_sound_effects_dispatch_routes_subtitled_through_caption_reburn(monkeypatch):
    regen_calls = _arm(monkeypatch)
    reburn_calls = _arm_reburn(monkeypatch)
    monkeypatch.setattr(
        gj, "validate_sound_effects_for_user", lambda *, sfx_raw, user_id: list(sfx_raw)
    )
    job = _job(
        variant_id="subtitled",
        text_mode="none",
        resolved_archetype="subtitled",
        music_track_id=None,
    )

    enqueue = gj.dispatch_set_sound_effects(
        job,
        "subtitled",
        sfx_raw=[{"id": "sfx-1", "src_gcs_path": "sfx/pop.mp3", "at_s": 1.0}],
        user_id=USER_ID,
        db_for_glossary=None,
    )

    v = job.assembly_plan["variants"][0]
    assert v["render_status"] == "rendering"
    assert v["sound_effects"][0]["id"] == "sfx-1"  # persisted with the gate write
    assert regen_calls == []
    assert reburn_calls == []  # R1-1: deferred until the caller commits
    assert enqueue is not None
    enqueue()
    assert len(reburn_calls) == 1
    assert reburn_calls[0]["kwargs"]["render_gen_id"] == v["render_generation_id"]
    assert reburn_calls[0]["queue"] == "overlay-jobs"


def test_media_overlay_dispatch_subtitled_without_base_keeps_fast_pass(monkeypatch):
    """Legacy safety (R2c): no cached caption base → the reburn can't run; the
    overlay fast pass still services the save (montage behavior)."""
    regen_calls = _arm(monkeypatch)
    reburn_calls = _arm_reburn(monkeypatch)
    job = _job(
        variant_id="subtitled",
        text_mode="none",
        resolved_archetype="subtitled",
        music_track_id=None,
        base_video_path=None,
    )

    gj.dispatch_set_media_overlays(job, "subtitled", overlays_raw=[_overlay()], user_id=USER_ID)

    v = job.assembly_plan["variants"][0]
    assert reburn_calls == []
    assert len(regen_calls) == 1
    assert regen_calls[0]["queue"] == "overlay-jobs"
    assert regen_calls[0]["kwargs"]["render_gen_id"] == v["render_generation_id"]
    assert regen_calls[0]["kwargs"]["media_overlays_override"][0]["id"] == "ov-1"


def test_editor_commit_after_overlay_apply_409s_instead_of_silent_clobber(monkeypatch):
    """CRITICAL regression (silent-clobber race, plans 005-009 vs editor #580).

    Sequence: editor session loads baseline → AI overlay apply lands through the
    dispatcher → editor Save echoes the pre-apply baseline. The Save must 409
    (baseline_conflict) with the AI-placed cards intact — on pre-fix code the
    dispatcher never moved the baseline, so this exact Save silently replaced
    `media_overlays` with the editor's stale list.
    """
    calls = _arm(monkeypatch)
    job = _job()
    editor_loaded_baseline = gj.variant_render_baseline(job.assembly_plan["variants"][0])

    gj.dispatch_set_media_overlays(
        job, "song_text", overlays_raw=[_overlay("ai-card")], user_id=USER_ID
    )
    assert len(calls) == 1
    # The apply-pass render finished; worker persisted the merged cards.
    v = job.assembly_plan["variants"][0]
    v["render_status"] = "ready"
    v["media_overlays"] = [_overlay("ai-card")]

    with pytest.raises(HTTPException) as exc:
        gj.prepare_editor_commit(
            job,
            "song_text",
            gj.EditorCommitRequest(
                media_overlays=[],  # editor's stale view: no cards
                base_generation=editor_loaded_baseline,
            ),
            user_id=USER_ID,
        )

    assert exc.value.status_code == 409
    assert exc.value.detail == "baseline_conflict"
    assert job.assembly_plan["variants"][0]["media_overlays"] == [_overlay("ai-card")]


def test_editor_commit_with_fresh_baseline_after_apply_succeeds(monkeypatch):
    """Companion: after a conflict-reload (client re-reads the moved baseline),
    the same Save goes through — supersession blocks stale writers, not users."""
    _arm(monkeypatch)
    job = _job()

    gj.dispatch_set_media_overlays(
        job, "song_text", overlays_raw=[_overlay("ai-card")], user_id=USER_ID
    )
    v = job.assembly_plan["variants"][0]
    v["render_status"] = "ready"
    v["media_overlays"] = [_overlay("ai-card")]
    fresh_baseline = gj.variant_render_baseline(v)

    from app.config import settings

    monkeypatch.setattr(settings, "media_overlays_enabled", True, raising=False)
    prep = gj.prepare_editor_commit(
        job,
        "song_text",
        gj.EditorCommitRequest(media_overlays=[], base_generation=fresh_baseline),
        user_id=USER_ID,
    )
    assert prep["has_render_section"] is True
    assert job.assembly_plan["variants"][0]["media_overlays"] is None
