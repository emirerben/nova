"""Subtitled + talking_head silence/filler/retake cut integration — plans/010 T5+T6.

Pins the `_render_subtitled_variant` wiring of the silence-cut stage:

- kill switch OFF ⇒ byte-identical dispatch (base transcription, no
  silencedetect, no keep_segments, no silence_cut key) — same contract style
  as test_generative_build_sequence's kill-switch pins
- has_audio gate short-circuits BEFORE any ASR call (eng review 3A)
- per-item `silence_cut_disabled` skips the stage entirely, retakes included
- safety-rail bailout renders uncut + `silence_cut_bailout` event; a clip
  below MIN_CLIP_S bails BEFORE any whisper/silencedetect spend (P3) and
  captions fall back to the base-transcription path
- happy path: keep_segments + the KEEP_SEGMENTS_PUNCH_IN constant reach the
  reframe; captions come from remap_words minus filler tokens (15A), NO second
  transcription of the base; `silence_cut` persisted on the variant
- cut-apply failure fails OPEN (R3a): uncut retry, `silence_cut_apply_failed`
  event, no persisted summary; analysis failure is cached once (7A) and both
  renders caption from the base path
- retake isolation: detector failure ⇒ zero retake cuts, silence cuts proceed;
  RETAKE_CUT_ENABLED off ⇒ detector never constructed
- per-job cache (7A): one whisper + one silencedetect + one cut encode across
  two variant renders of the same clip; locking is per-key (R3c) so distinct
  clips never serialize behind each other's compute
- regenerate hygiene (A1): a montage-path full re-render NULLs the stale
  `silence_cut` blob through the entry merge
- `_finalize_job` whitelist preserves `silence_cut` (the strip class pinned by
  test_finalize_job_preserves_ai_timeline)

And the `_render_talking_head_variant` wiring (T6 — the mechanics themselves
are pinned in tests/pipeline/test_talking_head_assembler.py):

- kill switch OFF ⇒ the assembler receives silence_cut_fn=None (byte-identical
  pre-T6 flow), no silence_cut key, no stage events
- per-item `silence_cut_disabled` skips the stage with the same event
- flag ON routes the assembler's analysis hook through the SHARED
  `_silence_cut_analysis` with the per-job cache (no parallel mechanism)
- full integration through the REAL assembler: spine cut via keep_segments +
  punch-in, usable_s from the re-probed cut spine, anchored b-roll windows,
  `silence_cut` persisted; bailout + retake-failure isolation on this path

Everything network/ffmpeg-shaped is stubbed (no API keys, no real encodes);
the real `build_cut_plan` / `remap_words` / `is_filler_token` run for real so
the assertions stay self-consistent with the pure module.
"""

from __future__ import annotations

import types

import pytest

import app.tasks.generative_build as gb
from app.pipeline.silence_cut import (
    BAILOUT_CLIP_TOO_SHORT,
    KEEP_SEGMENTS_PUNCH_IN,
    MIN_CLIP_S,
    SILENCE_CUT_VERBATIM_PROMPT,
    build_cut_plan,
    is_filler_token,
    remap_words,
)
from app.pipeline.transcribe import Word

JOB_ID = "00000000-0000-0000-0000-000000000042"


# ── Fixtures (deterministic; values verified against build_cut_plan) ─────────────


def _cut_words() -> list[Word]:
    """8 words / 6.5s. Yields exactly two removals with SILENCES below:
    the "um," filler → (0.88, 1.42) and the long pause → (2.5, 4.4).
    The trailing "uh" is BLOCKED from cutting by its segment signal but must
    still be stripped from caption input (hygiene, 15A)."""
    return [
        Word(text="so", start_s=0.5, end_s=0.7, confidence=1.0),
        Word(text="um,", start_s=1.0, end_s=1.3, confidence=1.0),
        Word(text="today", start_s=1.5, end_s=1.9, confidence=1.0),
        Word(text="we", start_s=2.0, end_s=2.2, confidence=1.0),
        Word(text="built", start_s=4.6, end_s=4.9, confidence=1.0),
        Word(text="the", start_s=5.0, end_s=5.2, confidence=1.0),
        Word(text="thing.", start_s=5.3, end_s=5.9, confidence=1.0),
        Word(text="uh", start_s=6.0, end_s=6.2, confidence=1.0, segment_no_speech_prob=0.9),
    ]


SILENCES = [(2.5, 4.4)]
DURATION = 6.5

# Removing (2.0, 9.8)-worth of trailing silence from a 10s two-word clip trips
# MAX_REMOVAL_FRAC → bailout "max_removal_exceeded".
BAILOUT_WORDS = [
    Word(text="hi", start_s=0.5, end_s=1.0, confidence=1.0),
    Word(text="there", start_s=1.2, end_s=1.5, confidence=1.0),
]
BAILOUT_SILENCES = [(2.0, 9.8)]
BAILOUT_DURATION = 10.0

# 12 words / 12s, no silences (rule 2/3 inert) — a (0,1) retake span maps to
# the single removal (0.0, 1.88, "retake").
# 10 tightly-packed words / 6.5s, no fillers, no silencedetect ranges (rule 2
# self-disables) → a clean plan with ZERO removals and no bailout.
NO_CUT_WORDS = [
    Word(text="we", start_s=0.5, end_s=0.7, confidence=1.0),
    Word(text="built", start_s=0.8, end_s=1.1, confidence=1.0),
    Word(text="the", start_s=1.2, end_s=1.4, confidence=1.0),
    Word(text="thing", start_s=1.5, end_s=1.9, confidence=1.0),
    Word(text="and", start_s=2.0, end_s=2.3, confidence=1.0),
    Word(text="it", start_s=2.4, end_s=2.6, confidence=1.0),
    Word(text="works", start_s=2.7, end_s=3.2, confidence=1.0),
    Word(text="great", start_s=3.3, end_s=3.9, confidence=1.0),
    Word(text="today", start_s=4.0, end_s=4.6, confidence=1.0),
    Word(text="friends.", start_s=4.7, end_s=5.9, confidence=1.0),
]

RETAKE_WORDS = [
    Word(text="hello", start_s=0.5, end_s=1.0, confidence=1.0),
    Word(text="world", start_s=1.2, end_s=1.8, confidence=1.0),
    Word(text="hello", start_s=2.0, end_s=2.5, confidence=1.0),
    Word(text="world", start_s=2.7, end_s=3.2, confidence=1.0),
    Word(text="and", start_s=4.0, end_s=4.4, confidence=1.0),
    Word(text="then", start_s=4.5, end_s=5.0, confidence=1.0),
    Word(text="more", start_s=5.2, end_s=5.8, confidence=1.0),
    Word(text="words", start_s=6.0, end_s=6.6, confidence=1.0),
    Word(text="to", start_s=7.0, end_s=7.4, confidence=1.0),
    Word(text="keep", start_s=7.6, end_s=8.2, confidence=1.0),
    Word(text="it", start_s=8.5, end_s=9.0, confidence=1.0),
    Word(text="long", start_s=9.2, end_s=11.5, confidence=1.0),
]


# ── Harness ──────────────────────────────────────────────────────────────────────


def _patch_pipeline(
    monkeypatch,
    *,
    words=None,
    silences=None,
    has_audio=True,
    duration=DURATION,
):
    """Stub every ffmpeg/network-shaped dependency of `_render_subtitled_variant`
    and return the call-capture dict. `build_cut_plan`/`remap_words` run REAL."""
    import app.pipeline.caption_correct as cc
    import app.pipeline.captions as captions_mod
    import app.pipeline.narrated_assembler as na
    import app.pipeline.probe as probe_mod
    import app.pipeline.reframe as reframe_mod
    import app.pipeline.transcribe as transcribe_mod
    import app.services.clip_speech as clip_speech_mod
    import app.services.pipeline_trace as pt
    import app.storage as storage

    calls: dict = {"transcribe": [], "detect": [], "reframe": [], "cues": [], "events": []}

    monkeypatch.setattr(
        probe_mod,
        "probe_video",
        lambda p: types.SimpleNamespace(
            duration_s=duration, aspect_ratio="9:16", has_audio=has_audio
        ),
        raising=False,
    )

    def _fake_reframe(input_path, start_s, end_s, aspect, ass, output_path, **kw):
        calls["reframe"].append({"input": input_path, "out": output_path, **kw})
        with open(output_path, "wb") as f:
            f.write(b"\x00" * 16)

    monkeypatch.setattr(reframe_mod, "reframe_and_export", _fake_reframe, raising=False)
    monkeypatch.setattr(
        reframe_mod,
        "resolve_output_fit",
        lambda probe, landscape_fit="fill", **kw: "crop",
        raising=False,
    )

    def _fake_transcribe(path, *, language=None, verbatim_prompt=None, **kw):
        calls["transcribe"].append(
            {"path": path, "language": language, "verbatim_prompt": verbatim_prompt}
        )
        return types.SimpleNamespace(
            words=list(words if words is not None else _cut_words()),
            language="en",
            low_confidence=False,
            full_text="",
        )

    monkeypatch.setattr(transcribe_mod, "transcribe_whisper", _fake_transcribe, raising=False)

    def _fake_detect(path, **kw):
        calls["detect"].append({"path": path, **kw})
        return list(silences if silences is not None else SILENCES)

    monkeypatch.setattr(clip_speech_mod, "detect_silences", _fake_detect, raising=False)

    def _fake_cues(cue_words, offset_s=0.0, *, attach_words=False):
        calls["cues"].append(list(cue_words))
        return [{"text": w.text, "start_s": w.start_s, "end_s": w.end_s} for w in cue_words]

    monkeypatch.setattr(captions_mod, "build_plain_cues", _fake_cues, raising=False)
    monkeypatch.setattr(
        captions_mod, "resplit_cues_into_sentences", lambda cues: cues, raising=False
    )

    def _write_ass(cues, ass_path, **kw):
        with open(ass_path, "w") as f:
            f.write("ass")

    monkeypatch.setattr(captions_mod, "generate_ass_from_cues", _write_ass, raising=False)
    monkeypatch.setattr(captions_mod, "generate_word_pop_ass", _write_ass, raising=False)
    monkeypatch.setattr(cc, "correct_caption_cues", lambda cues, lang, **kw: cues, raising=False)
    monkeypatch.setattr(na, "resolve_caption_font", lambda f: "TikTok Sans", raising=False)

    def _fake_burn(base, ass, fonts, out):
        with open(out, "wb") as f:
            f.write(b"\x01" * 24)

    monkeypatch.setattr(na, "burn_captions_on_video", _fake_burn, raising=False)
    monkeypatch.setattr(
        storage, "upload_public_read", lambda local, gcs: f"https://signed/{gcs}", raising=False
    )
    monkeypatch.setattr(
        pt,
        "record_pipeline_event",
        lambda stage, event, data=None: calls["events"].append((stage, event, data or {})),
        raising=False,
    )
    return calls


def _bomb_retake_detector(monkeypatch):
    import app.agents.retake_detector as rd

    def _boom(*a, **k):
        raise AssertionError("run_retake_detector must NOT be called on this path")

    monkeypatch.setattr(rd, "run_retake_detector", _boom, raising=False)


def _events_named(calls, name):
    return [e for e in calls["events"] if e[1] == name]


def _render(monkeypatch, tmp_path, *, disabled=False, cache=None, subdir="variant"):
    # `subdir` mirrors prod: every variant render gets its OWN variant_dir
    # (variant_{rank}) — multi-render tests must not share one, or the cache's
    # hardlinked cut base would collide with its own inode on reuse.
    vdir = tmp_path / subdir
    vdir.mkdir(exist_ok=True)
    return gb._render_subtitled_variant(
        job_id=JOB_ID,
        rank=1,
        spec={"variant_id": "subtitled", "archetype": "subtitled", "caption_style": "sentence"},
        clip_id_to_local={"c1": str(tmp_path / "clip.mp4")},
        variant_dir=str(vdir),
        language="en",
        silence_cut_disabled=disabled,
        silence_cut_cache=cache,
    )


# ── Kill switch (THE regression contract) ────────────────────────────────────────


def test_flag_off_is_byte_identical_dispatch(monkeypatch, tmp_path):
    monkeypatch.setattr(gb.settings, "silence_cut_enabled", False, raising=False)
    monkeypatch.setattr(gb.settings, "retake_cut_enabled", True, raising=False)
    calls = _patch_pipeline(monkeypatch)
    _bomb_retake_detector(monkeypatch)

    res = _render(monkeypatch, tmp_path)

    assert res["ok"] is True
    # Today's flow: ONE transcription, of the rendered BASE, no verbatim prompt.
    assert len(calls["transcribe"]) == 1
    assert calls["transcribe"][0]["path"].endswith("final_base.mp4")
    assert calls["transcribe"][0]["verbatim_prompt"] is None
    # No silencedetect, no cut kwargs, no silence_cut key, no stage events.
    assert calls["detect"] == []
    assert len(calls["reframe"]) == 1
    assert "keep_segments" not in calls["reframe"][0]
    assert res["silence_cut"] is None
    assert not [e for e in calls["events"] if e[0] == "silence_cut"]


def test_subtitled_text_lane_uploads_base_even_without_cues(monkeypatch, tmp_path):
    import app.storage as storage

    monkeypatch.setattr(gb.settings, "silence_cut_enabled", False, raising=False)
    monkeypatch.setattr(gb.settings, "subtitled_text_lane_enabled", True, raising=False)
    _patch_pipeline(monkeypatch, words=[])
    uploads: list[str] = []
    monkeypatch.setattr(
        storage,
        "upload_public_read",
        lambda _local, gcs: uploads.append(gcs) or f"https://signed/{gcs}",
        raising=False,
    )

    res = _render(monkeypatch, tmp_path)

    assert res["ok"] is True
    assert res["caption_cues"] is None
    assert res["base_video_path"] is not None
    assert [path.rsplit("/", 1)[-1] for path in uploads] == [
        "variant_1_subtitled.mp4",
        "variant_1_subtitled_base.mp4",
    ]


def test_subtitled_text_lane_flag_off_keeps_no_cue_base_upload_unchanged(monkeypatch, tmp_path):
    import app.storage as storage

    monkeypatch.setattr(gb.settings, "silence_cut_enabled", False, raising=False)
    monkeypatch.setattr(gb.settings, "subtitled_text_lane_enabled", False, raising=False)
    _patch_pipeline(monkeypatch, words=[])
    uploads: list[str] = []
    monkeypatch.setattr(
        storage,
        "upload_public_read",
        lambda _local, gcs: uploads.append(gcs) or f"https://signed/{gcs}",
        raising=False,
    )

    res = _render(monkeypatch, tmp_path)

    assert res["ok"] is True
    assert res["caption_cues"] is None
    assert res["base_video_path"] is None
    assert [path.rsplit("/", 1)[-1] for path in uploads] == ["variant_1_subtitled.mp4"]


# ── Gates (all fail-open to today's flow) ────────────────────────────────────────


def test_has_audio_gate_short_circuits_before_asr(monkeypatch, tmp_path):
    monkeypatch.setattr(gb.settings, "silence_cut_enabled", True, raising=False)
    monkeypatch.setattr(gb.settings, "retake_cut_enabled", True, raising=False)
    calls = _patch_pipeline(monkeypatch, has_audio=False)
    _bomb_retake_detector(monkeypatch)

    res = _render(monkeypatch, tmp_path)

    assert res["ok"] is True
    assert _events_named(calls, "silence_cut_skipped_no_audio")
    # The stage's verbatim ASR never ran — the only transcription is today's
    # base pass (the uncut fallback flow).
    assert len(calls["transcribe"]) == 1
    assert calls["transcribe"][0]["path"].endswith("final_base.mp4")
    assert calls["transcribe"][0]["verbatim_prompt"] is None
    assert calls["detect"] == []
    assert "keep_segments" not in calls["reframe"][0]
    assert res["silence_cut"] is None


def test_per_item_disable_skips_stage_including_retakes(monkeypatch, tmp_path):
    monkeypatch.setattr(gb.settings, "silence_cut_enabled", True, raising=False)
    monkeypatch.setattr(gb.settings, "retake_cut_enabled", True, raising=False)
    calls = _patch_pipeline(monkeypatch)
    _bomb_retake_detector(monkeypatch)

    res = _render(monkeypatch, tmp_path, disabled=True)

    assert res["ok"] is True
    assert _events_named(calls, "silence_cut_skipped_disabled")
    assert calls["detect"] == []
    assert len(calls["transcribe"]) == 1
    assert calls["transcribe"][0]["verbatim_prompt"] is None
    assert "keep_segments" not in calls["reframe"][0]
    assert res["silence_cut"] is None


def test_bailout_renders_uncut_with_event(monkeypatch, tmp_path):
    monkeypatch.setattr(gb.settings, "silence_cut_enabled", True, raising=False)
    monkeypatch.setattr(gb.settings, "retake_cut_enabled", False, raising=False)
    calls = _patch_pipeline(
        monkeypatch,
        words=BAILOUT_WORDS,
        silences=BAILOUT_SILENCES,
        duration=BAILOUT_DURATION,
    )

    res = _render(monkeypatch, tmp_path)

    assert res["ok"] is True
    events = _events_named(calls, "silence_cut_bailout")
    assert events and events[0][2]["reason"] == "max_removal_exceeded"
    # Video renders uncut …
    assert "keep_segments" not in calls["reframe"][0]
    # … but the verbatim transcript is NOT thrown away: captions come from it
    # (one whisper call total, on the ORIGINAL clip — never a second base pass).
    assert len(calls["transcribe"]) == 1
    assert calls["transcribe"][0]["path"].endswith("clip.mp4")
    assert calls["transcribe"][0]["verbatim_prompt"] == SILENCE_CUT_VERBATIM_PROMPT
    assert [w.text for w in calls["cues"][0]] == ["hi", "there"]
    # Bailouts are event-only — nothing persisted on the variant.
    assert res["silence_cut"] is None


def test_short_clip_bails_before_any_asr_spend(monkeypatch, tmp_path):
    """P3: below MIN_CLIP_S the plan can never cut — the analysis returns the
    no-op bailout plan BEFORE whisper/silencedetect/retakes run, and (empty-
    words consumer guard) captions come from the base-transcription fallback,
    never as zero cues from the empty verbatim entry."""
    monkeypatch.setattr(gb.settings, "silence_cut_enabled", True, raising=False)
    monkeypatch.setattr(gb.settings, "retake_cut_enabled", True, raising=False)
    assert 3.0 < MIN_CLIP_S  # fixture guard: keep the duration under the floor
    calls = _patch_pipeline(monkeypatch, duration=3.0)
    _bomb_retake_detector(monkeypatch)

    res = _render(monkeypatch, tmp_path)

    assert res["ok"] is True
    events = _events_named(calls, "silence_cut_bailout")
    assert events and events[0][2]["reason"] == BAILOUT_CLIP_TOO_SHORT
    # ZERO analysis spend: the only transcription is the base-captioning
    # fallback (no verbatim pass on the original clip, no silencedetect).
    assert len(calls["transcribe"]) == 1
    assert calls["transcribe"][0]["path"].endswith("final_base.mp4")
    assert calls["transcribe"][0]["verbatim_prompt"] is None
    assert calls["detect"] == []
    assert "keep_segments" not in calls["reframe"][0]
    assert res["silence_cut"] is None  # bailouts stay event-only


def test_analysis_failure_fails_open_and_caches_failure_once(monkeypatch, tmp_path):
    """Analysis blow-up (whisper 500) fails OPEN to the uncut flow, and the
    failure is cached (7A): a sibling render never re-spends the failing call
    — both variants caption from the base-transcription fallback."""
    import app.pipeline.transcribe as transcribe_mod

    monkeypatch.setattr(gb.settings, "silence_cut_enabled", True, raising=False)
    monkeypatch.setattr(gb.settings, "retake_cut_enabled", False, raising=False)
    calls = _patch_pipeline(monkeypatch)

    verbatim_calls = {"n": 0}

    def _flaky_transcribe(path, *, language=None, verbatim_prompt=None, **kw):
        calls["transcribe"].append(
            {"path": path, "language": language, "verbatim_prompt": verbatim_prompt}
        )
        if verbatim_prompt is not None:  # the analysis pass only
            verbatim_calls["n"] += 1
            raise RuntimeError("whisper 500")
        return types.SimpleNamespace(
            words=list(_cut_words()), language="en", low_confidence=False, full_text=""
        )

    monkeypatch.setattr(transcribe_mod, "transcribe_whisper", _flaky_transcribe, raising=False)
    cache = gb._SilenceCutCache(str(tmp_path / "silence_cut"))

    first = _render(monkeypatch, tmp_path, cache=cache)
    second = _render(monkeypatch, tmp_path, cache=cache, subdir="variant2")

    assert first["ok"] is True and second["ok"] is True
    assert first["silence_cut"] is None and second["silence_cut"] is None
    # The failing verbatim call was spent ONCE across both renders …
    assert verbatim_calls["n"] == 1
    assert len(_events_named(calls, "silence_cut_analysis_failed")) == 1
    # … and each render captions from its own base pass, uncut.
    base_calls = [c for c in calls["transcribe"] if c["verbatim_prompt"] is None]
    assert len(base_calls) == 2
    assert all(c["path"].endswith("final_base.mp4") for c in base_calls)
    assert all("keep_segments" not in r for r in calls["reframe"])


# ── Happy path ───────────────────────────────────────────────────────────────────


def test_happy_path_cuts_captions_and_persists(monkeypatch, tmp_path):
    monkeypatch.setattr(gb.settings, "silence_cut_enabled", True, raising=False)
    monkeypatch.setattr(gb.settings, "retake_cut_enabled", False, raising=False)
    calls = _patch_pipeline(monkeypatch)

    res = _render(monkeypatch, tmp_path)

    assert res["ok"] is True
    # Detection ran on the ORIGINAL clip with the shared verbatim prompt and the
    # cut-path silencedetect floor (d=0.1 — round 2 / 9A). One whisper call total.
    assert len(calls["transcribe"]) == 1
    assert calls["transcribe"][0]["path"].endswith("clip.mp4")
    assert calls["transcribe"][0]["verbatim_prompt"] == SILENCE_CUT_VERBATIM_PROMPT
    assert len(calls["detect"]) == 1
    assert calls["detect"][0]["path"].endswith("clip.mp4")
    assert calls["detect"][0]["min_silence_s"] == 0.1

    # The cut executes inside the reframe: exact plan segments + the punch-in
    # CONSTANT (never a literal 1.08).
    reframe = calls["reframe"][0]
    assert reframe["keep_segments"] == pytest.approx([(0.0, 0.88), (1.42, 2.5), (4.4, 6.5)])
    assert reframe["keep_segments_punch_in"] == KEEP_SEGMENTS_PUNCH_IN

    # Caption input == remap_words(words, plan) minus filler tokens (15A):
    # "um," was cut, "uh" survived the video (segment-signal block) but is
    # STILL stripped; all times are cut-relative.
    plan = build_cut_plan(_cut_words(), SILENCES, DURATION)
    expected = [w for w in remap_words(_cut_words(), plan) if not is_filler_token(w["text"])]
    got = calls["cues"][0]
    assert [w.text for w in got] == [w["text"] for w in expected]
    assert [w.text for w in got] == ["so", "today", "we", "built", "the", "thing."]
    assert [w.start_s for w in got] == pytest.approx([w["start_s"] for w in expected])
    assert [w.end_s for w in got] == pytest.approx([w["end_s"] for w in expected])
    assert max(w.end_s for w in got) <= DURATION - plan.time_saved_s + 1e-6

    # Persistence: plain dicts + version, ready for the finalize whitelist
    # (plan_summary shape — M2; original_duration_s feeds the admin strip).
    assert res["silence_cut"] == {
        "removed": [
            {"start_s": 0.88, "end_s": 1.42, "reason": "filler_lexical"},
            {"start_s": 2.5, "end_s": 4.4, "reason": "silence"},
        ],
        "time_saved_s": 2.44,
        "version": 1,
        "original_duration_s": 6.5,
    }
    events = _events_named(calls, "silence_cut_plan")
    assert events and events[0][2]["removed_count"] == 2
    assert events[0][2]["time_saved_s"] == 2.44
    assert res["caption_language"] == "en"


def test_zero_removal_plan_persists_empty_summary(monkeypatch, tmp_path):
    """A clean plan with NOTHING to cut is not a bailout: "nothing to cut" is
    information — persisted with removed=[] + the plan event (applied False),
    and the already-paid-for verbatim transcript still drives the captions."""
    monkeypatch.setattr(gb.settings, "silence_cut_enabled", True, raising=False)
    monkeypatch.setattr(gb.settings, "retake_cut_enabled", False, raising=False)
    calls = _patch_pipeline(monkeypatch, words=NO_CUT_WORDS, silences=[])

    res = _render(monkeypatch, tmp_path)

    assert res["ok"] is True
    # Exactly ONE whisper call — the verbatim analysis pass; captions reuse it
    # (no second transcription of the base).
    assert len(calls["transcribe"]) == 1
    assert calls["transcribe"][0]["verbatim_prompt"] == SILENCE_CUT_VERBATIM_PROMPT
    # Zero removals ⇒ no segmented encode …
    assert "keep_segments" not in calls["reframe"][0]
    # … but the summary + event still land (admin strip shows "0 cuts").
    assert res["silence_cut"] == {
        "removed": [],
        "time_saved_s": 0.0,
        "version": 1,
        "original_duration_s": 6.5,
    }
    events = _events_named(calls, "silence_cut_plan")
    assert events and events[0][2]["applied"] is False
    assert events[0][2]["removed_count"] == 0
    assert not _events_named(calls, "silence_cut_bailout")


def test_cut_apply_failure_falls_open_to_uncut(monkeypatch, tmp_path):
    """R3a: a cut-applying reframe failure costs the CUTS, never the variant —
    uncut retry (no keep_segments), `silence_cut_apply_failed` event, captions
    from the base-transcription fallback, and NO persisted summary (a
    removed[] blob on an uncut video lies to the admin viewer)."""
    import app.pipeline.reframe as reframe_mod

    monkeypatch.setattr(gb.settings, "silence_cut_enabled", True, raising=False)
    monkeypatch.setattr(gb.settings, "retake_cut_enabled", False, raising=False)
    calls = _patch_pipeline(monkeypatch)

    def _flaky_reframe(input_path, start_s, end_s, aspect, ass, output_path, **kw):
        calls["reframe"].append({"input": input_path, "out": output_path, **kw})
        if "keep_segments" in kw:
            raise RuntimeError("segment select filter blew up")
        with open(output_path, "wb") as f:
            f.write(b"\x00" * 16)

    monkeypatch.setattr(reframe_mod, "reframe_and_export", _flaky_reframe, raising=False)

    res = _render(monkeypatch, tmp_path)

    assert res["ok"] is True  # fail-open: never a variant failure
    # First attempt carried the plan; the retry is the plain uncut reframe.
    assert len(calls["reframe"]) == 2
    assert "keep_segments" in calls["reframe"][0]
    assert "keep_segments" not in calls["reframe"][1]
    events = _events_named(calls, "silence_cut_apply_failed")
    assert events and events[0][2]["variant_id"] == "subtitled"
    assert "blew up" in events[0][2]["error"]
    # No summary, no plan event — the shipped video is uncut.
    assert res["silence_cut"] is None
    assert not _events_named(calls, "silence_cut_plan")
    # Captions fall back to the base-transcription path (the remapped verbatim
    # words describe a cut timeline that no longer exists).
    base_calls = [c for c in calls["transcribe"] if c["verbatim_prompt"] is None]
    assert len(base_calls) == 1
    assert base_calls[0]["path"].endswith("final_base.mp4")


# ── Retakes ──────────────────────────────────────────────────────────────────────


def test_retake_flag_off_never_calls_detector(monkeypatch, tmp_path):
    monkeypatch.setattr(gb.settings, "silence_cut_enabled", True, raising=False)
    monkeypatch.setattr(gb.settings, "retake_cut_enabled", False, raising=False)
    calls = _patch_pipeline(monkeypatch)
    _bomb_retake_detector(monkeypatch)

    res = _render(monkeypatch, tmp_path)

    assert res["ok"] is True
    # Silence/filler cutting fully unaffected by the (off) retake lane.
    assert "keep_segments" in calls["reframe"][0]
    assert not _events_named(calls, "retake_detector_failed")


def test_retake_failure_isolated_cut_still_applies(monkeypatch, tmp_path):
    from app.agents._runtime import TerminalError

    monkeypatch.setattr(gb.settings, "silence_cut_enabled", True, raising=False)
    monkeypatch.setattr(gb.settings, "retake_cut_enabled", True, raising=False)
    calls = _patch_pipeline(monkeypatch)

    import app.agents.retake_detector as rd

    def _fail(*a, **k):
        raise TerminalError("agent exhausted retries")

    monkeypatch.setattr(rd, "run_retake_detector", _fail, raising=False)

    res = _render(monkeypatch, tmp_path)

    assert res["ok"] is True
    assert _events_named(calls, "retake_detector_failed")
    # Zero retake cuts, but the silence/filler plan still applied in full.
    assert calls["reframe"][0]["keep_segments"] == pytest.approx(
        [(0.0, 0.88), (1.42, 2.5), (4.4, 6.5)]
    )
    assert res["silence_cut"] is not None
    assert all(r["reason"] != "retake" for r in res["silence_cut"]["removed"])


def test_retake_spans_merge_into_plan(monkeypatch, tmp_path):
    monkeypatch.setattr(gb.settings, "silence_cut_enabled", True, raising=False)
    monkeypatch.setattr(gb.settings, "retake_cut_enabled", True, raising=False)
    calls = _patch_pipeline(monkeypatch, words=RETAKE_WORDS, silences=[], duration=12.0)

    import app.agents.retake_detector as rd

    seen: dict = {}

    def _fake_detector(inp, *, client=None, ctx=None):
        seen["input"] = inp
        return types.SimpleNamespace(
            retakes=[types.SimpleNamespace(start_word=0, end_word=1, reason="restarted")]
        )

    monkeypatch.setattr(rd, "run_retake_detector", _fake_detector, raising=False)

    res = _render(monkeypatch, tmp_path)

    assert res["ok"] is True
    # Detector received the verbatim transcript as contiguous indexed words.
    assert [w.i for w in seen["input"].words] == list(range(len(RETAKE_WORDS)))
    assert [w.text for w in seen["input"].words] == [w.text for w in RETAKE_WORDS]
    # The span became a "retake" removal merged into the SAME plan/apply.
    assert res["silence_cut"]["removed"] == [{"start_s": 0.0, "end_s": 1.88, "reason": "retake"}]
    assert calls["reframe"][0]["keep_segments"] == pytest.approx([(1.88, 12.0)])
    assert not _events_named(calls, "retake_detector_failed")


# ── Per-job cache (7A) ───────────────────────────────────────────────────────────


def test_cache_computes_once_and_reuses_cut_output(monkeypatch, tmp_path):
    monkeypatch.setattr(gb.settings, "silence_cut_enabled", True, raising=False)
    monkeypatch.setattr(gb.settings, "retake_cut_enabled", False, raising=False)
    calls = _patch_pipeline(monkeypatch)
    cache = gb._SilenceCutCache(str(tmp_path / "silence_cut"))

    first = _render(monkeypatch, tmp_path, cache=cache)
    second = _render(monkeypatch, tmp_path, cache=cache, subdir="variant2")

    assert first["ok"] is True and second["ok"] is True
    # ONE whisper call, ONE silencedetect pass, ONE cut encode across both
    # variants (7A) — the second render copies the cached cut output.
    assert len(calls["transcribe"]) == 1
    assert len(calls["detect"]) == 1
    assert len(calls["reframe"]) == 1
    # Both variants agree on the persisted plan (they share the entry).
    assert first["silence_cut"] == second["silence_cut"]
    entry = cache.clips[str(tmp_path / "clip.mp4")]
    assert entry["cut_video_path"] and entry["cut_video_path"].startswith(str(tmp_path))


def test_cache_per_key_locking_never_serializes_distinct_keys(monkeypatch, tmp_path):
    """R3c: a slow compute on one key must NOT block a different key — the
    global lock only guards slot get-or-insert; computes run outside it.
    Pure event choreography: no sleeps, no wall-clock races."""
    import threading

    import app.pipeline.transcribe as transcribe_mod
    import app.services.clip_speech as clip_speech_mod

    monkeypatch.setattr(gb.settings, "retake_cut_enabled", False, raising=False)
    monkeypatch.setattr(clip_speech_mod, "detect_silences", lambda path, **kw: [], raising=False)

    key1_started = threading.Event()
    release_key1 = threading.Event()
    key2_done = threading.Event()

    def _gated_transcribe(path, *, language=None, verbatim_prompt=None, **kw):
        if path.endswith("clip1.mp4"):
            key1_started.set()
            assert release_key1.wait(timeout=10), "test deadlock: key1 never released"
        return types.SimpleNamespace(
            words=list(_cut_words()), language="en", low_confidence=False, full_text=""
        )

    monkeypatch.setattr(transcribe_mod, "transcribe_whisper", _gated_transcribe, raising=False)

    cache = gb._SilenceCutCache(str(tmp_path / "silence_cut"))
    results: dict = {}

    def _worker1():
        results["k1"] = gb._silence_cut_analysis("clip1.mp4", DURATION, job_id=JOB_ID, cache=cache)

    def _worker2():
        # Enter only once key1 is provably mid-compute (its slot inserted, the
        # global lock long released).
        assert key1_started.wait(timeout=10)
        results["k2"] = gb._silence_cut_analysis("clip2.mp4", DURATION, job_id=JOB_ID, cache=cache)
        key2_done.set()

    t1 = threading.Thread(target=_worker1)
    t2 = threading.Thread(target=_worker2)
    t1.start()
    t2.start()
    # THE assertion: key2 completes WHILE key1 is still parked in its compute.
    # Under compute-under-global-lock this wait can only time out.
    assert key2_done.wait(timeout=10), "key2 serialized behind key1's slow compute"
    assert not release_key1.is_set()  # key1 really was still blocked
    release_key1.set()
    t1.join(timeout=10)
    t2.join(timeout=10)
    assert not t1.is_alive() and not t2.is_alive()

    assert results["k1"]["failed"] is False and results["k2"]["failed"] is False
    assert set(cache.clips) == {"clip1.mp4", "clip2.mp4"}
    assert cache.pending == {}  # slots cleaned up after publish


def test_cache_store_oserror_degrades_gracefully(monkeypatch, tmp_path):
    """P2 store hygiene: hardlink AND copy both refused (exotic tmp mount) —
    the variant still ships (ok True) and `cut_video_path` stays None, so the
    next variant simply pays its own encode. Never a job failure."""
    monkeypatch.setattr(gb.settings, "silence_cut_enabled", True, raising=False)
    monkeypatch.setattr(gb.settings, "retake_cut_enabled", False, raising=False)
    calls = _patch_pipeline(monkeypatch)
    cache = gb._SilenceCutCache(str(tmp_path / "silence_cut"))

    def _refuse(*a, **k):
        raise OSError("read-only file system")

    monkeypatch.setattr(gb.os, "link", _refuse)
    monkeypatch.setattr(gb.shutil, "copy2", _refuse)

    res = _render(monkeypatch, tmp_path, cache=cache)

    assert res["ok"] is True
    # The cut itself applied — only the sibling-reuse stash was lost.
    assert "keep_segments" in calls["reframe"][0]
    assert res["silence_cut"] is not None and res["silence_cut"]["removed"]
    entry = cache.clips[str(tmp_path / "clip.mp4")]
    assert entry["cut_video_path"] is None


# ── Regenerate hygiene (A1) ───────────────────────────────────────────────────────


def test_regenerate_full_render_clears_stale_silence_cut(monkeypatch):
    """A1: a montage-path full re-render (talking_head regen, timeline edit)
    never runs the cut stage — the ok-branch must write silence_cut=None into
    the merge patch or the admin strip keeps describing the PREVIOUS video's
    cuts. Reuses the timeline-render harness (persisted user_timeline ⇒ the
    override path, no ingest/Gemini)."""
    from tests.tasks.test_generative_timeline_render import (
        JOB_ID as TLR_JOB_ID,
    )
    from tests.tasks.test_generative_timeline_render import (
        _existing_variant,
        _regen_setup,
        _tl_slot,
    )

    stale = {
        "removed": [{"start_s": 0.88, "end_s": 1.42, "reason": "filler_lexical"}],
        "time_saved_s": 0.54,
        "version": 1,
        "original_duration_s": 6.5,
    }
    variant = _existing_variant(
        user_timeline={"slots": [_tl_slot(0)]},
        silence_cut=stale,
    )
    _job, updates, _dl = _regen_setup(monkeypatch, variants=[variant])

    gb._run_regenerate_variant(TLR_JOB_ID, "original_text", None, None, False)

    final = updates[-1]
    assert final["ok"] is True
    # Key PRESENT and None — a missing key would merge the stale blob through.
    assert "silence_cut" in final
    assert final["silence_cut"] is None


# ── Finalization whitelist (the silent-strip class) ──────────────────────────────


def test_finalize_job_preserves_silence_cut(monkeypatch):
    """`_finalize_job` rebuilds variants through an explicit whitelist that
    silently strips unlisted keys (the prod `no_timeline` incident class) —
    `silence_cut` MUST be listed or the admin cut-plan viewer loses its data
    the moment the job completes. Mirrors test_finalize_job_preserves_ai_timeline."""
    captured: dict = {}

    def _capture_set_status(job_id, status, extra_plan=None):
        captured["status"] = status
        captured["plan"] = extra_plan

    monkeypatch.setattr(gb, "_set_status", _capture_set_status)

    silence_cut = {
        "removed": [
            {"start_s": 0.88, "end_s": 1.42, "reason": "filler_lexical"},
            {"start_s": 2.5, "end_s": 4.4, "reason": "silence"},
        ],
        "time_saved_s": 2.44,
        "version": 1,
    }
    results = [
        {
            "variant_id": "subtitled",
            "rank": 1,
            "text_mode": "none",
            "render_status": "ready",
            "ok": True,
            "silence_cut": silence_cut,
        }
    ]

    gb._finalize_job(JOB_ID, results)

    assert captured["plan"]["variants"][0]["silence_cut"] == silence_cut, (
        "finalize stripped silence_cut"
    )


# ══ talking_head wiring (plans/010 T6) ═══════════════════════════════════════════


def _patch_th_stub(monkeypatch, *, summary=None):
    """Stub `assemble_talking_head` itself — pins the RENDER-FN wiring only
    (gates, hook plumbing, summary persistence). The assembler mechanics are
    pinned in tests/pipeline/test_talking_head_assembler.py."""
    import app.pipeline.talking_head_assembler as tha
    import app.services.pipeline_trace as pt
    import app.storage as storage

    calls: dict = {"assemble": [], "events": []}

    def _fake_assemble(**kw):
        calls["assemble"].append(kw)
        if summary is not None and kw.get("silence_cut_out") is not None:
            kw["silence_cut_out"]["summary"] = summary
        with open(kw["output_path"], "wb") as f:
            f.write(b"\x00" * 16)
        return kw["output_path"]

    monkeypatch.setattr(tha, "assemble_talking_head", _fake_assemble, raising=False)
    monkeypatch.setattr(
        storage, "upload_public_read", lambda local, gcs: f"https://signed/{gcs}", raising=False
    )
    monkeypatch.setattr(
        pt,
        "record_pipeline_event",
        lambda stage, event, data=None: calls["events"].append((stage, event, data or {})),
        raising=False,
    )
    return calls


def _render_th(monkeypatch, tmp_path, *, disabled=False, cache=None, probe_map=None, target=60.0):
    vdir = tmp_path / "th_variant"
    vdir.mkdir(exist_ok=True)
    return gb._render_talking_head_variant(
        job_id=JOB_ID,
        rank=1,
        spine_clip_id=None,
        clip_metas=[
            types.SimpleNamespace(clip_id="c1", content_type="talking_head", audio_type="dialogue"),
            types.SimpleNamespace(clip_id="c2", content_type="broll", audio_type="ambient"),
        ],
        clip_id_to_local={"c1": str(tmp_path / "a.mp4"), "c2": str(tmp_path / "b.mp4")},
        probe_map=probe_map or {},
        available_footage_s=target,
        agent_text=None,
        agent_form={},
        variant_dir=str(vdir),
        silence_cut_disabled=disabled,
        silence_cut_cache=cache,
    )


def test_talking_head_flag_off_passes_no_cut_fn(monkeypatch, tmp_path):
    # Kill switch: the assembler gets silence_cut_fn=None → its pre-T6 flow is
    # byte-identical (pinned assembler-side by test_assemble_without_cut_fn…).
    monkeypatch.setattr(gb.settings, "silence_cut_enabled", False, raising=False)
    monkeypatch.setattr(gb.settings, "retake_cut_enabled", True, raising=False)
    calls = _patch_th_stub(monkeypatch)
    _bomb_retake_detector(monkeypatch)

    res = _render_th(monkeypatch, tmp_path)

    assert res["ok"] is True
    assert calls["assemble"][0]["silence_cut_fn"] is None
    assert res["silence_cut"] is None
    assert not [e for e in calls["events"] if e[0] == "silence_cut"]


def test_talking_head_per_item_disable_skips_stage(monkeypatch, tmp_path):
    monkeypatch.setattr(gb.settings, "silence_cut_enabled", True, raising=False)
    monkeypatch.setattr(gb.settings, "retake_cut_enabled", True, raising=False)
    calls = _patch_th_stub(monkeypatch)
    _bomb_retake_detector(monkeypatch)

    res = _render_th(monkeypatch, tmp_path, disabled=True)

    assert res["ok"] is True
    events = _events_named(calls, "silence_cut_skipped_disabled")
    assert events and events[0][2]["variant_id"] == "talking_head"
    assert calls["assemble"][0]["silence_cut_fn"] is None
    assert res["silence_cut"] is None


def test_talking_head_cut_fn_routes_shared_analysis_with_cache(monkeypatch, tmp_path):
    # T6 must not invent a parallel mechanism: the hook handed to the assembler
    # IS `_silence_cut_analysis` with this job's id + per-job cache bound.
    monkeypatch.setattr(gb.settings, "silence_cut_enabled", True, raising=False)
    monkeypatch.setattr(gb.settings, "retake_cut_enabled", False, raising=False)
    calls = _patch_th_stub(monkeypatch)
    cache = gb._SilenceCutCache(str(tmp_path / "silence_cut"))

    seen: dict = {}

    def _fake_analysis(path, duration_s, *, job_id, cache, cache_key=None):
        seen.update(
            path=path, duration_s=duration_s, job_id=job_id, cache=cache, cache_key=cache_key
        )
        return {"failed": True, "plan": None, "retake_span_count": 0}

    monkeypatch.setattr(gb, "_silence_cut_analysis", _fake_analysis)

    res = _render_th(monkeypatch, tmp_path, cache=cache)

    assert res["ok"] is True
    fn = calls["assemble"][0]["silence_cut_fn"]
    assert callable(fn)
    # The assembler keys a pre-capped analysis WAV by its SOURCE spine (+cap) —
    # the closure must forward cache_key through to the shared analysis.
    fn("spine_cut_analysis.wav", 42.0, cache_key="spine.mp4::cap=120.0")
    assert seen == {
        "path": "spine_cut_analysis.wav",
        "duration_s": 42.0,
        "job_id": JOB_ID,
        "cache": cache,
        "cache_key": "spine.mp4::cap=120.0",
    }
    # cache_key is optional — omitted ⇒ None (analysis keys by the path itself).
    fn("spine.mp4", 42.0)
    assert seen["cache_key"] is None


def test_talking_head_summary_from_assembler_is_persisted(monkeypatch, tmp_path):
    monkeypatch.setattr(gb.settings, "silence_cut_enabled", True, raising=False)
    monkeypatch.setattr(gb.settings, "retake_cut_enabled", False, raising=False)
    summary = {
        "removed": [{"start_s": 0.88, "end_s": 1.42, "reason": "filler_lexical"}],
        "time_saved_s": 0.54,
        "version": 1,
    }
    _patch_th_stub(monkeypatch, summary=summary)

    res = _render_th(monkeypatch, tmp_path)

    assert res["ok"] is True
    assert res["silence_cut"] == summary


# ── Full integration: real assembler + real shared analysis, mocked ffmpeg/ASR ────


def _patch_th_full(monkeypatch, *, words=None, silences=None, cut_dur=4.06):
    """Real `assemble_talking_head` + real `_silence_cut_analysis`; every
    ffmpeg/ASR-shaped dependency stubbed. Returns the call-capture dict."""
    import app.pipeline.talking_head_assembler as tha
    import app.pipeline.transcribe as transcribe_mod
    import app.services.clip_speech as clip_speech_mod
    import app.services.pipeline_trace as pt
    import app.storage as storage

    calls: dict = {"transcribe": [], "detect": [], "reframe": [], "cmds": [], "events": []}

    # select_spine's coverage_fn default binds the real speech_coverage at
    # import time — stub the selection wholesale (spine=c1, broll=[c2]).
    monkeypatch.setattr(
        tha,
        "select_spine",
        lambda *a, **k: tha.SpineSelection(
            spine_clip_id="c1", spine_score=1.0, broll_clip_ids=["c2"]
        ),
        raising=False,
    )

    def _fake_reframe(input_path, start_s, end_s, aspect, ass, output_path, **kw):
        calls["reframe"].append({"input": input_path, "start": start_s, "end": end_s, **kw})

    monkeypatch.setattr(tha, "reframe_and_export", _fake_reframe, raising=False)
    monkeypatch.setattr(
        tha, "resolve_output_fit", lambda probe, landscape_fit="fill", **kw: "crop", raising=False
    )

    def _fake_run(cmd, **kwargs):
        calls["cmds"].append(cmd)
        with open(cmd[-1], "wb") as f:  # composite output (_encoding_args tail)
            f.write(b"\x00" * 16)
        return types.SimpleNamespace(returncode=0, stderr=b"")

    monkeypatch.setattr(tha.subprocess, "run", _fake_run, raising=False)
    # Re-probe of the CUT spine (the has_audio gate reads probe_map instead).
    monkeypatch.setattr(
        tha,
        "probe_video",
        lambda p: types.SimpleNamespace(duration_s=cut_dur, has_audio=True),
        raising=False,
    )

    def _record(stage, event, data=None):
        calls["events"].append((stage, event, data or {}))

    monkeypatch.setattr(tha, "record_pipeline_event", _record, raising=False)
    monkeypatch.setattr(pt, "record_pipeline_event", _record, raising=False)

    def _fake_transcribe(path, *, language=None, verbatim_prompt=None, **kw):
        calls["transcribe"].append(
            {"path": path, "language": language, "verbatim_prompt": verbatim_prompt}
        )
        return types.SimpleNamespace(
            words=list(words if words is not None else _cut_words()),
            language="en",
            low_confidence=False,
            full_text="",
        )

    monkeypatch.setattr(transcribe_mod, "transcribe_whisper", _fake_transcribe, raising=False)

    def _fake_detect(path, **kw):
        calls["detect"].append({"path": path, **kw})
        return list(silences if silences is not None else SILENCES)

    monkeypatch.setattr(clip_speech_mod, "detect_silences", _fake_detect, raising=False)
    monkeypatch.setattr(
        storage, "upload_public_read", lambda local, gcs: f"https://signed/{gcs}", raising=False
    )
    return calls


def _th_probe_map(*, duration, has_audio=True):
    return {
        "c1": types.SimpleNamespace(duration_s=duration, has_audio=has_audio),
        "c2": types.SimpleNamespace(duration_s=duration, has_audio=True),
    }


def test_talking_head_happy_path_cuts_spine_and_anchors_broll(monkeypatch, tmp_path):
    monkeypatch.setattr(gb.settings, "silence_cut_enabled", True, raising=False)
    monkeypatch.setattr(gb.settings, "retake_cut_enabled", False, raising=False)
    calls = _patch_th_full(monkeypatch, cut_dur=4.06)
    cache = gb._SilenceCutCache(str(tmp_path / "silence_cut"))

    res = _render_th(
        monkeypatch, tmp_path, cache=cache, probe_map=_th_probe_map(duration=DURATION), target=6.5
    )

    assert res["ok"] is True
    # Detection ran ONCE on the ORIGINAL spine (6.5s < cap → no pre-cap WAV),
    # with the shared verbatim prompt + the cut-path silencedetect floor.
    assert len(calls["transcribe"]) == 1
    assert calls["transcribe"][0]["path"].endswith("a.mp4")
    assert calls["transcribe"][0]["verbatim_prompt"] == SILENCE_CUT_VERBATIM_PROMPT
    assert calls["detect"][0]["min_silence_s"] == 0.1

    # Spine reframe carries the exact plan segments + the punch-in CONSTANT;
    # the b-roll reframe is untouched (never cut).
    spine = next(c for c in calls["reframe"] if c["input"].endswith("a.mp4"))
    broll = next(c for c in calls["reframe"] if c["input"].endswith("b.mp4"))
    assert spine["keep_segments"] == pytest.approx([(0.0, 0.88), (1.42, 2.5), (4.4, 6.5)])
    assert spine["keep_segments_punch_in"] == KEEP_SEGMENTS_PUNCH_IN
    assert "keep_segments" not in broll

    # usable_s comes from the re-probed CUT spine (4.06s): the composite trims
    # to it and the b-roll window fits inside the cut timeline.
    (composite,) = calls["cmds"]
    joined = " ".join(composite)
    assert "trim=0:4.060" in joined
    assert "enable='between(t,1.500,4.060)'" in joined

    # Persistence + the plan event, exactly like the subtitled path (shared
    # plan_summary shape — original_duration_s is the analysis window).
    assert res["silence_cut"] == {
        "removed": [
            {"start_s": 0.88, "end_s": 1.42, "reason": "filler_lexical"},
            {"start_s": 2.5, "end_s": 4.4, "reason": "silence"},
        ],
        "time_saved_s": 2.44,
        "version": 1,
        "original_duration_s": 6.5,
    }
    events = _events_named(calls, "silence_cut_plan")
    assert events and events[0][2]["applied"] is True
    assert events[0][2]["variant_id"] == "talking_head"
    assert events[0][2]["broll_anchors"] == 2


def test_talking_head_has_audio_gate_short_circuits_before_asr(monkeypatch, tmp_path):
    monkeypatch.setattr(gb.settings, "silence_cut_enabled", True, raising=False)
    monkeypatch.setattr(gb.settings, "retake_cut_enabled", True, raising=False)
    calls = _patch_th_full(monkeypatch)
    _bomb_retake_detector(monkeypatch)

    res = _render_th(
        monkeypatch,
        tmp_path,
        probe_map=_th_probe_map(duration=10.0, has_audio=False),
        target=10.0,
    )

    assert res["ok"] is True
    assert _events_named(calls, "silence_cut_skipped_no_audio")
    assert calls["transcribe"] == []  # the ASR never ran (eng review 3A)
    assert calls["detect"] == []
    spine = next(c for c in calls["reframe"] if c["input"].endswith("a.mp4"))
    assert "keep_segments" not in spine
    assert res["silence_cut"] is None


def test_talking_head_bailout_renders_uncut(monkeypatch, tmp_path):
    monkeypatch.setattr(gb.settings, "silence_cut_enabled", True, raising=False)
    monkeypatch.setattr(gb.settings, "retake_cut_enabled", False, raising=False)
    calls = _patch_th_full(monkeypatch, words=BAILOUT_WORDS, silences=BAILOUT_SILENCES)

    res = _render_th(
        monkeypatch,
        tmp_path,
        probe_map=_th_probe_map(duration=BAILOUT_DURATION),
        target=BAILOUT_DURATION,
    )

    assert res["ok"] is True
    events = _events_named(calls, "silence_cut_bailout")
    assert events and events[0][2]["reason"] == "max_removal_exceeded"
    spine = next(c for c in calls["reframe"] if c["input"].endswith("a.mp4"))
    assert "keep_segments" not in spine
    assert spine["end"] == pytest.approx(BAILOUT_DURATION)  # full uncut spine
    assert res["silence_cut"] is None
    assert not _events_named(calls, "silence_cut_plan")


def test_talking_head_retake_failure_isolated_cut_still_applies(monkeypatch, tmp_path):
    from app.agents._runtime import TerminalError

    monkeypatch.setattr(gb.settings, "silence_cut_enabled", True, raising=False)
    monkeypatch.setattr(gb.settings, "retake_cut_enabled", True, raising=False)
    calls = _patch_th_full(monkeypatch)

    import app.agents.retake_detector as rd

    def _fail(*a, **k):
        raise TerminalError("agent exhausted retries")

    monkeypatch.setattr(rd, "run_retake_detector", _fail, raising=False)

    res = _render_th(monkeypatch, tmp_path, probe_map=_th_probe_map(duration=DURATION), target=6.5)

    assert res["ok"] is True
    assert _events_named(calls, "retake_detector_failed")
    # Zero retake cuts, but the silence/filler plan still applied to the spine.
    spine = next(c for c in calls["reframe"] if c["input"].endswith("a.mp4"))
    assert spine["keep_segments"] == pytest.approx([(0.0, 0.88), (1.42, 2.5), (4.4, 6.5)])
    assert res["silence_cut"] is not None
    assert all(r["reason"] != "retake" for r in res["silence_cut"]["removed"])


def test_talking_head_cache_shares_analysis_across_renders(monkeypatch, tmp_path):
    # 7A: the spine clip is analyzed ONCE per job — a second talking_head
    # render (retry/self-narration sibling) reuses the cached entry.
    monkeypatch.setattr(gb.settings, "silence_cut_enabled", True, raising=False)
    monkeypatch.setattr(gb.settings, "retake_cut_enabled", False, raising=False)
    calls = _patch_th_full(monkeypatch)
    cache = gb._SilenceCutCache(str(tmp_path / "silence_cut"))

    first = _render_th(
        monkeypatch, tmp_path, cache=cache, probe_map=_th_probe_map(duration=DURATION), target=6.5
    )
    second = _render_th(
        monkeypatch, tmp_path, cache=cache, probe_map=_th_probe_map(duration=DURATION), target=6.5
    )

    assert first["ok"] is True and second["ok"] is True
    assert len(calls["transcribe"]) == 1  # ONE whisper pass across both renders
    assert len(calls["detect"]) == 1
    assert first["silence_cut"] == second["silence_cut"]
