"""Cumulative word-by-word reveal stage builder.

Pure transformation: a list of timed words + a line-end time → a list of
cumulative reveal stages. Each stage's `text` is the cumulative line built up
to and including that word. Stages are butted edge-to-edge so exactly one
stage is visible at any instant — the screen never holds two stacked overlays.

The algorithm is extracted verbatim from the music-lyric injector
(`lyric_injector._inject_per_word_pop`) so both pipelines share one
implementation. Behavior is locked by `tests/pipeline/test_lyric_injector.py`
(regression on the injector wrapper) and `tests/pipeline/test_text_reveal.py`
(direct unit tests on this helper).

Why the algorithm looks like it does:

- Middle stages whose natural span (`words[i+1].start_s - words[i].start_s`)
  is below `MIN_RENDERABLE_S` are DROPPED, not floor-clamped. Forcing a floor
  would extend an overlay past the next word's start, stacking two overlays
  for a few frames and producing a visible glitch. The dropped word still
  appears in the next surviving stage's cumulative text — no information is
  lost on screen, only the dedicated reveal moment for that word.
- The last word ALWAYS survives. A line whose terminal stage is dropped is
  meaningless — the viewer never sees the complete sentence.
- The last stage extends past `line_end_s` by `LAST_WORD_DWELL_S` so the
  complete line settles in the viewer's eye before clearing. Intentionally
  short — too long crowds the next line.
"""

from __future__ import annotations

from dataclasses import dataclass

LAST_WORD_DWELL_S = 0.30
MIN_RENDERABLE_S = 0.05


@dataclass(frozen=True, slots=True)
class Word:
    """One timed word. `end_s` is informational; only `start_s` drives stage
    boundaries (next stage starts at next word's start_s)."""

    text: str
    start_s: float
    end_s: float


@dataclass(frozen=True, slots=True)
class CumulativeStage:
    """One reveal stage.

    `text`           — cumulative line built up to and including this word.
    `start_s`, `end_s` — visibility window; butted to neighbors.
    `pop_animated_suffix` — the word added at this stage. The renderer's
        pop-in effect uses this to animate only the new tail while the prefix
        renders statically; without it the whole line would re-scale on each
        new word and the viewer would see the line flickering.
    """

    text: str
    start_s: float
    end_s: float
    pop_animated_suffix: str


def build_cumulative_stages(
    words: list[Word],
    line_end_s: float,
    *,
    dwell_s: float = LAST_WORD_DWELL_S,
    min_renderable_s: float = MIN_RENDERABLE_S,
) -> list[CumulativeStage]:
    """Build N cumulative reveal stages from N timed words.

    Two-pass:
      Pass 1 — decide which word indices survive `min_renderable_s` based on
               their NATURAL spans. The last word is forced to survive.
      Pass 2 — emit a stage per surviving word with `end_s` set to the NEXT
               surviving stage's `start_s` (not the immediate next word's),
               so dropped middle stages don't leave a sub-frame gap.

    Empty input → empty output. Caller is responsible for filtering
    whitespace-only words before calling.
    """
    if not words:
        return []

    # Pass 1: natural ends and keep mask.
    natural_ends: list[float] = [
        line_end_s if i == len(words) - 1 else words[i + 1].start_s for i in range(len(words))
    ]
    keep_mask: list[bool] = [
        (natural_ends[i] - words[i].start_s) >= min_renderable_s for i in range(len(words))
    ]
    # Terminal stage must survive; without it the cumulative line never
    # completes. If its natural span is short, the dwell extension below
    # pads it to renderable.
    keep_mask[-1] = True

    # Pre-compute "next kept stage's word.start_s" walking backward; O(n).
    next_kept_start: list[float | None] = [None] * len(words)
    next_start: float | None = None
    for i in range(len(words) - 1, -1, -1):
        if keep_mask[i]:
            next_kept_start[i] = next_start  # None marks the last kept stage
            next_start = words[i].start_s

    # Pass 2: emit stages.
    stages: list[CumulativeStage] = []
    for i, word in enumerate(words):
        if not keep_mask[i]:
            continue
        cumulative = " ".join(w.text.strip() for w in words[: i + 1] if w.text.strip())
        if not cumulative:
            continue
        if next_kept_start[i] is None:
            end_s = line_end_s + dwell_s
        else:
            end_s = next_kept_start[i]
        # Defensive: a terminal stage whose start_s is somehow past line_end_s
        # would emit end_s <= start_s. Guard with min_renderable_s so the
        # caller can still validate the window.
        if end_s - word.start_s < min_renderable_s:
            end_s = word.start_s + min_renderable_s
        stages.append(
            CumulativeStage(
                text=cumulative,
                start_s=round(word.start_s, 3),
                end_s=round(end_s, 3),
                pop_animated_suffix=word.text.strip(),
            )
        )
    return stages


# --- Cumulative phrase grouping + gap-closing -------------------------------
#
# `build_cumulative_stages` butts the stages it emits edge-to-edge, but a
# recipe's overlays can reach the renderer with intra-phrase gaps anyway:
# stages authored by an older builder (fixed `start + beat` windows), or stages
# that were partially retimed while siblings kept stale timings. When a gap
# falls inside one cumulative phrase, the entire accumulated line blanks out for
# those frames and then re-pops when the next word's overlay opens — the glitch
# reported on prod template 89cde014 (job 8eaee104: "Luck is" → blank @6.8s →
# "Luck is just"). `butt_join_cumulative_phrases` closes those gaps at the dict
# layer so the fix works for already-cached recipes without reanalysis.

# Two overlay windows that touch (start == prev_end) are NOT a gap; only a
# strictly-positive separation beyond this epsilon is closed.
_BUTT_JOIN_EPS = 1e-6


def _is_cumulative_extension(prev_text: str, cur_text: str) -> bool:
    """True if ``cur_text`` extends ``prev_text`` (cumulative reveal stage).

    Layer-2 reveal phrases hold the full line built up to and including each
    word, so stage k+1's text starts with stage k's text and is longer. Mirrors
    the cumulative-continuation rule in web ``phrase-grouping.ts``.
    """
    if not prev_text:
        return False
    return cur_text.startswith(prev_text) and len(cur_text) > len(prev_text)


def group_phrase_index_blocks(overlays: list[dict]) -> list[list[int]]:
    """Group overlay indices into phrase blocks (one on-screen phrase each).

    A phrase is a maximal run of consecutive overlays where each member's text
    is a cumulative extension of the previous member's; any overlay that doesn't
    extend the previous one starts a new phrase. Singleton/non-extending
    overlays become one-member blocks. This is what lets callers move or retime
    a whole reveal phrase as a unit instead of fragmenting its stages.

    Grouping is by TEXT ONLY (deliberately position-agnostic) so the admin
    re-sequencer can collapse a multi-lane layout into one sequential read.
    Callers that must not join across on-screen positions (the renderer's
    gap-closer) apply their own anchor guard — see `butt_join_cumulative_phrases`.
    """
    blocks: list[list[int]] = []
    cur: list[int] = []
    prev_text: str | None = None
    for i, o in enumerate(overlays):
        text = ""
        if isinstance(o, dict):
            text = str(o.get("sample_text") or o.get("text") or "").strip()
        if cur and prev_text is not None and _is_cumulative_extension(prev_text, text):
            cur.append(i)
        else:
            if cur:
                blocks.append(cur)
            cur = [i]
        prev_text = text
    if cur:
        blocks.append(cur)
    return blocks


_ANCHOR_EPS = 1e-3


def _same_overlay_anchor(a: dict, b: dict) -> bool:
    """True if two overlays render at the same on-screen anchor.

    Cumulative reveal stages of one phrase share their anchor exactly (same
    `position_x_frac`/`position_y_frac`, falling back to the `position` bucket
    when fracs are absent). A text-prefix match across DIFFERENT anchors is a
    coincidence (e.g. a classic top title "Go" and a lower subtitle "Going
    home"), not a reveal — joining those would extend a title over a gap and
    flash two overlays at the seam. This guard keeps the gap-closer to genuine
    same-anchor reveals.
    """
    ax, ay = a.get("position_x_frac"), a.get("position_y_frac")
    bx, by = b.get("position_x_frac"), b.get("position_y_frac")
    if ax is not None and ay is not None and bx is not None and by is not None:
        return (
            abs(float(ax) - float(bx)) <= _ANCHOR_EPS and abs(float(ay) - float(by)) <= _ANCHOR_EPS
        )
    return a.get("position", "center") == b.get("position", "center")


def _extend_overlay_end_to(cur: dict, nxt: dict) -> bool:
    """Extend ``cur``'s visibility end to ``nxt``'s start, in place, if a gap
    exists. Returns True if anything changed.

    The render path picks a member's effective window in this precedence
    (mirrors `_collect_absolute_overlays` + `agentic_timing.resolve_overlay_window`):
        end_s_override  >  end_pct (agentic, BOTH start_pct+end_pct present)  >  end_s

    The join must happen in the SAME representation the renderer uses for
    ``cur`` AND read ``nxt``'s start from that same representation — otherwise we
    extend a field the renderer ignores and silently no-op the fix while
    reporting success. So if ``cur`` is pct-timed but ``nxt`` has no
    ``start_pct`` (a partially-retimed sibling), we cannot join and return
    False rather than corrupting ``end_s``. Only ever EXTENDS (never shortens,
    never moves a start), so already-butted phrases are untouched (idempotent).
    """
    # Override space wins — overrides bypass both pct and base at render time.
    if cur.get("end_s_override") is not None:
        nxt_start = nxt.get("start_s_override")
        if nxt_start is None:
            nxt_start = nxt.get("start_s")
        if (
            nxt_start is not None
            and float(nxt_start) > float(cur["end_s_override"]) + _BUTT_JOIN_EPS
        ):
            cur["end_s_override"] = round(float(nxt_start), 3)
            return True
        return False
    # Agentic pct space: the renderer uses pct only when BOTH cur fields are
    # present (resolve_overlay_window). Mirror that, and require nxt's pct start
    # in the same space — never fall through to end_s, which cur won't render.
    if cur.get("start_pct") is not None and cur.get("end_pct") is not None:
        nxt_start_pct = nxt.get("start_pct")
        if (
            nxt_start_pct is not None
            and float(nxt_start_pct) > float(cur["end_pct"]) + _BUTT_JOIN_EPS
        ):
            cur["end_pct"] = round(float(nxt_start_pct), 6)
            return True
        return False
    # Base seconds space.
    if cur.get("end_s") is not None and nxt.get("start_s") is not None:
        if float(nxt["start_s"]) > float(cur["end_s"]) + _BUTT_JOIN_EPS:
            cur["end_s"] = round(float(nxt["start_s"]), 3)
            return True
    return False


def butt_join_cumulative_phrases(overlays: list[dict]) -> int:
    """Close intra-phrase timing gaps in a slot's overlays, in place.

    Groups ``overlays`` into cumulative phrase blocks and, within each block,
    extends every non-terminal member's end to the next member's start so the
    accumulated line stays continuously on screen as words are added. The
    terminal member's end (its dwell) is left untouched, joins never cross a
    block boundary (the intended clear-and-dwell BETWEEN phrases survives), and
    only same-anchor neighbours are joined (a text-prefix collision across
    different screen positions is left alone — see `_same_overlay_anchor`).

    Returns the number of overlay ends that were extended (0 if already
    gap-free). Idempotent: a second call extends nothing.
    """
    if not overlays:
        return 0
    extended = 0
    for block in group_phrase_index_blocks(overlays):
        if len(block) < 2:
            continue
        for a, b in zip(block[:-1], block[1:], strict=True):
            cur, nxt = overlays[a], overlays[b]
            if not (isinstance(cur, dict) and isinstance(nxt, dict)):
                continue
            if not _same_overlay_anchor(cur, nxt):
                continue
            if _extend_overlay_end_to(cur, nxt):
                extended += 1
    return extended
