"""Deterministic CanvasKit motion-preset validation and FFmpeg composition."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from functools import lru_cache
from pathlib import Path

import jsonschema
import structlog

from app import storage
from app.services.editor_limits import (
    MOTION_FPS,
    MOTION_MAX_ACTIVE_FRAMES,
    MOTION_MAX_COMPLEXITY_UNITS,
    MOTION_MAX_INSTANCE_FRAMES,
    MOTION_MAX_INSTANCES,
)

log = structlog.get_logger()

LEGACY_MOTION_RUNTIME_HASH = "motion-v1:ck0.40.0:b2556106:2abfa191:route-trace-v1"
MOTION_RUNTIME_V2_HASH = "motion-v2:ck0.40.0:b2556106:2abfa191:creator-blocks-v1"
MOTION_RUNTIME_V3_HASH = "motion-v3:ck0.40.0:b2556106:2abfa191:creator-blocks-v2"
MOTION_RUNTIME_V4_HASH = "motion-v4:ck0.40.0:b2556106:2abfa191:creator-blocks-v3"
PREVIOUS_MOTION_RUNTIME_HASH = MOTION_RUNTIME_V4_HASH
MOTION_RUNTIME_HASH = "motion-v5:ck0.40.0:b2556106:2abfa191:creator-blocks-v4-capacity"
COMPATIBLE_MOTION_RUNTIME_HASHES = frozenset(
    {MOTION_RUNTIME_V2_HASH, MOTION_RUNTIME_V3_HASH, MOTION_RUNTIME_V4_HASH, MOTION_RUNTIME_HASH}
)
_TIMEOUT_S = 600
_MAX_MOTION_ASSET_BYTES = 25 * 1024 * 1024
_MAX_MOTION_ASSET_PIXELS = 25_000_000
_MAX_MOTION_ASSET_DIMENSION = 16_384
_NORMALIZED_MOTION_ASSET_DIMENSION = 2_048


class MotionSceneError(RuntimeError):
    pass


def _runtime_candidates(module_file: Path) -> tuple[Path, ...]:
    """Return production-first runtime locations without assuming source depth."""
    source = module_file.resolve()
    return (
        Path("/app/motion-runtime"),
        *(parent / "packages" / "motion-runtime" for parent in source.parents),
    )


def _runtime_root() -> Path:
    for candidate in _runtime_candidates(Path(__file__)):
        if (candidate / "motion-scene.schema.json").is_file():
            return candidate
    raise MotionSceneError("motion runtime package is missing")


def _schema() -> dict:
    return json.loads((_runtime_root() / "motion-scene.schema.json").read_text())


@lru_cache(maxsize=1)
def _creator_catalog() -> dict:
    return json.loads((_runtime_root() / "creator-blocks.catalog.json").read_text())


@lru_cache(maxsize=1)
def _creator_complexity_weights() -> dict[str, int]:
    """Return catalog-owned v2 weights; immutable v1 scenes retain weight 1."""
    return {
        str(entry["preset_id"]): int(entry["complexity_weight"])
        for entry in _creator_catalog().get("presets", [])
    }


def _validate_catalog_numeric_steps(items: list[dict]) -> None:
    """Mirror TS decimal-step validation that JSON Schema cannot express safely."""
    catalog = _creator_catalog()
    controls = {item["key"]: item for item in catalog.get("control_definitions", [])}
    presets = {item["preset_id"]: item for item in catalog.get("presets", [])}

    def validate(definition: dict, value: object, path: str) -> None:
        step = definition.get("step")
        if (
            definition.get("type") != "number"
            or not isinstance(step, (int, float))
            or isinstance(value, bool)
            or not isinstance(value, (int, float))
        ):
            return
        minimum = float(definition.get("minimum", 0))
        quotient = (float(value) - minimum) / float(step)
        if abs(quotient - round(quotient)) > 1e-8:
            raise ValueError(f"{path}: {value} does not align to step {step}")

    for index, item in enumerate(items):
        if item.get("preset_version") != 2:
            continue
        preset = presets.get(item.get("preset_id"))
        if not isinstance(preset, dict):
            continue
        params = item.get("params")
        if isinstance(params, dict):
            for definition in preset.get("parameters", []):
                key = definition.get("key")
                if key in params:
                    validate(definition, params[key], f"motion_scenes.{index}.params.{key}")
        motion = item.get("motion")
        if isinstance(motion, dict):
            for key in preset.get("supported_controls", []):
                definition = controls.get(key)
                if not isinstance(definition, dict) or definition.get("storage") != "motion":
                    continue
                effective = {**definition, **preset.get("control_overrides", {}).get(key, {})}
                if key in motion:
                    validate(effective, motion[key], f"motion_scenes.{index}.motion.{key}")


def _weighted_motion_complexity(items: list[dict]) -> int:
    events: dict[int, int] = {}
    weights = _creator_complexity_weights()
    for item in items:
        weight = (
            weights.get(str(item.get("preset_id")), 1) if item.get("preset_version") == 2 else 1
        )
        start = int(item["start_frame"])
        end = int(item["end_frame_exclusive"])
        events[start] = events.get(start, 0) + weight
        events[end] = events.get(end, 0) - weight
    total = 0
    active_weight = 0
    previous: int | None = None
    for frame in sorted(events):
        if previous is not None:
            total += (frame - previous) * active_weight
        active_weight += events[frame]
        previous = frame
    return total


def validate_motion_instances(
    value: object,
    *,
    duration_frames: int | None = None,
) -> list[dict]:
    """Validate the canonical schema plus cross-field timeline invariants."""
    if isinstance(value, list) and len(value) > MOTION_MAX_INSTANCES:
        raise ValueError(f"motion_scenes supports at most {MOTION_MAX_INSTANCES} instances")
    if isinstance(value, list):
        for index, raw in enumerate(value):
            if not isinstance(raw, dict):
                continue
            end = raw.get("end_frame_exclusive")
            if isinstance(end, int) and end < 1:
                raise ValueError(
                    f"motion_scenes.{index}.end_frame_exclusive: {end} "
                    "is less than the minimum of 1"
                )
            intensity = raw.get("intensity")
            if isinstance(intensity, (int, float)) and not 0 <= intensity <= 1:
                raise ValueError(
                    f"motion_scenes.{index}.intensity: {intensity} is greater than the maximum of 1"
                )
            palette = raw.get("palette")
            if isinstance(palette, dict):
                for slot in ("primary", "accent"):
                    color = palette.get(slot)
                    if isinstance(color, str) and (
                        len(color) != 7
                        or not color.startswith("#")
                        or any(char not in "0123456789abcdefABCDEF" for char in color[1:])
                    ):
                        raise ValueError(
                            f"motion_scenes.{index}.palette.{slot}: "
                            f"{color!r} does not match #RRGGBB"
                        )
    validator = jsonschema.Draft202012Validator(_schema())
    errors = list(validator.iter_errors(value))
    if errors:
        exc = jsonschema.exceptions.best_match(errors)
        path = ".".join(str(part) for part in exc.absolute_path)
        prefix = f"motion_scenes.{path}" if path else "motion_scenes"
        raise ValueError(f"{prefix}: {exc.message}") from exc

    assert isinstance(value, list)
    _validate_catalog_numeric_steps(value)
    ids: set[str] = set()
    intervals: list[tuple[int, int]] = []
    cleaned: list[dict] = []
    for index, raw in enumerate(value):
        item = dict(raw)
        if item["id"] in ids:
            raise ValueError(f"motion_scenes.{index}.id must be unique")
        ids.add(item["id"])
        start = int(item["start_frame"])
        end = int(item["end_frame_exclusive"])
        if end <= start:
            raise ValueError(
                f"motion_scenes.{index}.end_frame_exclusive must be greater than start_frame"
            )
        if duration_frames is not None and end > duration_frames:
            raise ValueError(
                f"motion_scenes.{index}.end_frame_exclusive exceeds the video duration"
            )
        if end - start > MOTION_MAX_INSTANCE_FRAMES:
            raise ValueError(f"motion_scenes.{index} exceeds the 8 second instance limit")
        intervals.append((start, end))
        item["palette"] = {
            "primary": item["palette"]["primary"].upper(),
            "accent": item["palette"]["accent"].upper(),
        }
        cleaned.append(item)
    active_frames = 0
    merged: list[list[int]] = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    active_frames = sum(end - start for start, end in merged)
    if active_frames > MOTION_MAX_ACTIVE_FRAMES:
        raise ValueError(
            f"motion_scenes has {active_frames} active frames; "
            f"maximum is {MOTION_MAX_ACTIVE_FRAMES}"
        )
    complexity = _weighted_motion_complexity(cleaned)
    if complexity > MOTION_MAX_COMPLEXITY_UNITS:
        raise ValueError(
            f"motion_scenes has {complexity} weighted complexity units; "
            f"maximum is {MOTION_MAX_COMPLEXITY_UNITS}"
        )
    return cleaned


def _run(
    cmd: list[str],
    *,
    label: str,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            timeout=_TIMEOUT_S,
            check=False,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        raise MotionSceneError(f"{label} timed out") from exc
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace")[-1600:]
        raise MotionSceneError(f"{label} failed (rc={result.returncode}): {stderr}")
    return result


def _creator_font_path() -> Path:
    candidates = (
        Path("/app/assets/fonts/Inter-Bold.ttf"),
        Path(__file__).resolve().parents[2] / "assets" / "fonts" / "Inter-Bold.ttf",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise MotionSceneError("Creator Block font is missing")


def _motion_asset_refs(instances: list[dict]) -> list[dict]:
    refs: dict[str, dict] = {}
    for item in instances:
        if item.get("preset_id") not in {"card_stack", "film_strip"}:
            continue
        for ref in item.get("params", {}).get("assets", []):
            previous = refs.get(ref["asset_id"])
            if previous and previous["gcs_path"] != ref["gcs_path"]:
                raise MotionSceneError("One asset id references multiple storage paths")
            refs[ref["asset_id"]] = ref
    return list(refs.values())


def _probe_dimensions(path: str) -> tuple[int, int]:
    result = _run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "json",
            path,
        ],
        label="motion base dimension probe",
    )
    streams = json.loads(result.stdout.decode("utf-8")).get("streams", [])
    if not streams:
        raise MotionSceneError("Motion base has no video stream")
    width = int(streams[0]["width"])
    height = int(streams[0]["height"])
    if width <= 0 or height <= 0 or width * height > 2_073_600:
        raise MotionSceneError("Motion base dimensions are invalid")
    return width, height


def _normalize_motion_asset(path: str, *, index: int, tmpdir: str) -> str:
    """Bound untrusted image resources before CanvasKit retains decoded pixels."""
    try:
        encoded_bytes = os.path.getsize(path)
    except OSError as exc:
        raise MotionSceneError("Creator Block image resource is missing") from exc
    if encoded_bytes <= 0 or encoded_bytes > _MAX_MOTION_ASSET_BYTES:
        raise MotionSceneError("Creator Block image exceeds the encoded-size limit")
    probe = _run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=codec_type,width,height,nb_frames,duration:format=duration",
            "-of",
            "json",
            path,
        ],
        label="Creator Block image probe",
    )
    streams = json.loads(probe.stdout.decode("utf-8")).get("streams", [])
    if not streams:
        raise MotionSceneError("Creator Block resource is not a decodable image")
    stream = streams[0]
    width = int(stream.get("width") or 0)
    height = int(stream.get("height") or 0)
    frame_count = stream.get("nb_frames")
    duration = stream.get("duration")
    format_duration = json.loads(probe.stdout.decode("utf-8")).get("format", {}).get("duration")
    has_multiple_frames = str(frame_count).isdigit() and int(frame_count) > 1

    def _has_positive_duration(value: object) -> bool:
        if value in (None, "", "N/A"):
            return False
        try:
            return float(value) > 0  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return True

    has_timeline_duration = any(
        _has_positive_duration(value) for value in (duration, format_duration)
    )
    if stream.get("codec_type") != "video" or has_multiple_frames or has_timeline_duration:
        raise MotionSceneError("Creator Block resource is not a still image")
    if (
        width <= 0
        or height <= 0
        or width > _MAX_MOTION_ASSET_DIMENSION
        or height > _MAX_MOTION_ASSET_DIMENSION
        or width * height > _MAX_MOTION_ASSET_PIXELS
    ):
        raise MotionSceneError("Creator Block image dimensions are invalid")
    normalized = os.path.join(tmpdir, f"motion_asset_{index:02d}.png")
    _run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            path,
            "-vf",
            (
                f"scale={_NORMALIZED_MOTION_ASSET_DIMENSION}:"
                f"{_NORMALIZED_MOTION_ASSET_DIMENSION}:"
                "force_original_aspect_ratio=decrease"
            ),
            "-frames:v",
            "1",
            "-y",
            normalized,
        ],
        label="Creator Block image normalization",
    )
    if not os.path.isfile(normalized) or os.path.getsize(normalized) <= 0:
        raise MotionSceneError("Creator Block image normalization produced no output")
    return normalized


def _render_sequence(
    instances: list[dict],
    *,
    width: int,
    height: int,
    tmpdir: str,
    asset_generations: dict[str, str] | None = None,
    prepared_asset_paths: dict[str, str] | None = None,
) -> tuple[str, list[dict], int]:
    runtime_root = _runtime_root()
    frames_dir = os.path.join(tmpdir, "motion_frames")
    request_path = os.path.join(tmpdir, "motion_request.json")
    font_path = _creator_font_path()
    asset_paths: dict[str, str] = {}
    for index, ref in enumerate(_motion_asset_refs(instances)):
        prepared_path = (prepared_asset_paths or {}).get(ref["asset_id"])
        if prepared_path is not None:
            if not os.path.isfile(prepared_path) or os.path.getsize(prepared_path) <= 0:
                raise MotionSceneError("Prepared Creator Block asset is missing")
            asset_paths[ref["asset_id"]] = prepared_path
            continue
        local_path = os.path.join(tmpdir, f"motion_asset_{index:02d}")
        generation = (asset_generations or {}).get(ref["asset_id"])
        if asset_generations is not None and generation is None:
            raise MotionSceneError("Creator Block asset generation is missing")
        if generation is None:
            storage.download_to_file(ref["gcs_path"], local_path)
        else:
            storage.download_generation_to_file(
                ref["gcs_path"],
                local_path,
                generation=generation,
            )
        asset_paths[ref["asset_id"]] = _normalize_motion_asset(
            local_path,
            index=index,
            tmpdir=tmpdir,
        )
    Path(request_path).write_text(
        json.dumps(
            {
                "width": width,
                "height": height,
                "runtime_hash": MOTION_RUNTIME_HASH,
                "instances": instances,
                "output_dir": frames_dir,
                "font_path": str(font_path),
                "asset_paths": asset_paths,
            },
            separators=(",", ":"),
        )
    )

    deno = shutil.which("deno")
    if not deno:
        raise MotionSceneError("Deno is unavailable")
    deno_dir = os.environ.get("DENO_DIR")
    if not deno_dir:
        info = _run([deno, "info", "--json"], label="Deno cache discovery")
        try:
            deno_dir = str(json.loads(info.stdout.decode("utf-8"))["denoDir"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise MotionSceneError("Deno cache directory could not be resolved") from exc
    if not os.path.isabs(deno_dir):
        raise MotionSceneError("Deno cache directory must be absolute")
    read_roots = [
        str(runtime_root),
        tmpdir,
        deno_dir,
        str(font_path.parent),
        *(str(Path(path).parent) for path in asset_paths.values()),
    ]
    allow_read = f"--allow-read={','.join(read_roots)}"
    cmd = [
        deno,
        "run",
        "--cached-only",
        "--no-config",
        "--node-modules-dir=none",
        allow_read,
        f"--allow-write={tmpdir}",
        str(runtime_root / "server" / "render-sequence.ts"),
        request_path,
    ]
    result = _run(cmd, label="motion CanvasKit renderer")
    receipt = json.loads(result.stdout.decode("utf-8").splitlines()[-1])
    if receipt.get("runtime_hash") != MOTION_RUNTIME_HASH:
        raise MotionSceneError("motion worker returned a different runtime hash")
    segments = receipt.get("segments")
    if not isinstance(segments, list) or not segments:
        raise MotionSceneError("motion worker returned no segments")
    return frames_dir, segments, int(receipt.get("frame_count", 0))


def apply_motion_scenes(
    *,
    base_gcs_path: str,
    instances: list[dict],
    output_gcs_path: str,
    job_id: str,
    source_generation: str | None = None,
    asset_generations: dict[str, str] | None = None,
) -> None:
    """Render the motion layer once, composite below text, and upload its cache."""
    validated = validate_motion_instances(instances)
    if not validated:
        raise MotionSceneError("apply_motion_scenes requires at least one instance")

    with tempfile.TemporaryDirectory(prefix="nova_motion_") as tmpdir:
        base_local = os.path.join(tmpdir, "base.mp4")
        output_local = os.path.join(tmpdir, "motion_base.mp4")
        if source_generation is None:
            storage.download_to_file(base_gcs_path, base_local)
        else:
            storage.download_generation_to_file(
                base_gcs_path,
                base_local,
                generation=source_generation,
            )
        width, height = _probe_dimensions(base_local)
        frames_dir, segments, frame_count = _render_sequence(
            validated,
            width=width,
            height=height,
            tmpdir=tmpdir,
            asset_generations=asset_generations,
        )
        ffmpeg_inputs = ["ffmpeg", "-i", base_local]
        for index, _segment in enumerate(segments):
            ffmpeg_inputs.extend(
                [
                    "-framerate",
                    str(MOTION_FPS),
                    "-start_number",
                    "0",
                    "-i",
                    os.path.join(
                        frames_dir,
                        f"segment_{index:03d}",
                        "frame_%06d.png",
                    ),
                ]
            )
        filters: list[str] = []
        previous = "0:v"
        for index, segment in enumerate(segments, start=1):
            start_s = int(segment["start_frame"]) / MOTION_FPS
            layer = f"motion{index}"
            output = f"v{index}"
            filters.append(f"[{index}:v]format=rgba,setpts=PTS+{start_s:.9f}/TB[{layer}]")
            filters.append(
                f"[{previous}][{layer}]overlay=0:0:format=auto:eof_action=pass:"
                f"repeatlast=0:shortest=0[{output}]"
            )
            previous = output
        _run(
            [
                *ffmpeg_inputs,
                "-filter_complex",
                ";".join(filters),
                "-map",
                f"[{previous}]",
                "-map",
                "0:a?",
                "-c:v",
                "libx264",
                "-preset",
                "fast",
                "-crf",
                "18",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "copy",
                "-r",
                str(MOTION_FPS),
                "-y",
                output_local,
            ],
            label="motion FFmpeg composite",
        )
        storage.upload_public_read(output_local, output_gcs_path)
        log.info(
            "motion_scene_applied",
            job_id=job_id,
            scene_count=len(validated),
            frame_count=frame_count,
            runtime_hash=MOTION_RUNTIME_HASH,
        )
