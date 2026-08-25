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
from app.pipeline.agents.gemini_analyzer import build_recipe
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


def test_specs_for_narrated_is_single_voiceover_variant():
    specs = gb._specs_for_archetype("narrated", None, voiceover_gcs_path="gcs/voice.m4a")
    assert len(specs) == 1
    spec = specs[0]
    assert spec["variant_id"] == "narrated"
    assert spec["text_mode"] == "none"
    assert spec["track"] is None
    assert spec["archetype"] == "narrated"
    assert spec["voiceover_gcs_path"] == "gcs/voice.m4a"


def test_render_subtitled_variant_never_raises_without_clip():
    """No clip → a failure RECORD (never-raise contract), carrying the subtitled
    finalize shape the whitelist + on-video editor + reburn rely on."""
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        result = gb._render_subtitled_variant(
            job_id="j",
            rank=1,
            spec={"variant_id": "subtitled", "archetype": "subtitled", "caption_style": "sentence"},
            clip_id_to_local={},
            variant_dir=d,
            language="tr",
        )
    assert result["ok"] is False
    assert result["render_status"] == "failed"
    assert result["resolved_archetype"] == "subtitled"
    assert result["variant_id"] == "subtitled"
    assert result["text_mode"] == "none"
    # Reuses the narrated caption keys so finalize/editor/reburn work unchanged.
    assert result["voiceover_caption_style"] == "sentence"
    assert "voiceover_caption_font" in result
    assert result.get("error")


def test_caption_reburn_archetypes_lockstep():
    # The worker reburn guard must accept exactly narrated + subtitled (and nothing
    # else — a montage agent_text base must never be caption-reburned). Kept in
    # lockstep with the route gate's _CAPTION_EDIT_ARCHETYPES.
    assert gb._CAPTION_REBURN_ARCHETYPES == frozenset({"narrated", "subtitled"})
    assert "montage" not in gb._CAPTION_REBURN_ARCHETYPES


def test_specs_for_subtitled_is_single_own_audio_caption_variant():
    specs = gb._specs_for_archetype("subtitled", None)
    assert len(specs) == 1
    spec = specs[0]
    assert spec["variant_id"] == "subtitled"
    assert spec["text_mode"] == "none"  # captions burn via the caption path, not agent intro
    assert spec["track"] is None  # keeps the clip's own audio; no music bed
    assert spec["archetype"] == "subtitled"
    assert spec["caption_style"] == "sentence"  # default when no toggle set


def test_specs_for_subtitled_word_style_follows_toggle():
    specs = gb._specs_for_archetype("subtitled", None, voiceover_caption_style="word")
    assert specs[0]["caption_style"] == "word"  # word-by-word lime pop
    # anything else falls back to the safe sentence default
    assert (
        gb._specs_for_archetype("subtitled", None, voiceover_caption_style="nonsense")[0][
            "caption_style"
        ]
        == "sentence"
    )


# ── _resolve_archetype matrix ─────────────────────────────────────────────────


def test_resolve_montage_passthrough_no_fallback(monkeypatch):
    events = _trace_capture(monkeypatch)
    archetype, spine, _reason = gb._resolve_archetype(
        "montage", [_Meta("c1")], {"c1": "/a.mp4"}, job_id="j"
    )
    assert (archetype, spine) == ("montage", None)
    assert events == []  # montage is the default — no fallback noise


def test_resolve_day_vlog_flag_off_does_not_downgrade(monkeypatch):
    events = _trace_capture(monkeypatch)
    archetype, spine, _reason = gb._resolve_archetype(
        "day_vlog", [_Meta("c1")], {"c1": "/a.mp4"}, job_id="j"
    )
    assert (archetype, spine) == ("day_vlog", None)
    assert _reason == "flag_disabled"
    assert events == []


def test_resolve_day_vlog_enabled_requires_guide_media(monkeypatch):
    monkeypatch.setattr(gb.settings, "edit_format_day_vlog_enabled", True, raising=False)
    archetype, spine, reason = gb._resolve_archetype(
        "day_vlog",
        [_Meta("c1"), _Meta("c2")],
        {"c1": "/a.mp4", "c2": "/b.mp4"},
        job_id="j",
        filming_guide=[{"what": "first"}],
    )
    assert (archetype, spine, reason) == ("day_vlog", None, "insufficient_media")


def test_resolve_day_vlog_enabled_selects_strict_archetype(monkeypatch):
    monkeypatch.setattr(gb.settings, "edit_format_day_vlog_enabled", True, raising=False)
    archetype, spine, reason = gb._resolve_archetype(
        "day_vlog",
        [_Meta("c1"), _Meta("c2")],
        {"c1": "/a.mp4", "c2": "/b.mp4"},
        job_id="j",
        filming_guide=[{"what": "first"}, {"what": "last"}],
    )
    assert (archetype, spine, reason) == ("day_vlog", None, None)


def test_day_vlog_ignores_generic_montage_variant_set(monkeypatch):
    monkeypatch.setattr(gb.settings, "edit_format_day_vlog_enabled", True, raising=False)
    specs = gb._specs_for_archetype("day_vlog", None)
    assert len(specs) == 1
    assert specs[0]["archetype"] == "day_vlog"
    assert specs[0]["strict_day_vlog"] is True


def test_single_hero_selects_one_hero_and_never_downgrades(monkeypatch):
    monkeypatch.setattr(gb.settings, "edit_format_single_hero_enabled", False, raising=False)
    archetype, spine, reason = gb._resolve_archetype(
        "single_hero", [_Meta("c1"), _Meta("c2")], {"c1": "/a.mp4", "c2": "/b.mp4"}, job_id="j"
    )
    assert (archetype, spine, reason) == ("single_hero", None, "flag_disabled")

    monkeypatch.setattr(gb.settings, "edit_format_single_hero_enabled", True, raising=False)
    specs = gb._specs_for_archetype("single_hero", None)
    assert specs[0]["strict_single_hero"] is True
    assert specs[0]["single_hero_renderer_version"] == 1
    archetype, hero, reason = gb._resolve_archetype(
        "single_hero", [_Meta("c1"), _Meta("c2")], {"c1": "/a.mp4", "c2": "/b.mp4"}, job_id="j"
    )
    assert (archetype, hero, reason) == ("single_hero", "c1", None)


def test_single_hero_policy_pins_dominance_and_ownership():
    recipe_dict = gb._build_single_hero_recipe(
        ["hero", "cutaway"],
        available_footage_s=12.0,
        clip_durations_s={"hero": 8.0, "cutaway": 4.0},
        max_duration_s=60.0,
    )
    # The real renderer sends this policy dict through the shared recipe
    # constructor before matching. Pin that integration boundary so format-only
    # metadata cannot make a production render fail after analysis.
    recipe = build_recipe(recipe_dict)
    assert recipe.slots[0]["slot_type"] == "hero"
    assert recipe.slots[0]["target_duration_s"] / recipe.total_duration_s >= 0.60
    steps = [
        types.SimpleNamespace(clip_id="hero", slot=recipe.slots[0]),
        types.SimpleNamespace(clip_id="cutaway", slot=recipe.slots[1]),
    ]
    gb._validate_single_hero_steps(steps, "hero", ["cutaway"], max_duration_s=60.0)
    with pytest.raises(gb.SingleHeroPolicyError, match="hero"):
        gb._validate_single_hero_steps(steps[::-1], "hero", ["cutaway"], max_duration_s=60.0)
    assert gb._classify_error(gb.SingleHeroPolicyError("insufficient_media", "missing")) == (
        "single_hero_insufficient_media"
    )


def test_day_vlog_chronology_kill_switch_fails_closed(monkeypatch):
    monkeypatch.setattr(gb.settings, "NARRATIVE_CLIP_ORDER_ENABLED", False, raising=False)
    with pytest.raises(gb.DayVlogPolicyError, match="chronology"):
        gb._resolve_narrative_order(
            2,
            {"c1": "/a.mp4", "c2": "/b.mp4"},
            job_id="j",
            strict=True,
        )


def test_day_vlog_step_policy_pins_chronology_transitions_and_duration():
    steps = [
        types.SimpleNamespace(
            clip_id="c1",
            slot={"target_duration_s": 2.0, "transition_in": "cut", "transition_duration_s": 0},
        ),
        types.SimpleNamespace(
            clip_id="c2",
            slot={
                "target_duration_s": 2.5,
                "transition_in": "crossfade",
                "transition_duration_s": 0.2,
            },
        ),
    ]
    gb._validate_day_vlog_steps(steps, ["c1", "c2"], max_duration_s=10)


def test_day_vlog_step_policy_rejects_reordered_or_long_transition():
    steps = [
        types.SimpleNamespace(
            clip_id="c2",
            slot={"target_duration_s": 2.0, "transition_in": "cut", "transition_duration_s": 0},
        ),
        types.SimpleNamespace(
            clip_id="c1",
            slot={
                "target_duration_s": 2.0,
                "transition_in": "crossfade",
                "transition_duration_s": 0.3,
            },
        ),
    ]
    with pytest.raises(gb.DayVlogPolicyError):
        gb._validate_day_vlog_steps(steps, ["c1", "c2"], max_duration_s=10)


def test_resolve_subtitled_flag_off_falls_back_to_montage(monkeypatch):
    monkeypatch.setattr(gb.settings, "subtitled_archetype_enabled", False, raising=False)
    events = _trace_capture(monkeypatch)
    archetype, spine, _reason = gb._resolve_archetype(
        "subtitled", [_Meta("c1")], {"c1": "/a.mp4"}, job_id="j"
    )
    assert (archetype, spine) == ("montage", None)
    assert any(e[1] == "archetype_fallback" and e[2]["reason"] == "flag_disabled" for e in events)


def test_resolve_subtitled_flag_on_selects_subtitled(monkeypatch):
    monkeypatch.setattr(gb.settings, "subtitled_archetype_enabled", True, raising=False)
    events = _trace_capture(monkeypatch)
    archetype, spine, _reason = gb._resolve_archetype(
        "subtitled", [_Meta("c1")], {"c1": "/a.mp4"}, job_id="j"
    )
    assert (archetype, spine) == ("subtitled", None)
    assert any(e[1] == "archetype_selected" and e[2]["archetype"] == "subtitled" for e in events)


def test_resolve_subtitled_no_speech_still_selects(monkeypatch):
    """Subtitled has NO speech-coverage gate — a quiet clip still resolves to subtitled
    (the caption layer shows the empty state), never a silent montage fallback."""
    monkeypatch.setattr(gb.settings, "subtitled_archetype_enabled", True, raising=False)
    monkeypatch.setattr(clip_speech, "speech_coverage", lambda *_a, **_k: 0.0, raising=False)
    _trace_capture(monkeypatch)
    archetype, spine, _reason = gb._resolve_archetype(
        "subtitled", [_Meta("c1")], {"c1": "/a.mp4"}, job_id="j"
    )
    assert (archetype, spine) == ("subtitled", None)


def test_resolve_narrated_flag_off_falls_through_to_voiceover(monkeypatch):
    monkeypatch.setattr(gb.settings, "narrated_archetype_enabled", False, raising=False)
    events = _trace_capture(monkeypatch)
    archetype, spine, _reason = gb._resolve_archetype(
        "narrated",
        [_Meta("c1"), _Meta("c2")],
        {"c1": "/a.mp4", "c2": "/b.mp4"},
        job_id="j",
        voiceover_gcs_path="gcs/voice.m4a",
        filming_guide=[
            {"shot_id": "s1", "what": "open the app"},
            {"shot_id": "s2", "what": "tap profile"},
        ],
    )
    assert (archetype, spine) == ("voiceover", None)
    assert any(e[1] == "archetype_fallback" and e[2]["reason"] == "flag_disabled" for e in events)


def test_resolve_narrated_requires_voiceover(monkeypatch):
    """Self-narration OFF (the default): narrated without a voiceover falls back to
    montage — the pre-self-narration behavior, pinned as the kill-switch baseline."""
    monkeypatch.setattr(gb.settings, "narrated_archetype_enabled", True, raising=False)
    monkeypatch.setattr(gb.settings, "narrated_self_narration_enabled", False, raising=False)
    events = _trace_capture(monkeypatch)
    archetype, spine, _reason = gb._resolve_archetype(
        "narrated",
        [_Meta("c1"), _Meta("c2")],
        {"c1": "/a.mp4", "c2": "/b.mp4"},
        job_id="j",
        voiceover_gcs_path=None,
        filming_guide=[
            {"shot_id": "s1", "what": "open the app"},
            {"shot_id": "s2", "what": "tap profile"},
        ],
    )
    assert (archetype, spine) == ("montage", None)
    assert any(
        e[1] == "archetype_fallback" and e[2]["reason"] == "archetype_not_implemented"
        for e in events
    )


def test_resolve_narrated_one_step_guide_still_selects_narrated(monkeypatch):
    """A voiceover + a thin (1-shot) guide must NOT drop to voiceover-montage —
    the renderer auto-segments the narration, so narrated still wins (captions)."""
    monkeypatch.setattr(gb.settings, "narrated_archetype_enabled", True, raising=False)
    _trace_capture(monkeypatch)
    archetype, spine, _reason = gb._resolve_archetype(
        "narrated",
        [_Meta("c1"), _Meta("c2")],
        {"c1": "/a.mp4", "c2": "/b.mp4"},
        job_id="j",
        voiceover_gcs_path="gcs/voice.m4a",
        filming_guide=[{"shot_id": "s1", "what": "open the app"}],
    )
    assert (archetype, spine) == ("narrated", None)


def test_resolve_narrated_enabled_selects_before_voiceover(monkeypatch):
    monkeypatch.setattr(gb.settings, "narrated_archetype_enabled", True, raising=False)
    events = _trace_capture(monkeypatch)
    archetype, spine, _reason = gb._resolve_archetype(
        "narrated",
        [_Meta("c1"), _Meta("c2")],
        {"c1": "/a.mp4", "c2": "/b.mp4"},
        job_id="j",
        voiceover_gcs_path="gcs/voice.m4a",
        filming_guide=[
            {"shot_id": "s1", "what": "open the app"},
            {"shot_id": "s2", "what": "tap profile"},
        ],
    )
    assert (archetype, spine) == ("narrated", None)
    sel = [e for e in events if e[1] == "archetype_selected"]
    assert sel and sel[0][2]["archetype"] == "narrated"


# ── narrated self-narration (no recorded voiceover; NARRATED_SELF_NARRATION_ENABLED) ──


def test_resolve_self_narration_single_clip_selects_subtitled(monkeypatch):
    """1 clip whose audio carries speech → subtitled (own audio + editable captions).
    The flag is the SOLE gate: subtitled_archetype_enabled=False (the declared-format
    kill switch) must NOT block this resolution outcome."""
    monkeypatch.setattr(gb.settings, "narrated_self_narration_enabled", True, raising=False)
    monkeypatch.setattr(gb.settings, "subtitled_archetype_enabled", False, raising=False)
    monkeypatch.setattr(clip_speech, "speech_coverage", lambda path: 0.9)
    events = _trace_capture(monkeypatch)
    archetype, spine, reason = gb._resolve_archetype(
        "narrated_planned", [_Meta("c1")], {"c1": "/a.mp4"}, job_id="j", voiceover_gcs_path=None
    )
    assert (archetype, spine, reason) == ("subtitled", None, None)
    sel = [e for e in events if e[1] == "archetype_selected"]
    assert sel and sel[0][2]["archetype"] == "subtitled"
    assert sel[0][2]["via"] == "narrated_self_narration"


def test_resolve_self_narration_multi_clip_selects_talking_head_spine(monkeypatch):
    """2+ clips → talking_head spined by the highest-speech clip. Independent of
    edit_format_talking_head_enabled (sole-gate contract)."""
    monkeypatch.setattr(gb.settings, "narrated_self_narration_enabled", True, raising=False)
    monkeypatch.setattr(gb.settings, "edit_format_talking_head_enabled", False, raising=False)
    coverage = {"/a.mp4": 0.2, "/b.mp4": 0.9}
    monkeypatch.setattr(clip_speech, "speech_coverage", lambda path: coverage[path])
    events = _trace_capture(monkeypatch)
    archetype, spine, reason = gb._resolve_archetype(
        "narrated_ready",
        [_Meta("c1"), _Meta("c2")],
        {"c1": "/a.mp4", "c2": "/b.mp4"},
        job_id="j",
        voiceover_gcs_path=None,
        clip_durations_s={"c1": 3.0, "c2": 8.0},
    )
    assert (archetype, spine, reason) == ("talking_head", "c2", None)
    sel = [e for e in events if e[1] == "archetype_selected"]
    assert sel and sel[0][2]["via"] == "narrated_self_narration"
    assert sel[0][2]["spine_clip_id"] == "c2"


def test_resolve_self_narration_short_spine_falls_back_to_montage(monkeypatch):
    """Tiny source clips should not auto-pick talking_head: the assembler would
    have no room to schedule B-roll and would render a single-clip result."""
    monkeypatch.setattr(gb.settings, "narrated_self_narration_enabled", True, raising=False)
    coverage = {"/a.mp4": 0.2, "/b.mp4": 0.9}
    monkeypatch.setattr(clip_speech, "speech_coverage", lambda path: coverage[path])
    events = _trace_capture(monkeypatch)
    archetype, spine, reason = gb._resolve_archetype(
        "narrated_ready",
        [_Meta("c1"), _Meta("c2")],
        {"c1": "/a.mp4", "c2": "/b.mp4"},
        job_id="j",
        voiceover_gcs_path=None,
        clip_durations_s={"c1": 1.4, "c2": 1.8},
    )
    assert (archetype, spine, reason) == ("montage", None, "spine_too_short")
    assert any(e[1] == "archetype_fallback" and e[2]["reason"] == "spine_too_short" for e in events)


def test_resolve_self_narration_no_speech_falls_back_with_reason(monkeypatch):
    """Flag on but no clip clears the speech floor → montage, and the reason rides
    the 3rd tuple slot so the orchestrator can persist it for the item-page banner."""
    monkeypatch.setattr(gb.settings, "narrated_self_narration_enabled", True, raising=False)
    monkeypatch.setattr(clip_speech, "speech_coverage", lambda path: 0.0)
    events = _trace_capture(monkeypatch)
    archetype, spine, reason = gb._resolve_archetype(
        "narrated_ready",
        [_Meta("c1"), _Meta("c2")],
        {"c1": "/a.mp4", "c2": "/b.mp4"},
        job_id="j",
        voiceover_gcs_path=None,
    )
    assert (archetype, spine, reason) == ("montage", None, "no_speech")
    assert any(e[1] == "archetype_fallback" and e[2]["reason"] == "no_speech" for e in events)


def test_resolve_self_narration_voiceover_still_wins(monkeypatch):
    """A recorded voiceover beats self-narration: the narrated archetype renders
    exactly as before, even with the self-narration flag on."""
    monkeypatch.setattr(gb.settings, "narrated_archetype_enabled", True, raising=False)
    monkeypatch.setattr(gb.settings, "narrated_self_narration_enabled", True, raising=False)
    _trace_capture(monkeypatch)
    archetype, spine, reason = gb._resolve_archetype(
        "narrated_planned",
        [_Meta("c1")],
        {"c1": "/a.mp4"},
        job_id="j",
        voiceover_gcs_path="gcs/voice.m4a",
        filming_guide=[{"shot_id": "s1", "what": "open the app"}],
    )
    assert (archetype, spine, reason) == ("narrated", None, None)


def test_resolve_self_narration_coverage_at_floor_selects(monkeypatch):
    """Boundary pin: coverage EXACTLY at _MIN_SPINE_COVERAGE selects (the gate is
    strict-less-than) — an off-by-one flip to <= would silently pass otherwise."""
    monkeypatch.setattr(gb.settings, "narrated_self_narration_enabled", True, raising=False)
    monkeypatch.setattr(clip_speech, "speech_coverage", lambda path: gb._MIN_SPINE_COVERAGE)
    _trace_capture(monkeypatch)
    archetype, spine, reason = gb._resolve_archetype(
        "narrated_ready", [_Meta("c1")], {"c1": "/a.mp4"}, job_id="j", voiceover_gcs_path=None
    )
    assert (archetype, spine, reason) == ("subtitled", None, None)


def test_pick_speech_spine_no_local_paths_returns_none(monkeypatch):
    """No clip has a local path → (None, -1.0); callers fall back on the floor check."""
    best_id, best_cov = gb._pick_speech_spine([_Meta("c1")], {}, job_id="j")
    assert best_id is None
    assert best_cov == -1.0


def test_resolve_talking_head_flag_off_falls_back(monkeypatch):
    monkeypatch.setattr(gb.settings, "edit_format_talking_head_enabled", False, raising=False)
    events = _trace_capture(monkeypatch)
    archetype, spine, _reason = gb._resolve_archetype(
        "talking_head", [_Meta("c1")], {"c1": "/a.mp4"}, job_id="j"
    )
    assert (archetype, spine) == ("montage", None)
    assert any(e[1] == "archetype_fallback" and e[2]["reason"] == "flag_disabled" for e in events)


def test_resolve_talking_head_no_speech_falls_back(monkeypatch):
    monkeypatch.setattr(gb.settings, "edit_format_talking_head_enabled", True, raising=False)
    monkeypatch.setattr(clip_speech, "speech_coverage", lambda path: 0.0)
    events = _trace_capture(monkeypatch)
    archetype, spine, _reason = gb._resolve_archetype(
        "talking_head", [_Meta("c1"), _Meta("c2")], {"c1": "/a.mp4", "c2": "/b.mp4"}, job_id="j"
    )
    assert (archetype, spine) == ("montage", None)
    assert any(e[1] == "archetype_fallback" and e[2]["reason"] == "no_speech" for e in events)


def test_resolve_talking_head_picks_highest_speech_clip(monkeypatch):
    monkeypatch.setattr(gb.settings, "edit_format_talking_head_enabled", True, raising=False)
    coverage = {"/a.mp4": 0.1, "/b.mp4": 0.8, "/c.mp4": 0.05}
    monkeypatch.setattr(clip_speech, "speech_coverage", lambda path: coverage[path])
    events = _trace_capture(monkeypatch)
    archetype, spine, _reason = gb._resolve_archetype(
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
    archetype, spine, _reason = gb._resolve_archetype(
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
    # A text-free composite IS the base — the editor needs one to play (below).
    assert res["base_video_path"] == "generative-jobs/j/base_1_talking_head.mp4"


def test_talking_head_caches_the_pre_burn_composite_as_base(monkeypatch, tmp_path):
    """The cached base must be the TEXT-FREE composite, never the burned output.

    Without a base the API emits no `base_video_url`, so EditorCanvas falls back to
    playing `output_url` — which already has the intro in its pixels — while still
    drawing its own DOM text layer. The intro then shows TWICE, one copy draggable
    and one not.
    """
    uploads: dict[str, bytes] = {}

    def _assemble(*, output_path, **kw):
        with open(output_path, "wb") as f:
            f.write(b"COMPOSITE")

    def _capture(local, gcs):
        with open(local, "rb") as f:
            uploads[gcs] = f.read()
        return f"https://signed/{gcs}"

    def _burn(src, overlays, out, tmpdir, **kw):
        with open(out, "wb") as f:
            f.write(b"BURNED")

    import app.pipeline.generative_overlays as go
    import app.pipeline.probe as probe
    import app.pipeline.talking_head_assembler as tha
    import app.pipeline.text_overlay_skia as tos
    import app.storage as storage

    monkeypatch.setattr(tha, "assemble_talking_head", _assemble, raising=False)
    monkeypatch.setattr(storage, "upload_public_read", _capture, raising=False)
    monkeypatch.setattr(tos, "burn_text_overlays_skia", _burn, raising=False)
    monkeypatch.setattr(
        probe, "probe_video", lambda p: types.SimpleNamespace(duration_s=8.0), raising=False
    )
    monkeypatch.setattr(
        go, "build_persistent_intro_overlays", lambda **kw: [{"text": "hook"}], raising=False
    )
    monkeypatch.setattr(
        gb,
        "_resolve_intro_overlay_params",
        lambda *a, **k: ({"text": "hook", "effect": "static"}, 69, "computed"),
        raising=False,
    )

    res = gb._render_talking_head_variant(
        job_id="j",
        rank=1,
        spine_clip_id="c1",
        clip_metas=[_Meta("c1")],
        clip_id_to_local={"c1": "/a.mp4"},
        probe_map={},
        available_footage_s=10.0,
        agent_text=types.SimpleNamespace(text="hook", highlight_word=None, word_roles=None),
        agent_form={},
        variant_dir=str(tmp_path),
    )

    assert res["ok"] is True
    assert res["base_video_path"] == "generative-jobs/j/base_1_talking_head.mp4"
    # Distinct objects: base = pre-burn pixels, output = post-burn pixels.
    assert res["video_path"] != res["base_video_path"]
    assert uploads[res["base_video_path"]] == b"COMPOSITE"
    assert uploads[res["video_path"]] == b"BURNED"


def test_talking_head_base_upload_failure_still_ships_the_render(monkeypatch, tmp_path):
    # Best-effort contract (mirrors the narrated caption-free base): losing the base
    # costs fast-reburn + WYSIWYG editing, never the render itself.
    def _assemble(*, output_path, **kw):
        with open(output_path, "wb") as f:
            f.write(b"\x00" * 16)

    def _upload(local, gcs):
        if "/base_" in gcs:
            raise RuntimeError("gcs down")
        return f"https://signed/{gcs}"

    import app.pipeline.talking_head_assembler as tha
    import app.storage as storage

    monkeypatch.setattr(tha, "assemble_talking_head", _assemble, raising=False)
    monkeypatch.setattr(storage, "upload_public_read", _upload, raising=False)

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
    assert res["base_video_path"] is None
    assert res["output_url"].startswith("https://signed/generative-jobs/j/")


# ── Voiceover archetype ────────────────────────────────────────────────────────


def test_resolve_voiceover_wins_over_footage(monkeypatch):
    """A user-supplied voiceover forces the voiceover archetype regardless of the
    declared edit_format or what the footage contains (it's an uploaded-asset signal,
    not a footage-derived one)."""
    events = _trace_capture(monkeypatch)
    archetype, spine, _reason = gb._resolve_archetype(
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


def test_content_plan_voiceover_uses_captioned_narrated_archetype(monkeypatch):
    """Plan-item voiceover is a transcript-caption contract, even if format drifted."""
    monkeypatch.setattr(gb.settings, "narrated_archetype_enabled", True, raising=False)
    events = _trace_capture(monkeypatch)
    archetype, spine, _reason = gb._resolve_archetype(
        "montage",
        [_Meta("c1")],
        {"c1": "/a.mp4"},
        job_id="j",
        voiceover_gcs_path="voiceover-uploads/abc/voice.webm",
        prefer_narrated_voiceover=True,
    )
    assert (archetype, spine) == ("narrated", None)
    assert any(e[1] == "archetype_selected" and e[2].get("archetype") == "narrated" for e in events)


@pytest.mark.parametrize(
    ("edit_format", "job_mode", "has_voiceover", "expected"),
    [
        ("montage", "content_plan", True, True),
        ("narrated_ready", "generative", True, True),
        ("montage", "generative", True, False),
        ("montage", "content_plan", False, False),
    ],
)
def test_content_plan_voiceover_skips_narrated_prework(
    monkeypatch, edit_format, job_mode, has_voiceover, expected
):
    monkeypatch.setattr(gb.settings, "narrated_archetype_enabled", True, raising=False)
    assert (
        gb._narrated_voiceover_prework_enabled(
            narrated_archetype_enabled=gb.settings.narrated_archetype_enabled,
            has_voiceover=has_voiceover,
            edit_format=edit_format,
            job_mode=job_mode,
        )
        is expected
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


# ── narrated_planned / narrated_ready dispatch ────────────────────────────────


def test_resolve_narrated_planned_dispatches(monkeypatch):
    monkeypatch.setattr(gb.settings, "narrated_archetype_enabled", True, raising=False)
    events = _trace_capture(monkeypatch)
    archetype, spine, _reason = gb._resolve_archetype(
        "narrated_planned",
        [_Meta("c1"), _Meta("c2")],
        {"c1": "/a.mp4", "c2": "/b.mp4"},
        job_id="j",
        voiceover_gcs_path="gcs/voice.m4a",
        filming_guide=[
            {"shot_id": "s1", "what": "stir the sauce"},
            {"shot_id": "s2", "what": "plate the dish"},
        ],
    )
    assert (archetype, spine) == ("narrated", None)
    sel = [e for e in events if e[1] == "archetype_selected"]
    assert sel and sel[0][2]["archetype"] == "narrated"


def test_resolve_narrated_ready_dispatches_without_filming_guide(monkeypatch):
    """narrated_ready needs only a voiceover — no pre-written filming_guide required."""
    monkeypatch.setattr(gb.settings, "narrated_archetype_enabled", True, raising=False)
    events = _trace_capture(monkeypatch)
    archetype, spine, _reason = gb._resolve_archetype(
        "narrated_ready",
        [_Meta("c1"), _Meta("c2")],
        {"c1": "/a.mp4", "c2": "/b.mp4"},
        job_id="j",
        voiceover_gcs_path="gcs/voice.m4a",
        filming_guide=[],  # empty — no pre-defined steps
    )
    assert (archetype, spine) == ("narrated", None)
    sel = [e for e in events if e[1] == "archetype_selected"]
    assert sel and sel[0][2]["archetype"] == "narrated"


def test_resolve_narrated_ready_flag_off_falls_through(monkeypatch):
    monkeypatch.setattr(gb.settings, "narrated_archetype_enabled", False, raising=False)
    events = _trace_capture(monkeypatch)
    archetype, spine, _reason = gb._resolve_archetype(
        "narrated_ready",
        [_Meta("c1"), _Meta("c2")],
        {"c1": "/a.mp4", "c2": "/b.mp4"},
        job_id="j",
        voiceover_gcs_path="gcs/voice.m4a",
        filming_guide=[],
    )
    assert (archetype, spine) == ("voiceover", None)
    assert any(e[1] == "archetype_fallback" and e[2]["reason"] == "flag_disabled" for e in events)


def test_resolve_narrated_planned_empty_guide_still_narrated(monkeypatch):
    """narrated_planned + voiceover but an EMPTY/thin guide must still pick narrated.

    Regression: an empty filming guide used to drop the item to voiceover-montage,
    which silently lost the captions. The renderer auto-segments instead.
    """
    monkeypatch.setattr(gb.settings, "narrated_archetype_enabled", True, raising=False)
    # empty guide
    archetype, spine, _reason = gb._resolve_archetype(
        "narrated_planned",
        [_Meta("c1"), _Meta("c2")],
        {"c1": "/a.mp4", "c2": "/b.mp4"},
        job_id="j",
        voiceover_gcs_path="gcs/voice.m4a",
        filming_guide=[],
    )
    assert (archetype, spine) == ("narrated", None)
    # one-step guide
    archetype, spine, _reason = gb._resolve_archetype(
        "narrated_planned",
        [_Meta("c1")],
        {"c1": "/a.mp4"},
        job_id="j",
        voiceover_gcs_path="gcs/voice.m4a",
        filming_guide=[{"shot_id": "s1", "what": "only step"}],
    )
    assert (archetype, spine) == ("narrated", None)
