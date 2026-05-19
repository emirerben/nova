"""Layer-2 text-overlay extraction pipeline.

Six stages, all behind `settings.text_overlay_v2_enabled` (default OFF):

  A. Frame extraction      — ffmpeg, 2 fps                            [shipped — v0.4.31.0]
  B. Per-frame OCR         — Cloud Vision (prod) / Apple Vision (dev) [shipped — v0.4.29.0]
  C. Temporal grouping     — FrameDetection[] -> TextEvent[]          [shipped — v0.4.30.0]
  D. Phrase reconstruction — TextEvent[] -> Phrase[]                  [shipped — v0.4.30.0]
  E. Transcript alignment  — fix OCR text against audio transcript    [next slice]
  F. Classification        — pick effect/role/size/color per phrase   [next slice]
  G. Output + integration  — Phrase[] -> TemplateTextOutput           [final slice]

`run_phrase_pipeline(video_path)` is the end-to-end entry point —
A→B→C→D — and is the first slice runnable against a real video. It
returns Phrase[] ready for stage E to consume.
"""

from app.pipeline.text_overlay_v2.grouping import group_detections_into_events
from app.pipeline.text_overlay_v2.phrases import reconstruct_phrases
from app.pipeline.text_overlay_v2.pipeline import (
    run_phrase_pipeline,
    run_phrase_pipeline_from_frames,
)

__all__ = [
    "group_detections_into_events",
    "reconstruct_phrases",
    "run_phrase_pipeline",
    "run_phrase_pipeline_from_frames",
]
