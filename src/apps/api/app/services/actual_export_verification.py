"""Measured export checks for the first promoted phone adapter (KRI-734).

Read pixels and PCM from the candidate and immutable full-timeline analysis
proxies. Never paint desired text or copy a renderer's receipt as output proof.
This bounded verifier deliberately does not interpret effects it cannot check.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import re
import subprocess
import tempfile
import time
from collections.abc import Mapping
from fractions import Fraction
from pathlib import Path

import numpy as np
from PIL import Image

from app.kria.device_render import BRAND_TAIL_SECONDS, DeviceRenderStatus
from app.kria.recipes import MediaTransform
from app.kria.recipes_v2 import EditRecipeV2
from app.pipeline.cloud_render_evidence import normalize_text

VERIFIER_VERSION = "phone-simple-cut-v1"
POLICY_FIELD = "actual_export_policy"
SAMPLE_RATE = 8000
# Both the phone export and its analysis proxies are local ISO media files.
# Do not autodetect a disguised playlist/container or let a decoder fetch URLs.
_LOCAL_MP4_INPUT = ("-protocol_whitelist", "file,pipe", "-f", "mov")


def unsupported_reasons(recipe) -> list[str]:
    """Coverage declaration, independent of whether the output looks plausible."""
    if not isinstance(recipe, EditRecipeV2):
        return ["recipe_version"]
    reasons = []
    tracks = [track for track in recipe.tracks if track.clips]
    if len(tracks) != 1 or tracks[0].kind != "video":
        return ["track_composition"]
    clips = sorted(tracks[0].clips, key=lambda clip: clip.timeline_start)
    if recipe.duration > 60 or len(clips) > 16 or len(recipe.text_layers) > 4:
        reasons.append("verification_budget")
    if any(
        (recipe.camera_pulses, recipe.motion_scenes, recipe.visual_fills, recipe.drawing_layers)
    ):
        reasons.append("visual_effects")
    original_ids = {a.id for a in recipe.asset_manifest.assets if a.kind == "original"}
    cursor = 0
    for clip in clips:
        if clip.source_asset_id not in original_ids:
            reasons.append("non_original_picture")
        if (
            abs(clip.timeline_start - cursor) > 0.0001
            or clip.rate != 1
            or clip.transition is not None
            or clip.look is not None
            or clip.transform != MediaTransform()
            or clip.source_crop is not None
            or clip.still_layout is not None
            or clip.hold_duration is not None
            or clip.text is not None
            or clip.audio_fade_in
            or clip.audio_fade_out
        ):
            reasons.append("clip_treatment")
        if clip.source_duration < 0.4:
            reasons.append("short_window")
        cursor = clip.timeline_start + clip.source_duration / clip.rate
    audio = recipe.audio
    if (
        audio.music_asset_id
        or audio.narration_asset_id
        or audio.target_lufs is not None
        or audio.duck_original_during_music
        or audio.duck_music_during_speech
        or audio.mute_windows
        or audio.fade_in
        or audio.fade_out
    ):
        reasons.append("audio_treatment")
    for layer in recipe.text_layers:
        phases = layer.animation_phases
        if (
            layer.effect not in {"none", "static"}
            or layer.rotation_degrees
            or layer.bend
            or layer.motion
            or layer.fade
            or layer.background
            or layer.karaoke
            or layer.giant_title
            or layer.handwriting
            or layer.discrete_reveal
            or layer.smooth_reveal
            or layer.staggered
            or layer.reveal_bounds
            or (
                phases
                and (phases.entrance != "none" or phases.exit != "none" or phases.loop != "none")
            )
            or layer.end - layer.start < 0.4
            or any(run.fill.alpha == 0 for run in layer.runs)
        ):
            reasons.append("text_treatment")
        if not layer.runs:
            reasons.append("empty_text")
    return sorted(set(reasons))


def _run(arguments: list[str], *, data: bytes | None = None) -> bytes:
    return subprocess.run(arguments, input=data, check=True, capture_output=True, timeout=20).stdout


def _probe(path: Path) -> dict:
    return json.loads(
        _run(
            [
                "ffprobe",
                "-v",
                "error",
                *_LOCAL_MP4_INPUT,
                "-show_streams",
                "-show_format",
                "-of",
                "json",
                str(path),
            ]
        )
    )


def _frame(
    path: Path, at: float, *, size: tuple[int, int] | None = None, region=None
) -> Image.Image:
    args = [
        "ffmpeg",
        "-v",
        "error",
        # One sampled frame does not benefit from decoder look-ahead or a
        # threaded PNG encoder. Bound both on the single-vCPU API machines.
        "-threads",
        "1",
        "-filter_threads",
        "1",
        "-ss",
        str(round(at, 6)),
        *_LOCAL_MP4_INPUT,
        "-i",
        str(path),
        "-frames:v",
        "1",
    ]
    if size:
        width, height = size
        args += [
            "-vf",
            f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height}",
        ]
    elif region is not None:
        x0, y0, x1, y1 = region
        # Convert before cropping: YUV chroma subsampling must not shift an odd
        # crop origin. These are the same RGB pixels Pillow previously cropped.
        args += ["-vf", f"format=rgb24,crop={x1 - x0}:{y1 - y0}:{x0}:{y0}:exact=1"]
    return Image.open(
        io.BytesIO(_run(args + ["-f", "image2pipe", "-vcodec", "png", "-threads", "1", "-"]))
    ).convert("RGB")


def _audio(path: Path, start: float, duration: float) -> np.ndarray:
    raw = _run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-ss",
            str(start),
            *_LOCAL_MP4_INPUT,
            "-i",
            str(path),
            "-t",
            str(duration),
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(SAMPLE_RATE),
            "-f",
            "f32le",
            "-",
        ]
    )
    return np.frombuffer(raw, dtype="<f4").astype(np.float64)


def _frames(path: Path, times, *, size: tuple[int, int], probe=None, deadline=None, region=None):
    """Decode the same first-at/after samples with bounded process reuse.

    Keep exact input PTS in a private metadata sidecar, so sparse/VFR samples can
    share an observed frame without moving any requested sample time. Any batch
    uncertainty falls back to the original independent seek path.
    """
    deadline = deadline if deadline is not None else time.monotonic() + 90
    # Preserve the original _frame helper's rounding for either numeric type.
    # The picture caller still normalizes linspace offsets to float, as before.
    requested = [float(round(at, 6)) for at in times]
    try:
        probe = probe or _probe(path)
        video = next(s for s in probe["streams"] if s["codec_type"] == "video")
        base = Fraction(str(probe["format"].get("start_time", "0")))
        tick = Fraction(video["time_base"])
        unique = sorted(set(requested))
        observed = {}
        frame_bytes = size[0] * size[1] * 3
        maximum = max(1, min(17, (64 * 1024 * 1024) // frame_bytes))
        while unique:
            if time.monotonic() > deadline:
                raise TimeoutError("verification_budget")
            group = [at for at in unique[:maximum] if at - unique[0] <= 8]
            unique = unique[len(group) :]
            # Accurate input seeking rounds the requested clock to the stream
            # time base (nearest, ties away from zero), before frame selection.
            clocks = [(Fraction(str(at)) + base) / tick for at in group]
            thresholds = [
                math.floor(value + Fraction(1, 2))
                if value >= 0
                else math.ceil(value - Fraction(1, 2))
                for value in clocks
            ]
            expression = "+".join(
                f"gte(pts,{pts})*eq(selected_n,{i})" for i, pts in enumerate(thresholds)
            )
            with tempfile.TemporaryDirectory(prefix="kria-sample-clock-") as directory:
                clock = Path(directory) / "clock.txt"
                geometry = (
                    f"format=rgb24,crop={region[2] - region[0]}:{region[3] - region[1]}:"
                    f"{region[0]}:{region[1]}:exact=1"
                    if region is not None
                    else (
                        f"scale={size[0]}:{size[1]}:force_original_aspect_ratio=increase,"
                        f"crop={size[0]}:{size[1]},format=rgb24"
                    )
                )
                filters = (
                    f"select='{expression}',{geometry},"
                    "metadata=mode=add:key=lavfi.kria_sample:value=1,"
                    f"metadata=mode=print:file={clock}"
                )
                raw = _run(
                    [
                        "ffmpeg",
                        "-v",
                        "error",
                        "-threads",
                        "1",
                        "-filter_threads",
                        "1",
                        "-copyts",
                        "-ss",
                        str(group[0]),
                        *_LOCAL_MP4_INPUT,
                        "-i",
                        str(path),
                        "-frames:v",
                        str(len(group)),
                        "-vf",
                        filters,
                        "-fps_mode",
                        "passthrough",
                        "-threads",
                        "1",
                        "-f",
                        "rawvideo",
                        "-pix_fmt",
                        "rgb24",
                        "-",
                    ]
                )
                points = [
                    int(value) for value in re.findall(r"\bpts:\s*(-?\d+)", clock.read_text())
                ]
            count, remainder = divmod(len(raw), frame_bytes)
            if remainder or len(points) < count or not count:
                raise ValueError("incomplete_picture_batch")
            points = points[:count]
            if points != sorted(points):
                raise ValueError("non_monotonic_picture_batch")
            for at, threshold in zip(group, thresholds, strict=True):
                index = next(i for i, pts in enumerate(points) if pts >= threshold)
                data = raw[index * frame_bytes : (index + 1) * frame_bytes]
                observed[at] = Image.frombytes("RGB", size, data)
        return [observed[at] for at in requested]
    except (OSError, ValueError, KeyError, StopIteration, subprocess.SubprocessError):
        result = []
        for at in requested:
            if time.monotonic() > deadline:
                raise TimeoutError("verification_budget")
            result.append(
                _frame(path, at, region=region)
                if region is not None
                else _frame(path, at, size=size)
            )
        return result


def _picture_similarity(expected: np.ndarray, actual: np.ndarray, mask: np.ndarray) -> float:
    """Local SSIM, complementing absolute error on dark/low-contrast pictures.

    Standard luminance/contrast/structure terms, 8x8 windows, L=1 and
    K1=.01/K2=.03. Only wholly unoccluded windows contribute.
    """
    reference = expected @ np.array([0.2126, 0.7152, 0.0722]) / 255
    observed = actual @ np.array([0.2126, 0.7152, 0.0722]) / 255
    scores = []
    for y in range(0, mask.shape[0] - 7, 8):
        for x in range(0, mask.shape[1] - 7, 8):
            if not mask[y : y + 8, x : x + 8].all():
                continue
            a, b = reference[y : y + 8, x : x + 8], observed[y : y + 8, x : x + 8]
            ma, mb = float(a.mean()), float(b.mean())
            va, vb = float(a.var()), float(b.var())
            covariance = float(((a - ma) * (b - mb)).mean())
            scores.append(
                ((2 * ma * mb + 0.01**2) * (2 * covariance + 0.03**2))
                / ((ma * ma + mb * mb + 0.01**2) * (va + vb + 0.03**2))
            )
    if not scores:
        raise ValueError("insufficient unoccluded picture evidence")
    return float(np.mean(scores))


def _bounds(layer, recipe) -> tuple[int, int, int, int]:
    # Runs carry absolute positions and glyph origins. A conservative OCR region
    # includes ascenders, descenders and the final glyph, never the whole frame.
    x0 = min(run.x - run.font_size * 0.25 for run in layer.runs)
    y0 = min(run.baseline_y - run.font_size * 1.4 for run in layer.runs)
    x1 = max(
        run.x
        + (max(g.x for g in run.glyphs) if run.glyphs else len(run.text) * run.font_size)
        + run.font_size * 1.5
        for run in layer.runs
    )
    y1 = max(run.baseline_y + run.font_size * 0.5 for run in layer.runs)
    return (
        max(0, int(x0)),
        max(0, int(y0)),
        min(recipe.canvas.width, math.ceil(x1)),
        min(recipe.canvas.height, math.ceil(y1)),
    )


def _read_text(image: Image.Image, colors: list[tuple[int, int, int]]) -> set[str]:
    image = image.resize((image.width * 2, image.height * 2))
    pixels = np.asarray(image, dtype=float)
    distance = np.min([np.linalg.norm(pixels - color, axis=2) for color in colors], axis=0)
    # A second OCR view isolates the recipe's opaque paint colour from a busy
    # background. It still reads actual pixels; it never draws the desired words.
    # Keep the same confidence bar for both views (the physical Gameday export
    # reads correctly at a cut but Tesseract gives the unprocessed word 0 confidence).
    painted = Image.fromarray(np.where(distance < 70, 0, 255).astype(np.uint8))
    candidates = set()
    for view in (image, painted):
        buffer = io.BytesIO()
        view.save(buffer, format="PNG")
        raw = _run(["tesseract", "stdin", "stdout", "--psm", "6", "tsv"], data=buffer.getvalue())
        rows = csv.DictReader(io.StringIO(raw.decode()), delimiter="\t")
        candidates.add(
            normalize_text(" ".join(row["text"] for row in rows if float(row["conf"]) >= 40))
        )
    return candidates


def _audio_measurement(expected: np.ndarray, actual: np.ndarray) -> dict:
    # Ignore codec/clip declick edges. A limited +/- 10 ms correlation search
    # allows codec timing differences without accepting a different source window.
    margin = int(0.1 * SAMPLE_RATE)
    n = min(len(expected), len(actual)) - 2 * margin
    if n < SAMPLE_RATE // 10:
        return {"status": "missing", "reason": "short_audio_evidence"}
    expected = expected[margin : margin + n]
    rms = float(np.sqrt(np.mean(expected**2)))
    centre = actual[margin : margin + n]
    if rms < 0.0001:
        actual_rms = float(np.sqrt(np.mean(centre**2)))
        return {"status": "passed" if actual_rms < 0.002 else "failed", "rms": round(actual_rms, 6)}
    expected = expected - expected.mean()
    expected_energy = float(expected @ expected)
    best = (-1.0, 0.0, 0)
    for lag in range(-80, 81):
        observed = actual[margin + lag : margin + lag + n]
        observed = observed - observed.mean()
        energy = float(observed @ observed)
        cross = float(expected @ observed)
        corr = cross / math.sqrt(expected_energy * energy) if energy > 0 else 0
        if corr > best[0]:
            best = (corr, cross / expected_energy, lag)
    corr, gain, lag = best
    return {
        "status": "passed" if corr >= 0.97 and 0.85 <= gain <= 1.15 else "failed",
        "correlation": round(corr, 6),
        "gain": round(gain, 6),
        "lag_samples": lag,
    }


def verify_actual_export(
    output: Path,
    status: DeviceRenderStatus,
    sources: Mapping[str, Path],
    *,
    brand_tail: str = "none",
) -> dict:
    """Return checked/rejected/partial/unsupported, with bounded measured evidence.

    Caller resolves sources ONLY from server-owned immutable receipts and binds
    the returned report to the reserved storage generation under its row fence.
    Missing tooling, unreadable media or uncertainty can never become checked.
    """
    recipe = status.request.recipe
    report = {
        "verifier_version": VERIFIER_VERSION,
        "identity": status.request.identity.model_dump(mode="json"),
        "status": "partial",
        "checks": [],
        "picture_sampling": "at_most_0.5s_between_samples",
        "renderer_version": recipe.renderer_version,
        "recipe_schema_version": recipe.schema_version,
        "required_capabilities": sorted(recipe.required_capabilities),
    }
    checks = report["checks"]
    unsupported = unsupported_reasons(recipe)
    if unsupported:
        return {**report, "status": "unsupported", "unsupported": unsupported}
    deadline = time.monotonic() + 90
    try:
        with output.open("rb") as stream:
            report["sha256"] = hashlib.file_digest(stream, "sha256").hexdigest()
        report["file_size_bytes"] = output.stat().st_size
        probe = _probe(output)
        duration = float(probe["format"]["duration"])
        expected_duration = recipe.duration + BRAND_TAIL_SECONDS[brand_tail]
        checks.append(
            {
                "kind": "duration",
                "status": "passed"
                if math.isfinite(duration)
                and abs(duration - expected_duration) <= max(0.1, 2 / recipe.frame_rate)
                else "failed",
                "actual_s": duration,
                "expected_s": expected_duration,
            }
        )
        if checks[-1]["status"] == "failed":
            return {**report, "status": "rejected"}
        # Keep samples small; references and candidate use identical cover fit.
        size = (96, max(16, round(96 * recipe.canvas.height / recipe.canvas.width)))
        rects = [_bounds(layer, recipe) for layer in recipe.text_layers]
        mask = np.ones((size[1], size[0]), dtype=bool)
        # The shipped watermark is fixed brand furniture, not authored content.
        if brand_tail == "standard":
            mask[int(size[1] * 0.85) :, int(size[0] * 0.6) :] = False
        for x0, y0, x1, y1 in rects:
            if x1 <= x0 or y1 <= y0:
                raise ValueError("unmeasurable_text_region")
            mask[
                int(y0 * size[1] / recipe.canvas.height) : math.ceil(
                    y1 * size[1] / recipe.canvas.height
                ),
                int(x0 * size[0] / recipe.canvas.width) : math.ceil(
                    x1 * size[0] / recipe.canvas.width
                ),
            ] = False
        if mask.mean() < 0.55:
            return {
                **report,
                "status": "unsupported",
                "unsupported": ["text_occludes_picture_evidence"],
            }
        for track in recipe.tracks:
            for clip in track.clips:
                if time.monotonic() > deadline:
                    raise TimeoutError("verification_budget")
                source = sources.get(clip.source_asset_id)
                if source is None:
                    checks.append(
                        {
                            "kind": "picture",
                            "clip_id": clip.id,
                            "status": "missing",
                            "reason": "source_unavailable",
                        }
                    )
                    continue
                count = max(3, math.ceil(clip.source_duration / 0.5) + 1)
                differences = []
                similarities = []
                offsets = [float(t) for t in np.linspace(0.1, clip.source_duration - 0.1, count)]
                source_probe = _probe(source)
                expected_frames = _frames(
                    source,
                    [clip.source_start + offset for offset in offsets],
                    size=size,
                    probe=source_probe,
                    deadline=deadline,
                )
                actual_frames = _frames(
                    output,
                    [clip.timeline_start + offset for offset in offsets],
                    size=size,
                    probe=probe,
                    deadline=deadline,
                )
                for reference, candidate in zip(expected_frames, actual_frames, strict=True):
                    if time.monotonic() > deadline:
                        raise TimeoutError("verification_budget")
                    expected = np.asarray(reference, dtype=float)
                    actual = np.asarray(candidate, dtype=float)
                    differences.append(float(np.abs(expected - actual)[mask].mean() / 255))
                    similarities.append(_picture_similarity(expected, actual, mask))
                worst = max(differences)
                similarity = min(similarities)
                # SSIM's relative luminance term is unstable near black: an
                # ordinary H.264 round trip can move a 10-code pixel by 1-2
                # codes. Permit that small absolute codec error; larger changes
                # must also preserve structure. Evaluate each sample separately.
                picture_matches = all(
                    error <= 2 / 255 or (error <= 0.1 and score >= 0.95)
                    for error, score in zip(differences, similarities, strict=True)
                )
                checks.append(
                    {
                        "kind": "picture",
                        "clip_id": clip.id,
                        "status": "passed" if picture_matches else "failed",
                        "samples": count,
                        "max_mean_pixel_error": round(worst, 6),
                        "min_local_ssim": round(similarity, 6),
                    }
                )
                has_audio = any(s["codec_type"] == "audio" for s in source_probe["streams"])
                gain = clip.volume * recipe.audio.original_volume
                observed = _audio(output, clip.timeline_start, clip.source_duration)
                expected = (
                    _audio(source, clip.source_start, clip.source_duration) * gain
                    if has_audio and gain
                    else np.zeros(round(clip.source_duration * SAMPLE_RATE))
                )
                # Whole-clip correlation must not hide a short missing/extra sound.
                for chunk, start in enumerate(range(0, len(expected), SAMPLE_RATE)):
                    if len(expected) - start < SAMPLE_RATE // 2:
                        start = max(0, len(expected) - SAMPLE_RATE)
                    measure = _audio_measurement(
                        expected[start : start + SAMPLE_RATE], observed[start : start + SAMPLE_RATE]
                    )
                    checks.append({"kind": "audio", "clip_id": clip.id, "chunk": chunk, **measure})
        for layer, rect in zip(recipe.text_layers, rects, strict=True):
            wanted = normalize_text(" ".join(run.text for run in layer.runs))
            times = [
                (layer.start + (layer.end - layer.start) * fraction, True)
                for fraction in (0.15, 0.5, 0.85)
            ]
            if layer.start > 0.2:
                times.append((layer.start - 0.15, False))
            if layer.end < expected_duration - 0.2:
                times.append((layer.end + 0.15, False))
            x0, y0, x1, y1 = rect
            text_frames = _frames(
                output,
                [at for at, _ in times],
                size=(x1 - x0, y1 - y0),
                probe=probe,
                deadline=deadline,
                region=rect,
            )
            for (at, present), text_frame in zip(times, text_frames, strict=True):
                if time.monotonic() > deadline:
                    raise TimeoutError("verification_budget")
                colors = [
                    tuple(
                        round(channel * 255)
                        for channel in (run.fill.red, run.fill.green, run.fill.blue)
                    )
                    for run in layer.runs
                ]
                observed_text = _read_text(text_frame, colors)
                matches = wanted in observed_text
                checks.append(
                    {
                        "kind": "text",
                        "layer_id": layer.id,
                        "at_s": round(at, 4),
                        "expected_visible": present,
                        "status": "passed" if matches == present else "failed",
                    }
                )
    except (OSError, ValueError, KeyError, subprocess.SubprocessError, TimeoutError) as exc:
        checks.append({"kind": "evidence", "status": "missing", "reason": type(exc).__name__})
    report["status"] = (
        "rejected"
        if any(c["status"] == "failed" for c in checks)
        else "partial"
        if not checks or any(c["status"] == "missing" for c in checks)
        else "checked"
    )
    return report
