"""KRI-734: decoded MP4 evidence, deliberately wrong exports included.

These oracles are authored before the verifier. Only fixture encoding uses
FFmpeg drawtext: the verifier must independently read actual pixels/audio.
"""

from __future__ import annotations

import copy
import io
import json
import shutil
import subprocess
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageFont

from app.kria.device_render import DeviceRenderStatus, make_device_request
from app.kria.recipes_v2 import EditRecipeV2
from app.services import actual_export_verification as measured
from app.services.actual_export_verification import _audio, _frame, verify_actual_export

pytestmark = pytest.mark.timeout(120)

FONT = next(
    (
        p
        for p in [
            Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
            Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        ]
        if p.exists()
    ),
    None,
)
FFMPEG = (
    str(Path("/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg"))
    if Path("/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg").exists()
    else "ffmpeg"
)
FFPROBE = str(Path(FFMPEG).with_name("ffprobe")) if "/" in FFMPEG else "ffprobe"


@pytest.mark.parametrize(
    "canvas,expected",
    [
        ((1080, 1920), (30, 1386, 223, 1505)),
        ((2160, 3840), (60, 2772, 446, 3010)),
        ((1080, 1350), (21.09375, 974.53125, 156.796875, 1058.203125)),
    ],
)
def test_watermark_evidence_bounds_match_native_compositor(canvas, expected):
    # Independent placement oracle: KriaMediaEngine BrandingTests pins these
    # approved tile bounds, including the 4:5 height-constrained scale.
    assert measured._watermark_bounds(*canvas) == pytest.approx(expected)


def test_concurrent_text_evidence_survives_an_oversubscribed_parent(monkeypatch):
    """Actual OCR pixels/processes, no wider deadline or altered recognition bar.

    Failure: inherited OpenMP teams compete under CPU quotas and exhaust the
    five-second read budget. A child resource policy must isolate OCR from an
    oversubscribed parent while still reading the exact displayed word.
    """
    assert FONT is not None, "Text evidence requires the integration font"
    image = Image.new("RGB", (1000, 180), "white")
    ImageDraw.Draw(image).text(
        (20, 40), "Approved", fill="black", font=ImageFont.truetype(str(FONT), 72)
    )
    monkeypatch.setenv("OMP_NUM_THREADS", "64")
    monkeypatch.setenv("OMP_THREAD_LIMIT", "64")
    with ThreadPoolExecutor(max_workers=32) as pool:
        results = list(
            pool.map(lambda _: measured._read_text(image, [(0, 0, 0)], ["Approved"]), range(32))
        )
    assert all(result.complete for result in results)


def ffmpeg(*args):
    subprocess.run(
        [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", *map(str, args)],
        check=True,
        capture_output=True,
        timeout=30,
    )


@pytest.fixture(autouse=True)
def report_missing_export_evidence(monkeypatch):
    """Keep a failed CI measurement actionable without changing its verdict."""
    run = measured._run

    def diagnose(arguments, **kwargs):
        try:
            return run(arguments, **kwargs)
        except subprocess.SubprocessError as exc:
            stderr = getattr(exc, "stderr", b"") or b""
            if isinstance(stderr, bytes):
                stderr = stderr.decode(errors="replace")
            print("Export measurement failed:", type(exc).__name__, arguments, stderr[-2000:])
            raise

    monkeypatch.setattr(measured, "_run", diagnose)


@pytest.fixture(scope="module")
def exports(tmp_path_factory):
    if not shutil.which("ffmpeg") or not shutil.which("tesseract"):
        pytest.fail("Actual-export gate requires FFmpeg and Tesseract; do not skip it")
    root = tmp_path_factory.mktemp("actual_export")
    # Both the picture and sound change over source time, so a wrong window is
    # distinguishable. Independent colours/frequencies distinguish source IDs.
    sources = {}
    for name, color, hz in [("a", "red", 330), ("b", "blue", 770)]:
        path = root / f"{name}.mp4"
        ffmpeg(
            "-f",
            "lavfi",
            "-i",
            f"color={color}:s=640x360:r=30:d=4",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency={hz}:sample_rate=48000:duration=4",
            "-vf",
            "negate=enable='gte(t,2)'",
            "-af",
            "volume='if(gte(t,2),0.4,1)':eval=frame",
            "-c:v",
            "libx264",
            "-preset",
            "fast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            path,
        )
        sources[name] = path
    assets = [
        {"id": name, "relative_path": name, "fingerprint": {"hex": ch * 64, "byte_count": 1}}
        for name, ch in [("a", "a"), ("b", "b"), ("font", "f")]
    ]
    recipe = EditRecipeV2(
        schema_version=2,
        renderer_version="kria-ios-2",
        canvas={"width": 640, "height": 360},
        assets=assets,
        asset_manifest={
            "version": 1,
            "assets": [
                {
                    "kind": "original",
                    "id": name,
                    "media_id": name,
                    "fingerprint": {"sha256": ch * 64, "byte_count": 1},
                }
                for name, ch in [("a", "a"), ("b", "b")]
            ]
            + [
                {
                    "kind": "library",
                    "id": "font",
                    "catalog": "font",
                    "catalog_id": "font.ttf",
                    "generation": "f" * 64,
                    "fingerprint": {"sha256": "f" * 64, "byte_count": 1},
                }
            ],
        },
        tracks=[
            {
                "id": "v",
                "kind": "video",
                "clips": [
                    {
                        "id": name,
                        "source_asset_id": name,
                        "source_start": 0,
                        "source_duration": 1.5,
                        "timeline_start": start,
                        "rate": 1,
                    }
                    for name, start in [("a", 0), ("b", 1.5)]
                ],
            }
        ],
        text_layers=[
            {
                "id": "title",
                "start": 0,
                "end": 1.5,
                "anchor_x": 45,
                "anchor_y": 50,
                "rotation_degrees": 0,
                "effect": "none",
                "runs": [
                    {
                        "text": "Approved",
                        "font_asset_id": "font",
                        "font_size": 32,
                        "x": 45,
                        "baseline_y": 65,
                        "shaped": True,
                        "letter_spacing": 0,
                        "stroke_width": 0,
                        "stroke": {"red": 0, "green": 0, "blue": 0, "alpha": 0},
                        "fill": {"red": 1, "green": 1, "blue": 1, "alpha": 1},
                    }
                ],
            }
        ],
    )
    # The text schema requires a pinned font even though verification never paints it.
    return root, sources, recipe


def encode(
    root,
    sources,
    *,
    order=("a", "b"),
    start=0,
    title=True,
    title_text="Approved",
    title_window=(0, 1.5),
    title_y=35,
    gain=1,
    speed=1,
    tail=0,
):
    path = root / f"{uuid.uuid4()}.mp4"
    graph = (
        f"[0:v]trim=start={start}:duration=1.5,setpts=(PTS-STARTPTS)/{speed}[v0];"
        "[1:v]trim=duration=1.5,setpts=PTS-STARTPTS[v1];"
        f"[0:a]atrim=start={start}:duration=1.5,asetpts=PTS-STARTPTS,atempo={speed},volume={gain}[a0];"
        f"[1:a]atrim=duration=1.5,asetpts=PTS-STARTPTS,volume={gain}[a1];"
        "[v0][a0][v1][a1]concat=n=2:v=1:a=1[v][a]"
    )
    picture, audio = "v", "a"
    if tail:
        graph += (
            f";[v]fps=30,tpad=stop_mode=clone:stop_duration={tail}[tail];"
            f"[a]apad=pad_dur={tail}[padded]"
        )
        picture, audio = "tail", "padded"
    if title:
        assert FONT is not None, "Actual-export fixture needs the production font bundle"
        graph += (
            f";[{picture}]drawtext=fontfile={FONT}:"
            f"text={title_text}:fontsize=32:fontcolor=white:x=45:y={title_y}:"
            f"enable='between(t,{title_window[0]},{title_window[1]})'[t]"
        )
    ffmpeg(
        "-i",
        sources[order[0]],
        "-i",
        sources[order[1]],
        "-filter_complex",
        graph,
        "-map",
        "[t]" if title else f"[{picture}]",
        "-map",
        f"[{audio}]",
        "-r",
        30,
        "-c:v",
        "libx264",
        "-preset",
        "fast",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        path,
    )
    return path


def status(recipe):
    return DeviceRenderStatus(
        phase="syncing",
        request=make_device_request(
            job_id=uuid.UUID(int=734), variant_id="first", revision=1, recipe=recipe
        ),
    )


def test_decoded_picture_audio_and_text_match_the_approved_windows(exports):
    root, sources, recipe = exports
    report = verify_actual_export(encode(root, sources), status(recipe), sources)
    assert report["status"] == "checked", report
    assert {c["kind"] for c in report["checks"]} >= {"picture", "audio", "text", "duration"}


@pytest.mark.parametrize(
    "mutation,options,kind",
    [
        ("source", {"order": ("b", "b")}, "picture"),
        ("order", {"order": ("b", "a")}, "picture"),
        ("window", {"start": 2}, "picture"),
        ("text", {"title": False}, "text"),
        ("wrong_copy", {"title_text": "Rejected"}, "text"),
        ("missing_letter", {"title_text": "pproved"}, "text"),
        ("extra_punctuation", {"title_text": "Approved?"}, "text"),
        ("position", {"title_y": 200}, "text"),
        ("text_time", {"title_window": (0, 0.3)}, "text"),
        ("extra_text_time", {"title_window": (0, 3)}, "text"),
        ("double_audio", {"gain": 2}, "audio"),
        ("mute_audio", {"gain": 0}, "audio"),
        ("rate", {"speed": 2}, "duration"),
    ],
)
def test_wrong_export_never_counts_as_checked(exports, mutation, options, kind):
    root, sources, recipe = exports
    report = verify_actual_export(encode(root, sources, **options), status(recipe), sources)
    assert report["status"] == "rejected", (mutation, report)
    assert any(c["kind"] == kind and c["status"] == "failed" for c in report["checks"]), report


def test_unavailable_source_and_unsupported_treatment_are_explicit(exports):
    root, sources, recipe = exports
    file = encode(root, sources)
    missing = verify_actual_export(file, status(recipe), {})
    assert missing["status"] == "partial"
    changed = copy.deepcopy(recipe)
    changed.tracks[0].clips[0].rate = 0.5
    assert verify_actual_export(file, status(changed), sources)["status"] == "unsupported"


@pytest.mark.parametrize("title_end,expected", [(3, "checked"), (4.6, "rejected")])
def test_title_ending_at_edit_boundary_cannot_leak_into_the_brand_tail(
    exports, title_end, expected
):
    root, sources, recipe = exports
    recipe = copy.deepcopy(recipe)
    recipe.text_layers[0].end = 3
    file = encode(root, sources, tail=1.6, title_window=(0, title_end))
    # A long audio stream alone does not prove the fixture contains tail frames.
    video = next(s for s in measured._probe(file)["streams"] if s["codec_type"] == "video")
    assert float(video["duration"]) == pytest.approx(4.6, abs=1 / 30)
    report = verify_actual_export(file, status(recipe), sources, brand_tail="standard")
    assert report["status"] == expected, {
        "status": report["status"],
        "incomplete_checks": [c for c in report["checks"] if c["status"] != "passed"],
    }
    assert any(
        c["kind"] == "text" and c.get("expected_visible") is False for c in report["checks"]
    ), report


@pytest.mark.parametrize("order,expected", [(("a", "b"), "checked"), (("b", "a"), "rejected")])
def test_dark_footage_cannot_pass_just_because_every_pixel_is_near_black(exports, order, expected):
    root, sources, recipe = exports
    recipe = copy.deepcopy(recipe)
    recipe.text_layers = []
    recipe.audio.original_volume = 0
    dark = {}
    for key, source in sources.items():
        dark[key] = root / f"dark-{key}.mp4"
        ffmpeg(
            "-i",
            source,
            "-vf",
            "colorchannelmixer=rr=0.04:gg=0.04:bb=0.04",
            "-c:v",
            "libx264",
            "-preset",
            "fast",
            "-c:a",
            "copy",
            dark[key],
        )
    file = encode(root, dark, order=order, title=False, gain=0)
    report = verify_actual_export(file, status(recipe), dark)
    assert report["status"] == expected, report
    if expected == "rejected":
        assert any(c["kind"] == "picture" and c["status"] == "failed" for c in report["checks"]), (
            report
        )


def test_uploaded_media_cannot_make_the_verifier_fetch_a_playlist_url(exports):
    """Real demux boundary: a file named MP4 is still untrusted input bytes."""
    root, sources, recipe = exports
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            self.send_error(404)

        def log_message(self, *args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        runner = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        runner.start()
        try:
            playlist = root / "not-a-real-mp4.mp4"
            playlist.write_text(
                "#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:1\n"
                "#EXT-X-MEDIA-SEQUENCE:0\n#EXTINF:1.0,\n"
                f"http://127.0.0.1:{server.server_port}/segment.ts\n#EXT-X-ENDLIST\n"
            )
            report = verify_actual_export(playlist, status(recipe), sources)
            assert report["status"] != "checked"
            assert requests == [], "Uploaded media must never cause decoder network requests"
        finally:
            server.shutdown()
            runner.join(timeout=1)


def test_matching_codecs_and_pixels_in_a_non_mp4_container_are_not_ready(exports):
    root, sources, recipe = exports
    recipe = copy.deepcopy(recipe)
    recipe.audio.original_volume = 0
    candidate = encode(root, sources, gain=0)
    disguised = root / "matroska-named-mp4.mp4"
    ffmpeg("-i", candidate, "-c", "copy", "-f", "matroska", disguised)
    report = verify_actual_export(disguised, status(recipe), sources)
    assert report["status"] != "checked", "A format the iPhone cannot play must never be ready"


@pytest.mark.parametrize(
    "timing", ["cfr", "vfr", "rotated", "offset", "coarse_timebase", "fine_timebase"]
)
def test_decoder_optimization_preserves_reference_pixels_and_pcm(tmp_path, timing, monkeypatch):
    """Performance changes must preserve the independent original decoder output.

    Reference oracle authored before optimization; fine-clock coverage extends
    it before the numpy-clock fix. Process reuse must preserve pixels and PCM.
    """
    source = tmp_path / "source.mp4"
    ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "testsrc2=s=160x96:r=30:d=12",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=477:sample_rate=48000:duration=12",
        "-c:v",
        "libx264",
        "-preset",
        "fast",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        source,
    )
    if timing == "vfr":
        changed = tmp_path / "vfr.mp4"
        ffmpeg(
            "-i",
            source,
            "-vf",
            "setpts='if(lt(N,30),N,30+(N-30)*2)/(30*TB)'",
            "-fps_mode",
            "vfr",
            "-c:v",
            "libx264",
            "-preset",
            "fast",
            "-c:a",
            "copy",
            changed,
        )
        source = changed
    if timing == "rotated":
        changed = tmp_path / "rotated.mp4"
        ffmpeg("-i", source, "-c", "copy", "-metadata:s:v:0", "rotate=90", changed)
        source = changed
    if timing == "offset":
        changed = tmp_path / "offset.mp4"
        ffmpeg("-i", source, "-c", "copy", "-output_ts_offset", "1.25", changed)
        source = changed
    if timing == "coarse_timebase":
        changed = tmp_path / "coarse.mp4"
        ffmpeg("-i", source, "-c", "copy", "-video_track_timescale", "1000", changed)
        source = changed
    if timing == "fine_timebase":
        changed = tmp_path / "fine.mp4"
        ffmpeg("-i", source, "-c", "copy", "-video_track_timescale", "1000000", changed)
        source = changed
    times = (
        0,
        0.0005,
        0.1,
        0.1005,
        # The original np.linspace caller rounds a numpy scalar before seek.
        # Converting it to Python float first changes half-microsecond ties.
        np.float64(0.1000005),
        0.333333,
        0.333334,
        1.234567,
        1.5,
        5.333333,
        8.333334,
        9.234567,
        10.1,
    )

    def no_fallback(*args, **kwargs):
        pytest.fail("The supported fixture must use batch decoding, not silently fall back")

    with monkeypatch.context() as batch_only:
        batch_only.setattr(measured, "_frame", no_fallback)
        batches = measured._frames(source, times, size=(96, 54))
        window = measured._frames(source, times[4:], size=(96, 54))
        regions = measured._frames(source, times, size=(66, 54), region=(7, 9, 73, 63))
    assert all(
        np.array_equal(np.asarray(a), np.asarray(b))
        for a, b in zip(batches[4:], window, strict=True)
    ), "A later source window must preserve the same sample clock"
    for at, batched, region_frame in zip(times, batches, regions, strict=True):
        # These explicit commands retain the pre-optimization sampling behavior.
        # They do not call the production decoder helper or inspect its flags.
        raw = subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-ss",
                str(round(at, 6)),
                "-protocol_whitelist",
                "file,pipe",
                "-f",
                "mov",
                "-i",
                str(source),
                "-frames:v",
                "1",
                "-vf",
                "scale=96:54:force_original_aspect_ratio=increase,crop=96:54",
                "-f",
                "image2pipe",
                "-vcodec",
                "png",
                "-",
            ],
            check=True,
            capture_output=True,
            timeout=20,
        ).stdout
        reference = np.asarray(Image.open(io.BytesIO(raw)).convert("RGB"))
        assert np.array_equal(np.asarray(_frame(source, at, size=(96, 54))), reference)
        assert np.array_equal(np.asarray(batched), reference), f"{timing} at {at}"
        full = subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-ss",
                str(round(at, 6)),
                "-protocol_whitelist",
                "file,pipe",
                "-f",
                "mov",
                "-i",
                str(source),
                "-frames:v",
                "1",
                "-f",
                "image2pipe",
                "-vcodec",
                "png",
                "-",
            ],
            check=True,
            capture_output=True,
            timeout=20,
        ).stdout
        region = (7, 9, 73, 63)
        cropped = Image.open(io.BytesIO(full)).convert("RGB").crop(region)
        assert np.array_equal(np.asarray(region_frame), np.asarray(cropped))
        assert np.array_equal(np.asarray(_frame(source, at, region=region)), np.asarray(cropped))
        pcm = subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-ss",
                str(at),
                "-protocol_whitelist",
                "file,pipe",
                "-f",
                "mov",
                "-i",
                str(source),
                "-t",
                "0.75",
                "-vn",
                "-ac",
                "1",
                "-ar",
                "8000",
                "-f",
                "f32le",
                "-",
            ],
            check=True,
            capture_output=True,
            timeout=20,
        ).stdout
        assert np.array_equal(_audio(source, at, 0.75), np.frombuffer(pcm, dtype="<f4"))


@pytest.mark.parametrize(
    "treatment,expected",
    [("duplicate", "checked"), ("left_muted", "rejected"), ("double", "rejected")],
)
def test_native_mono_to_stereo_preserves_samples_without_hiding_channel_errors(
    exports, treatment, expected
):
    root, sources, recipe = exports
    mono = encode(root, sources)
    output = root / f"channels-{treatment}.mp4"
    audio = {
        "duplicate": "pan=stereo|c0=c0|c1=c0",
        "left_muted": "pan=stereo|c0=0*c0|c1=c0",
        "double": "volume=2,pan=stereo|c0=c0|c1=c0",
    }[treatment]
    ffmpeg("-i", mono, "-c:v", "copy", "-af", audio, "-c:a", "aac", output)
    report = verify_actual_export(output, status(recipe), sources)
    assert report["status"] == expected, report
    if expected == "rejected":
        assert any(
            value["kind"] == "audio" and value["status"] == "failed" for value in report["checks"]
        )


def test_silent_sources_accept_silent_export_and_reject_unrequested_audio(exports):
    root, sources, recipe = exports
    audible = encode(root, sources)
    silent = root / "silent-export.mp4"
    ffmpeg("-i", audible, "-c:v", "copy", "-an", silent)
    silent_sources = {}
    for key, source in sources.items():
        path = root / f"silent-source-{key}.mp4"
        ffmpeg("-i", source, "-c:v", "copy", "-an", path)
        silent_sources[key] = path
    report = verify_actual_export(silent, status(recipe), silent_sources)
    assert report["status"] == "checked", {
        "status": report["status"],
        "incomplete_checks": [c for c in report["checks"] if c["status"] != "passed"],
    }
    wrong = verify_actual_export(audible, status(recipe), silent_sources)
    assert wrong["status"] == "rejected", wrong
    assert any(
        value["kind"] == "audio" and value["status"] == "failed" for value in wrong["checks"]
    )


# --- KRI-810: exact displayed-frame clock and overlay-isolated text evidence ---
#
# Failure modes written before the verifier change (plan F3/F4/F6-F9/F12):
# - a non-frame-grid source_start (1.508 s at 30 fps) makes the last sample of a
#   clip compare the neighbour of the frame the renderer displayed;
# - a one-frame display tolerance must not compound into a wider window, so a
#   moving picture shifted by one or two whole frames still fails;
# - white text with a stroke and a blurred shadow over a white/high-key picture
#   merges into the background for the colour-threshold OCR view;
# - isolating the overlay must never read words that are not in the pixels:
#   a missing word, wrong casing or missing layer still fails exact copy;
# - without a decodable background the new evidence cannot accept anything.

CLOCK_START = 1.508
CLOCK_DURATION = 2.812


def _single_clip(recipe, *, start, duration, text_layers=()):
    recipe = copy.deepcopy(recipe)
    clip = recipe.tracks[0].clips[0]
    clip.source_start = start
    clip.source_duration = duration
    recipe.tracks[0].clips = [clip]
    recipe.text_layers = list(text_layers)
    recipe.audio.original_volume = 0
    return recipe


@pytest.fixture(scope="module")
def moving(exports):
    """Every frame differs: diagonal stripes advance 23 px per 30 fps frame."""
    root, _, _ = exports
    path = root / "moving.mp4"
    ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "nullsrc=s=640x360:r=30:d=6,"
        r"geq=lum='255*lt(mod(X+Y-N*23\,150)\,75)':cb=128:cr=128,format=yuv420p",
        "-c:v",
        "libx264",
        "-preset",
        "fast",
        "-pix_fmt",
        "yuv420p",
        path,
    )
    return path


def _cut(root, source, *, start, duration, fps=30):
    path = root / f"{uuid.uuid4()}.mp4"
    ffmpeg(
        "-i",
        source,
        "-filter_complex",
        f"[0:v]trim=start={start}:duration={duration},setpts=PTS-STARTPTS[v]",
        "-map",
        "[v]",
        "-r",
        fps,
        "-c:v",
        "libx264",
        "-preset",
        "fast",
        "-pix_fmt",
        "yuv420p",
        path,
    )
    return path


def _composited(root, source, *, start, duration, fps=30, next_frame_at=None):
    """Render a cut the way a compositor does, independent of FFmpeg's resampler.

    Output frame n shows the last source frame presented at/before
    start + n/fps (held frames repeat). FFmpeg's trim + -r conversion picks a
    different frame across versions on VFR input, so it cannot stand in for
    the phone here.
    """

    def probe(entries):
        return subprocess.run(
            [
                FFPROBE,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                entries,
                "-of",
                "csv=p=0",
                str(source),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout

    width, height = (int(value) for value in probe("stream=width,height").strip().split(","))
    clock = [float(line.split(",")[0]) for line in probe("frame=pts_time").split()]
    raw = subprocess.run(
        [
            FFMPEG,
            "-v",
            "error",
            "-i",
            str(source),
            "-fps_mode",
            "passthrough",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "-",
        ],
        check=True,
        capture_output=True,
        timeout=60,
    ).stdout
    size = width * height * 3
    assert len(raw) == size * len(clock) and clock == sorted(clock)
    chosen = [
        max(i for i, t in enumerate(clock) if t <= start + n / fps + 1e-6)
        for n in range(round(duration * fps))
    ]
    if next_frame_at is not None:
        chosen[next_frame_at] = min(chosen[next_frame_at] + 1, len(clock) - 1)
    path = root / f"{uuid.uuid4()}.mp4"
    subprocess.run(
        [
            FFMPEG,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "-s",
            f"{width}x{height}",
            "-r",
            str(fps),
            "-i",
            "-",
            "-c:v",
            "libx264",
            "-preset",
            "fast",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ],
        input=b"".join(raw[i * size : (i + 1) * size] for i in chosen),
        check=True,
        capture_output=True,
        timeout=60,
    )
    return path


@pytest.mark.parametrize(
    "shift_frames,expected", [(0, "checked"), (1, "rejected"), (-1, "rejected")]
)
def test_on_grid_whole_clip_frame_shift_never_counts_as_checked(
    exports, moving, shift_frames, expected
):
    """KRI-831: independent frame-clock exports, not a widened pixel threshold."""
    root, _, recipe = exports
    recipe = _single_clip(recipe, start=1.5, duration=CLOCK_DURATION)
    output = _composited(root, moving, start=1.5 + shift_frames / 30, duration=CLOCK_DURATION)
    report = verify_actual_export(output, status(recipe), {"a": moving})
    (root / f"kri831-on-grid-{shift_frames}.json").write_text(
        json.dumps({"expected": expected, "output": str(output), "report": report}, indent=2)
    )
    assert report["status"] == expected, report
    pictures = [check for check in report["checks"] if check["kind"] == "picture"]
    assert pictures
    if shift_frames:
        assert any(check["status"] == "failed" for check in pictures), report
    # KRI-1042: the stored per-sample evidence names the source frame the export
    # really showed (the previous one included), while the verdict above is unchanged.
    (picture,) = pictures
    shown = {0: "displayed", 1: "next", -1: "previous"}[shift_frames]
    assert len(picture["sample_evidence"]) == picture["samples"], picture
    for sample in picture["sample_evidence"]:
        assert sample[f"ssim_{shown}"] >= 0.95, picture["sample_evidence"]
        assert all(
            sample[f"ssim_{frame}"] < 0.95
            for frame in ("previous", "displayed", "next")
            if frame != shown and f"ssim_{frame}" in sample
        ), picture["sample_evidence"]


@pytest.mark.parametrize("order,expected", [(("a", "b"), "checked"), (("b", "a"), "rejected")])
def test_unknown_source_frame_clock_keeps_the_nominal_verdict_and_its_evidence(
    exports, monkeypatch, order, expected
):
    """KRI-1042: per-sample evidence never turns the nominal-clock fallback into missing."""
    root, sources, recipe = exports
    output = encode(root, sources, order=order)

    def unknown_clock(*args, **kwargs):
        raise ValueError("missing_frame_clock")

    monkeypatch.setattr(measured, "_frame_clock", unknown_clock)
    report = verify_actual_export(output, status(recipe), sources)
    assert report["status"] == expected, report
    pictures = [c for c in report["checks"] if c["kind"] == "picture"]
    assert {c["picture_clock"] for c in pictures} == {"nominal-v1"}
    for picture in pictures:
        assert len(picture["sample_evidence"]) == picture["samples"]
        assert all(
            set(sample) == {"ssim_displayed", "error_displayed"}
            for sample in picture["sample_evidence"]
        )


@pytest.mark.parametrize("displayed_ssim,uses_profile", [(0.94996, True), (0.95004, False)])
def test_picture_evidence_preserves_profile_threshold_decisions(
    exports, moving, monkeypatch, displayed_ssim, uses_profile
):
    from app.services.export_verification_admin import PictureSample

    root, _, recipe = exports
    recipe = _single_clip(recipe, start=1.5, duration=CLOCK_DURATION)
    output = _composited(root, moving, start=1.5, duration=CLOCK_DURATION)
    picture_scores = measured._picture_scores

    def threshold_scores(reference, actual, mask):
        _, score = picture_scores(reference, actual, mask)
        return np.float64(0.023456), np.float64(displayed_ssim if score >= 0.95 else 0.7)

    monkeypatch.setattr(measured, "_picture_scores", threshold_scores)
    report = json.loads(json.dumps(verify_actual_export(output, status(recipe), {"a": moving})))
    assert report["status"] == "checked", report
    (picture,) = [check for check in report["checks"] if check["kind"] == "picture"]
    assert picture["profile_samples"] == (picture["samples"] if uses_profile else 0), picture
    below_bar = [sample for sample in picture["sample_evidence"] if sample["ssim_displayed"] < 0.95]
    assert picture["profile_samples"] == len(below_bar), picture
    for sample in picture["sample_evidence"]:
        validated = PictureSample.model_validate(sample)
        assert validated.ssim_displayed == displayed_ssim
        assert validated.error_displayed == 0.023456


def test_on_grid_single_neighbour_frame_remains_allowed(exports, moving):
    """KRI-831: retain isolated resampling tolerance, without shifting the whole clip."""
    root, _, recipe = exports
    recipe = _single_clip(recipe, start=1.5, duration=CLOCK_DURATION)
    output = _composited(root, moving, start=1.5, duration=CLOCK_DURATION, next_frame_at=3)
    report = verify_actual_export(output, status(recipe), {"a": moving})
    (root / "kri831-single-neighbour.json").write_text(
        json.dumps({"expected": "checked", "output": str(output), "report": report}, indent=2)
    )
    assert report["status"] == "checked", report
    pictures = [check for check in report["checks"] if check["kind"] == "picture"]
    assert len(pictures) == 1
    assert pictures[0]["neighbour_samples"] == 1


def test_on_grid_original_export_keeps_resampled_proxy_tolerance(exports, moving):
    """A correct original clock need not share its resampled proxy's sample grid."""
    root, _, recipe = exports
    proxy = root / "moving-proxy-2997.mp4"
    ffmpeg(
        "-i",
        moving,
        "-r",
        "30000/1001",
        "-fps_mode",
        "cfr",
        "-c:v",
        "libx264",
        "-preset",
        "fast",
        "-pix_fmt",
        "yuv420p",
        proxy,
    )
    recipe = _single_clip(recipe, start=0, duration=CLOCK_DURATION)
    output = _composited(root, moving, start=0, duration=CLOCK_DURATION)
    request = status(recipe)
    report = verify_actual_export(output, request, {"a": proxy})
    (root / "kri831-resampled-proxy.json").write_text(
        json.dumps(
            {
                "expected": "checked",
                "output": str(output),
                "source_proxy": str(proxy),
                "request": request.model_dump(mode="json"),
                "report": report,
            },
            indent=2,
        )
    )
    assert report["status"] == "checked", report


@pytest.mark.parametrize(
    "shift_frames,expected",
    [(0, "checked"), (1, "rejected"), (2, "rejected"), (-2, "rejected")],
)
def test_off_grid_source_start_compares_the_displayed_frame_without_widening_the_window(
    exports, moving, shift_frames, expected
):
    root, _, recipe = exports
    recipe = _single_clip(recipe, start=CLOCK_START, duration=CLOCK_DURATION)
    output = _cut(root, moving, start=CLOCK_START + shift_frames / 30, duration=CLOCK_DURATION)
    report = verify_actual_export(output, status(recipe), {"a": moving})
    assert report["status"] == expected, report
    picture = [c for c in report["checks"] if c["kind"] == "picture"]
    assert picture and all(c["picture_clock"] == "output-pts-bracketed-v1" for c in picture)
    if expected == "rejected":
        assert any(c["status"] == "failed" for c in picture), report


def test_variable_frame_rate_proxy_with_held_frames_matches_its_cfr_export(exports, moving):
    """A proxy that holds frames (15 fps stretch) against the 30 fps render of it."""
    root, _, recipe = exports
    proxy = root / "moving-vfr.mp4"
    ffmpeg(
        "-i",
        moving,
        "-vf",
        "setpts='if(lt(N,45),N,45+(N-45)*2)/(30*TB)'",
        "-fps_mode",
        "vfr",
        "-t",
        "6",
        "-c:v",
        "libx264",
        "-preset",
        "fast",
        "-pix_fmt",
        "yuv420p",
        proxy,
    )
    recipe = _single_clip(recipe, start=CLOCK_START, duration=CLOCK_DURATION)
    output = _composited(root, proxy, start=CLOCK_START, duration=CLOCK_DURATION)
    report = verify_actual_export(output, status(recipe), {"a": proxy})
    assert report["status"] == "checked", [c for c in report["checks"] if c["status"] != "passed"]


BRIGHT_TITLE = "Ancient City of Troy"


@pytest.fixture(scope="module")
def high_key(exports):
    """Low-resolution proxy of a white sky with lit window strips and dark frames."""
    root, _, _ = exports
    path = root / "high-key.mp4"
    ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "nullsrc=s=320x180:r=30:d=4,"
        r"geq=lum='if(lt(mod(X+N/3\,48)\,10)\,70\,if(lt(mod(X+N/3\,48)\,16)\,252\,"
        r"236-Y/12))':cb=128:cr=126,format=yuv420p",
        "-c:v",
        "libx264",
        "-preset",
        "fast",
        "-pix_fmt",
        "yuv420p",
        path,
    )
    return path


def _title_overlay(root, text):
    """Fixture-only painting: white fill, black stroke, blurred drop shadow."""
    from PIL import ImageFilter  # noqa: PLC0415

    assert FONT is not None
    font = ImageFont.truetype(str(FONT), 40)
    shadow = Image.new("L", (640, 360), 0)
    ImageDraw.Draw(shadow).text((60, 128), text, fill=160, font=font, anchor="ls")
    shadow = shadow.filter(ImageFilter.GaussianBlur(6))
    overlay = Image.new("RGBA", (640, 360), (0, 0, 0, 0))
    overlay.putalpha(shadow)
    ImageDraw.Draw(overlay).text(
        (60, 125),
        text,
        fill=(255, 255, 255, 255),
        font=font,
        anchor="ls",
        stroke_width=1,
        stroke_fill=(0, 0, 0, 230),
    )
    path = root / f"title-{uuid.uuid4()}.png"
    overlay.save(path)
    return path


def _bright_recipe(recipe):
    layer = copy.deepcopy(recipe.text_layers[0])
    layer.start, layer.end = 0, 2
    run = layer.runs[0]
    run.text, run.font_size, run.x, run.baseline_y = BRIGHT_TITLE, 40, 60, 125
    return _single_clip(recipe, start=0.5, duration=2.5, text_layers=[layer])


def _bright_export(root, source, text):
    path = root / f"{uuid.uuid4()}.mp4"
    inputs = ["-i", source]
    graph = "[0:v]trim=start=0.5:duration=2.5,setpts=PTS-STARTPTS,scale=640:360[v]"
    if text is not None:
        inputs += ["-loop", "1", "-i", _title_overlay(root, text)]
        graph += ";[v][1:v]overlay=0:0:shortest=1:enable='between(t,0,2)'[v]"
    ffmpeg(
        *inputs,
        "-filter_complex",
        graph,
        "-map",
        "[v]",
        "-t",
        "2.5",
        "-r",
        30,
        "-c:v",
        "libx264",
        "-preset",
        "fast",
        "-pix_fmt",
        "yuv420p",
        path,
    )
    return path


@pytest.mark.parametrize(
    "rendered,expected",
    [
        (BRIGHT_TITLE, "checked"),
        ("Ancient City of", "rejected"),
        ("Ancient City of troy", "rejected"),
        ("Ancient Town of Troy", "rejected"),
        (None, "rejected"),
    ],
)
def test_white_title_over_a_high_key_picture_is_read_without_painting_the_expected_copy(
    exports, high_key, rendered, expected
):
    root, _, recipe = exports
    recipe = _bright_recipe(recipe)
    report = verify_actual_export(
        _bright_export(root, high_key, rendered), status(recipe), {"a": high_key}
    )
    assert report["status"] == expected, report
    text = [c for c in report["checks"] if c["kind"] == "text"]
    if expected == "rejected":
        assert any(c["status"] == "failed" and c["expected_visible"] for c in text), report
    else:
        assert all(c["status"] == "passed" for c in report["checks"]), report


def test_overlay_isolation_cannot_accept_text_without_a_decoded_background(
    exports, high_key, monkeypatch
):
    root, _, recipe = exports
    recipe = _bright_recipe(recipe)
    output = _bright_export(root, high_key, BRIGHT_TITLE)
    frames = measured._frames

    def no_background(*args, **kwargs):
        if kwargs.get("canvas") is not None:
            raise subprocess.CalledProcessError(1, "ffmpeg")
        return frames(*args, **kwargs)

    monkeypatch.setattr(measured, "_frames", no_background)
    report = verify_actual_export(output, status(recipe), {"a": high_key})
    assert report["status"] != "checked", report


# --- KRI-1042: copy read once per layer, glyphs shown in every visible sample ---
#
# Failure modes written before the verifier change. A correct export must pass when:
# - a white "shirt" sits behind the title's first letters in the early samples, so
#   no view reads the whole copy there (the Lisbon export, job 3daccc8c);
# - the copy contains a straight quote mark (Tesseract TSV is not CSV-quoted);
# - a short letter-like mark in the picture adds one stray token to every read;
# - a second layer replaces the first in the same place.
# A wrong export must still fail when the text is missing, has a wrong or extra
# word, appears late, sits outside its region, or is still shown (readable or not)
# after the layer ends.

PLAIN_TITLE = "Ancient City of Troy"


def _crowd_source(root, name, *, shirt=False, mark=None):
    """Dark moving texture; an optional white shirt and a static letter-like mark.

    The shirt covers the title's first letters and the read area's left border for
    source time < 2 s and from 2.55 s (export time < 1.5 s and from 2.05 s).
    """
    base = "60+25*sin(X/9+T*2)*cos(Y/13)"
    lum = (
        f"if(lt(X\\,100)*between(Y\\,40\\,170)*(lt(T\\,2)+gte(T\\,2.55))\\,245\\,{base})"
        if shirt
        else base
    )
    graph = f"nullsrc=s=640x360:r=30:d=4,geq=lum='{lum}':cb=128:cr=128,format=yuv420p"
    if mark:
        graph += f",drawtext=fontfile={FONT}:text={mark}:fontsize=30:fontcolor=white:x=560:y=103"
    path = root / f"{name}.mp4"
    ffmpeg("-f", "lavfi", "-i", graph, "-c:v", "libx264", "-preset", "fast", path)
    return path


@pytest.fixture(scope="module")
def crowd(exports):
    root, _, _ = exports
    assert FONT is not None
    return {
        "plain": _crowd_source(root, "crowd-plain"),
        "shirt": _crowd_source(root, "crowd-shirt", shirt=True),
        "marked": _crowd_source(root, "crowd-marked", mark="x"),
    }


def _plain_title(root, text, *, baseline=125):
    """Fixture-only painting: plain white fill, no stroke or shadow (as the phone draws)."""
    overlay = Image.new("RGBA", (640, 360), (0, 0, 0, 0))
    ImageDraw.Draw(overlay).text(
        (60, baseline),
        text,
        fill=(255, 255, 255, 255),
        font=ImageFont.truetype(str(FONT), 40),
        anchor="ls",
    )
    path = root / f"plain-title-{uuid.uuid4()}.png"
    overlay.save(path)
    return path


def _titled_export(root, source, titles):
    """Cut source 0.5-3.0 s and paint each (text, start, end[, baseline]) title."""
    path = root / f"{uuid.uuid4()}.mp4"
    inputs = ["-i", source]
    graph = "[0:v]trim=start=0.5:duration=2.5,setpts=PTS-STARTPTS[v0]"
    for index, (text, start, end, *baseline) in enumerate(titles, start=1):
        inputs += ["-loop", "1", "-i", _plain_title(root, text, baseline=(baseline or [125])[0])]
        graph += (
            f";[v{index - 1}][{index}:v]overlay=0:0:shortest=1:"
            f"enable='between(t,{start},{end})'[v{index}]"
        )
    ffmpeg(
        *inputs,
        "-filter_complex",
        graph,
        "-map",
        f"[v{len(titles)}]",
        "-t",
        "2.5",
        "-r",
        30,
        "-c:v",
        "libx264",
        "-preset",
        "fast",
        "-pix_fmt",
        "yuv420p",
        path,
    )
    return path


def _titled_recipe(recipe, layers):
    """One clip (source 0.5-3.0 s) with each (id, copy, start, end) layer at (60, 125)."""
    template = recipe.text_layers[0]
    built = []
    for layer_id, text, start, end in layers:
        layer = copy.deepcopy(template)
        layer.id, layer.start, layer.end = layer_id, start, end
        run = layer.runs[0]
        run.text, run.font_size, run.x, run.baseline_y = text, 40, 60, 125
        built.append(layer)
    return _single_clip(recipe, start=0.5, duration=2.5, text_layers=built)


def _texts(report):
    return [c for c in report["checks"] if c["kind"] == "text"]


@pytest.mark.parametrize(
    "case,source,copy_text,titles,proof",
    [
        ("plain", "plain", PLAIN_TITLE, [(PLAIN_TITLE, 0, 2)], "copy"),
        ("shirt_behind_first_letters", "shirt", PLAIN_TITLE, [(PLAIN_TITLE, 0, 2)], "presence"),
        (
            "straight_quote",
            "plain",
            'Ancient "City" of Troy',
            [('Ancient "City" of Troy', 0, 2)],
            "copy",
        ),
        ("stray_mark", "marked", PLAIN_TITLE, [(PLAIN_TITLE, 0, 2)], "copy"),
    ],
)
def test_correct_titles_pass_when_the_copy_is_read_once_and_drawn_throughout(
    exports, crowd, case, source, copy_text, titles, proof
):
    root, _, recipe = exports
    recipe = _titled_recipe(recipe, [("title", copy_text, 0, 2)])
    output = _titled_export(root, crowd[source], titles)
    report = verify_actual_export(output, status(recipe), {"a": crowd[source]})
    (root / f"kri1042-{case}.json").write_text(json.dumps(report, indent=2))
    assert report["status"] == "checked", (case, _texts(report))
    visible = [c for c in _texts(report) if c["expected_visible"]]
    assert len(visible) == 3 and any(c["proof"] == "copy" for c in visible), visible
    assert any(c["proof"] == proof for c in visible), (case, visible)
    if case == "stray_mark":
        assert any(c["extra_tokens"] == 1 for c in visible), visible


@pytest.mark.parametrize("second", ["Ancient City of Rome", f"{PLAIN_TITLE} 2"])
def test_a_second_layer_in_the_same_place_is_not_the_first_layer_lingering(exports, crowd, second):
    """The second copy shares most words, or is the first plus one short token."""
    root, _, recipe = exports
    recipe = _titled_recipe(recipe, [("first", PLAIN_TITLE, 0, 1), ("second", second, 1, 2)])
    output = _titled_export(root, crowd["plain"], [(PLAIN_TITLE, 0, 0.99), (second, 1, 2)])
    report = verify_actual_export(output, status(recipe), {"a": crowd["plain"]})
    assert report["status"] == "checked", _texts(report)
    assert {(c["layer_id"], c["expected_visible"]) for c in _texts(report)} >= {
        ("first", False),
        ("second", False),
    }


@pytest.mark.parametrize(
    "case,source,titles,failing",
    [
        ("missing", "plain", [], "visible"),
        ("wrong_word", "plain", [("Ancient Town of Troy", 0, 2)], "visible"),
        ("extra_word", "plain", [("Ancient City of old Troy", 0, 2)], "visible"),
        ("late", "plain", [(PLAIN_TITLE, 1.4, 2)], "visible"),
        ("outside_region", "plain", [(PLAIN_TITLE, 0, 2, 300)], "visible"),
        ("lingers_readable", "plain", [(PLAIN_TITLE, 0, 2.5)], "hidden"),
        ("lingers_behind_shirt", "shirt", [(PLAIN_TITLE, 0, 2.5)], "hidden"),
    ],
)
def test_wrong_titles_still_fail_the_relaxed_text_check(
    exports, crowd, case, source, titles, failing
):
    root, _, recipe = exports
    recipe = _titled_recipe(recipe, [("title", PLAIN_TITLE, 0, 2)])
    output = _titled_export(root, crowd[source], titles)
    report = verify_actual_export(output, status(recipe), {"a": crowd[source]})
    (root / f"kri1042-negative-{case}.json").write_text(json.dumps(report, indent=2))
    assert report["status"] == "rejected", (case, _texts(report))
    assert any(
        c["status"] == "failed" and c["expected_visible"] == (failing == "visible")
        for c in _texts(report)
    ), (case, _texts(report))


# --- KRI-1042: frame profile against a soft analysis proxy (verifier v5) ---
#
# Failure modes written before the verifier change:
# - the phone's analysis proxy is 568x320 at ~0.7 Mbps; against it a correct export of
#   detailed handheld footage scores 0.91-0.95 SSIM, under the 0.95 bar (Lisbon job
#   3daccc8c), so the export is rejected;
# - a one- or two-frame shift either way, or a cut two frames late, moves the best match
#   to a neighbouring source frame; a lower bar must still reject it, on fast footage,
#   and on a slow zoom where the shifted picture still scores 0.89-0.95;
# - a 30 fps proxy of a 25 fps original shows some pictures twice; a repeated neighbour
#   must not read as a shift, and is no evidence against one;
# - still footage has no best frame, so below the exact bar it must not pass;
# - a wrong clip, a 2% zoom or a 1% offset keeps the timing but not the picture.

SOFT_SOURCES = {
    # Mandelbrot detail (still at this scale), then the camera over 1600x900 of it.
    "handheld": (
        0.06,
        "crop=1280:720:x='160+80*sin(2*PI*0.9*t)+16*sin(2*PI*5.3*t)'"
        ":y='90+50*sin(2*PI*0.6*t+1)+12*sin(2*PI*4.1*t)',tmix=frames=2",
    ),
    "calm": (0.02, "crop=1280:720:x='160+45*t':y='90+10*t'"),
    "zoom": (
        0.02,
        "zoompan=z=1+0.08*it:d=1:x=iw/2-(iw/zoom/2):y=ih/2-(ih/zoom/2):s=1280x720:fps=25",
    ),
    "still_grain": (0.06, "crop=1280:720:x=160:y=90,noise=alls=40:allf=t"),
}
SOFT_EXPORT = (270, 480)
SOFT_GEOMETRY = {
    "cover": "crop=ih*9/16:ih",
    "zoom2": "crop=ih*9/16/1.02:ih/1.02",
    "offset1": "crop=ih*9/16:ih:(iw-ih*9/16)/2+0.01*ih*9/16:0",
}


def _raw_frames(source, vf, size):
    raw = subprocess.run(
        [FFMPEG, "-v", "error", "-i", str(source), "-vf", f"{vf},scale={size[0]}:{size[1]}"]
        + ["-fps_mode", "passthrough", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        check=True,
        capture_output=True,
        timeout=60,
    ).stdout
    frame = size[0] * size[1] * 3
    return [raw[i * frame : (i + 1) * frame] for i in range(len(raw) // frame)]


def _held(frames, fps_in, windows, fps=30):
    """Frame n of each window shows the last source frame at/before start + n/fps."""
    return b"".join(
        frames[min(int((start + n / fps) * fps_in + 1e-6), len(frames) - 1)]
        for start, duration in windows
        for n in range(round(duration * fps))
    )


def _encode_raw(data, size, path, *quality):
    subprocess.run(
        [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "rawvideo"]
        + ["-pix_fmt", "rgb24", "-s", f"{size[0]}x{size[1]}", "-r", "30", "-i", "-"]
        + ["-c:v", "libx264", "-pix_fmt", "yuv420p", *quality, str(path)],
        input=data,
        check=True,
        capture_output=True,
        timeout=60,
    )
    return path


@pytest.fixture(scope="module")
def soft_proxy(exports):
    """Per footage: a 568x320 ~0.7 Mbps 30 fps proxy, and an exporter of 9:16 cuts."""
    root, _, _ = exports
    stills, built = {}, {}

    def footage(name):
        if name in built:
            return built[name]
        kind, fps_in = name.rsplit("_", 1)
        scale, camera = SOFT_SOURCES[kind]
        if scale not in stills:
            stills[scale] = root / f"mandelbrot-{scale}.png"
            ffmpeg(
                "-f",
                "lavfi",
                "-i",
                f"mandelbrot=s=1600x900:maxiter=300:start_scale={scale}:end_scale={scale}",
                "-frames:v",
                1,
                stills[scale],
            )
        source = root / f"soft-{name}.mp4"
        ffmpeg(
            "-loop",
            1,
            "-framerate",
            fps_in,
            "-i",
            stills[scale],
            "-t",
            4,
            "-vf",
            f"{camera},format=yuv420p",
            "-c:v",
            "libx264",
            "-preset",
            "fast",
            "-crf",
            14,
            source,
        )
        frames = _raw_frames(source, "null", (568, 320))
        proxy = _encode_raw(
            _held(frames, int(fps_in), [(0, len(frames) / int(fps_in))]),
            (568, 320),
            root / f"soft-{name}-proxy.mp4",
            *("-preset", "medium", "-b:v", "700k", "-maxrate", "800k", "-bufsize", "1400k"),
        )
        cache = {}

        def cut(windows, geometry="cover"):
            if geometry not in cache:
                cache[geometry] = _raw_frames(source, SOFT_GEOMETRY[geometry], SOFT_EXPORT)
            return _encode_raw(
                _held(cache[geometry], int(fps_in), windows),
                SOFT_EXPORT,
                root / f"{uuid.uuid4()}.mp4",
                *("-preset", "fast", "-crf", "17"),
            )

        built[name] = proxy, cut
        return built[name]

    return footage


def _portrait(recipe, windows):
    """Contiguous clips of source "a" at (source_start, duration), on a 9:16 canvas."""
    recipe = _single_clip(recipe, start=windows[0][0], duration=windows[0][1])
    recipe.canvas = recipe.canvas.model_copy(update={"width": 1080, "height": 1920})
    template = recipe.tracks[0].clips[0]
    clips, cursor = [], 0.0
    for index, (start, duration) in enumerate(windows):
        clip = copy.deepcopy(template)
        clip.id = f"c{index}"
        clip.source_start, clip.source_duration, clip.timeline_start = start, duration, cursor
        clips.append(clip)
        cursor += duration
    recipe.tracks[0].clips = clips
    return recipe


SOFT_WINDOWS = [(0.5, 1.9)]
ZOOM_WINDOWS = [(1.5, 1.9)]


@pytest.mark.parametrize("footage", ["handheld_25", "handheld_30"])
def test_correct_export_against_a_soft_proxy_passes_on_its_frame_profile(
    exports, soft_proxy, footage
):
    root, _, recipe = exports
    proxy, cut = soft_proxy(footage)
    report = verify_actual_export(
        cut(SOFT_WINDOWS), status(_portrait(recipe, SOFT_WINDOWS)), {"a": proxy}
    )
    (root / f"kri1042-profile-{footage}.json").write_text(json.dumps(report, indent=2))
    assert report["status"] == "checked", report
    (picture,) = [c for c in report["checks"] if c["kind"] == "picture"]
    below_bar = [s for s in picture["sample_evidence"] if s["ssim_displayed"] < 0.95]
    assert below_bar and picture["profile_samples"] == len(below_bar), picture


@pytest.mark.parametrize(
    "footage,export_windows,geometry,recipe_windows",
    [
        ("handheld_25", [(0.5 + 1 / 30, 1.9)], "cover", SOFT_WINDOWS),
        ("handheld_25", [(0.5 - 1 / 30, 1.9)], "cover", SOFT_WINDOWS),
        ("handheld_25", [(0.5 + 2 / 30, 1.9)], "cover", SOFT_WINDOWS),
        ("handheld_25", [(0.5 - 2 / 30, 1.9)], "cover", SOFT_WINDOWS),
        ("handheld_30", [(0.5 + 1 / 30, 1.9)], "cover", SOFT_WINDOWS),
        ("handheld_30", [(0.5 - 1 / 30, 1.9)], "cover", SOFT_WINDOWS),
        ("calm_25", [(0.5 - 1 / 30, 1.9)], "cover", SOFT_WINDOWS),
        ("zoom_25", [(1.5 - 1 / 30, 1.9)], "cover", ZOOM_WINDOWS),
        ("zoom_25", [(1.5 + 2 / 30, 1.9)], "cover", ZOOM_WINDOWS),
        ("zoom_25", [(1.5 - 2 / 30, 1.9)], "cover", ZOOM_WINDOWS),
        ("handheld_25", [(0.5, 1.2 + 2 / 30), (2.6, 1.0 - 2 / 30)], "cover", None),
        ("handheld_25", [(2.0, 1.9)], "cover", SOFT_WINDOWS),
        ("handheld_25", SOFT_WINDOWS, "zoom2", SOFT_WINDOWS),
        ("handheld_25", SOFT_WINDOWS, "offset1", SOFT_WINDOWS),
    ],
    ids=[
        "handheld25-1-frame-late",
        "handheld25-1-frame-early",
        "handheld25-2-frames-late",
        "handheld25-2-frames-early",
        "handheld30-1-frame-late",
        "handheld30-1-frame-early",
        "calm25-1-frame-early",
        "zoom25-1-frame-early",
        "zoom25-2-frames-late",
        "zoom25-2-frames-early",
        "handheld25-cut-2-frames-late",
        "handheld25-wrong-window",
        "handheld25-zoom-2pct",
        "handheld25-offset-1pct",
    ],
)
def test_frame_profile_never_accepts_a_shifted_or_wrong_picture(
    exports, soft_proxy, footage, export_windows, geometry, recipe_windows
):
    root, _, recipe = exports
    proxy, cut = soft_proxy(footage)
    recipe_windows = recipe_windows or [(0.5, 1.2), (2.6, 1.0)]
    report = verify_actual_export(
        cut(export_windows, geometry), status(_portrait(recipe, recipe_windows)), {"a": proxy}
    )
    assert report["status"] == "rejected", report
    assert any(c["kind"] == "picture" and c["status"] == "failed" for c in report["checks"])


def test_still_footage_below_the_exact_bar_has_no_profile_to_pass_on(exports, soft_proxy):
    """Grain over a still: every frame differs, none is the best match."""
    root, _, recipe = exports
    proxy, cut = soft_proxy("still_grain_25")
    report = verify_actual_export(
        cut(SOFT_WINDOWS), status(_portrait(recipe, SOFT_WINDOWS)), {"a": proxy}
    )
    (picture,) = [c for c in report["checks"] if c["kind"] == "picture"]
    assert min(s["ssim_displayed"] for s in picture["sample_evidence"]) < 0.95, picture
    assert (picture["status"], picture["profile_samples"]) == ("failed", 0), picture
