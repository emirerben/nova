import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.pipeline.lyric_injector import inject_lyric_overlays
from app.pipeline.lyrics_preview import (
    PREVIEW_CRF,
    PREVIEW_WINDOW_S,
    LyricsPreviewInputError,
    build_lyrics_preview_ass_files,
    build_lyrics_preview_recipe,
    render_lyrics_preview,
)
from app.pipeline.text_overlay import generate_animated_overlay_ass


def _track(**overrides):
    track = SimpleNamespace(
        id="track-preview",
        audio_gcs_path="music/track/audio.m4a",
        duration_s=5.0,
        track_config={},
        lyrics_cached={
            "lines": [
                {
                    "text": "hello world",
                    "start_s": 1.0,
                    "end_s": 2.0,
                    "words": [
                        {"text": "hello", "start_s": 1.0, "end_s": 1.5},
                        {"text": "world", "start_s": 1.5, "end_s": 2.0},
                    ],
                }
            ]
        },
    )
    for key, value in overrides.items():
        setattr(track, key, value)
    return track


def test_preview_ass_byte_identical_to_production_path(tmp_path: Path) -> None:
    track = _track()
    cfg = {"enabled": True, "style": "line", "post_dwell_s": 1.0}

    preview_files = build_lyrics_preview_ass_files(track, cfg, str(tmp_path / "preview"))

    production_dir = tmp_path / "production"
    production_dir.mkdir()
    recipe = {"slots": [{"position": 1, "target_duration_s": 5.0, "text_overlays": []}]}
    recipe = inject_lyric_overlays(
        recipe,
        track.lyrics_cached,
        best_start_s=0.0,
        best_end_s=5.0,
        lyrics_config=cfg,
    )
    production_files = generate_animated_overlay_ass(
        recipe["slots"][0]["text_overlays"],
        slot_duration_s=5.0,
        output_dir=str(production_dir),
        slot_index=0,
    )

    assert production_files
    assert [Path(p).read_text() for p in preview_files] == [
        Path(p).read_text() for p in production_files
    ]


def test_preview_rejects_missing_lyrics_cached(tmp_path: Path) -> None:
    with pytest.raises(LyricsPreviewInputError, match="cached lyrics"):
        build_lyrics_preview_ass_files(_track(lyrics_cached=None), {}, str(tmp_path))


def test_preview_recipe_clamps_to_20s_window_when_track_is_longer() -> None:
    """Tracks longer than PREVIEW_WINDOW_S get a clamped 20s preview slot.

    History: PR opening the Line Templates dashboard (2026-05-25). Before
    the clamp the preview rendered the entire 3-4 minute track, which made
    the iteration loop pointlessly slow for a feature focused on hook timing.
    """
    long_track = _track(duration_s=185.0)
    recipe = build_lyrics_preview_recipe(long_track, {})
    assert recipe["slots"][0]["target_duration_s"] == PREVIEW_WINDOW_S


def test_preview_recipe_renders_full_length_when_track_is_shorter_than_window() -> None:
    """Tracks shorter than PREVIEW_WINDOW_S keep their full duration — the
    clamp is a ceiling, not a floor. Locks the byte-identical guarantee for
    short fixtures (the 5s track used in the production-parity test above).
    """
    short_track = _track(duration_s=5.0)
    recipe = build_lyrics_preview_recipe(short_track, {})
    assert recipe["slots"][0]["target_duration_s"] == 5.0


def test_preview_recipe_at_exact_window_boundary() -> None:
    """Boundary value `duration_s == PREVIEW_WINDOW_S` lands on the clamp's
    inclusive side. Locks that a future refactor swapping `min(a, b)` for an
    `if a > b` guard would not silently shift behavior at 20.0s.
    """
    boundary_track = _track(duration_s=PREVIEW_WINDOW_S)
    recipe = build_lyrics_preview_recipe(boundary_track, {})
    assert recipe["slots"][0]["target_duration_s"] == PREVIEW_WINDOW_S


def test_preview_recipe_falls_back_to_best_end_s_when_duration_unknown_dict_shape() -> None:
    """When `duration_s` is missing or non-positive, the recipe falls back to
    `track_config.best_end_s` and clamps that against the preview window.

    Covers the production `track_config` shape (JSONB → dict at SQLAlchemy load).
    """
    track = _track(duration_s=0.0, track_config={"best_end_s": 12.0})
    recipe = build_lyrics_preview_recipe(track, {})
    assert recipe["slots"][0]["target_duration_s"] == 12.0


def test_preview_recipe_falls_back_to_best_end_s_when_duration_unknown_object_shape() -> None:
    """Same fallback, but `track_config` is an object with `.best_end_s` rather
    than a dict. Defensive coverage so the resolver doesn't crash if any caller
    passes a Pydantic model or SimpleNamespace into the preview pipeline.
    """
    track = _track(
        duration_s=0.0,
        track_config=SimpleNamespace(best_end_s=12.0),
    )
    recipe = build_lyrics_preview_recipe(track, {})
    assert recipe["slots"][0]["target_duration_s"] == 12.0


def test_preview_recipe_fallback_also_clamps_to_window() -> None:
    """If `best_end_s` exceeds PREVIEW_WINDOW_S the fallback still respects the
    20s ceiling. Catches a bug where the clamp lived only on the primary path.
    """
    track = _track(duration_s=0.0, track_config={"best_end_s": 90.0})
    recipe = build_lyrics_preview_recipe(track, {})
    assert recipe["slots"][0]["target_duration_s"] == PREVIEW_WINDOW_S


def test_preview_recipe_raises_when_duration_and_best_end_s_both_missing() -> None:
    """Neither `duration_s` nor `best_end_s` — the recipe can't pick a slot
    length, so it raises rather than producing a zero-length preview.
    """
    track = _track(duration_s=0.0, track_config={})
    with pytest.raises(LyricsPreviewInputError, match="duration is unknown"):
        build_lyrics_preview_recipe(track, {})


def test_preview_recipe_raises_on_negative_duration() -> None:
    """Negative duration is treated as unknown, not as a literal slot length."""
    track = _track(duration_s=-5.0, track_config={})
    with pytest.raises(LyricsPreviewInputError, match="duration is unknown"):
        build_lyrics_preview_recipe(track, {})


def test_render_lyrics_preview_builds_browser_safe_ffmpeg(monkeypatch, tmp_path: Path) -> None:
    def fake_download(_gcs_path: str, local_path: str) -> None:
        Path(local_path).write_bytes(b"audio")

    def fake_run(cmd, **_kwargs):
        Path(cmd[-1]).write_bytes(b"mp4")
        return SimpleNamespace(returncode=0, stderr=b"")

    monkeypatch.setattr("app.pipeline.lyrics_preview.download_to_file", fake_download)
    monkeypatch.setattr("app.pipeline.lyrics_preview.subprocess.run", fake_run)
    monkeypatch.setattr(
        "app.pipeline.lyrics_preview.upload_public_read",
        lambda _local, _obj: "https://example.com/preview.mp4",
    )

    output_url, meta = render_lyrics_preview(_track(), {"enabled": True, "style": "line"})

    assert output_url == "https://example.com/preview.mp4"
    cmd = meta["ffmpeg_cmd"]
    assert "-nostdin" in cmd
    assert "yuv420p" in cmd
    assert "+faststart" in cmd
    assert "-shortest" in cmd
    assert any("subtitles=" in part for part in cmd)
    # Audio codec must be AAC for cross-browser playback (Safari + iOS won't
    # play opus or vorbis in mp4). _encoding_args pulls in BODY_SLOT_AUDIO_OUT_ARGS
    # which sets this — pin it so a future refactor of that constant can't
    # silently break the preview's browser playback.
    assert "aac" in cmd

    # Encoder policy (test_encoder_policy.py) locks the preset class but NOT
    # the CRF literal. Pin CRF here so a future tweak forces a conscious
    # quality-budget decision rather than a silent preset/CRF drift.
    assert "-crf" in cmd
    crf_value = cmd[cmd.index("-crf") + 1]
    assert crf_value == PREVIEW_CRF, (
        f"preview CRF drifted to {crf_value!r} — update PREVIEW_CRF constant + this test"
    )
    assert "ultrafast" not in cmd  # regression guard for the v0 → v1 flip

    # -t is the layer that actually caps the final MP4 duration; -shortest
    # alone is not enough because lavfi `color=...` is an infinite source.
    # The 5s test track resolves to a 5s preview (≤ PREVIEW_WINDOW_S).
    assert "-t" in cmd
    t_value = cmd[cmd.index("-t") + 1]
    assert t_value == "5.000", f"unexpected -t cap {t_value!r}, expected 5.000s"
    assert meta["preview_duration_s"] == 5.0


def test_render_lyrics_preview_lavfi_source_uses_output_settings(
    monkeypatch, tmp_path: Path
) -> None:
    """The lavfi black-canvas spec must read from `settings.output_*` (not hardcoded
    1080x1920:r=30). Locks against a future drift between the production output
    resolution and the preview's source resolution.
    """
    from app.config import settings  # noqa: PLC0415

    def fake_download(_gcs_path: str, local_path: str) -> None:
        Path(local_path).write_bytes(b"audio")

    def fake_run(cmd, **_kwargs):
        Path(cmd[-1]).write_bytes(b"mp4")
        return SimpleNamespace(returncode=0, stderr=b"")

    monkeypatch.setattr("app.pipeline.lyrics_preview.download_to_file", fake_download)
    monkeypatch.setattr("app.pipeline.lyrics_preview.subprocess.run", fake_run)
    monkeypatch.setattr(
        "app.pipeline.lyrics_preview.upload_public_read",
        lambda _local, _obj: "https://example.com/preview.mp4",
    )

    _, meta = render_lyrics_preview(_track(), {"enabled": True, "style": "line"})
    expected = (
        f"color=c=black:s={settings.output_width}x{settings.output_height}:r={settings.output_fps}"
    )
    assert expected in meta["ffmpeg_cmd"], (
        f"lavfi source string {expected!r} not found in cmd: {meta['ffmpeg_cmd']}"
    )


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg + ffprobe required for the duration-cap integration test",
)
@pytest.mark.timeout(180)
def test_render_lyrics_preview_final_mp4_duration_caps_at_window(
    monkeypatch, tmp_path: Path
) -> None:
    """Integration: with a 60-second audio source and a long-duration track,
    the final MP4 must be ≤ PREVIEW_WINDOW_S (with a small encoder tolerance).

    This is the only test that actually executes FFmpeg + ffprobe. It catches
    a class of bug the mocked tests can't: that `-shortest` plus an infinite
    lavfi color source would otherwise let the output run for the full audio
    duration. The fix layer is the explicit `-t {preview_duration_s}` flag
    emitted by `_build_preview_ffmpeg_cmd`.

    History: previous revision relied on `-shortest` alone, which silently
    rendered 3-minute previews because lavfi `color=...` never ends.
    """
    # Build a 60-second AAC audio file so the audio source is far longer than
    # the 20s window. If `-t` is missing or wrong, the output MP4 will be ~60s.
    long_audio = tmp_path / "audio.aac"
    audio_build = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-nostdin",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=60",
            "-c:a",
            "aac",
            str(long_audio),
        ],
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert audio_build.returncode == 0, audio_build.stderr.decode(errors="replace")[-500:]
    assert long_audio.exists() and long_audio.stat().st_size > 0

    # Capture the final MP4 by mocking the GCS upload to copy the local file
    # out of the tempdir before render_lyrics_preview returns and tears it down.
    captured: dict[str, Path] = {}

    def fake_download(_gcs_path: str, local_path: str) -> None:
        # Stand in for GCS: copy our 60s synthetic audio into the renderer's
        # tempdir so the real ffmpeg can mux it.
        shutil.copyfile(str(long_audio), local_path)

    def fake_upload(local_path: str, _object_path: str) -> str:
        captured_path = tmp_path / "final_output.mp4"
        shutil.copyfile(local_path, captured_path)
        captured["mp4"] = captured_path
        return f"https://example.com/{Path(local_path).name}"

    monkeypatch.setattr("app.pipeline.lyrics_preview.download_to_file", fake_download)
    monkeypatch.setattr("app.pipeline.lyrics_preview.upload_public_read", fake_upload)

    # Track claims 185s duration — well past the 20s window. _resolve_preview_duration_s
    # must clamp to PREVIEW_WINDOW_S, and the FFmpeg `-t` must follow.
    track = _track(duration_s=185.0)
    output_url, meta = render_lyrics_preview(track, {"enabled": True, "style": "line"})

    assert output_url.startswith("https://"), output_url
    assert meta["preview_duration_s"] == PREVIEW_WINDOW_S, (
        f"resolver returned {meta['preview_duration_s']}, expected {PREVIEW_WINDOW_S}"
    )

    # ffprobe the captured MP4: format.duration must be ~20s, NOT ~60s.
    mp4_path = captured["mp4"]
    probe = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(mp4_path),
        ],
        capture_output=True,
        timeout=15,
        check=True,
    )
    duration_str = probe.stdout.decode().strip()
    actual_duration_s = float(duration_str)

    # Encoder rounding can push the actual output 0.1–0.5s past the requested
    # -t value (closed-GOP boundary alignment + audio frame boundaries).
    # Reject anything that's clearly wrong (running into the 60s audio).
    assert actual_duration_s <= PREVIEW_WINDOW_S + 0.5, (
        f"final MP4 ran for {actual_duration_s:.2f}s — should be ≤ {PREVIEW_WINDOW_S}s. "
        f"-t cap is not bounding the output. Cmd was: {meta['ffmpeg_cmd']}"
    )
    # And it should be close to 20s, not 5s — i.e. the clamp actually ran the
    # whole 20-second window when the source allows it.
    assert actual_duration_s >= PREVIEW_WINDOW_S - 0.5, (
        f"final MP4 ran for {actual_duration_s:.2f}s — clamp truncated too aggressively"
    )
