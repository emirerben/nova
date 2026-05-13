"""Text-overlay forensics: video-to-video diff primitives.

Building blocks for offline analyzer scripts that compare a Nova-pipeline
output video against the original recipe it was meant to match. Each module
is single-purpose so analyzers can wire only what they need.

  frames      FFmpeg-based frame sampling + audio extraction.
  masking     Color masks, bboxes, stroke width, serif score.
  events      TextEvent dataclass, contiguous-frame clustering, animation
              classifier (slide-up / fade-in / font-cycle / scale-up / static).
  ocr         pytesseract wrapper that runs on color-masked crops only.
  safe_crop   16:9 -> 9:16 center-crop projection math.
  diff        Property-by-property diff engine + severity classifier +
              plain-English top-N summary.

No module here imports any pipeline code (`app/`), Celery, or the DB. The
package is offline, deterministic, file-in / JSON-out.
"""
