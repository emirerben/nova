"""Shared Layer-2 overlay rendering constants.

Single source of truth for the uniform render size every Layer-2 overlay
ships at. Imported by:

- `app.tasks.template_text_extraction` — the bridge that writes
  `text_size` / `text_size_px` into recipe overlay dicts.
- `app.pipeline.text_overlay_v2.pipeline._emit_cumulative_line_overlays` —
  Pass-1 line-overflow split must measure widths at the SAME size the
  renderer will use, or it will split at the wrong word count.

History: the two paths drifted in v0.4.42.2 — the bridge wrote 120 px but
the cumulative emit measured at the classifier's `size_class` (often
"small"/36 px) — and the line-overflow split silently never fired on prod
template 89cde014. Extracting the constants here so a future bump to
"xlarge"/150 px (or any other change) lands in BOTH places automatically.
"""

from __future__ import annotations

# Visible to both `text_size` (named-tier) and `text_size_px` (exact-px)
# consumers. Renderer prefers `text_size_px` when set, so 120 wins regardless
# of the named tier — but the named tier is what gets persisted in older
# recipe dicts, so keep them in sync.
LAYER2_RENDER_TEXT_SIZE = "large"
LAYER2_RENDER_TEXT_SIZE_PX = 120
