"""KRI-1032: the iPhone editor opens a phone montage as the render it shows, not as the approval.

The creator's saved/shared copy of Plaza de Mayo M1 ("maximo 3", job 97d55f9a) had every clip
audible, a centre crop instead of the render's bars, and its labels at y 0.78 in the approved
plan's font while the device render (the same edit) had one audible clip, bars, and Montserrat
labels at y 0.68. The editor compiles its preview and that copy from what these two reads serve:
the status variant (`text_elements`, `editor_capabilities`) and the timeline (`slots`).

Ways this can fail, written before the fix:
1. The status route walks the PUBLIC assembly plan, which strips `_phone_editor_plan_v1`, so the
   guided revision falls back to `guided_story_execution_plan`: text before the phone text
   policy and the face placement (y 0.78, "Alte Haas Grotesk").
2. The initial guided revision (`guided_editor_revision_from_approval`) drops each moment's
   `source_audio_muted` (KRI-942 "mute the rest"), so no slot is `muted` and the preview plays
   the camera sound of every clip; a first Save then compiles the same loss.
3. It drops the moments' `source_crop`, so a cropped render previews uncropped.
4. The bars/crop capability reports a value other than the variant's `landscape_fit`, or a reason
   the app treats as "this format ignores bars" (`unsupported_archetype`, `cloud_unsupported`).
5. A revision Save compiles a runtime plan whose moments lose the mutes.

The fixture is the run's real assembly plan (admin debug view after publication) with the
private editor plan rebuilt from the pinned rows the way `_run_phone_guided_job` writes it.
"""

from __future__ import annotations

import copy
import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

import app.routes.generative_jobs as gj
from app.services.client_protocol import set_client_protocol

_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "kri1032_plaza_m1_montage_job.json"
# What the app treats as "this format ignores the bars/crop choice" (NativeEditorDocument.swift).
_FIT_IGNORED_REASONS = {"unsupported_archetype", "cloud_unsupported"}
# Recoleta: the one clip the creator kept audible.
_AUDIBLE_MOMENT = "unified-cut-5"
_TEXT_FIELDS = ("id", "text", "x_frac", "y_frac", "font_family", "size_px", "color", "alignment")


@pytest.fixture
def plaza_job(monkeypatch):
    monkeypatch.setattr(gj.settings, "guided_story_editor_v2_enabled", True)
    set_client_protocol(23)  # TestFlight build 1784, the build that made the copy
    raw = json.loads(_FIXTURE.read_text())
    job = SimpleNamespace(
        id=uuid.UUID(raw["job_id"]),
        user_id=uuid.uuid4(),
        status=raw["status"],
        current_phase=None,
        content_plan_item_id=None,
        mode=raw["mode"],
        all_candidates=copy.deepcopy(raw["all_candidates"]),
        assembly_plan=copy.deepcopy(raw["assembly_plan"]),
    )
    yield job
    set_client_protocol(None)


def _pinned(job) -> dict:
    return job.assembly_plan["variants"][0]


def _status(job) -> dict:
    (variant,) = gj._variants_for_response(job)
    return variant


def _timeline(job) -> dict:
    return gj.dispatch_get_timeline(
        job, "guided_story", sign_url=lambda path, _ttl: f"https://signed.invalid/{path}"
    )


def _text(rows: list[dict]) -> list[tuple]:
    return sorted(tuple(row.get(field) for field in _TEXT_FIELDS) for row in rows)


def test_fixture_is_the_render_the_creator_approved(plaza_job):
    pinned = _pinned(plaza_job)
    approved = plaza_job.assembly_plan["guided_story_execution_plan"]
    assert pinned["landscape_fit"] == "fit"
    assert [
        m["moment_id"] for m in pinned["story_timeline"] if not m.get("source_audio_muted")
    ] == [_AUDIBLE_MOMENT]
    # The approval differs from the render exactly where the creator's copy did.
    assert {row["y_frac"] for row in approved["text_elements"]} == {0.16, 0.78}
    assert {row["y_frac"] for row in pinned["text_elements"]} == {0.14448, 0.68177}
    assert not any(m.get("source_audio_muted") for m in approved["story_timeline"])


def test_editor_text_is_the_rendered_text(plaza_job):
    served = _status(plaza_job)["text_elements"]
    assert _text(served) == _text(_pinned(plaza_job)["text_elements"])


def test_editor_slots_keep_the_rendered_clip_sound(plaza_job):
    slots = {slot["slot_id"]: slot for slot in _timeline(plaza_job)["slots"]}
    expected = {
        moment["moment_id"]: bool(moment.get("source_audio_muted"))
        for moment in _pinned(plaza_job)["story_timeline"]
    }
    assert {slot_id: bool(slot.get("muted")) for slot_id, slot in slots.items()} == expected
    assert slots[_AUDIBLE_MOMENT].get("muted") is None


def test_editor_letterboxes_like_the_render(plaza_job):
    fit = _status(plaza_job)["editor_capabilities"]["landscape_fit"]
    assert fit["value"] == _pinned(plaza_job)["landscape_fit"] == "fit"
    assert fit.get("reason") not in _FIT_IGNORED_REASONS


def test_unedited_save_keeps_the_mutes(plaza_job):
    from app.pipeline.guided_story import compile_guided_runtime_plan
    from app.services.phone_editor_sources import editor_sources_for_variant

    variant = _pinned(plaza_job)
    revision = gj._guided_v2_revision(plaza_job, variant)
    runtime = compile_guided_runtime_plan(
        variant["_phone_editor_plan_v1"],
        plaza_job.assembly_plan["guided_edit"],
        revision,
        admitted_sources=editor_sources_for_variant(variant),
    )
    assert [
        m["moment_id"] for m in runtime["story_timeline"] if not m.get("source_audio_muted")
    ] == [_AUDIBLE_MOMENT]


def test_initial_revision_keeps_a_rendered_crop(plaza_job):
    crop = {"x": 0.2, "y": 0.0, "width": 0.6, "height": 1.0}
    _pinned(plaza_job)["_phone_editor_plan_v1"]["story_timeline"][1]["source_crop"] = crop
    segments = gj._guided_v2_revision(plaza_job, _pinned(plaza_job))["segments"]
    assert segments[1]["source_crop"] == crop
    assert all("source_crop" not in s for i, s in enumerate(segments) if i != 1)


# ── The native half: the editor document the iPhone receives for this video ──────────────
# `KRI1032MontageCopyParityE2ETests` loads these exact reads into the real editor with
# generated local originals of the same shapes, builds the preview and the saved copy, and
# checks them against the render. No URLs, storage paths or media ids leave the server here.
# Regenerate after an intended server change, then re-run the native test:
#     KRIA_REGENERATE_FIXTURES=1 pytest tests/routes/test_kri1032_editor_matches_render.py
IOS_FIXTURE = Path(__file__).resolve().parents[3] / "ios/Tests/Fixtures/KRI1032PlazaM1Editor.json"
_IOS_VARIANT_KEYS = (
    "variant_id",
    "render_generation_id",
    "render_status",
    "render_destination",
    "resolved_archetype",
    "duration_s",
    "edit_duration_s",
    "orientation",
    "landscape_fit",
    "source_audio_preserved",
    "music_playback_mode",
    "text_elements",
    "editor_capabilities",
)
_IOS_SLOT_DROP = ("source_gcs_path",)


def _ios_fixture(job) -> dict:
    status = _status(job)
    timeline = _timeline(job)
    approved = job.assembly_plan["guided_edit"]["approved_proposal"]
    media = {row["media_id"]: row for row in approved["media"]}
    sources = [
        {
            "clip_index": clip["clip_index"],
            "kind": clip["kind"],
            "duration_s": clip["duration_s"],
            # width / height of the creator's original (the editor measures the file itself)
            "aspect": media[clip["media_id"]]["aspect"],
        }
        for clip in timeline["clips"]
    ]
    pinned = _pinned(job)
    return {
        "_generator": "src/apps/api/tests/routes/test_kri1032_editor_matches_render.py",
        "variant": {key: status[key] for key in _IOS_VARIANT_KEYS if key in status},
        "slots": [
            {key: value for key, value in slot.items() if key not in _IOS_SLOT_DROP}
            for slot in timeline["slots"]
        ],
        "sources": sources,
        # What the device render played (the receipts' truth): the native test holds the
        # preview and the saved copy to these.
        "render": {
            "landscape_fit": pinned["landscape_fit"],
            "muted_slots": [
                m["moment_id"] for m in pinned["story_timeline"] if m.get("source_audio_muted")
            ],
            "text": [
                {"id": row["id"], "x_frac": row["x_frac"], "y_frac": row["y_frac"]}
                for row in pinned["text_elements"]
            ],
        },
    }


def test_ios_fixture_is_what_the_server_serves(plaza_job):
    import os

    built = json.loads(json.dumps(_ios_fixture(plaza_job)))
    if os.environ.get("KRIA_REGENERATE_FIXTURES") == "1":
        IOS_FIXTURE.write_text(json.dumps(built, indent=1, sort_keys=True) + "\n")
    assert IOS_FIXTURE.exists(), f"missing {IOS_FIXTURE}: regenerate (KRIA_REGENERATE_FIXTURES=1)"
    assert json.loads(IOS_FIXTURE.read_text()) == built, (
        "the server's editor reads changed: KRIA_REGENERATE_FIXTURES=1 and re-run "
        "KRI1032MontageCopyParityE2ETests"
    )
