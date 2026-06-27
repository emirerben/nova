from __future__ import annotations

import subprocess

import pytest

from app.pipeline.narrated_alignment import StepTiming
from app.pipeline.narrated_assembler import (
    CAPTION_FONT,
    NarratedClip,
    _assemble_footage_bed,
    _atempo_chain,
    _build_caption_overlay,
    _fit_clip_segment,
    _rebase_words_to_assembled,
    assemble_narrated,
    burn_captions_on_video,
    is_valid_caption_font,
    resolve_caption_font,
)
from app.pipeline.single_pass import SinglePassInput
from app.pipeline.transcribe import Transcript, Word


def _run(cmd: list[str]) -> None:
    result = subprocess.run(cmd, capture_output=True, check=False, timeout=30)
    assert result.returncode == 0, result.stderr.decode(errors="replace")


def _has_libass() -> bool:
    out = subprocess.run(
        ["ffmpeg", "-hide_banner", "-filters"], capture_output=True, check=False, timeout=30
    )
    return b" subtitles " in out.stdout


def _duration(path) -> float:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=nokey=1:noprint_wrappers=1",
            str(path),
        ],
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    return float(result.stdout.decode().strip())


def _make_clip(path, color: str) -> None:
    _run(
        [
            "ffmpeg",
            "-f",
            "lavfi",
            "-i",
            f"color=c={color}:s=320x568:d=1.2:r=30",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=channel_layout=stereo:sample_rate=44100",
            "-shortest",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-y",
            str(path),
        ]
    )


def _make_clip_dur(path, color: str, dur: float) -> None:
    _run(
        [
            "ffmpeg",
            "-f",
            "lavfi",
            "-i",
            f"color=c={color}:s=320x568:d={dur}:r=30",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=channel_layout=stereo:sample_rate=44100",
            "-shortest",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-y",
            str(path),
        ]
    )


def _make_voiceover(path, dur: float = 2.0) -> None:
    _run(
        [
            "ffmpeg",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:duration={dur}",
            "-c:a",
            "aac",
            "-y",
            str(path),
        ]
    )


# ── _fit_clip_segment: reflow short clips so visuals never end early ──────────


def test_fit_segment_long_clip_trims_at_native_speed() -> None:
    inp = _fit_clip_segment("c.mp4", 0.0, 2.0, probe=lambda _p: 5.0)
    assert inp.speed_factor == 1.0
    assert inp.start_s == 0.0
    assert inp.end_s == 2.0


def test_fit_segment_short_clip_slows_to_fill() -> None:
    # 1.0s of footage owed a 2.0s step → slow (speed_factor < 1.0), output == step.
    inp = _fit_clip_segment("c.mp4", 0.0, 2.0, probe=lambda _p: 1.0)
    assert inp.speed_factor < 1.0
    usable = inp.end_s - inp.start_s
    assert usable < 2.0  # reads only the footage that exists (under EOF)
    # output duration == source window / speed_factor ≈ the step it must fill
    assert abs(usable / inp.speed_factor - 2.0) < 1e-6


def test_fit_segment_source_start_past_eof_restarts_at_zero() -> None:
    inp = _fit_clip_segment("c.mp4", 5.0, 1.0, probe=lambda _p: 3.0)
    assert inp.start_s == 0.0
    assert inp.end_s == 1.0
    assert inp.speed_factor == 1.0


def test_fit_segment_unprobeable_falls_back_to_trim() -> None:
    inp = _fit_clip_segment("c.mp4", 0.0, 1.5, probe=lambda _p: 0.0)
    assert inp.start_s == 0.0
    assert inp.end_s == 1.5
    assert inp.speed_factor == 1.0


# ── _rebase_words_to_assembled: caption clock follows the visuals ─────────────


def test_rebase_contiguous_steps_is_identity() -> None:
    steps = [
        StepTiming(step_id="s1", start_s=0.0, end_s=1.0, confidence=1.0),
        StepTiming(step_id="s2", start_s=1.0, end_s=2.0, confidence=1.0),
    ]
    words = [
        Word(text="a", start_s=0.2, end_s=0.4, confidence=1.0),
        Word(text="b", start_s=1.4, end_s=1.6, confidence=1.0),
    ]
    out = _rebase_words_to_assembled(words, steps)
    assert [round(w.start_s, 3) for w in out] == [0.2, 1.4]


def test_rebase_offset_and_gaps_compresses_and_drops_gap_words() -> None:
    # narrated_ready: first segment starts at 2.0s, a [3,4] gap precedes seg1.
    steps = [
        StepTiming(step_id="seg0", start_s=2.0, end_s=3.0, confidence=1.0),
        StepTiming(step_id="seg1", start_s=4.0, end_s=5.0, confidence=1.0),
    ]
    words = [
        Word(text="x", start_s=2.5, end_s=2.7, confidence=1.0),  # seg0 → 0.5
        Word(text="gap", start_s=3.5, end_s=3.7, confidence=1.0),  # in gap → dropped
        Word(text="y", start_s=4.5, end_s=4.7, confidence=1.0),  # seg1 → 1.5
    ]
    out = _rebase_words_to_assembled(words, steps)
    assert [w.text for w in out] == ["x", "y"]
    assert [round(w.start_s, 3) for w in out] == [0.5, 1.5]


# ── original-audio bed (PR2): atempo chain + bed assembly ────────────────────


def test_atempo_chain_no_slow_is_empty() -> None:
    assert _atempo_chain(1.0) == ""


def test_atempo_chain_single_factor_in_range() -> None:
    assert _atempo_chain(0.7) == "atempo=0.7000"


def test_atempo_chain_chains_below_half_to_product() -> None:
    # 0.25 cannot be one atempo (min 0.5); two 0.5 factors multiply to 0.25.
    chain = _atempo_chain(0.25)
    factors = [float(p.split("=")[1]) for p in chain.split(",")]
    assert all(0.5 <= f <= 1.0 for f in factors)
    prod = 1.0
    for f in factors:
        prod *= f
    assert abs(prod - 0.25) < 1e-3


def test_assemble_footage_bed_matches_total_duration(tmp_path) -> None:
    clip_a = tmp_path / "a.mp4"
    clip_b = tmp_path / "b.mp4"
    _make_clip_dur(clip_a, "red", 2.0)
    _make_clip_dur(clip_b, "blue", 2.0)
    inputs = [
        SinglePassInput(kind="clip", clip_path=str(clip_a), start_s=0.0, end_s=1.0),
        SinglePassInput(kind="clip", clip_path=str(clip_b), start_s=0.0, end_s=1.5),
    ]
    bed = tmp_path / "bed.wav"
    out = _assemble_footage_bed(inputs, str(bed), str(tmp_path))
    assert out == str(bed)
    assert bed.exists()
    # bed length == sum of step output durations (1.0 + 1.5)
    assert abs(_duration(bed) - 2.5) <= 0.2


def test_assemble_footage_bed_silent_clip_contributes_silence(tmp_path) -> None:
    """A clip with no audio stream still yields a matching-length bed segment."""
    silent = tmp_path / "silent.mp4"
    # video-only clip (no -i anullsrc, no audio stream)
    _run(
        [
            "ffmpeg",
            "-f",
            "lavfi",
            "-i",
            "color=c=green:s=320x568:d=2.0:r=30",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-y",
            str(silent),
        ]
    )
    inputs = [
        SinglePassInput(kind="clip", clip_path=str(silent), start_s=0.0, end_s=1.0),
    ]
    bed = tmp_path / "bed.wav"
    out = _assemble_footage_bed(inputs, str(bed), str(tmp_path))
    assert out == str(bed)
    assert abs(_duration(bed) - 1.0) <= 0.2


def test_narrated_with_bed_renders(tmp_path) -> None:
    """End-to-end: a positive bed_level renders (footage bed ducked under voice)."""
    clip_a = tmp_path / "a.mp4"
    clip_b = tmp_path / "b.mp4"
    voiceover = tmp_path / "vo.m4a"
    output = tmp_path / "out.mp4"
    _make_clip(clip_a, "red")
    _make_clip(clip_b, "blue")
    _make_voiceover(voiceover)

    assemble_narrated(
        [
            StepTiming(step_id="s1", start_s=0.0, end_s=1.0, confidence=1.0),
            StepTiming(step_id="s2", start_s=1.0, end_s=2.0, confidence=1.0),
        ],
        [
            NarratedClip(step_id="s1", clip_path=str(clip_a)),
            NarratedClip(step_id="s2", clip_path=str(clip_b)),
        ],
        str(voiceover),
        str(output),
        str(tmp_path),
        bed_level=0.5,
    )

    assert output.exists()
    assert output.stat().st_size > 0
    assert abs(_duration(output) - 2.0) <= 0.3


def test_assemble_narrated_ffmpeg_smoke(tmp_path) -> None:
    clip_a = tmp_path / "a.mp4"
    clip_b = tmp_path / "b.mp4"
    voiceover = tmp_path / "voiceover.m4a"
    output = tmp_path / "out.mp4"
    _make_clip(clip_a, "red")
    _make_clip(clip_b, "blue")
    _make_voiceover(voiceover)

    assemble_narrated(
        [
            StepTiming(step_id="s1", start_s=0.0, end_s=1.0, confidence=1.0),
            StepTiming(step_id="s2", start_s=1.0, end_s=2.0, confidence=1.0),
        ],
        [
            NarratedClip(step_id="s1", clip_path=str(clip_a)),
            NarratedClip(step_id="s2", clip_path=str(clip_b)),
        ],
        str(voiceover),
        str(output),
        str(tmp_path),
    )

    assert output.exists()
    assert output.stat().st_size > 0
    assert abs(_duration(output) - 2.0) <= 0.3


def test_short_clip_fills_step_no_early_freeze(tmp_path) -> None:
    """REGRESSION: a clip shorter than its step must NOT end the visuals early.

    Reproduces the reported "video stopped at 0:17 while the voice kept going":
    a 0.5s clip owed a 2.0s narration step. Before the reflow fix single_pass
    hit clip EOF at 0.5s and the montage ran short; now the clip is slowed to
    fill, so the assembled visual spans the full voiceover length.
    """
    short = tmp_path / "short.mp4"
    normal = tmp_path / "normal.mp4"
    voiceover = tmp_path / "vo.m4a"
    output = tmp_path / "out.mp4"
    _make_clip_dur(short, "red", 0.5)
    _make_clip_dur(normal, "blue", 1.5)
    _make_voiceover(voiceover, dur=3.0)

    assemble_narrated(
        [
            StepTiming(step_id="s1", start_s=0.0, end_s=2.0, confidence=1.0),
            StepTiming(step_id="s2", start_s=2.0, end_s=3.0, confidence=1.0),
        ],
        [
            NarratedClip(step_id="s1", clip_path=str(short)),
            NarratedClip(step_id="s2", clip_path=str(normal)),
        ],
        str(voiceover),
        str(output),
        str(tmp_path),
    )

    assert output.exists()
    # total step duration is 3.0s; the short clip must be reflowed to fill its
    # 2.0s step rather than ending at 0.5s (which would yield ~1.5s of video).
    assert abs(_duration(output) - 3.0) <= 0.3


def test_captions_burned_from_transcript(tmp_path) -> None:
    """Captions: the spoken narration is generated + burned over the visuals."""
    if not _has_libass():
        pytest.skip("ffmpeg built without libass (subtitles filter)")
    clip_a = tmp_path / "a.mp4"
    clip_b = tmp_path / "b.mp4"
    voiceover = tmp_path / "vo.m4a"
    output = tmp_path / "out.mp4"
    _make_clip(clip_a, "red")
    _make_clip(clip_b, "blue")
    _make_voiceover(voiceover)

    transcript = Transcript(
        words=[
            Word(text="welcome", start_s=0.0, end_s=0.5, confidence=1.0),
            Word(text="to", start_s=0.5, end_s=0.8, confidence=1.0),
            Word(text="the", start_s=1.0, end_s=1.3, confidence=1.0),
            Word(text="stadium", start_s=1.3, end_s=1.9, confidence=1.0),
        ]
    )

    assemble_narrated(
        [
            StepTiming(step_id="s1", start_s=0.0, end_s=1.0, confidence=1.0),
            StepTiming(step_id="s2", start_s=1.0, end_s=2.0, confidence=1.0),
        ],
        [
            NarratedClip(step_id="s1", clip_path=str(clip_a)),
            NarratedClip(step_id="s2", clip_path=str(clip_b)),
        ],
        str(voiceover),
        str(output),
        str(tmp_path),
        transcript=transcript,
    )

    assert output.exists()
    assert output.stat().st_size > 0
    assert abs(_duration(output) - 2.0) <= 0.3
    # the caption file was generated from the narration words
    ass = tmp_path / "narrated_captions.ass"
    assert ass.exists()
    assert "stadium" in ass.read_text(encoding="utf-8")


def test_assemble_returns_cues_and_caption_free_base(tmp_path) -> None:
    """On-video editor foundation: dual-output produces a caption-FREE base + the
    editable cues, alongside the captioned default video."""
    if not _has_libass():
        pytest.skip("ffmpeg built without libass")
    clip_a = tmp_path / "a.mp4"
    clip_b = tmp_path / "b.mp4"
    voiceover = tmp_path / "vo.m4a"
    output = tmp_path / "out.mp4"
    base = tmp_path / "out_base.mp4"
    _make_clip(clip_a, "red")
    _make_clip(clip_b, "blue")
    _make_voiceover(voiceover)
    transcript = Transcript(
        words=[
            Word(text="welcome", start_s=0.0, end_s=0.5, confidence=1.0),
            Word(text="to", start_s=0.5, end_s=0.8, confidence=1.0),
            Word(text="the", start_s=1.0, end_s=1.3, confidence=1.0),
            Word(text="stadium", start_s=1.3, end_s=1.9, confidence=1.0),
        ]
    )

    cues = assemble_narrated(
        [
            StepTiming(step_id="s1", start_s=0.0, end_s=1.0, confidence=1.0),
            StepTiming(step_id="s2", start_s=1.0, end_s=2.0, confidence=1.0),
        ],
        [
            NarratedClip(step_id="s1", clip_path=str(clip_a)),
            NarratedClip(step_id="s2", clip_path=str(clip_b)),
        ],
        str(voiceover),
        str(output),
        str(tmp_path),
        transcript=transcript,
        base_output_path=str(base),
    )

    # cues are returned for persistence, with the spoken words
    assert cues and any("stadium" in c["text"] for c in cues)
    assert all({"text", "start_s", "end_s"} <= set(c) for c in cues)
    # both the captioned default AND the caption-free base exist, same length
    assert output.exists() and base.exists()
    assert abs(_duration(output) - _duration(base)) <= 0.2
    # the base differs from the captioned video (captions burned in only one)
    assert output.stat().st_size != base.stat().st_size


def test_burn_captions_on_video(tmp_path) -> None:
    """Reburn helper: burn an ASS onto an existing video, preserving duration."""
    if not _has_libass():
        pytest.skip("ffmpeg built without libass")
    from app.pipeline.captions import generate_ass_from_cues

    src = tmp_path / "base.mp4"
    _make_clip_dur(src, "green", 2.0)
    ass = tmp_path / "cap.ass"
    generate_ass_from_cues(
        [{"text": "edited caption", "start_s": 0.2, "end_s": 1.8}],
        str(ass),
        font_name="TikTok Sans",
    )
    out = tmp_path / "burned.mp4"
    burn_captions_on_video(str(src), str(ass), str(tmp_path), str(out))
    assert out.exists() and out.stat().st_size > 0
    assert abs(_duration(out) - 2.0) <= 0.3


# ── caption_style branch: sentence (default) vs word-by-word (qbuilder) ───────


def _caption_overlay_transcript() -> Transcript:
    # One sentence, 6 words → sentence-mode = 1 cue; word-mode = 6 cues.
    return Transcript(
        words=[
            Word(text="this", start_s=0.0, end_s=0.3, confidence=1.0),
            Word(text="is", start_s=0.3, end_s=0.5, confidence=1.0),
            Word(text="what", start_s=0.5, end_s=0.9, confidence=1.0),
            Word(text="word", start_s=0.9, end_s=1.3, confidence=1.0),
            Word(text="captions", start_s=1.3, end_s=1.8, confidence=1.0),
            Word(text="do.", start_s=1.8, end_s=2.0, confidence=1.0),
        ]
    )


def test_build_caption_overlay_sentence_is_one_block(tmp_path) -> None:
    steps = [StepTiming(step_id="s1", start_s=0.0, end_s=2.0, confidence=1.0)]
    ass_paths, _fonts, cues = _build_caption_overlay(
        _caption_overlay_transcript(), steps, 2.0, str(tmp_path), caption_style="sentence"
    )
    assert len(cues) == 1  # whole sentence in one block
    assert cues[0]["text"] == "this is what word captions do."
    content = open(ass_paths[0], encoding="utf-8").read()
    assert ",78," in content and ",120," not in content  # plain sentence style


def test_build_caption_overlay_word_is_one_cue_per_word(tmp_path) -> None:
    steps = [StepTiming(step_id="s1", start_s=0.0, end_s=2.0, confidence=1.0)]
    ass_paths, _fonts, cues = _build_caption_overlay(
        _caption_overlay_transcript(), steps, 2.0, str(tmp_path), caption_style="word"
    )
    assert [c["text"] for c in cues] == ["this", "is", "what", "word", "captions", "do."]
    content = open(ass_paths[0], encoding="utf-8").read()
    assert ",120," in content  # big centered word style
    assert content.count("Dialogue:") == 6


def test_build_caption_overlay_defaults_to_sentence(tmp_path) -> None:
    steps = [StepTiming(step_id="s1", start_s=0.0, end_s=2.0, confidence=1.0)]
    _ass, _fonts, cues = _build_caption_overlay(
        _caption_overlay_transcript(), steps, 2.0, str(tmp_path)
    )
    assert len(cues) == 1  # no caption_style kwarg → sentence path (unchanged)


# ── caption font: UI font choice → libass family (resolve + validate) ─────────


def _registry_fonts() -> dict:
    from app.pipeline.text_overlay import _FONT_REGISTRY

    return _FONT_REGISTRY.get("fonts", {})


def test_resolve_caption_font_none_and_unknown_default() -> None:
    assert resolve_caption_font(None) == CAPTION_FONT
    assert resolve_caption_font("") == CAPTION_FONT
    assert resolve_caption_font("Definitely Not A Real Font 123") == CAPTION_FONT


def test_resolve_caption_font_maps_registry_key_to_ass_name() -> None:
    # A known font resolves to its registry ass_name (e.g. "Playfair Display Regular"
    # → "Playfair Display") — that family is what libass matches in fontsdir.
    fonts = _registry_fonts()
    diff = [
        (k, v["ass_name"])
        for k, v in fonts.items()
        if not v.get("deprecated") and v.get("ass_name") and v["ass_name"] != k
    ]
    assert diff, "expected at least one font whose registry key != ass_name"
    name, ass = diff[0]
    assert resolve_caption_font(name) == ass


def test_resolve_caption_font_deprecated_falls_back() -> None:
    fonts = _registry_fonts()
    dep = [k for k, v in fonts.items() if v.get("deprecated")]
    if dep:  # registry may have no deprecated fonts; only assert when present
        assert resolve_caption_font(dep[0]) == CAPTION_FONT


def test_is_valid_caption_font() -> None:
    fonts = _registry_fonts()
    known = next(k for k, v in fonts.items() if not v.get("deprecated"))
    assert is_valid_caption_font(None) is True  # null = reset to default
    assert is_valid_caption_font(known) is True
    assert is_valid_caption_font("Not A Font") is False


def test_build_caption_overlay_honors_caption_font(tmp_path) -> None:
    # Pick a font whose registry KEY differs from its ass_name, so the assertion
    # proves the key→ass_name MAPPING happened (not just that the raw key was echoed
    # — which would also pass the ASS-injection gate without the registry lookup).
    fonts = _registry_fonts()
    name, ass_name = next(
        (k, v["ass_name"])
        for k, v in fonts.items()
        if not v.get("deprecated") and v.get("ass_name") and v["ass_name"] != k
    )
    steps = [StepTiming(step_id="s1", start_s=0.0, end_s=2.0, confidence=1.0)]
    ass_paths, _fonts, _cues = _build_caption_overlay(
        _caption_overlay_transcript(), steps, 2.0, str(tmp_path), caption_font=name
    )
    content = open(ass_paths[0], encoding="utf-8").read()
    assert name not in content  # the raw registry key must NOT be burned, only ass_name
    assert f"Style: Default,{ass_name}," in content
