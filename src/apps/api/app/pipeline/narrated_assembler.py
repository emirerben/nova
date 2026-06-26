"""Assemble narrated walkthrough clips against aligned voiceover timings."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from app.pipeline.narrated_alignment import StepTiming
from app.pipeline.probe import probe_video
from app.pipeline.reframe import resolve_output_fit
from app.pipeline.single_pass import SinglePassInput, SinglePassSpec, run_single_pass
from app.tasks.template_orchestrate import _mix_user_voiceover


@dataclass(frozen=True, slots=True)
class NarratedClip:
    step_id: str
    clip_path: str
    source_start_s: float = 0.0


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _coerce_clip(value: NarratedClip | dict | Any) -> NarratedClip:
    if isinstance(value, NarratedClip):
        return value
    step_id = str(_field(value, "step_id", _field(value, "shot_id", "")) or "")
    clip_path = str(_field(value, "clip_path", _field(value, "local_path", "")) or "")
    source_start = float(_field(value, "source_start_s", _field(value, "start_s", 0.0)) or 0.0)
    if not step_id:
        raise ValueError("narrated clip assignment missing step_id/shot_id")
    if not clip_path:
        raise ValueError(f"narrated clip assignment for {step_id} missing clip_path")
    return NarratedClip(step_id=step_id, clip_path=clip_path, source_start_s=source_start)


def assemble_narrated(
    step_timings: list[StepTiming],
    clip_assignments: list[NarratedClip | dict | Any],
    voiceover_local_path: str,
    output_path: str,
    tmpdir: str,
    landscape_fit: str = "fill",
) -> None:
    """Hard-cut one visual clip per narrated step, then lay voiceover on top."""
    os.makedirs(tmpdir, exist_ok=True)
    coerced_clips = [_coerce_clip(c) for c in clip_assignments]
    clips_by_step = {c.step_id: c for c in coerced_clips}

    inputs: list[SinglePassInput] = []
    total_duration_s = 0.0
    for timing in step_timings:
        duration_s = max(0.001, float(timing.end_s) - float(timing.start_s))
        clip = clips_by_step.get(timing.step_id)
        if clip is None:
            raise ValueError(f"no narrated clip assignment for step_id={timing.step_id}")
        try:
            _probe = probe_video(clip.clip_path)
        except Exception:  # noqa: BLE001 — probe failure → fall back to crop
            _probe = None
        inputs.append(
            SinglePassInput(
                kind="clip",
                clip_path=clip.clip_path,
                start_s=max(0.0, clip.source_start_s),
                end_s=max(0.0, clip.source_start_s) + duration_s,
                aspect_ratio="16:9",
                output_fit=resolve_output_fit(_probe, landscape_fit=landscape_fit),
                has_audio=False,
            )
        )
        total_duration_s += duration_s

    silent_video_path = os.path.join(tmpdir, "narrated_visuals.mp4")
    run_single_pass(
        SinglePassSpec(
            inputs=inputs,
            transitions=["none"] * max(0, len(inputs) - 1),
            output_duration_s=total_duration_s,
        ),
        silent_video_path,
    )
    _mix_user_voiceover(
        silent_video_path,
        voiceover_local_path,
        output_path,
        tmpdir,
        mix=1.0,
        target_duration_s=total_duration_s,
    )
