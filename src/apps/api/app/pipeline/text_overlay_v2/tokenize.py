"""Stage B.5: tokenize OCR detections into per-word detections.

OCR backends return one detection per text block. A "block" may be a single
word, a single line of multiple words, or a multi-line paragraph (Cloud
Vision DOCUMENT_TEXT_DETECTION emits whole blocks; Apple Vision is closer
to per-line). The downstream stages C/D work best when each detection is a
SINGLE WORD — that way stage C's text-equality grouping merges the same
word across frames correctly, and stage D emits one phrase per word so the
final overlays carry per-word introduction timing.

Without this step, OCR inconsistencies like t=3.0 'if you' + t=3.5 'if
you\\nput' + t=4.0 'if you' + 'put in' get clustered by stage D into one
phrase with `lines=['if you', 'if you', 'put', 'put in']` — duplicate
readings stacked as redundant lines. After tokenization, the words 'if',
'you', 'put', 'in' each become their own per-frame detections; stage C
groups them by (word_text, bbox_overlap, time); stage D emits one phrase
per word; stage G emits one TemplateTextOverlay per word with that word's
first-seen `start_s` and last-seen `end_s`.

Bbox estimation: per-word bboxes are derived by proportionally dividing
the parent block's bbox along its dominant axis. For a single-line block
of N words, each word claims a horizontal slice proportional to its char
count. For a multi-line block, lines first get proportional vertical
slices, then each line tokenizes by word. This is an approximation — the
true per-word bbox would require backend-native word-level OCR — but for
downstream grouping (which only cares about same-word same-bbox matching)
and rendering (positioning is mostly center-anchored anyway) it's enough.

Pure function. Tested via `tests/pipeline/text_overlay_v2/test_tokenize.py`.
"""

from __future__ import annotations

from app.agents._schemas.text_overlay_ocr import FrameDetection, OcrPolygon


def _bbox_to_polygon(x_min: float, y_min: float, x_max: float, y_max: float) -> OcrPolygon:
    """Build a clockwise-from-top-left polygon for an axis-aligned bbox."""
    return OcrPolygon(
        points=[(x_min, y_min), (x_max, y_min), (x_max, y_max), (x_min, y_max)]
    )


def split_detection_into_words(det: FrameDetection) -> list[FrameDetection]:
    """Split one multi-word `FrameDetection` into one detection per word.

    The parent's `text` is split on newlines into lines, then each line is
    split on whitespace into words. The parent's bbox is divided
    proportionally: lines get vertical slices weighted by 1 each
    (uniform height per line — line-height varies less than char width),
    then each word within a line gets a horizontal slice proportional to
    its char count.

    Single-word single-line detections are returned unchanged (no
    allocation). Edge cases:

    - Empty/whitespace-only text → returns the input unchanged. The
      detection survives upstream filters; let stage C/D decide.
    - Single line with N words → N detections sharing the line's bbox
      vertically, horizontally divided.
    - Multi-line with mixed word counts per line → each line gets equal
      vertical share, words within each line get proportional horizontal share.

    `frame_t_s` and `confidence` are inherited unchanged for every output
    detection.
    """
    lines = det.text.split("\n")
    # Drop blank lines but keep the parent's text shape — a detection of
    # just "\n" survives upstream OCR's min_length=1 filter only as
    # whitespace; treat it as a passthrough no-op.
    non_blank_lines = [line for line in lines if line.strip()]
    if not non_blank_lines:
        return [det]

    # Pre-tokenize each non-blank line so we know if there's any splitting
    # to do at all (cheap fast path for single-word detections).
    per_line_words = [line.split() for line in non_blank_lines]
    total_word_count = sum(len(words) for words in per_line_words)
    if total_word_count <= 1:
        return [det]

    parent_aabb = det.polygon.aabb()
    px_min, py_min, px_max, py_max = parent_aabb
    parent_height = py_max - py_min
    parent_width = px_max - px_min
    line_height = parent_height / len(per_line_words)

    out: list[FrameDetection] = []
    for line_idx, words in enumerate(per_line_words):
        line_y_min = py_min + line_idx * line_height
        line_y_max = line_y_min + line_height
        total_chars = sum(len(w) for w in words)
        # Guard against pathological all-empty input post-split (shouldn't
        # happen since we filtered non_blank_lines, but defensive).
        if total_chars == 0:
            continue
        cursor_x = px_min
        for w in words:
            w_width = parent_width * (len(w) / total_chars)
            word_polygon = _bbox_to_polygon(
                cursor_x, line_y_min, cursor_x + w_width, line_y_max
            )
            out.append(
                FrameDetection(
                    frame_t_s=det.frame_t_s,
                    text=w,
                    polygon=word_polygon,
                    confidence=det.confidence,
                )
            )
            cursor_x += w_width

    return out


def tokenize_detections_into_words(
    detections: list[FrameDetection],
) -> list[FrameDetection]:
    """Apply `split_detection_into_words` to every detection in the list.

    Order is preserved (each parent's words appear contiguously in the
    output in left-to-right reading order, lines top-to-bottom). The list
    is flattened — callers should not rely on a one-to-one index mapping
    to the input.
    """
    out: list[FrameDetection] = []
    for det in detections:
        out.extend(split_detection_into_words(det))
    return out
