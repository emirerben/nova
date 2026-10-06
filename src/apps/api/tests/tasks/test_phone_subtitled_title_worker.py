"""KRI-467: `_run_phone_subtitled_job` draws the creator's confirmed hook title.

The worker reads ``creator_strategy.opening_title`` (+ its confirmed hold),
builds the title row on the cut timeline, keeps it off the speaker's face,
compiles it under the captions and persists it as an editable text row.
``PHONE_SUBTITLED_TITLE_ENABLED=false`` is byte-identical to the untitled job.
"""

from __future__ import annotations

import copy

import pytest

import app.pipeline.phone_subtitled_title as title_mod
from app.pipeline.render_geometry import NormalizedBox, ProtectedRegion
from app.services.device_render import device_status
from app.tasks import generative_build as gb
from tests.tasks.test_generative_build_silence_cut import DURATION
from tests.tasks.test_phone_subtitled_narrated_dispatch import (
    _raw_preflight_snapshot,
    _setup_subtitled,
    _setup_talking_head,
    _words,
)

_TITLE = "3 sourdough mistakes"


def _faces(monkeypatch, regions=()):
    calls: list = []

    def sample(path, anchors, **kwargs):
        calls.append((path, list(anchors)))
        return list(regions), {"attempted": len(anchors), "decoded": len(anchors)}

    monkeypatch.setattr(title_mod, "sample_face_regions", sample)
    return calls


def _titled(monkeypatch, *, hold=2.0, **setup):
    job, snapshot, session, binding = _setup_subtitled(monkeypatch, **setup)
    job.all_candidates["creator_strategy"] = {
        "opening_title": _TITLE,
        **({"opening_title_duration_s": hold} if hold is not None else {}),
    }
    return job, snapshot, session, binding


def _title_layers(job):
    recipe = device_status(job, "subtitled").request.recipe
    return [layer for layer in recipe.text_layers if layer.id.startswith("text-")]


def test_the_sourdough_title_renders_for_the_first_two_seconds(monkeypatch):
    calls = _faces(monkeypatch)
    job, _snapshot, _session, _binding = _titled(monkeypatch)

    gb._run_generative_job(str(job.id))

    assert job.status == "awaiting_device"
    variant = job.assembly_plan["variants"][0]
    [row] = variant["text_elements"]
    assert (row["id"], row["text"]) == ("opening-title", _TITLE)
    assert (row["start_s"], row["end_s"]) == (0.0, 2.0)
    assert "read_only" not in row["source_params"]
    assert variant["text_elements_user_edited"] is True
    assert variant["opening_title_placement"]["status"] == "preset"
    # Sampled on the speaker's own analysis proxy.
    assert calls and calls[0][0] == "/tmp/c0.mp4"
    [title] = _title_layers(job)
    assert (title.start, title.end, title.effect) == (0.0, 2.0, "fade-in")
    recipe = device_status(job, "subtitled").request.recipe
    assert [layer.id for layer in recipe.text_layers][1:] == [
        f"caption-{index}" for index in range(len(variant["caption_cues"]))
    ]


def test_the_title_moves_off_the_speakers_face(monkeypatch):
    _faces(monkeypatch, [ProtectedRegion(0.0, 2.0, NormalizedBox(0.25, 0.04, 0.75, 0.42), "face")])
    job, _snapshot, _session, _binding = _titled(monkeypatch)

    gb._run_generative_job(str(job.id))

    [row] = job.assembly_plan["variants"][0]["text_elements"]
    assert row["y_frac"] > 0.42
    assert job.assembly_plan["variants"][0]["opening_title_placement"]["status"] == "moved"


def test_without_a_confirmed_hold_the_title_fades_after_the_first_word(monkeypatch):
    _faces(monkeypatch)
    job, _snapshot, _session, _binding = _titled(
        monkeypatch, hold=None, transcript_words=_words(("Hello", 0.2, 0.6), ("there.", 0.6, 1.0))
    )

    gb._run_generative_job(str(job.id))

    [row] = job.assembly_plan["variants"][0]["text_elements"]
    assert row["end_s"] == pytest.approx(1.6)


def test_a_cleanup_cut_keeps_the_title_on_the_cut_timeline(monkeypatch):
    calls = _faces(monkeypatch)
    job, snapshot, _session, binding = _titled(monkeypatch, duration_s=DURATION)
    snapshot["speech_cleanup_contract"] = "required_v1"
    snapshot["_speech_cleanup_internal"] = {
        "preflight_snapshot": _raw_preflight_snapshot(storage_path=binding.proxy_path)
    }

    gb._run_phone_subtitled_job(str(job.id), snapshot, job.all_candidates, ownership_epoch=3)

    [title] = _title_layers(job)
    assert (title.start, title.end) == (0.0, 2.0)
    # The cut removed (0.88, 1.42): an anchor past 0.88 s of output reads the
    # source 0.54 s later.
    [(_path, anchors)] = calls
    assert anchors[-1] == pytest.approx(2.0 * 11 / 12 + 0.54, abs=1e-3)


def test_a_multi_clip_talking_head_gets_the_title_placed_on_its_speaker(monkeypatch):
    """A self-narrated voiceover item without a recorded voiceover renders
    through the same phone Talking job (KRI-136); its title used to be
    dropped there silently."""
    calls = _faces(monkeypatch)
    job, _snapshot, _bindings, _transcribed = _setup_talking_head(monkeypatch, spine="c1")
    job.all_candidates["creator_strategy"] = {"opening_title": _TITLE}

    gb._run_generative_job(str(job.id))

    assert job.status == "awaiting_device"
    [row] = job.assembly_plan["variants"][0]["text_elements"]
    assert row["text"] == _TITLE
    assert row["end_s"] == pytest.approx(1.5)  # a second after "Hello" ends (0.5 s)
    assert [path for path, _anchors in calls] == ["/tmp/c1.mp4"]
    [title] = _title_layers(job)
    assert (title.start, title.end) == (0.0, pytest.approx(1.5))


def test_switched_off_the_job_is_exactly_the_untitled_one(monkeypatch):
    _faces(monkeypatch)
    plain, _s, _sess, _b = _setup_subtitled(monkeypatch)
    gb._run_generative_job(str(plain.id))
    expected_variant = copy.deepcopy(plain.assembly_plan["variants"][0])
    expected_recipe = device_status(plain, "subtitled").request.recipe

    monkeypatch.setattr(gb.settings, "phone_subtitled_title_enabled", False)
    job, _snapshot, _session, _binding = _titled(monkeypatch)
    gb._run_generative_job(str(job.id))

    variant = job.assembly_plan["variants"][0]
    assert "text_elements" not in variant
    assert variant == {**expected_variant, "render_generation_id": variant["render_generation_id"]}
    assert device_status(job, "subtitled").request.recipe == expected_recipe
