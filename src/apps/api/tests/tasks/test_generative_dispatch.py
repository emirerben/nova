"""Lane D — archetype dispatch tests for the generative-edit pipeline.

Pins the dispatch LOGIC (no ffmpeg/Gemini/GCS): which archetype a declared
edit_format resolves to against the footage, the per-archetype variant set, and the
SpineExtractionError → montage degrade contract. The real talking_head render is
verified separately (`make local-render MODE=generative --edit-format talking_head`).
"""

from __future__ import annotations

import types

import pytest

import app.services.clip_speech as clip_speech
import app.services.pipeline_trace as pt
import app.tasks.generative_build as gb
from app.pipeline.talking_head_assembler import SpineExtractionError, TalkingHeadAssemblyError


class _Meta:
    def __init__(self, clip_id):
        self.clip_id = clip_id
        self.hook_score = 5.0
        self.best_moments = []
        self.text_safe_zone = None
        self.visual_density = 5.0


def _trace_capture(monkeypatch) -> list[tuple]:
    """Capture record_pipeline_event calls (the lazy import resolves the source module)."""
    events: list[tuple] = []
    monkeypatch.setattr(
        pt,
        "record_pipeline_event",
        lambda stage, event, data=None: events.append((stage, event, data)),
    )
    return events


# ── _specs_for_archetype ──────────────────────────────────────────────────────


def test_specs_for_montage_matches_variant_specs():
    track = types.SimpleNamespace(id="t1", lyrics_cached={"lines": [{"text": "hi"}]})
    assert gb._specs_for_archetype("montage", track) == gb._variant_specs(track)


def test_specs_for_talking_head_is_single_original_audio_variant():
    specs = gb._specs_for_archetype("talking_head", None)
    assert len(specs) == 1
    spec = specs[0]
    assert spec["variant_id"] == "talking_head"
    assert spec["text_mode"] == "agent_text"
    assert spec["track"] is None
    assert spec["archetype"] == "talking_head"


# ── _resolve_archetype matrix ─────────────────────────────────────────────────


def test_resolve_montage_passthrough_no_fallback(monkeypatch):
    events = _trace_capture(monkeypatch)
    archetype, spine = gb._resolve_archetype("montage", [_Meta("c1")], {"c1": "/a.mp4"}, job_id="j")
    assert (archetype, spine) == ("montage", None)
    assert events == []  # montage is the default — no fallback noise


def test_resolve_unimplemented_format_falls_back(monkeypatch):
    events = _trace_capture(monkeypatch)
    archetype, spine = gb._resolve_archetype(
        "day_vlog", [_Meta("c1")], {"c1": "/a.mp4"}, job_id="j"
    )
    assert (archetype, spine) == ("montage", None)
    assert any(
        e[1] == "archetype_fallback" and e[2]["reason"] == "archetype_not_implemented"
        for e in events
    )


def test_resolve_talking_head_flag_off_falls_back(monkeypatch):
    monkeypatch.setattr(gb.settings, "edit_format_talking_head_enabled", False, raising=False)
    events = _trace_capture(monkeypatch)
    archetype, spine = gb._resolve_archetype(
        "talking_head", [_Meta("c1")], {"c1": "/a.mp4"}, job_id="j"
    )
    assert (archetype, spine) == ("montage", None)
    assert any(e[1] == "archetype_fallback" and e[2]["reason"] == "flag_disabled" for e in events)


def test_resolve_talking_head_no_speech_falls_back(monkeypatch):
    monkeypatch.setattr(gb.settings, "edit_format_talking_head_enabled", True, raising=False)
    monkeypatch.setattr(clip_speech, "speech_coverage", lambda path: 0.0)
    events = _trace_capture(monkeypatch)
    archetype, spine = gb._resolve_archetype(
        "talking_head", [_Meta("c1"), _Meta("c2")], {"c1": "/a.mp4", "c2": "/b.mp4"}, job_id="j"
    )
    assert (archetype, spine) == ("montage", None)
    assert any(e[1] == "archetype_fallback" and e[2]["reason"] == "no_speech" for e in events)


def test_resolve_talking_head_picks_highest_speech_clip(monkeypatch):
    monkeypatch.setattr(gb.settings, "edit_format_talking_head_enabled", True, raising=False)
    coverage = {"/a.mp4": 0.1, "/b.mp4": 0.8, "/c.mp4": 0.05}
    monkeypatch.setattr(clip_speech, "speech_coverage", lambda path: coverage[path])
    events = _trace_capture(monkeypatch)
    archetype, spine = gb._resolve_archetype(
        "talking_head",
        [_Meta("c1"), _Meta("c2"), _Meta("c3")],
        {"c1": "/a.mp4", "c2": "/b.mp4", "c3": "/c.mp4"},
        job_id="j",
    )
    assert archetype == "talking_head"
    assert spine == "c2"  # highest speech_coverage
    sel = [e for e in events if e[1] == "archetype_selected"]
    assert sel and sel[0][2]["spine_clip_id"] == "c2"


def test_resolve_talking_head_coverage_error_scores_zero(monkeypatch):
    # A probe failure on one clip must not abort resolution — it scores 0 and the
    # other clip can still qualify the format.
    monkeypatch.setattr(gb.settings, "edit_format_talking_head_enabled", True, raising=False)

    def _flaky(path):
        if path == "/a.mp4":
            raise RuntimeError("ffprobe blew up")
        return 0.7

    monkeypatch.setattr(clip_speech, "speech_coverage", _flaky)
    _trace_capture(monkeypatch)
    archetype, spine = gb._resolve_archetype(
        "talking_head", [_Meta("c1"), _Meta("c2")], {"c1": "/a.mp4", "c2": "/b.mp4"}, job_id="j"
    )
    assert (archetype, spine) == ("talking_head", "c2")


# ── _render_talking_head_variant: degrade vs failure-record contract ──────────


def _patch_th_render(monkeypatch, *, assemble):
    """Stub the lazily-imported helpers so _render_talking_head_variant runs without
    ffmpeg/GCS. `assemble` is the assemble_talking_head stub (raises or writes output)."""
    import app.pipeline.talking_head_assembler as tha
    import app.storage as storage

    monkeypatch.setattr(tha, "assemble_talking_head", assemble, raising=False)
    monkeypatch.setattr(
        storage, "upload_public_read", lambda local, gcs: f"https://signed/{gcs}", raising=False
    )


def test_talking_head_spine_error_propagates(monkeypatch, tmp_path):
    # SpineExtractionError must escape so the orchestrator degrades the WHOLE job.
    def _raise(**kw):
        raise SpineExtractionError("corrupt spine")

    _patch_th_render(monkeypatch, assemble=_raise)
    with pytest.raises(SpineExtractionError):
        gb._render_talking_head_variant(
            job_id="j",
            rank=1,
            spine_clip_id="c1",
            clip_metas=[_Meta("c1")],
            clip_id_to_local={"c1": "/a.mp4"},
            probe_map={},
            available_footage_s=10.0,
            agent_text=None,
            agent_form={},
            variant_dir=str(tmp_path),
        )


def test_talking_head_composite_error_becomes_failure_record(monkeypatch, tmp_path):
    # A non-spine error (composite ffmpeg failure) must NOT degrade the job — it
    # becomes a per-variant failure record, like _render_generative_variant.
    def _raise(**kw):
        raise TalkingHeadAssemblyError("composite failed")

    _patch_th_render(monkeypatch, assemble=_raise)
    res = gb._render_talking_head_variant(
        job_id="j",
        rank=1,
        spine_clip_id="c1",
        clip_metas=[_Meta("c1")],
        clip_id_to_local={"c1": "/a.mp4"},
        probe_map={},
        available_footage_s=10.0,
        agent_text=None,
        agent_form={},
        variant_dir=str(tmp_path),
    )
    assert res["ok"] is False
    assert res["render_status"] == "failed"
    assert res["resolved_archetype"] == "talking_head"
    assert "composite failed" in res["error"]


def test_talking_head_success_no_text(monkeypatch, tmp_path):
    # agent_text=None → no burn; the composite IS the final output.
    def _assemble(*, output_path, **kw):
        with open(output_path, "wb") as f:
            f.write(b"\x00" * 16)  # non-empty so the size guard passes

    _patch_th_render(monkeypatch, assemble=_assemble)
    res = gb._render_talking_head_variant(
        job_id="j",
        rank=1,
        spine_clip_id="c1",
        clip_metas=[_Meta("c1")],
        clip_id_to_local={"c1": "/a.mp4"},
        probe_map={},
        available_footage_s=10.0,
        agent_text=None,
        agent_form={},
        variant_dir=str(tmp_path),
    )
    assert res["ok"] is True
    assert res["render_status"] == "ready"
    assert res["resolved_archetype"] == "talking_head"
    assert res["music_track_id"] is None
    assert res["text_mode"] == "none"
    assert res["output_url"].startswith("https://signed/generative-jobs/j/")


# ── Voiceover archetype ────────────────────────────────────────────────────────


def test_resolve_voiceover_wins_over_footage(monkeypatch):
    """A user-supplied voiceover forces the voiceover archetype regardless of the
    declared edit_format or what the footage contains (it's an uploaded-asset signal,
    not a footage-derived one)."""
    events = _trace_capture(monkeypatch)
    archetype, spine = gb._resolve_archetype(
        "talking_head",
        [_Meta("c1")],
        {"c1": "/a.mp4"},
        job_id="j",
        voiceover_gcs_path="voiceover-uploads/abc/voice.webm",
    )
    assert (archetype, spine) == ("voiceover", None)
    assert any(
        e[1] == "archetype_selected" and e[2].get("archetype") == "voiceover" for e in events
    )


def test_specs_for_voiceover_only_when_no_track():
    specs = gb._specs_for_archetype(
        "voiceover", None, voiceover_gcs_path="voiceover-uploads/a/voice.webm"
    )
    assert [s["variant_id"] for s in specs] == ["voiceover_only"]
    s = specs[0]
    assert s["archetype"] == "voiceover"
    assert s["track"] is None
    assert s["voiceover_gcs_path"] == "voiceover-uploads/a/voice.webm"
    assert s["mix"] == gb._VOICEOVER_ONLY_DEFAULT_MIX


def test_specs_for_voiceover_includes_music_when_track():
    track = types.SimpleNamespace(id="t1", lyrics_cached={})
    specs = gb._specs_for_archetype(
        "voiceover", track, voiceover_gcs_path="voiceover-uploads/a/voice.webm"
    )
    assert [s["variant_id"] for s in specs] == ["voiceover_only", "voiceover_music"]
    music = specs[1]
    assert music["track"] is track
    assert music["voiceover_gcs_path"] == "voiceover-uploads/a/voice.webm"
    assert music["mix"] == gb._VOICEOVER_MUSIC_DEFAULT_MIX
