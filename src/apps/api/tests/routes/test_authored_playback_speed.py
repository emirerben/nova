"""Authored phone Save keeps the creator's exact selected source window."""

import copy
import uuid

import pytest
from fastapi import HTTPException

from app.config import settings
from app.kria.device_render import make_device_request
from app.pipeline.phone_authored_timeline import compile_phone_authored_timeline
from app.routes import generative_jobs as gj
from app.services.authored_editor import (
    prepare_authored_editor_commit,
    resolve_authored_phone_slots,
)
from app.services.device_render import device_status, pin_device_request
from app.services.kria_editor_ops import build_editor_snapshot, compile_editor_ops
from app.services.phone_editor_sources import EDITOR_SOURCES_FIELD
from tests.pipeline.test_authored_timeline import fixture


def _authored_phone_job(monkeypatch):
    """A saved phone-authored draft with an ordinary editable text row."""
    job, variant, previous = fixture()
    job.id, job.user_id = uuid.uuid4(), uuid.uuid4()
    job.status, job.current_phase = "variants_ready", None
    variant.update(
        resolved_archetype="narrated",
        render_generation_id="first",
        render_status="ready",
        duration_s=12,
        text_mode="agent_text",
        text_elements=[
            {"id": "clip-label-new", "text": "Selected action", "start_s": 0, "end_s": 3}
        ],
    )
    job.assembly_plan["variants"] = [variant]
    pin_device_request(
        job,
        make_device_request(job_id=job.id, variant_id="original_text", revision=1, recipe=previous),
        base_generation="first",
    )
    monkeypatch.setattr(settings, "phone_rendering_enabled", True)
    monkeypatch.setattr(settings, "GENERATIVE_TIMELINE_EDITOR_ENABLED", True)
    monkeypatch.setattr(
        settings,
        "phone_render_verified_features",
        [
            "basicComposition",
            "local1080Export",
            "audioMix",
            "variableSpeed",
            "positionedText",
            "animatedText",
            "authoredText",
        ],
    )
    return job, variant


def _save_phone_slot(monkeypatch, slot: gj.TimelineSlotEdit):
    """Drive the real authored Save transaction through its emitted recipe."""
    job, _variant = _authored_phone_job(monkeypatch)
    prepare_authored_editor_commit(
        job,
        "original_text",
        gj.EditorCommitRequest(
            base_generation="first", editor_state_version=1, timeline_slots=[slot]
        ),
        validation_arguments={},
    )
    return job, device_status(job, "original_text").request.recipe


def _saved_authored_speed(monkeypatch):
    """Persist the exact 2x window before exercising a stale partial Save."""
    job, variant = _authored_phone_job(monkeypatch)
    prepare_authored_editor_commit(
        job,
        "original_text",
        gj.EditorCommitRequest(
            base_generation="first",
            editor_state_version=1,
            timeline_slots=[
                gj.TimelineSlotEdit(
                    clip_index=1, in_s=2, source_end_s=8, duration_s=3, playback_rate=2
                )
            ],
        ),
        validation_arguments={},
    )
    return job, job.assembly_plan["variants"][0]


def test_authored_phone_chat_speed_saves_reopens_and_keeps_exact_source_edges(monkeypatch):
    """Chat speed uses the normal device Save, not a mocked capability map."""
    job, variant = _authored_phone_job(monkeypatch)
    variant["user_timeline"]["slots"][0].update(
        in_s=2, duration_s=6, source_end_s=8, playback_rate=1
    )
    variant["text_elements"][0]["end_s"] = 6

    for rate in (0.75, 1.3, 1.5, 3.0, 1.0):
        snapshot = build_editor_snapshot(job, variant)
        assert snapshot["timeline_patch_capabilities"]["playback_rate"] is True
        payload = compile_editor_ops(
            job,
            variant,
            [
                {
                    "op": "patch_slots",
                    "selector": {"slot_indexes": [0]},
                    "patch": {"playback_rate": rate},
                }
            ],
        ).payload
        # The device route marks an authored timeline Save with its supported
        # wire-version; chat sends the same atomic payload through that gate.
        payload.editor_state_version = 1
        gj.prepare_editor_commit(job, "original_text", payload)
        variant = job.assembly_plan["variants"][0]
        saved = variant["user_timeline"]["slots"][0]
        recipe = device_status(job, "original_text").request.recipe
        clip = next(track for track in recipe.tracks if track.kind == "video").clips[0]

        expected_duration = round((6 / rate) * 30) / 30
        assert (saved["in_s"], saved["source_end_s"], saved["playback_rate"]) == (2, 8, rate)
        assert saved["duration_s"] == pytest.approx(expected_duration, abs=1e-6)
        assert (clip.source_start, clip.rate) == (2, rate)
        assert clip.source_duration <= 6
        assert clip.source_duration / rate + (clip.hold_duration or 0) == pytest.approx(
            expected_duration, abs=1e-6
        )
        assert gj.editor_deletion_timeline(job, variant)[0]["source_end_s"] == 8


def test_authored_stale_save_omitting_speed_fields_keeps_saved_exact_window(monkeypatch):
    """A closed speed capability cannot turn a stale partial Save into a 1x trim."""
    job, variant = _saved_authored_speed(monkeypatch)
    saved = variant["user_timeline"]["slots"][0]
    monkeypatch.setattr(
        settings,
        "phone_render_verified_features",
        ["basicComposition", "local1080Export", "audioMix"],
    )

    stale_slot = gj.TimelineSlotEdit(slot_id=saved["slot_id"], clip_index=1, in_s=2, duration_s=3)
    resolved = resolve_authored_phone_slots(job, variant, [stale_slot])
    assert (resolved[0]["source_end_s"], resolved[0]["playback_rate"]) == (8, 2)
    before = copy.deepcopy(job.assembly_plan)

    # The old app cannot render variable speed after its capability closes, so
    # the real route rejects rather than accepting the stale partial Save and
    # silently resetting its selected window to 1x.
    with pytest.raises(HTTPException) as exc:
        gj.prepare_editor_commit(
            job,
            "original_text",
            gj.EditorCommitRequest(
                base_generation=variant["render_generation_id"],
                editor_state_version=1,
                timeline_slots=[stale_slot],
            ),
        )

    assert exc.value.status_code == 422
    assert job.assembly_plan == before


def test_authored_explicit_speed_reset_stays_blocked_when_variable_speed_closes(monkeypatch):
    """Explicit null is a reset request, so the Save guard rejects it atomically."""
    job, variant = _saved_authored_speed(monkeypatch)
    saved = variant["user_timeline"]["slots"][0]
    monkeypatch.setattr(
        settings,
        "phone_render_verified_features",
        ["basicComposition", "local1080Export", "audioMix"],
    )
    reset_slot = gj.TimelineSlotEdit(
        slot_id=saved["slot_id"], clip_index=1, in_s=2, duration_s=3, playback_rate=None
    )
    reset = resolve_authored_phone_slots(job, variant, [reset_slot])
    assert (reset[0]["playback_rate"], reset[0]["source_end_s"]) == (1, None)
    before = copy.deepcopy(job.assembly_plan)

    with pytest.raises(HTTPException) as exc:
        gj.prepare_editor_commit(
            job,
            "original_text",
            gj.EditorCommitRequest(
                base_generation=variant["render_generation_id"],
                editor_state_version=1,
                timeline_slots=[reset_slot],
            ),
        )

    assert exc.value.status_code == 422
    assert job.assembly_plan == before


@pytest.mark.parametrize(
    ("rate", "duration"),
    [(2.0, 3.0), (0.5, 12.0)],
)
def test_authored_save_compiles_exact_source_window_at_each_speed(monkeypatch, rate, duration):
    job, recipe = _save_phone_slot(
        monkeypatch,
        gj.TimelineSlotEdit(
            clip_index=1, in_s=2, source_end_s=8, duration_s=duration, playback_rate=rate
        ),
    )
    saved = job.assembly_plan["variants"][0]["user_timeline"]["slots"][0]
    clip = recipe.tracks[0].clips[0]
    assert saved["source_end_s"] == 8
    assert saved["duration_s"] == duration
    assert (clip.source_start, clip.source_duration, clip.rate) == (2, 6, rate)


@pytest.mark.parametrize("rate", [0.25, 0.5, 2.0, 4.0])
def test_authored_source_endpoint_survives_repeated_speed_roundtrips(rate):
    job, variant, _ = fixture()
    first = resolve_authored_phone_slots(
        job,
        variant,
        [
            gj.TimelineSlotEdit(
                slot_id="added",
                clip_index=1,
                in_s=2,
                source_end_s=8,
                duration_s=6 / rate,
                playback_rate=rate,
            )
        ],
    )
    variant["user_timeline"]["slots"] = first
    reopened = resolve_authored_phone_slots(
        job,
        variant,
        [
            gj.TimelineSlotEdit(
                slot_id="added",
                clip_index=1,
                in_s=2,
                duration_s=first[0]["duration_s"],
                playback_rate=rate,
            )
        ],
    )
    assert reopened[0]["source_end_s"] == 8
    assert reopened[0]["duration_s"] == pytest.approx(first[0]["duration_s"])


def test_authored_trim_omitting_endpoint_does_not_restore_old_window():
    job, variant, _ = fixture()
    variant["user_timeline"]["slots"][0].update(playback_rate=2, source_end_s=8, duration_s=3)
    trimmed = resolve_authored_phone_slots(
        job,
        variant,
        [
            gj.TimelineSlotEdit(
                slot_id="added", clip_index=1, in_s=2, duration_s=2.5, playback_rate=2
            )
        ],
    )
    assert trimmed[0]["source_end_s"] is None


def test_authored_legacy_rate_only_window_keeps_its_existing_contract():
    job, variant, previous = fixture()
    saved = resolve_authored_phone_slots(
        job,
        variant,
        [gj.TimelineSlotEdit(slot_id="added", clip_index=1, in_s=2, duration_s=3, playback_rate=2)],
    )
    variant["user_timeline"]["slots"] = saved
    recipe = compile_phone_authored_timeline(job, variant, previous)
    assert saved[0]["source_end_s"] is None
    clip = recipe.tracks[0].clips[0]
    assert (clip.source_start, clip.source_duration, clip.rate) == (2, 6, 2)


def test_authored_photo_speed_is_rejected_before_recipe_compile():
    job, variant, _ = fixture()
    variant[EDITOR_SOURCES_FIELD]["sources"][0]["kind"] = "image"
    with pytest.raises(HTTPException, match="phone_edit_unsupported"):
        resolve_authored_phone_slots(
            job,
            variant,
            [gj.TimelineSlotEdit(clip_index=1, in_s=0, duration_s=1, playback_rate=2)],
        )
