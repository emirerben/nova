"""Backend Save JSON is the native hydration fixture, including null endpoints."""

import json
from pathlib import Path

import pytest

from app.config import settings
from app.routes.generative_jobs import EditorCommitRequest, TimelineSlotEdit
from app.services.authored_editor import prepare_authored_editor_commit
from app.services.device_render import device_status
from app.services.editor_empty_drafts import editor_sections, prepare_saved_render_windows
from app.services.phone_editor_sources import EDITOR_SOURCES_FIELD
from tests.routes.test_authored_playback_speed import _authored_phone_job

FIXTURE = Path(__file__).resolve().parents[3] / "ios/Tests/Fixtures/saved_editor_legacy_wire.json"


def saved_wire(monkeypatch):
    job, _ = _authored_phone_job(monkeypatch)
    variant = job.assembly_plan["variants"][0]
    monkeypatch.setattr(
        settings,
        "phone_render_verified_features",
        [*settings.phone_render_verified_features, "crossfade"],
    )
    source = variant[EDITOR_SOURCES_FIELD]["sources"][0]
    source["duration_s"] = 1.92
    source["source_binding"]["original"]["duration_s"] = 1.92
    rows = [
        dict(
            slot_id="first",
            clip_index=0,
            in_s=0,
            duration_s=1.91,
            transition_after="crossfade",
            transition_duration_s=0.157,
        ),
        dict(slot_id="second", clip_index=1, in_s=0, duration_s=58 / 30),
    ]
    variant["user_timeline"]["slots"] = rows
    variant["text_elements"] = [
        dict(id="saved-title", text="Saved title", start_s=1.753, end_s=2.417)
    ]
    prep = prepare_authored_editor_commit(
        job,
        "original_text",
        EditorCommitRequest(
            base_generation="first",
            editor_state_version=2,
            timeline_slots=[TimelineSlotEdit.model_validate(row) for row in rows],
        ),
        validation_arguments={},
    )
    prepare_saved_render_windows(job, "original_text", prep)
    assert "render_recovery" not in prep
    recipe = device_status(job, "original_text").request.recipe
    first, tail = next(track for track in recipe.tracks if track.kind == "video").clips
    assert first.source_duration == pytest.approx(1.91)
    assert tail.timeline_start == pytest.approx(1.753)
    assert tail.transition.duration == pytest.approx(0.157)
    assert tail.source_duration == pytest.approx(1.92)
    assert tail.rate == 1
    assert tail.hold_duration == pytest.approx(58 / 30 - 1.92)
    assert recipe.duration == pytest.approx(1.753 + 58 / 30)
    assert recipe.text_layers[0].start == pytest.approx(1.753)
    assert recipe.text_layers[0].end == pytest.approx(2.417)
    return {
        "editor_payload": {
            "editor_state_version": 2,
            "sections": editor_sections(job, "original_text"),
        }
    }


def test_backend_save_serialization_matches_native_reopen_fixture(monkeypatch):
    wire = json.loads(json.dumps(saved_wire(monkeypatch)))
    assert wire == json.loads(FIXTURE.read_text())
    row = wire["editor_payload"]["sections"]["timeline_slots"][-1]
    assert "source_end_s" in row and row["source_end_s"] is None
    assert row["duration_s"] == 58 / 30
