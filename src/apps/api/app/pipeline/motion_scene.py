"""Deterministic CanvasKit motion-preset validation and FFmpeg composition."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import jsonschema
import structlog

from app import storage
from app.config import settings

log = structlog.get_logger()

MOTION_FPS = 30
MOTION_MAX_ACTIVE_FRAMES = 8 * MOTION_FPS
MOTION_RUNTIME_HASH = "motion-v1:ck0.40.0:b2556106:2abfa191:route-trace-v1"
_TIMEOUT_S = 600


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


def validate_motion_instances(
    value: object,
    *,
    duration_frames: int | None = None,
) -> list[dict]:
    """Validate the canonical schema plus cross-field timeline invariants."""
    try:
        jsonschema.Draft202012Validator(_schema()).validate(value)
    except jsonschema.ValidationError as exc:
        path = ".".join(str(part) for part in exc.absolute_path)
        prefix = f"motion_scenes.{path}" if path else "motion_scenes"
        raise ValueError(f"{prefix}: {exc.message}") from exc

    assert isinstance(value, list)
    ids: set[str] = set()
    first_frame = 1800
    last_frame = 0
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
        first_frame = min(first_frame, start)
        last_frame = max(last_frame, end)
        item["palette"] = {
            "primary": item["palette"]["primary"].upper(),
            "accent": item["palette"]["accent"].upper(),
        }
        cleaned.append(item)
    if cleaned and last_frame - first_frame > MOTION_MAX_ACTIVE_FRAMES:
        raise ValueError(
            f"motion_scenes spans {last_frame - first_frame} frames; "
            f"maximum is {MOTION_MAX_ACTIVE_FRAMES}"
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


def _render_sequence(
    instances: list[dict],
    *,
    width: int,
    height: int,
    tmpdir: str,
) -> tuple[str, int, int]:
    runtime_root = _runtime_root()
    frames_dir = os.path.join(tmpdir, "motion_frames")
    request_path = os.path.join(tmpdir, "motion_request.json")
    first_frame = min(int(item["start_frame"]) for item in instances)
    last_frame = max(int(item["end_frame_exclusive"]) for item in instances)
    Path(request_path).write_text(
        json.dumps(
            {
                "width": width,
                "height": height,
                "runtime_hash": MOTION_RUNTIME_HASH,
                "instances": instances,
                "output_dir": frames_dir,
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
    read_roots = [str(runtime_root), tmpdir, deno_dir]
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
    return frames_dir, first_frame, last_frame


def apply_motion_scenes(
    *,
    base_gcs_path: str,
    instances: list[dict],
    output_gcs_path: str,
    job_id: str,
) -> None:
    """Render the motion layer once, composite below text, and upload its cache."""
    validated = validate_motion_instances(instances)
    if not validated:
        raise MotionSceneError("apply_motion_scenes requires at least one instance")

    width, height = settings.output_width, settings.output_height
    with tempfile.TemporaryDirectory(prefix="nova_motion_") as tmpdir:
        base_local = os.path.join(tmpdir, "base.mp4")
        output_local = os.path.join(tmpdir, "motion_base.mp4")
        storage.download_to_file(base_gcs_path, base_local)
        frames_dir, first_frame, last_frame = _render_sequence(
            validated,
            width=width,
            height=height,
            tmpdir=tmpdir,
        )
        start_s = first_frame / MOTION_FPS
        frame_count = last_frame - first_frame
        _run(
            [
                "ffmpeg",
                "-i",
                base_local,
                "-framerate",
                str(MOTION_FPS),
                "-start_number",
                "0",
                "-i",
                os.path.join(frames_dir, "frame_%06d.png"),
                "-filter_complex",
                (
                    f"[1:v]format=rgba,setpts=PTS+{start_s:.9f}/TB[motion];"
                    "[0:v][motion]overlay=0:0:format=auto:eof_action=pass:"
                    "repeatlast=0:shortest=0[v]"
                ),
                "-map",
                "[v]",
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
