"""Resolve participant focus from bounded frames of generation-pinned shots."""

from __future__ import annotations

import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from app.agents._model_client import default_client
from app.agents._runtime import RunContext
from app.agents.narration_focus import FocusFrame, classify_narration_focus
from app.pipeline.guided_story import GuidedStoryError


def sample_times(moment: dict[str, Any]) -> list[float]:
    start = float(moment["source_start_s"])
    end = float(moment["source_end_s"])
    if moment["kind"] == "image":
        return [start]
    return [start + (end - start) * fraction for fraction in (0.1, 0.5, 0.9)]


def _contact_sheet(source: Path, moment: dict[str, Any], destination: Path) -> list[FocusFrame]:
    import subprocess  # noqa: PLC0415

    import pillow_heif  # noqa: PLC0415
    from PIL import Image, ImageDraw, ImageOps  # noqa: PLC0415

    pillow_heif.register_heif_opener()
    times = sample_times(moment)
    samples = [FocusFrame(sample_id=f"frame-{i + 1}", source_time_s=t) for i, t in enumerate(times)]
    sheet = Image.new("RGB", (480 * len(samples), 512), "#181818")
    for index, sample in enumerate(samples):
        if moment["kind"] == "image":
            frame_path = source
        else:
            frame_path = destination.parent / f"frame-{index}.jpg"
            subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-ss",
                    str(sample.source_time_s),
                    "-i",
                    str(source),
                    "-frames:v",
                    "1",
                    "-vf",
                    "scale=480:480:force_original_aspect_ratio=decrease",
                    "-threads",
                    "1",
                    str(frame_path),
                ],
                check=True,
                capture_output=True,
                timeout=45,
            )
        with Image.open(frame_path) as opened:
            if opened.width * opened.height > 25_000_000:
                raise ValueError("focus source exceeds supported image size")
            frame = ImageOps.exif_transpose(opened).convert("RGB")
            frame.thumbnail((480, 480))
            sheet.paste(frame, (index * 480 + (480 - frame.width) // 2, (480 - frame.height) // 2))
        ImageDraw.Draw(sheet).text((index * 480 + 12, 488), sample.sample_id, fill="white")
    sheet.save(destination, "JPEG", quality=85)
    return samples


def analyze_guided_participant_focus(
    timeline: list[dict[str, Any]],
    *,
    job_id: str,
) -> list[dict[str, Any]]:
    """Return ordered per-shot evidence; never reinterpret missing evidence as a person."""
    from app.storage import download_generation_to_file, object_metadata  # noqa: PLC0415

    def analyze(moment: dict[str, Any]) -> dict[str, Any]:
        try:
            metadata = object_metadata(moment["gcs_path"])
            if str(metadata.generation) != str(moment["generation"]):
                raise ValueError("focus source generation changed")
            with tempfile.TemporaryDirectory(prefix="guided-focus-") as directory:
                root = Path(directory)
                source = root / ("source" + (Path(moment["gcs_path"]).suffix or ".mp4"))
                download_generation_to_file(
                    moment["gcs_path"],
                    str(source),
                    generation=str(moment["generation"]),
                )
                sheet = root / "focus.jpg"
                samples = _contact_sheet(source, moment, sheet)
                client = default_client()
                uploaded = client.upload_media(str(sheet), timeout=60)
                evidence = classify_narration_focus(
                    client,
                    asset_id=moment["media_id"],
                    frame_contact_sheet_uri=uploaded.uri,
                    frame_samples=samples,
                    source_window_start_s=float(moment["source_start_s"]),
                    source_window_end_s=float(moment["source_end_s"]),
                    ctx=RunContext(job_id=job_id),
                )
                return {**moment, **evidence}
        except Exception as exc:  # noqa: BLE001 - requested participant labels must be auditable
            raise GuidedStoryError(
                "guided_narration_focus_unavailable",
                "The requested player labels could not be checked against your footage. "
                "Please retry.",
            ) from exc

    with ThreadPoolExecutor(max_workers=3, thread_name_prefix="guided-focus") as pool:
        return list(pool.map(analyze, timeline))
