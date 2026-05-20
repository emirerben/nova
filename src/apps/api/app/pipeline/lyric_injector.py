"""Inject cached lyrics into a TemplateRecipe-shaped dict at job time.

INPUT
-----
- recipe_dict : dict with `slots: [{position, target_duration_s, text_overlays, ...}]`
                produced by `generate_music_recipe`. Slot time is relative
                to the *clipped section* (best_start_s → best_end_s).
- lyrics_cached : MusicTrack.lyrics_cached JSONB. See app.agents.lyrics.LyricsOutput
                  for the shape — the relevant field is `lines[]` with absolute
                  timing in the full track timeline.
- best_start_s, best_end_s : the section that the recipe slots cover. We
                  subtract best_start_s from every lyric timestamp so the
                  injected overlays sit in slot-relative time (matching how
                  text_overlays already work today).
- lyrics_config : per-template style override:
                  {
                    "enabled": bool,
                    "style": "karaoke" | "per-word-pop",
                    "position": "bottom" | ...,
                    "text_color": "#FFFFFF",
                    "highlight_color": "#FFFF00",   # karaoke only
                    "font_style": "display" | "sans" | "serif",
                    "text_size": "medium" | "large" | ...,
                    "outline_px": 2,
                    "lines_per_screen": 1,           # karaoke only (v1: always 1)
                  }

OUTPUT
------
Mutates `recipe_dict["slots"][n]["text_overlays"]` in place and returns the
recipe. Each lyric overlay is assigned to whichever slot its timing lands in.

The injector NEVER raises on bad input. If lyrics_cached is missing or
malformed, the recipe is returned unmodified — the caller (e.g. _run_music_job)
should treat this as "lyrics opt-in but unavailable for this track".
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

import structlog

log = structlog.get_logger()


@dataclass(frozen=True, slots=True)
class _SlotWindow:
    """A slot's absolute [start_s, end_s] window in section-relative coords."""

    index: int
    start_s: float
    end_s: float


# Minimum overlay duration. ASS rendering of <100ms overlays produces flicker;
# Whisper sometimes emits 30-50ms words. Karaoke extends short overlays to this
# floor; per-word-pop instead drops stages shorter than _MIN_RENDERABLE_S
# (see _inject_per_word_pop) because forcing a floor there would overlap the
# next stage and glitch the screen.
_MIN_OVERLAY_DURATION_S = 0.18

# Per-word-pop: how long the final-word overlay (the one carrying the complete
# line text) lingers after the last word ends. Lets the full line settle in
# the viewer's eye before the next line starts. Intentionally short — too long
# and the next line gets crowded out.
_LAST_WORD_DWELL_S = 0.30

# Per-word-pop: if a middle word's natural span (next.start_s - this.start_s)
# is below this threshold, the stage is DROPPED rather than floor-clamped.
# The next stage subsumes the dropped word via the cumulative-text mechanism
# so nothing is visually lost. Forcing a minimum duration on these stages
# would overlap the next stage and produce a visible glitch (two stacked
# overlays for a few frames).
_MIN_RENDERABLE_S = 0.05


def inject_lyric_overlays(
    recipe_dict: dict,
    lyrics_cached: dict | None,
    best_start_s: float,
    best_end_s: float,
    lyrics_config: dict | None,
) -> dict:
    """Inject lyric overlays into recipe slots. Returns the modified recipe."""

    cfg = lyrics_config or {}
    if not cfg.get("enabled"):
        return recipe_dict
    if not lyrics_cached:
        log.info("lyric_inject_skipped_no_cache", reason="lyrics_cached missing")
        return recipe_dict

    style = cfg.get("style") or "karaoke"
    if style not in ("karaoke", "per-word-pop"):
        log.warning("lyric_inject_unknown_style", style=style)
        return recipe_dict

    lines = lyrics_cached.get("lines") or []
    if not lines:
        log.info("lyric_inject_skipped_empty", reason="lyrics_cached.lines empty")
        return recipe_dict

    slots = recipe_dict.get("slots") or []
    if not slots:
        return recipe_dict

    # Build per-slot section-relative windows so we can route each overlay
    # to the slot whose video time contains the lyric's start.
    slot_windows = _build_slot_windows(slots)
    if not slot_windows:
        return recipe_dict

    # Working copy of slots so we don't mutate the caller's dict if anything
    # downstream of this fails halfway through.
    new_slots = copy.deepcopy(slots)

    # Filter to lines that actually overlap the selected section AND clamp
    # their internal timings to section-relative coordinates.
    section_lines = _select_section_lines(lines, best_start_s, best_end_s)
    if not section_lines:
        log.info(
            "lyric_inject_no_lines_in_section",
            best_start_s=best_start_s,
            best_end_s=best_end_s,
            total_lines=len(lines),
        )
        return recipe_dict

    if style == "karaoke":
        injected = _inject_karaoke(section_lines, slot_windows, new_slots, cfg)
    else:  # per-word-pop
        injected = _inject_per_word_pop(section_lines, slot_windows, new_slots, cfg)

    recipe_dict["slots"] = new_slots
    log.info(
        "lyric_inject_done",
        style=style,
        section_lines=len(section_lines),
        overlays_injected=injected,
    )
    return recipe_dict


# ── Internals ─────────────────────────────────────────────────────────────────


def _build_slot_windows(slots: list[dict]) -> list[_SlotWindow]:
    cursor = 0.0
    windows: list[_SlotWindow] = []
    for idx, slot in enumerate(slots):
        dur = float(slot.get("target_duration_s", 0.0))
        if dur <= 0:
            continue
        windows.append(_SlotWindow(index=idx, start_s=cursor, end_s=cursor + dur))
        cursor += dur
    return windows


def _select_section_lines(
    lines: list[dict],
    best_start_s: float,
    best_end_s: float,
) -> list[dict]:
    """Return only lines that fit fully inside [best_start_s, best_end_s].

    Returned lines have their `start_s`, `end_s`, and per-word `start_s`/`end_s`
    rebased so they're in **section-relative** coordinates (0 = best_start_s).
    Partial lines (start before section / end after) are dropped — splitting
    a karaoke line mid-word would look broken.
    """
    out: list[dict] = []
    for line in lines:
        try:
            ls = float(line.get("start_s", 0.0))
            le = float(line.get("end_s", 0.0))
        except (TypeError, ValueError):
            continue
        if le <= ls or le <= best_start_s or ls >= best_end_s:
            continue
        # v1: require full containment. Partial lyrics splits are tracked as
        # a NOT in scope item in the plan.
        if ls < best_start_s or le > best_end_s:
            continue

        rebased_words: list[dict] = []
        for w in line.get("words") or []:
            try:
                ws = float(w.get("start_s", 0.0)) - best_start_s
                we = float(w.get("end_s", 0.0)) - best_start_s
            except (TypeError, ValueError):
                continue
            rebased_words.append({"text": str(w.get("text", "")), "start_s": ws, "end_s": we})

        out.append(
            {
                "text": str(line.get("text", "")),
                "start_s": ls - best_start_s,
                "end_s": le - best_start_s,
                "words": rebased_words,
            }
        )
    return out


def _slot_for_time(t: float, windows: list[_SlotWindow]) -> _SlotWindow | None:
    """Pick the slot whose time window contains `t`. Last slot is inclusive."""
    if not windows:
        return None
    last = windows[-1]
    if t >= last.end_s - 1e-3:
        return last
    for w in windows:
        if w.start_s <= t < w.end_s:
            return w
    return None


def _ensure_overlay_list(slot: dict) -> list[dict]:
    arr = slot.get("text_overlays")
    if not isinstance(arr, list):
        arr = []
        slot["text_overlays"] = arr
    return arr


def _common_overlay_fields(cfg: dict) -> dict[str, Any]:
    """Style fields shared by every injected lyric overlay."""
    out: dict[str, Any] = {
        "role": "lyrics",
        "position": cfg.get("position") or "bottom",
        "font_style": cfg.get("font_style") or "sans",
        "text_size": cfg.get("text_size") or "medium",
        "text_color": cfg.get("text_color") or "#FFFFFF",
    }
    if cfg.get("font_family"):
        out["font_family"] = cfg["font_family"]
    outline = cfg.get("outline_px")
    if outline is not None:
        out["outline_px"] = int(outline)
    return out


def _inject_karaoke(
    section_lines: list[dict],
    windows: list[_SlotWindow],
    slots: list[dict],
    cfg: dict,
) -> int:
    """One overlay per line. Effect='karaoke-line' + word_timings tag."""
    base = _common_overlay_fields(cfg)
    highlight = cfg.get("highlight_color") or "#FFFF00"
    injected = 0

    for line in section_lines:
        slot_win = _slot_for_time(line["start_s"], windows)
        if slot_win is None:
            continue

        # Overlay times must be slot-relative for the existing pipeline to
        # treat them as a normal text overlay.
        rel_start = max(0.0, line["start_s"] - slot_win.start_s)
        slot_dur = slot_win.end_s - slot_win.start_s
        rel_end = min(slot_dur, line["end_s"] - slot_win.start_s)
        rel_end = max(rel_start + _MIN_OVERLAY_DURATION_S, rel_end)
        rel_end = min(rel_end, slot_dur)
        if rel_end <= rel_start:
            continue

        # Word timings stay relative to the overlay's own start; durations
        # encoded in centiseconds so the ASS writer can drop them straight
        # into a `\kf<cs>` tag.
        word_timings = []
        prev_end_rel = 0.0
        for w in line["words"]:
            text = (w.get("text") or "").strip()
            if not text:
                continue
            w_rel_start = max(0.0, w["start_s"] - line["start_s"])
            w_rel_end = max(w_rel_start + 0.05, w["end_s"] - line["start_s"])
            # Each word's `duration_cs` runs from the previous word's end so
            # the karaoke sweep stays continuous even if the alignment left
            # tiny gaps between words.
            dur_s = w_rel_end - prev_end_rel
            prev_end_rel = w_rel_end
            word_timings.append(
                {
                    "text": text,
                    "start_s": round(w_rel_start, 3),
                    "end_s": round(w_rel_end, 3),
                    "duration_cs": max(5, int(round(dur_s * 100))),
                }
            )

        overlay = dict(base)
        overlay.update(
            {
                "text": line["text"],
                "effect": "karaoke-line",
                "start_s": round(rel_start, 3),
                "end_s": round(rel_end, 3),
                "highlight_color": highlight,
                "word_timings": word_timings,
            }
        )
        _ensure_overlay_list(slots[slot_win.index]).append(overlay)
        injected += 1

    return injected


def _inject_per_word_pop(
    section_lines: list[dict],
    windows: list[_SlotWindow],
    slots: list[dict],
    cfg: dict,
) -> int:
    """One overlay per word that carries the cumulative line text built up to
    and including that word. Consecutive stages are butted edge-to-edge —
    each middle word's overlay ends EXACTLY at the next word's start_s, so the
    screen never holds two overlays at once. Only the final word of a line
    gets a small dwell (`_LAST_WORD_DWELL_S`) so the complete line lingers
    briefly before clearing.

    Why cumulative text and not per-word isolation: the prior implementation
    emitted single-word overlays that vanished after each word, making the
    lyrics unreadable ("tek kelime gidiyor sonra direk gidiyor"). Carrying the
    cumulative line means the viewer sees the line build up word by word and
    can actually read it.

    Why no `_MIN_OVERLAY_DURATION_S` floor here: extending a middle word's
    overlay past the next word's start would put two overlays on screen
    simultaneously, glitching the render. Short stages are DROPPED instead —
    the next stage's cumulative text still contains the dropped word, so
    nothing is visually lost.
    """
    base = _common_overlay_fields(cfg)
    injected = 0

    for line in section_lines:
        words = [w for w in line.get("words", []) if (w.get("text") or "").strip()]
        if not words:
            continue

        # Two-pass to keep "butted edge-to-edge" honest when middle stages
        # are dropped. Pass 1 decides which word indices survive the
        # `_MIN_RENDERABLE_S` floor based on their NATURAL spans. Pass 2 sets
        # each kept stage's end_s to the NEXT KEPT stage's word.start_s
        # (rather than the immediate next word's start_s) so dropped middle
        # stages don't leave a sub-frame gap in the timeline.
        natural_ends: list[float] = [
            line["end_s"] if i == len(words) - 1 else words[i + 1]["start_s"]
            for i in range(len(words))
        ]
        keep_mask = [
            (natural_ends[i] - words[i]["start_s"]) >= _MIN_RENDERABLE_S for i in range(len(words))
        ]
        # The last word always survives — line accumulation is meaningless
        # without its terminal stage. If it's somehow shorter than the
        # threshold, the dwell extension below will pad it to renderable.
        keep_mask[-1] = True

        # Pre-compute "next kept stage's word.start_s" for each kept index.
        # Walking forward only — O(n) total.
        next_kept_start: list[float | None] = [None] * len(words)
        next_start: float | None = None
        for i in range(len(words) - 1, -1, -1):
            if keep_mask[i]:
                if next_start is None:
                    next_kept_start[i] = None  # marker: this is the last kept stage
                else:
                    next_kept_start[i] = next_start
                next_start = words[i]["start_s"]

        for i, word in enumerate(words):
            if not keep_mask[i]:
                continue
            slot_win = _slot_for_time(word["start_s"], windows)
            if slot_win is None:
                continue

            cumulative_text = " ".join((w.get("text") or "").strip() for w in words[: i + 1])

            # End at the next kept stage's start_s (no gap, no overlap). If
            # this IS the last kept stage, extend past line.end_s by the
            # dwell so the full line settles before the next line begins.
            if next_kept_start[i] is None:
                end_section_s = line["end_s"] + _LAST_WORD_DWELL_S
            else:
                end_section_s = next_kept_start[i]

            slot_dur = slot_win.end_s - slot_win.start_s
            rel_start = max(0.0, word["start_s"] - slot_win.start_s)
            rel_end = min(slot_dur, end_section_s - slot_win.start_s)

            # Defensive: a stage that survives Pass 1 could still end up too
            # short here if `_slot_for_time` clipped it to the slot boundary.
            # Drop rather than floor-clamp (same reason as Pass 1). Warn so
            # user reports of "missing word X" can be traced back to this
            # boundary clip without needing to reconstruct the slot windows.
            if rel_end - rel_start < _MIN_RENDERABLE_S:
                log.warning(
                    "lyric_stage_dropped_by_slot_clip",
                    word=word.get("text"),
                    rel_duration=rel_end - rel_start,
                    word_start_s=word["start_s"],
                    slot_start_s=slot_win.start_s,
                    slot_end_s=slot_win.end_s,
                )
                continue

            # The renderer's pop-in effect scales the entire dialogue text from
            # 30% to 115% to 100% over 250ms. Without intervention, every new
            # cumulative-stage would re-scale the full accumulated line — the
            # viewer would see the whole line flickering on each new word. We
            # mark only the newly added word as the animation target so the
            # prefix words render statically and only the new tail pops in.
            new_word = (word.get("text") or "").strip()
            overlay = dict(base)
            overlay.update(
                {
                    "text": cumulative_text,
                    "effect": "pop-in",
                    "start_s": round(rel_start, 3),
                    "end_s": round(rel_end, 3),
                    "pop_animated_suffix": new_word,
                }
            )
            _ensure_overlay_list(slots[slot_win.index]).append(overlay)
            injected += 1

    return injected
