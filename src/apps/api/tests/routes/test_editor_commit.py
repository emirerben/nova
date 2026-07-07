"""E2 transactional editor commit + E4 editor_capabilities.

Dispatch-level tests (house style of test_generative_timeline.py: fake
SimpleNamespace jobs, no DB) for the validate-all-first / baseline-conflict /
atomic-stage / one-kick contract, plus endpoint-level tests for the plan-items
route (mock-DB pattern of test_text_elements_route.py) covering the title
section and the single-commit + kick-after-commit flow.
"""

from __future__ import annotations

import copy
import types
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import app.routes.generative_jobs as gj
from app.auth import get_current_user
from app.database import get_db
from app.main import app

REGEN = "app.tasks.generative_build.regenerate_generative_variant"
REBURN_NARRATED = "app.tasks.generative_build.reburn_narrated_captions"

GRID = [0.0, 0.5, 1.1, 1.6, 2.4, 3.0, 3.8]

_VALID_ELEMENT = {
    "id": "abc123",
    "text": "Hello world",
    "start_s": 0.0,
    "end_s": 2.0,
    "role": "generative_intro",
    "position": "middle",
}


def _ai_slots(prefix: str) -> list[dict]:
    return [
        {
            "slot_id": "s1",
            "clip_index": 0,
            "source_gcs_path": f"{prefix}clip_0.mp4",
            "source_duration_s": 10.0,
            "in_s": 0.0,
            "duration_s": 1.1,
            "duration_beats": 2,
            "order": 0,
            "moment_energy": 0.8,
            "moment_description": "crowd wave",
        },
        {
            "slot_id": "s2",
            "clip_index": 1,
            "source_gcs_path": f"{prefix}clip_1.mp4",
            "source_duration_s": 10.0,
            "in_s": 1.0,
            "duration_s": 1.9,
            "duration_beats": 3,
            "order": 1,
            "moment_energy": None,
            "moment_description": None,
        },
    ]


def _job(**variant_extra):
    """Fake Job with one editor-eligible montage variant (song_text)."""
    jid = uuid.uuid4()
    prefix = f"generative-jobs/{jid}/sources/"
    variant = {
        "variant_id": "song_text",
        "text_mode": "agent_text",
        "music_track_id": "t1",
        "render_status": "ready",
        "render_finished_at": "2026-07-01T00:00:00Z",
        "video_path": f"generative-jobs/{jid}/variant.mp4",
        "output_url": "https://signed/variant.mp4",
        "base_video_path": f"generative-jobs/{jid}/base_1.mp4",
        "ai_timeline": {"beat_grid": list(GRID), "slots": _ai_slots(prefix)},
        "mix": 0.5,  # lets the mix section validate on this fixture
        **variant_extra,
    }
    return types.SimpleNamespace(
        id=jid,
        assembly_plan={"variants": [variant]},
        all_candidates={"clip_paths": [f"slot-uploads/u/clip_{i}.mp4" for i in range(3)]},
        status="variants_ready",
        mode="content_plan",
    )


def _arm(monkeypatch, *, object_exists=True):
    from app.config import settings

    monkeypatch.setattr(settings, "GENERATIVE_TIMELINE_EDITOR_ENABLED", True)
    monkeypatch.setattr(settings, "sound_effects_enabled", True, raising=False)
    monkeypatch.setattr(settings, "media_overlays_enabled", True, raising=False)
    monkeypatch.setattr(gj.storage, "object_exists", lambda p: object_exists)
    # _variants_for_response re-signs base_video_path on read — keep it local.
    monkeypatch.setattr(gj, "signed_get_url", lambda p, ttl=None: f"https://signed/{p}")


def _slot_edits() -> list[gj.TimelineSlotEdit]:
    return [
        gj.TimelineSlotEdit(slot_id="s1", clip_index=0, in_s=0.0, duration_beats=2),
        gj.TimelineSlotEdit(slot_id="s2", clip_index=1, in_s=1.0, duration_beats=3),
    ]


def _commit_req(**kw) -> gj.EditorCommitRequest:
    kw.setdefault("base_generation", "2026-07-01T00:00:00Z")
    return gj.EditorCommitRequest(**kw)


# ── happy path: all sections stage atomically + exactly one render kick ────────


def test_happy_path_persists_all_sections_and_kicks_once(monkeypatch):
    _arm(monkeypatch)
    job = _job()
    req = _commit_req(
        text_elements=[dict(_VALID_ELEMENT)],
        timeline_slots=_slot_edits(),
        mix=gj.EditorCommitMix(music_level=0.2, original_level=0.8),
    )

    prep = gj.prepare_editor_commit(job, "song_text", req)

    v = job.assembly_plan["variants"][0]
    # All sections landed in ONE staged variant update.
    assert v["text_elements"][0]["text"] == "Hello world"
    assert v["text_elements_user_edited"] is True
    assert [s["slot_id"] for s in v["user_timeline"]["slots"]] == ["s1", "s2"]
    assert v["mix"] == 0.2
    assert v["original_audio_level"] == 0.8
    # Generation bumped + variant flipped to rendering.
    assert v["render_generation_id"] == prep["generation"]
    assert v["render_status"] == "rendering"
    assert prep["has_render_section"] is True
    assert prep["sections"] == {
        "text_elements": True,
        "caption_cues": False,
        "timeline": True,
        "mix": True,
        "sound_effects": False,
        "media_overlays": False,
    }

    # Exactly ONE render kick, carrying the new token + the timeline override.
    calls: list[dict] = []
    monkeypatch.setattr(
        "app.tasks.generative_build.regenerate_generative_variant",
        types.SimpleNamespace(apply_async=lambda **k: calls.append(k)),
        raising=False,
    )
    gj.enqueue_editor_commit_render(str(job.id), "song_text", prep)
    assert len(calls) == 1
    kwargs = calls[0]["kwargs"]
    assert kwargs["render_gen_id"] == prep["generation"]
    assert [s["slot_id"] for s in kwargs["timeline_override"]] == ["s1", "s2"]
    assert kwargs["mix_override"] == 0.2
    # Timeline present → full re-assembly → default queue (no overlay-jobs pin).
    assert "queue" not in calls[0]


def test_sequence_text_commit_serializes_saved_elements_not_projection(monkeypatch):
    """Regression: sequence scene_timings may still be present on read, but saved
    text_elements must round-trip as the authored list after editor-commit.
    """
    _arm(monkeypatch)
    saved = {
        **_VALID_ELEMENT,
        "id": "qa-live",
        "text": "QA live text",
        "position": "custom",
        "x_frac": 0.5,
        "y_frac": 0.4,
    }
    job = _job(
        variant_id="original_text",
        intro_mode="sequence",
        scenes=[
            {"text": "Original scene one", "start_s": 0.0, "end_s": 1.0},
            {"text": "Original scene two", "start_s": 1.0, "end_s": 2.0},
        ],
    )

    gj.prepare_editor_commit(
        job,
        "original_text",
        _commit_req(text_elements=[saved]),
    )

    out = gj._variants_for_response(job)[0]
    assert out["scene_timings"] == [
        {"text": "Original scene one", "start_s": 0.0, "end_s": 1.0},
        {"text": "Original scene two", "start_s": 1.0, "end_s": 2.0},
    ]
    assert out["text_elements_user_edited"] is True
    assert [e["text"] for e in out["text_elements"]] == ["QA live text"]
    assert out["text_elements"][0]["id"] == "qa-live"


def test_narrated_caption_commit_persists_cues_and_reburns_caption_task(monkeypatch):
    _arm(monkeypatch)
    job = _job(
        variant_id="narrated",
        text_mode="none",
        resolved_archetype="narrated",
        base_video_path="generative-jobs/job/base-captionless.mp4",
        caption_cues=[
            {"text": "Old line", "start_s": 0.0, "end_s": 1.0},
        ],
        mix=None,
    )
    cues = [
        {"text": "Updated voice line", "start_s": 0.2, "end_s": 1.4},
        {"text": "Second cue", "start_s": 1.5, "end_s": 2.2},
    ]

    prep = gj.prepare_editor_commit(job, "narrated", _commit_req(caption_cues=cues))

    v = job.assembly_plan["variants"][0]
    assert v["caption_cues"] == cues
    assert v["render_generation_id"] == prep["generation"]
    assert v["render_status"] == "rendering"
    assert prep["sections"] == {
        "text_elements": False,
        "caption_cues": True,
        "timeline": False,
        "mix": False,
        "sound_effects": False,
        "media_overlays": False,
    }

    calls: list[dict] = []
    monkeypatch.setattr(
        REBURN_NARRATED,
        types.SimpleNamespace(apply_async=lambda **k: calls.append(k)),
        raising=False,
    )
    gj.enqueue_editor_commit_render(str(job.id), "narrated", prep)
    assert calls == [{"args": [str(job.id), "narrated"]}]


def test_text_only_commit_rides_overlay_jobs_queue(monkeypatch):
    _arm(monkeypatch)
    job = _job()
    prep = gj.prepare_editor_commit(
        job, "song_text", _commit_req(text_elements=[dict(_VALID_ELEMENT)])
    )
    calls: list[dict] = []
    monkeypatch.setattr(
        "app.tasks.generative_build.regenerate_generative_variant",
        types.SimpleNamespace(apply_async=lambda **k: calls.append(k)),
        raising=False,
    )
    gj.enqueue_editor_commit_render(str(job.id), "song_text", prep)
    assert len(calls) == 1
    assert calls[0]["queue"] == "overlay-jobs"


def test_sfx_only_commit_persists_and_kicks_sfx_pass(monkeypatch):
    _arm(monkeypatch)
    job = _job()
    sfx = [
        {
            "id": "sfx-1",
            "sound_effect_id": None,
            "src_gcs_path": "users/u123/plan/item/sfx/pop.mp3",
            "at_s": 1.2,
            "gain": 0.8,
            "duration_s": 0.6,
            "label": "Pop",
        }
    ]

    prep = gj.prepare_editor_commit(
        job,
        "song_text",
        _commit_req(sound_effects=sfx),
        user_id="u123",
    )

    v = job.assembly_plan["variants"][0]
    assert v["sound_effects"][0]["id"] == "sfx-1"
    assert v["sound_effects"][0]["src_gcs_path"] == "users/u123/plan/item/sfx/pop.mp3"
    assert v["sound_effects"][0]["at_s"] == 1.2
    assert v["sound_effects"][0]["gain"] == 0.8
    assert v["sound_effects"][0]["duration_s"] == 0.6
    assert v["render_generation_id"] == prep["generation"]

    calls: list[dict] = []
    monkeypatch.setattr(
        "app.tasks.generative_build.regenerate_generative_variant",
        types.SimpleNamespace(apply_async=lambda **k: calls.append(k)),
        raising=False,
    )
    gj.enqueue_editor_commit_render(str(job.id), "song_text", prep)
    assert len(calls) == 1
    assert calls[0]["queue"] == "overlay-jobs"
    assert calls[0]["kwargs"]["render_gen_id"] == prep["generation"]
    assert calls[0]["kwargs"]["sfx_override"][0]["id"] == "sfx-1"
    assert calls[0]["kwargs"]["sfx_override"][0]["src_gcs_path"] == (
        "users/u123/plan/item/sfx/pop.mp3"
    )


def test_overlay_only_commit_persists_and_kicks_overlay_pass(monkeypatch):
    _arm(monkeypatch)
    job = _job()
    overlays = [
        {
            "id": "ov-1",
            "kind": "image",
            "src_gcs_path": "users/u123/plan/item/overlays/card.png",
            "position": "center",
            "x_frac": 0.5,
            "y_frac": 0.5,
            "scale": 0.35,
            "start_s": 0.4,
            "end_s": 2.4,
            "z": 0,
        }
    ]

    prep = gj.prepare_editor_commit(
        job,
        "song_text",
        _commit_req(media_overlays=overlays),
        user_id="u123",
    )

    v = job.assembly_plan["variants"][0]
    assert v["media_overlays"][0]["id"] == "ov-1"
    assert v["media_overlays"][0]["src_gcs_path"] == "users/u123/plan/item/overlays/card.png"
    assert v["media_overlays"][0]["start_s"] == 0.4
    assert v["media_overlays"][0]["end_s"] == 2.4

    calls: list[dict] = []
    monkeypatch.setattr(
        "app.tasks.generative_build.regenerate_generative_variant",
        types.SimpleNamespace(apply_async=lambda **k: calls.append(k)),
        raising=False,
    )
    gj.enqueue_editor_commit_render(str(job.id), "song_text", prep)
    assert len(calls) == 1
    assert calls[0]["queue"] == "overlay-jobs"
    assert calls[0]["kwargs"]["render_gen_id"] == prep["generation"]
    assert calls[0]["kwargs"]["media_overlays_override"][0]["id"] == "ov-1"
    assert calls[0]["kwargs"]["media_overlays_override"][0]["src_gcs_path"] == (
        "users/u123/plan/item/overlays/card.png"
    )


def test_overlay_editor_commit_enforces_fullscreen_overlap_contract(monkeypatch):
    _arm(monkeypatch)
    job = _job()
    before = copy.deepcopy(job.assembly_plan)
    overlays = [
        {
            "id": "fs-1",
            "kind": "image",
            "src_gcs_path": "users/u123/plan/item/overlays/full.png",
            "position": "center",
            "x_frac": 0.5,
            "y_frac": 0.5,
            "scale": 1.0,
            "start_s": 1.0,
            "end_s": 3.0,
            "z": 0,
            "display_mode": "fullscreen",
        },
        {
            "id": "pip-1",
            "kind": "image",
            "src_gcs_path": "users/u123/plan/item/overlays/card.png",
            "position": "center",
            "x_frac": 0.5,
            "y_frac": 0.5,
            "scale": 0.35,
            "start_s": 2.0,
            "end_s": 4.0,
            "z": 1,
        },
    ]

    with pytest.raises(HTTPException) as exc:
        gj.prepare_editor_commit(
            job,
            "song_text",
            _commit_req(media_overlays=overlays),
            user_id="u123",
        )

    assert exc.value.status_code == 422
    assert "overlap a full-screen moment" in str(exc.value.detail)
    assert job.assembly_plan == before


def test_sfx_and_overlay_commit_kicks_overlay_pass_for_terminal_sfx_reapply(monkeypatch):
    _arm(monkeypatch)
    job = _job()
    sfx = [
        {
            "id": "sfx-1",
            "sound_effect_id": None,
            "src_gcs_path": "users/u123/plan/item/sfx/pop.mp3",
            "at_s": 1.2,
            "gain": 0.8,
        }
    ]
    overlays = [
        {
            "id": "ov-1",
            "kind": "image",
            "src_gcs_path": "users/u123/plan/item/overlays/card.png",
            "position": "center",
            "x_frac": 0.5,
            "y_frac": 0.5,
            "scale": 0.35,
            "start_s": 0.4,
            "end_s": 2.4,
            "z": 0,
        }
    ]

    prep = gj.prepare_editor_commit(
        job,
        "song_text",
        _commit_req(sound_effects=sfx, media_overlays=overlays),
        user_id="u123",
    )

    calls: list[dict] = []
    monkeypatch.setattr(
        "app.tasks.generative_build.regenerate_generative_variant",
        types.SimpleNamespace(apply_async=lambda **k: calls.append(k)),
        raising=False,
    )
    gj.enqueue_editor_commit_render(str(job.id), "song_text", prep)
    assert calls[0]["kwargs"]["media_overlays_override"][0]["id"] == "ov-1"
    assert calls[0]["kwargs"]["media_overlays_override"][0]["src_gcs_path"] == (
        "users/u123/plan/item/overlays/card.png"
    )
    assert "sfx_override" not in calls[0]["kwargs"]


def test_commit_succeeds_while_variant_is_rendering(monkeypatch):
    """The whole point of E2: NO require_editable_variant — saving during an
    in-flight render supersedes it instead of 409ing."""
    _arm(monkeypatch)
    job = _job(render_status="rendering", render_generation_id="tok-old")
    req = _commit_req(
        text_elements=[dict(_VALID_ELEMENT)],
        base_generation="tok-old",  # baseline = current render_generation_id
    )
    prep = gj.prepare_editor_commit(job, "song_text", req)
    v = job.assembly_plan["variants"][0]
    assert v["render_generation_id"] == prep["generation"] != "tok-old"


# ── stale baseline → 409 baseline_conflict, nothing persisted ──────────────────


def test_stale_base_generation_409_nothing_persisted(monkeypatch):
    _arm(monkeypatch)
    job = _job()
    before = copy.deepcopy(job.assembly_plan)
    req = _commit_req(
        text_elements=[dict(_VALID_ELEMENT)],
        timeline_slots=_slot_edits(),
        base_generation="something-older",
    )
    with pytest.raises(HTTPException) as exc:
        gj.prepare_editor_commit(job, "song_text", req)
    assert exc.value.status_code == 409
    assert exc.value.detail == "baseline_conflict"
    assert job.assembly_plan == before


def test_baseline_prefers_render_generation_id_over_finished_at(monkeypatch):
    """Once a variant is token-stamped, render_finished_at no longer matches."""
    _arm(monkeypatch)
    job = _job(render_generation_id="tok-live")
    with pytest.raises(HTTPException) as exc:
        gj.prepare_editor_commit(
            job, "song_text", _commit_req(text_elements=[], base_generation="2026-07-01T00:00:00Z")
        )
    assert exc.value.status_code == 409
    # And the stamped token DOES pass.
    prep = gj.prepare_editor_commit(
        job, "song_text", _commit_req(text_elements=[], base_generation="tok-live")
    )
    assert prep["generation"]


# ── invalid section → 422, nothing persisted ───────────────────────────────────


def test_invalid_timeline_section_422_nothing_persisted(monkeypatch):
    """Valid text section + invalid timeline section must persist NEITHER."""
    _arm(monkeypatch)
    job = _job()
    before = copy.deepcopy(job.assembly_plan)
    bad_slots = [gj.TimelineSlotEdit(slot_id="s1", clip_index=99, in_s=0.0, duration_s=1.0)]
    req = _commit_req(text_elements=[dict(_VALID_ELEMENT)], timeline_slots=bad_slots)
    with pytest.raises(HTTPException) as exc:
        gj.prepare_editor_commit(job, "song_text", req)
    assert exc.value.status_code == 422
    assert exc.value.detail == {"code": "TIMELINE_UNKNOWN_CLIP"}
    assert job.assembly_plan == before


def test_song_timeline_seconds_snap_to_beat_grid(monkeypatch):
    _arm(monkeypatch)
    job = _job()
    slots = [
        gj.TimelineSlotEdit(slot_id="s1", clip_index=0, in_s=0.0, duration_s=0.9),
        gj.TimelineSlotEdit(slot_id="s2", clip_index=1, in_s=1.0, duration_s=1.4),
    ]

    resolved = gj.resolve_timeline_slots_for_edit(job, job.assembly_plan["variants"][0], slots)

    assert resolved[0]["duration_beats"] == 2
    assert resolved[0]["duration_s"] == 1.1
    assert resolved[1]["duration_beats"] == 2
    assert resolved[1]["duration_s"] == 1.3


def test_no_music_timeline_seconds_snap_to_half_second(monkeypatch):
    _arm(monkeypatch)
    job = _job(
        variant_id="original_text",
        music_track_id=None,
    )
    prefix = f"generative-jobs/{job.id}/sources/"
    job.assembly_plan["variants"][0]["ai_timeline"] = {
        "beat_grid": [],
        "slots": [{**_ai_slots(prefix)[0], "duration_s": 1.0, "duration_beats": None}],
    }
    slots = [
        gj.TimelineSlotEdit(slot_id="s1", clip_index=0, in_s=0.0, duration_s=1.24),
    ]

    resolved = gj.resolve_timeline_slots_for_edit(job, job.assembly_plan["variants"][0], slots)

    assert resolved[0]["duration_beats"] is None
    assert resolved[0]["duration_s"] == 1.0


def test_invalid_text_section_422_nothing_persisted(monkeypatch):
    """end_s <= start_s in the text section fails the whole commit."""
    _arm(monkeypatch)
    job = _job()
    before = copy.deepcopy(job.assembly_plan)
    bad_element = {**_VALID_ELEMENT, "start_s": 2.0, "end_s": 1.0}
    req = _commit_req(text_elements=[bad_element], timeline_slots=_slot_edits())
    with pytest.raises(HTTPException) as exc:
        gj.prepare_editor_commit(job, "song_text", req)
    assert exc.value.status_code == 422
    assert job.assembly_plan == before


def test_editor_commit_strictly_rejects_dropped_text_element(monkeypatch):
    """editor-commit must 422 when TextElement coercion would drop user text."""
    _arm(monkeypatch)
    job = _job()
    before = copy.deepcopy(job.assembly_plan)
    bad_element = {**_VALID_ELEMENT, "id": "bad-font", "font_family": "illegal font"}

    with pytest.raises(HTTPException) as exc:
        gj.prepare_editor_commit(
            job,
            "song_text",
            _commit_req(text_elements=[bad_element], timeline_slots=_slot_edits()),
        )

    assert exc.value.status_code == 422
    assert "bad-font" in str(exc.value.detail)
    assert "font_family" in str(exc.value.detail)
    assert "'illegal font'" in str(exc.value.detail)
    assert job.assembly_plan == before


@pytest.mark.parametrize(
    ("name", "variant"),
    [
        (
            "song_text_linear_karaoke",
            {
                "variant_id": "song_text",
                "text_mode": "agent_text",
                "music_track_id": "t1",
                "intro_text": "This changed my life",
                "intro_layout": "linear",
                "intro_effect": "karaoke-line",
                "intro_text_color": "#FFFFFF",
                "intro_text_size_px": 72,
                "duration_s": 12.0,
                "base_video_path": "generative-jobs/j/base.mp4",
            },
        ),
        (
            "song_text_registry_font",
            {
                "variant_id": "song_text",
                "text_mode": "agent_text",
                "music_track_id": "t1",
                "intro_text": "Registry font round trip",
                "intro_layout": "linear",
                "intro_effect": "fade-in",
                "intro_font_family": "Playfair Display",
                "duration_s": 12.0,
                "base_video_path": "generative-jobs/j/base.mp4",
            },
        ),
        (
            "original_text_sequence",
            {
                "variant_id": "original_text",
                "text_mode": "agent_text",
                "music_track_id": None,
                "intro_mode": "sequence",
                "intro_layout": "cluster",
                "sequence_base_size_px": 64,
                "scenes": [
                    {
                        "text": "First words here",
                        "words": ["First", "words", "here"],
                        "start_s": 0.0,
                        "end_s": 1.2,
                    },
                    {
                        "text": "Second words",
                        "words": ["Second", "words"],
                        "start_s": 1.2,
                        "end_s": 2.6,
                    },
                ],
                "duration_s": 12.0,
                "base_video_path": "generative-jobs/j/base.mp4",
            },
        ),
        (
            "subtitled_captions",
            {
                "variant_id": "subtitled",
                "text_mode": "agent_text",
                "caption_cues": [
                    {"text": "Caption one", "start_s": 0.0, "end_s": 1.0},
                    {"text": "Caption two", "start_s": 1.0, "end_s": 2.0},
                ],
                "voiceover_caption_font": "Playfair Display",
                "base_video_path": "generative-jobs/j/base.mp4",
            },
        ),
    ],
)
def test_projected_text_elements_strict_validate_for_editor_commit(name, variant):
    from app.agents._schemas.text_element import text_elements_for_variant

    projected = [e.model_dump() for e in text_elements_for_variant(variant)]
    assert projected, name

    validated, _materialized = gj.validate_text_elements_payload(
        variant,
        projected,
        require_base=False,
        strict_drop=True,
    )

    assert len(validated) == len(projected), name


def test_editor_commit_accepts_registry_font_names(monkeypatch):
    _arm(monkeypatch)
    job = _job()
    element = {**_VALID_ELEMENT, "font_family": "Playfair Display"}

    prep = gj.prepare_editor_commit(
        job,
        "song_text",
        _commit_req(text_elements=[element], timeline_slots=_slot_edits()),
    )

    assert prep["has_render_section"] is True
    assert job.assembly_plan["variants"][0]["text_elements"][0]["font_family"] == "Playfair Display"


def test_mix_on_variant_without_voice_bed_422(monkeypatch):
    _arm(monkeypatch)
    job = _job(mix=None)  # song variant with no voice bed
    with pytest.raises(HTTPException) as exc:
        gj.prepare_editor_commit(
            job, "song_text", _commit_req(mix=gj.EditorCommitMix(music_level=0.3))
        )
    assert exc.value.status_code == 422


def test_prod_song_text_empty_text_and_mixed_beat_slots_round_trip(monkeypatch):
    """Regression for prod item a170f347 (job 45a2bf6c).

    The actual rejected field was not text or timeline: it was the frontend
    adding mix.music_level=0.0 to this non-voiceover song_text variant. Keep the
    real text/timeline shape here so future fixes do not regress the round-trip.
    """
    _arm(monkeypatch)
    job_id = uuid.UUID("45a2bf6c-9b07-44c2-923d-6126c3dd7db1")
    prefix = f"generative-jobs/{job_id}/sources/"
    clips = [f"{prefix}{i:03d}_clip.mp4" for i in range(17)]
    raw_slots = [
        (0, 1.25, 0.5, 1, 4.705),
        (3, 0.0, 0.5, 1, 7.774),
        (1, 0.0, 0.939, 2, 3.513),
        (11, 0.0, 0.512, 1, 5.239),
        (13, 0.0, 0.5, None, 1.14),
        (2, 0.0, 0.5, 1, 3.637),
        (5, 0.0, 0.5, 1, 3.134),
        (15, 1.77, 0.5, 1, 15.882),
        (14, 0.01, 0.938, 2, 6.974),
        (6, 0.0, 2.859, 4, 4.067),
        (7, 1.0, 0.5, 1, 3.537),
        (10, 0.0, 1.967, None, 1.967),
        (16, 0.0, 0.5, 1, 4.133),
        (8, 2.2, 0.5, None, 5.644),
        (4, 0.0, 0.5, 1, 3.303),
        (12, 0.0, 0.5, 1, 6.206),
        (9, 0.0, 0.5, None, 29.667),
    ]
    slots = [
        {
            "slot_id": f"prod-slot-{order}",
            "clip_index": clip_index,
            "source_gcs_path": clips[clip_index],
            "source_duration_s": source_duration_s,
            "in_s": in_s,
            "duration_s": duration_s,
            "duration_beats": duration_beats,
            "order": order,
        }
        for order, (clip_index, in_s, duration_s, duration_beats, source_duration_s) in enumerate(
            raw_slots
        )
    ]
    variant = {
        "variant_id": "song_text",
        "text_mode": "agent_text",
        "intro_mode": "linear",
        "intro_text": "pov: you finally balance the cup",
        "intro_text_size_px": 58,
        "intro_highlight_word": "balance",
        "text_elements": [],
        "text_elements_user_edited": True,
        "base_video_path": f"generative-jobs/{job_id}/base_1_song_text.mp4",
        "music_track_id": "c1180879-592c-48b2-bdcc-6f14f48f1160",
        "track_title": "Waka Waka",
        "render_generation_id": "0398dae9402548b2aad878e80ee035f8",
        "render_finished_at": "2026-06-30T05:48:39.313921Z",
        "mix": None,
        "ai_timeline": {
            "slots": slots,
            "beat_grid": [
                0.34,
                0.809,
                1.278,
                1.748,
                2.26,
                2.729,
                3.177,
                3.646,
                4.116,
                4.585,
                5.054,
                5.566,
                6.036,
                7.913,
                8.084,
                8.382,
                8.852,
                9.321,
                9.812,
                10.132,
                10.75,
                11.241,
                11.689,
                12.158,
                12.649,
                13.097,
                13.566,
            ],
        },
    }
    job = types.SimpleNamespace(
        id=job_id,
        assembly_plan={"variants": [variant]},
        all_candidates={"clip_paths": clips},
        status="variants_ready",
        mode="content_plan",
    )
    posted_slots = [
        gj.TimelineSlotEdit(
            slot_id=s["slot_id"],
            clip_index=s["clip_index"],
            in_s=s["in_s"],
            duration_s=s["duration_s"],
            duration_beats=s["duration_beats"],
            removed=False,
        )
        for s in slots
    ]

    text, _materialized = gj.validate_text_elements_payload(
        variant,
        [],
        require_base=False,
        strict_drop=True,
    )
    resolved = gj.resolve_timeline_slots_for_edit(job, variant, posted_slots)

    assert text == []
    assert len(resolved) == 17
    assert [
        (idx, slot.duration_beats)
        for idx, slot in enumerate(posted_slots)
        if slot.duration_beats is None
    ] == [(4, None), (11, None), (13, None), (16, None)]

    prep = gj.prepare_editor_commit(
        job,
        "song_text",
        _commit_req(
            text_elements=[],
            timeline_slots=posted_slots,
            base_generation="0398dae9402548b2aad878e80ee035f8",
        ),
    )
    assert prep["sections"]["text_elements"] is True
    assert prep["sections"]["timeline"] is True

    with pytest.raises(HTTPException) as exc:
        gj.prepare_editor_commit(
            job,
            "song_text",
            _commit_req(
                text_elements=[],
                timeline_slots=posted_slots,
                mix=gj.EditorCommitMix(music_level=0.0),
                base_generation=prep["generation"],
            ),
        )
    assert exc.value.status_code == 422
    assert exc.value.detail == "This edit has no voiceover to mix."


def test_no_sections_422(monkeypatch):
    _arm(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        gj.prepare_editor_commit(_job(), "song_text", _commit_req())
    assert exc.value.status_code == 422


def test_unknown_variant_404(monkeypatch):
    _arm(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        gj.prepare_editor_commit(_job(), "nope", _commit_req(text_elements=[dict(_VALID_ELEMENT)]))
    assert exc.value.status_code == 404


def test_title_only_commit_kicks_no_render(monkeypatch):
    """A title-only commit persists nothing on the variant and enqueues nothing."""
    _arm(monkeypatch)
    job = _job()
    before = copy.deepcopy(job.assembly_plan)
    prep = gj.prepare_editor_commit(job, "song_text", _commit_req(title="New title"))
    assert prep["has_render_section"] is False
    assert prep["generation"] == "2026-07-01T00:00:00Z"  # baseline unchanged
    assert job.assembly_plan == before

    calls: list[dict] = []
    monkeypatch.setattr(
        "app.tasks.generative_build.regenerate_generative_variant",
        types.SimpleNamespace(apply_async=lambda **k: calls.append(k)),
        raising=False,
    )
    gj.enqueue_editor_commit_render(str(job.id), "song_text", prep)
    assert calls == []


# ── endpoint (plan-items surface): title in the same transaction ───────────────


def _user() -> MagicMock:
    u = MagicMock()
    u.id = uuid.uuid4()
    return u


def _result(value) -> MagicMock:
    r = MagicMock()
    r.scalar_one_or_none = MagicMock(return_value=value)
    return r


def _owned_item(user_id, *, job):
    item = MagicMock()
    item.id = uuid.uuid4()
    item.theme = "Old title"
    item.current_job = job
    item.current_job_id = job.id
    item.user_edited = False
    plan = MagicMock()
    plan.user_id = user_id
    return item, plan


def _db(execute_results: list, plan, job=None) -> AsyncMock:
    db = AsyncMock()
    db.commit = AsyncMock()
    db.execute = AsyncMock(side_effect=[_result(v) for v in execute_results])

    async def _get(model, _pk, **_kwargs):
        if model is gj.Job:
            return job
        return plan

    db.get = AsyncMock(side_effect=_get)
    return db


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def teardown_function() -> None:
    app.dependency_overrides.clear()


def test_endpoint_happy_path_title_and_text(client: TestClient, monkeypatch) -> None:
    _arm(monkeypatch)
    user = _user()
    job = _job()
    item, plan = _owned_item(user.id, job=job)
    db = _db([item], plan, job)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    with patch(REGEN) as regen:
        regen.apply_async = MagicMock()
        resp = client.post(
            f"/plan-items/{item.id}/variants/song_text/editor-commit",
            json={
                "text_elements": [dict(_VALID_ELEMENT)],
                "title": "Fresh title",
                "base_generation": "2026-07-01T00:00:00Z",
            },
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["sections"] == {
        "text_elements": True,
        "caption_cues": False,
        "timeline": False,
        "mix": False,
        "sound_effects": False,
        "media_overlays": False,
        "title": True,
    }
    v = job.assembly_plan["variants"][0]
    assert body["generation"] == v["render_generation_id"]
    assert item.theme == "Fresh title"
    assert item.user_edited is True
    db.commit.assert_awaited_once()  # ONE transaction for job-JSON + title
    db.get.assert_any_await(gj.Job, job.id, with_for_update=True)
    regen.apply_async.assert_called_once()


def test_endpoint_stale_baseline_409(client: TestClient, monkeypatch) -> None:
    _arm(monkeypatch)
    user = _user()
    job = _job()
    item, plan = _owned_item(user.id, job=job)
    db = _db([item], plan, job)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    with patch(REGEN) as regen:
        regen.apply_async = MagicMock()
        resp = client.post(
            f"/plan-items/{item.id}/variants/song_text/editor-commit",
            json={
                "text_elements": [dict(_VALID_ELEMENT)],
                "base_generation": "stale-token",
            },
        )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "baseline_conflict"
    db.commit.assert_not_awaited()
    regen.apply_async.assert_not_called()


def test_endpoint_empty_title_422_nothing_persisted(client: TestClient, monkeypatch) -> None:
    _arm(monkeypatch)
    user = _user()
    job = _job()
    before = copy.deepcopy(job.assembly_plan)
    item, plan = _owned_item(user.id, job=job)
    db = _db([item], plan, job)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    resp = client.post(
        f"/plan-items/{item.id}/variants/song_text/editor-commit",
        json={
            "text_elements": [dict(_VALID_ELEMENT)],
            "title": "   ",
            "base_generation": "2026-07-01T00:00:00Z",
        },
    )
    assert resp.status_code == 422
    assert job.assembly_plan == before
    assert item.theme == "Old title"
    db.commit.assert_not_awaited()


def test_sound_effects_flag_off_rejects_editor_section(monkeypatch):
    _arm(monkeypatch)
    from app.config import settings

    monkeypatch.setattr(settings, "sound_effects_enabled", False, raising=False)
    job = _job()
    before = copy.deepcopy(job.assembly_plan)

    with pytest.raises(HTTPException) as exc:
        gj.prepare_editor_commit(
            job,
            "song_text",
            _commit_req(
                sound_effects=[
                    {
                        "id": "sfx-1",
                        "src_gcs_path": "users/u123/plan/item/sfx/pop.mp3",
                        "at_s": 0.2,
                        "gain": 1,
                    }
                ]
            ),
            user_id="u123",
        )

    assert exc.value.status_code == 422
    assert "Sound effects are not available" in str(exc.value.detail)
    assert job.assembly_plan == before


def test_media_overlays_flag_off_rejects_editor_section(monkeypatch):
    _arm(monkeypatch)
    from app.config import settings

    monkeypatch.setattr(settings, "media_overlays_enabled", False, raising=False)
    job = _job()
    before = copy.deepcopy(job.assembly_plan)

    with pytest.raises(HTTPException) as exc:
        gj.prepare_editor_commit(
            job,
            "song_text",
            _commit_req(
                media_overlays=[
                    {
                        "id": "ov-1",
                        "kind": "image",
                        "src_gcs_path": "users/u123/plan/item/overlays/card.png",
                        "position": "center",
                        "x_frac": 0.5,
                        "y_frac": 0.5,
                        "scale": 0.35,
                        "start_s": 0,
                        "end_s": 2,
                        "z": 0,
                    }
                ]
            ),
            user_id="u123",
        )

    assert exc.value.status_code == 422
    assert "Media overlays are not available" in str(exc.value.detail)
    assert job.assembly_plan == before


# ── E4: editor_capabilities per archetype ──────────────────────────────────────


def _caps(job, variant_id: str) -> dict:
    v = next(v for v in gj._variants_for_response(job) if v.get("variant_id") == variant_id)
    return v["editor_capabilities"]


def test_capabilities_montage_song_text_all_on(monkeypatch):
    _arm(monkeypatch)
    caps = _caps(_job(), "song_text")
    assert caps == {
        "text_elements": True,
        "timeline": True,
        "split_clips": True,
        "mix": True,  # fixture carries mix=0.5
        "sfx": True,
        "overlays": True,
        # _arm leaves overlay_autoplace_enabled at its default (False).
        "suggestions": False,
        "reason": None,
        "sfx_reason": None,
        "overlays_reason": None,
        "suggestions_reason": "autoplace_disabled",
    }


def test_capabilities_lyrics_variant(monkeypatch):
    _arm(monkeypatch)
    job = _job(variant_id="song_lyrics", text_mode="lyrics", mix=None)
    caps = _caps(job, "song_lyrics")
    assert caps["text_elements"] is False
    assert caps["timeline"] is False
    assert caps["split_clips"] is False
    assert caps["mix"] is False
    assert caps["reason"] == "lyrics_sync"
    assert caps["sfx"] is True
    assert caps["overlays"] is True


def test_capabilities_voiceover_variant(monkeypatch):
    _arm(monkeypatch)
    job = _job(variant_id="voiceover_music", mix=None)
    caps = _caps(job, "voiceover_music")
    assert caps["timeline"] is False
    assert caps["split_clips"] is False
    assert caps["mix"] is True  # voiceover variants always mixable
    assert caps["reason"] == "voiceover_bed_fit"
    assert caps["sfx"] is True
    assert caps["overlays"] is True


def test_capabilities_talking_head_archetype(monkeypatch):
    _arm(monkeypatch)
    job = _job(variant_id="original_text", resolved_archetype="talking_head", mix=None)
    caps = _caps(job, "original_text")
    assert caps["timeline"] is False
    assert caps["reason"] == "no_slot_timeline"
    assert caps["text_elements"] is True  # text editing is independent of the slot grid
    assert caps["sfx"] is True
    assert caps["overlays"] is True


def test_capabilities_narrated_caption_archetype_text_elements_on(monkeypatch):
    _arm(monkeypatch)
    job = _job(
        variant_id="narrated",
        text_mode="none",
        resolved_archetype="narrated",
        base_video_path="generative-jobs/job/base-captionless.mp4",
        mix=None,
    )
    caps = _caps(job, "narrated")
    assert caps["text_elements"] is True
    assert caps["timeline"] is False
    assert caps["split_clips"] is False
    assert caps["reason"] == "locked_to_voiceover"
    assert caps["sfx"] is False
    assert caps["overlays"] is False
    assert caps["sfx_reason"] == "caption_archetype"
    assert caps["overlays_reason"] == "caption_archetype"


def test_capabilities_subtitled_caption_archetype_text_elements_off(monkeypatch):
    _arm(monkeypatch)
    job = _job(
        variant_id="subtitled",
        text_mode="none",
        resolved_archetype="subtitled",
        mix=None,
    )
    caps = _caps(job, "subtitled")
    assert caps["text_elements"] is False
    assert caps["reason"] == "Captions for this edit are managed in the Captions tab"
    assert caps["sfx"] is False
    assert caps["overlays"] is False
    assert caps["sfx_reason"] == "caption_archetype"
    assert caps["overlays_reason"] == "caption_archetype"


def test_capabilities_expired_sources(monkeypatch):
    """Legacy job cutting from 24h-swept uploads → timeline off with the honest reason.
    (Liveness is the persisted prefix check — no GCS call is made here.)"""
    _arm(monkeypatch)
    job = _job()
    for s in job.assembly_plan["variants"][0]["ai_timeline"]["slots"]:
        s["source_gcs_path"] = "slot-uploads/legacy/clip.mp4"
    caps = _caps(job, "song_text")
    assert caps["timeline"] is False
    assert caps["reason"] == "sources_expired"


def test_capabilities_flags_off(monkeypatch):
    _arm(monkeypatch)
    from app.config import settings

    monkeypatch.setattr(settings, "sound_effects_enabled", False, raising=False)
    monkeypatch.setattr(settings, "media_overlays_enabled", False, raising=False)

    caps = _caps(_job(), "song_text")
    assert caps["sfx"] is False
    assert caps["overlays"] is False
    assert caps["sfx_reason"] == "sound_effects_disabled"
    assert caps["overlays_reason"] == "media_overlays_disabled"


def test_capabilities_timeline_flag_off(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "GENERATIVE_TIMELINE_EDITOR_ENABLED", False)
    caps = _caps(_job(), "song_text")
    assert caps["timeline"] is False
    assert caps["split_clips"] is False
    assert caps["reason"] == "disabled"


# ── AI overlay suggestions: capability + accepted_suggestion_ids resolution ────


def _arm_autoplace(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "overlay_autoplace_enabled", True, raising=False)


def _suggestion(id_: str, overlay_id: str) -> dict:
    return {
        "id": id_,
        "asset_id": "asset-1",
        "confidence_tier": "high",
        "reason": "You mention the dashboard here",
        "transcript_anchor": "dashboard",
        "overlay": {"id": overlay_id, "kind": "image"},
        "sfx": None,
    }


def test_capabilities_suggestions_on_for_eligible_speech_variant(monkeypatch):
    _arm(monkeypatch)
    _arm_autoplace(monkeypatch)
    job = _job(variant_id="original_text", music_track_id=None, mix=None)
    caps = _caps(job, "original_text")
    assert caps["suggestions"] is True
    assert caps["suggestions_reason"] is None


def test_capabilities_suggestions_off_on_song_variant(monkeypatch):
    _arm(monkeypatch)
    _arm_autoplace(monkeypatch)
    caps = _caps(_job(), "song_text")  # fixture carries music_track_id="t1"
    assert caps["suggestions"] is False
    assert caps["suggestions_reason"] == "song_or_lyric_variant"


def test_capabilities_suggestions_inherit_overlays_reason(monkeypatch):
    _arm(monkeypatch)
    _arm_autoplace(monkeypatch)
    from app.config import settings

    monkeypatch.setattr(settings, "media_overlays_enabled", False, raising=False)
    job = _job(variant_id="original_text", music_track_id=None, mix=None)
    caps = _caps(job, "original_text")
    assert caps["suggestions"] is False
    assert caps["suggestions_reason"] == "media_overlays_disabled"


def test_commit_clears_accepted_suggestion_envelopes_atomically(monkeypatch):
    _arm(monkeypatch)
    job = _job(
        overlay_suggestions=[
            _suggestion("sug-1", "ov-accepted"),
            _suggestion("sug-2", "ov-pending"),
        ],
        overlay_suggest_status="ready",
    )
    overlays = [
        {
            "id": "ov-accepted",
            "kind": "image",
            "src_gcs_path": "users/u123/plan/item/pool/card.png",
            "position": "center",
            "x_frac": 0.5,
            "y_frac": 0.5,
            "scale": 0.35,
            "start_s": 0.4,
            "end_s": 2.4,
            "z": 0,
        }
    ]

    gj.prepare_editor_commit(
        job,
        "song_text",
        _commit_req(media_overlays=overlays, accepted_suggestion_ids=["sug-1"]),
        user_id="u123",
    )

    v = job.assembly_plan["variants"][0]
    assert [s["id"] for s in v["overlay_suggestions"]] == ["sug-2"]
    assert v["overlay_suggest_status"] == "ready"  # still pending rows left
    assert v["media_overlays"][0]["id"] == "ov-accepted"


def test_commit_clearing_last_envelope_flips_status_to_zero(monkeypatch):
    _arm(monkeypatch)
    job = _job(
        overlay_suggestions=[_suggestion("sug-1", "ov-accepted")],
        overlay_suggest_status="ready",
    )
    overlays = [
        {
            "id": "ov-accepted",
            "kind": "image",
            "src_gcs_path": "users/u123/plan/item/pool/card.png",
            "position": "center",
            "x_frac": 0.5,
            "y_frac": 0.5,
            "scale": 0.35,
            "start_s": 0.4,
            "end_s": 2.4,
            "z": 0,
        }
    ]

    gj.prepare_editor_commit(
        job,
        "song_text",
        _commit_req(media_overlays=overlays, accepted_suggestion_ids=["sug-1"]),
        user_id="u123",
    )

    v = job.assembly_plan["variants"][0]
    assert v["overlay_suggestions"] is None
    assert v["overlay_suggest_status"] == "zero"


def test_commit_unknown_accepted_ids_noop(monkeypatch):
    """Replayed/double Save: ids already cleared must not error or over-clear."""
    _arm(monkeypatch)
    job = _job(
        overlay_suggestions=[_suggestion("sug-2", "ov-pending")],
        overlay_suggest_status="ready",
    )

    gj.prepare_editor_commit(
        job,
        "song_text",
        _commit_req(media_overlays=[], accepted_suggestion_ids=["sug-gone"]),
        user_id="u123",
    )

    v = job.assembly_plan["variants"][0]
    assert [s["id"] for s in v["overlay_suggestions"]] == ["sug-2"]
    assert v["overlay_suggest_status"] == "ready"


def test_accepted_ids_without_media_overlays_section_422(monkeypatch):
    _arm(monkeypatch)
    job = _job()
    with pytest.raises(HTTPException) as exc:
        gj.prepare_editor_commit(
            job,
            "song_text",
            _commit_req(title="New title", accepted_suggestion_ids=["sug-1"]),
        )
    assert exc.value.status_code == 422
    assert "media_overlays" in str(exc.value.detail)


def test_commit_keeps_envelope_when_accepted_card_not_committed(monkeypatch):
    """An accepted id whose card is absent from the committed overlay list
    (buggy/malicious client) must NOT clear the envelope — the suggestion
    would be silently lost with nothing persisted in its place."""
    _arm(monkeypatch)
    job = _job(
        overlay_suggestions=[_suggestion("sug-1", "ov-accepted")],
        overlay_suggest_status="ready",
    )

    gj.prepare_editor_commit(
        job,
        "song_text",
        _commit_req(media_overlays=[], accepted_suggestion_ids=["sug-1"]),
        user_id="u123",
    )

    v = job.assembly_plan["variants"][0]
    assert [s["id"] for s in v["overlay_suggestions"]] == ["sug-1"]
    assert v["overlay_suggest_status"] == "ready"
