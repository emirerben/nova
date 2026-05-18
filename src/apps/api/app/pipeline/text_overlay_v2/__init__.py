"""Layer-2 text-overlay extraction pipeline.

Six stages, all behind `settings.text_overlay_v2_enabled` (default OFF):

  A. Frame extraction      — ffmpeg, 2 fps                            [PR 2 slice 2]
  B. Per-frame OCR         — Cloud Vision (prod) / Apple Vision (dev) [PR 1]
  C. Temporal grouping     — FrameDetection[] -> TextEvent[]          [this slice]
  D. Phrase reconstruction — TextEvent[] -> Phrase[]                  [this slice]
  E. Transcript alignment  — fix OCR text against audio transcript    [PR 2 slice 3]
  F. Classification        — pick effect/role/size/color per phrase   [PR 2 slice 3]
  G. Output                — Phrase[] -> TemplateTextOutput           [PR 2 slice 4]

Stages C and D are pure logic — no I/O, no LLM calls. They are the
conceptual heart of why this pipeline beats the existing single-Gemini-call
agent: per-frame OCR reads the words, but deciding which words belong to
which phrase is what Gemini gets wrong (drops words, conflates phrases).
The pure-logic split also means stages C/D are fully testable in CI with
fixture data — no GCP credentials, no network, no model calls.
"""

from app.pipeline.text_overlay_v2.grouping import group_detections_into_events
from app.pipeline.text_overlay_v2.phrases import reconstruct_phrases

__all__ = ["group_detections_into_events", "reconstruct_phrases"]
