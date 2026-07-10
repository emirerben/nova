"""Subtitled silence/filler/retake cut integration — plans/010 T5.

Pins the `_render_subtitled_variant` wiring of the silence-cut stage:

- kill switch OFF ⇒ byte-identical dispatch (base transcription, no
  silencedetect, no keep_segments, no silence_cut key) — same contract style
  as test_generative_build_sequence's kill-switch pins
- has_audio gate short-circuits BEFORE any ASR call (eng review 3A)
- per-item `silence_cut_disabled` skips the stage entirely, retakes included
- safety-rail bailout renders uncut + `silence_cut_bailout` event
- happy path: keep_segments + the KEEP_SEGMENTS_PUNCH_IN constant reach the
  reframe; captions come from remap_words minus filler tokens (15A), NO second
  transcription of the base; `silence_cut` persisted on the variant
- retake isolation: detector failure ⇒ zero retake cuts, silence cuts proceed;
  RETAKE_CUT_ENABLED off ⇒ detector never constructed
- per-job cache (7A): one whisper + one silencedetect + one cut encode across
  two variant renders of the same clip
- `_finalize_job` whitelist preserves `silence_cut` (the strip class pinned by
  test_finalize_job_preserves_ai_timeline)

Everything network/ffmpeg-shaped is stubbed (no API keys, no real encodes);
the real `build_cut_plan` / `remap_words` / `is_filler_token` run for real so
the assertions stay self-consistent with the pure module.
"""

from __future__ import annotations

import types

import pytest

import app.tasks.generative_build as gb
from app.pipeline.silence_cut import (
    KEEP_SEGMENTS_PUNCH_IN,
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


def _render(monkeypatch, tmp_path, *, disabled=False, cache=None):
    vdir = tmp_path / "variant"
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

    # Persistence: plain dicts + version, ready for the finalize whitelist.
    assert res["silence_cut"] == {
        "removed": [
            {"start_s": 0.88, "end_s": 1.42, "reason": "filler_lexical"},
            {"start_s": 2.5, "end_s": 4.4, "reason": "silence"},
        ],
        "time_saved_s": 2.44,
        "version": 1,
    }
    events = _events_named(calls, "silence_cut_plan")
    assert events and events[0][2]["removed_count"] == 2
    assert events[0][2]["time_saved_s"] == 2.44
    assert res["caption_language"] == "en"


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
    second = _render(monkeypatch, tmp_path, cache=cache)

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
