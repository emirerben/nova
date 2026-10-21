"""KRI-734: decoded MP4 evidence, deliberately wrong exports included.

These oracles are authored before the verifier. Only fixture encoding uses
FFmpeg drawtext: the verifier must independently read actual pixels/audio.
"""

from __future__ import annotations

import copy
import io
import shutil
import subprocess
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

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
