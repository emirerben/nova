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

from app.pipeline.text_reveal import (
    MIN_RENDERABLE_S as _MIN_RENDERABLE_S,
)
from app.pipeline.text_reveal import (
    Word as _RevealWord,
)
from app.pipeline.text_reveal import (
    build_cumulative_stages,
)

log = structlog.get_logger()


@dataclass(frozen=True, slots=True)
class _SlotWindow:
    """A slot's absolute [start_s, end_s] window in section-relative coords."""

    index: int
    start_s: float
    end_s: float


# Minimum overlay duration. ASS rendering of <100ms overlays produces flicker;
# Whisper sometimes emits 30-50ms words. Karaoke extends short overlays to this
# floor; per-word-pop instead delegates to `text_reveal.build_cumulative_stages`
# which drops stages shorter than _MIN_RENDERABLE_S because forcing a floor
# would overlap the next stage and glitch the screen.
_MIN_OVERLAY_DURATION_S = 0.18

# `_LAST_WORD_DWELL_S` and `_MIN_RENDERABLE_S` are re-exported from
# `text_reveal` (above) so existing callers/tests that import these names from
# this module continue to work. The single source of truth is `text_reveal`.

# `"line"` style defaults. The line style is the calm YouTube-lyric-video
# look: full line appears in white in near-sync with the vocal, holds past
# the last word, then fades out. Tuned for hip-hop / pop tempos against the
# Travis Scott "Highest in the Room" reference.
#
# Pre-roll 400 ms + fade-in 50 ms produces fully-readable text ~350 ms BEFORE
# the vocal — matching the "lyric video" feel of 7clouds-style YouTube refs
# where text leads the vocal by ~300 ms. Previous defaults (pre_roll 100 ms,
# fade_in 150 ms with ASS slope=0.5 quadratic ease) made text reach 95 %
# opacity ~35 ms AFTER the vocal hit, which felt "late" against any reference
# lyric video. See empirical drift analysis in commit message + the
# diff_lyric_sync.py diagnostic script under api/scripts/ for the math.
# Post-dwell of 1s is the breathing room past the vocal end that the
# karaoke effect lacked (the karaoke overlay cuts at line.end_s, i.e. the
# exact frame the last word's vocal stops). Dense lines are capped by a
# small fade-bound overlap budget so lyrics can cross-dissolve without
# ghosting through an entire section.
_LINE_PRE_ROLL_S = 0.40
_LINE_POST_DWELL_S = 1.0
_LINE_NEXT_LINE_GAP_S = 0.10
_LINE_FADE_IN_MS = 50
_LINE_FADE_OUT_MS = 250
_LINE_HOLD_TO_NEXT_THRESHOLD_MS = 500
# Upper bound on cross-dissolve overlap with the next lyric line. The
# actual cap applied per-line is the minimum of this value and
# (fade_in_s + fade_out_s); see _inject_line. Bounding by fade duration
# prevents ghosting when a long post_dwell is combined with short fades.
_LINE_MAX_OVERLAP_S = 0.4
_LINE_DEFAULT_FONT_FAMILY = "Inter Tight"
_MIN_LINE_VISIBLE_S = 0.20


def _resolve_fade_ms(cfg: dict, *, s_key: str, ms_key: str, default_ms: int) -> int:
    """Resolve fade duration, preferring the seconds alias over legacy ms."""
    if cfg.get(s_key) is not None:
        return max(0, int(round(float(cfg[s_key]) * 1000)))
    return max(0, int(float(cfg.get(ms_key, default_ms))))


@dataclass(slots=True)
class _LineOverlayWindow:
    lyric_line_id: str
    text: str
    line_start_s: float
    line_end_s: float
    section_start_s: float
    section_end_s: float
    fade_in_ms: int
    fade_out_ms: int


def inject_lyric_overlays(
    recipe_dict: dict,
    lyrics_cached: dict | None,
    best_start_s: float,
    best_end_s: float,
    lyrics_config: dict | None,
) -> dict:
    """Inject lyric overlays into recipe slots. Returns the modified recipe."""

    cfg = dict(lyrics_config or {})
    if not cfg.get("enabled"):
        return recipe_dict
    if not lyrics_cached:
        log.info("lyric_inject_skipped_no_cache", reason="lyrics_cached missing")
        return recipe_dict

    # Style set: when one is chosen (by the LyricStyleSelectorAgent at job time
    # or pinned in lyrics_config), it supplies the lyric style + styling
    # DEFAULTS. Explicit lyrics_config fields still win, so admin per-track
    # tuning is preserved. The set's lyric role implies which injector runs
    # unless lyrics_config pins `style`.
    if cfg.get("style_set_id"):
        cfg = _apply_style_set_defaults(cfg, cfg["style_set_id"])

    style = cfg.get("style") or "karaoke"
    if style not in ("karaoke", "per-word-pop", "line"):
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
    elif style == "line":
        injected = _inject_line(section_lines, slot_windows, new_slots, cfg)
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


def _apply_style_set_defaults(cfg: dict, set_id: str) -> dict:
    """Layer a style set's lyric styling onto cfg as DEFAULTS.

    Returns a new cfg where the set supplies `style` + styling/timing for keys
    lyrics_config left unset. Existing (non-None) lyrics_config keys are never
    overwritten — the set is a default source, lyrics_config is the override.
    """
    from app.pipeline.style_sets import (  # noqa: PLC0415
        lyric_role_for_style,
        lyric_style_for_set,
        resolve_overlay_style,
    )

    out = dict(cfg)
    out["style"] = out.get("style") or lyric_style_for_set(set_id)
    resolved = resolve_overlay_style(set_id, lyric_role_for_style(out["style"]))

    def _default(key: str, value: Any) -> None:
        if value is not None and out.get(key) is None:
            out[key] = value

    _default("position", resolved.get("position"))
    _default("text_color", resolved.get("text_color"))
    _default("highlight_color", resolved.get("highlight_color"))
    _default("text_size", resolved.get("text_size"))
    _default("text_size_px", resolved.get("text_size_px"))
    _default("font_family", resolved.get("font_family"))
    _default("position_y_frac", resolved.get("position_y_frac"))
    # The renderers/injectors call the stroke field `outline_px`; sets use the
    # canonical `stroke_width`.
    _default("outline_px", resolved.get("stroke_width"))
    timing = resolved.get("timing", {})
    for k in ("pre_roll_s", "post_dwell_s", "next_line_gap_s", "fade_in_ms", "fade_out_ms"):
        _default(k, timing.get(k))
    return out


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
    and including that word. Consecutive stages are butted edge-to-edge — each
    middle word's overlay ends EXACTLY at the next word's start_s, so the
    screen never holds two overlays at once. Only the final word of a line gets
    a small dwell (`text_reveal.LAST_WORD_DWELL_S`).

    Delegates the stage-building algorithm to
    `text_reveal.build_cumulative_stages` so the same logic is shared with the
    Layer-2 text-overlay path. This function adds the lyric-injector-specific
    concerns: routing each stage to a slot window, converting section-relative
    coordinates to slot-relative, and dropping stages clipped sub-renderable by
    the slot boundary.
    """
    base = _common_overlay_fields(cfg)
    injected = 0

    for line in section_lines:
        word_dicts = [w for w in line.get("words", []) if (w.get("text") or "").strip()]
        if not word_dicts:
            continue
        words = [
            _RevealWord(
                text=str(w.get("text", "")),
                start_s=float(w["start_s"]),
                end_s=float(w["end_s"]),
            )
            for w in word_dicts
        ]
        stages = build_cumulative_stages(words, line_end_s=float(line["end_s"]))

        for stage in stages:
            slot_win = _slot_for_time(stage.start_s, windows)
            if slot_win is None:
                continue
            slot_dur = slot_win.end_s - slot_win.start_s
            rel_start = max(0.0, stage.start_s - slot_win.start_s)
            rel_end = min(slot_dur, stage.end_s - slot_win.start_s)

            # Defensive: a stage that survives the helper's renderable check
            # could still end up too short here if `_slot_for_time` clipped it
            # to the slot boundary. Drop rather than floor-clamp — floor would
            # overlap the next stage. Warn so user reports of "missing word X"
            # can be traced back to this boundary clip.
            if rel_end - rel_start < _MIN_RENDERABLE_S:
                log.warning(
                    "lyric_stage_dropped_by_slot_clip",
                    word=stage.pop_animated_suffix,
                    rel_duration=rel_end - rel_start,
                    stage_start_s=stage.start_s,
                    slot_start_s=slot_win.start_s,
                    slot_end_s=slot_win.end_s,
                )
                continue

            overlay = dict(base)
            overlay.update(
                {
                    "text": stage.text,
                    "effect": "pop-in",
                    "start_s": round(rel_start, 3),
                    "end_s": round(rel_end, 3),
                    # The renderer's pop-in effect scales the entire dialogue
                    # from 30%→115%→100% over 250ms. Marking only the newly
                    # added word as the animation target keeps the prefix
                    # static so the viewer doesn't see the whole line re-pop
                    # on every new word.
                    "pop_animated_suffix": stage.pop_animated_suffix,
                }
            )
            _ensure_overlay_list(slots[slot_win.index]).append(overlay)
            injected += 1

    return injected


def _inject_line(
    section_lines: list[dict],
    windows: list[_SlotWindow],
    slots: list[dict],
    cfg: dict,
) -> int:
    """One overlay per line, plain text with smooth fade in/out.

    Differences from `_inject_karaoke`:
      - No per-word `\\kf` timings — the renderer draws the line as a single
        static block (no color sweep).
      - The overlay's visible window is **expanded** past the raw line span:
        starts at `line.start_s - pre_roll`, ends at
        `min(line.end_s + post_dwell, next_visual_start + overlap_budget)`.
        This is the YouTube-lyric-video "settle time" — without it, the line
        cuts the same frame the vocal ends (the karaoke complaint).
      - Each overlay carries `fade_in_ms` / `fade_out_ms` so the ASS renderer
        emits a `\\fad(in, out)` tag for a soft alpha transition.

    Tunable via lyrics_config:
      - `pre_roll_s` (default `_LINE_PRE_ROLL_S`)
      - `post_dwell_s` (default `_LINE_POST_DWELL_S`)
      - `next_line_gap_s` (default `_LINE_NEXT_LINE_GAP_S`)
      - `max_overlap_s` (default `_LINE_MAX_OVERLAP_S`)
      - `fade_in_s` / `fade_in_ms` (default `_LINE_FADE_IN_MS`)
      - `fade_out_s` / `fade_out_ms` (default `_LINE_FADE_OUT_MS`)

    Deprecated config fields still accepted as no-ops for one release:
      - `hold_to_next_threshold_ms`
    """
    base = _common_overlay_fields(cfg)
    base.setdefault("font_family", _LINE_DEFAULT_FONT_FAMILY)
    # Default lyric font size sits between "small" (36) and "medium" (72). The
    # libass LyricLine Style ships with Fontsize=90 — long lyrics at that size
    # render past the 1080px frame edge because the lyric-line dialogue uses
    # \q2 (no auto-wrap). 56px gives single-row fit for most lyrics while
    # still looking like a real lyric video, not a subtitle. The wrap+shrink
    # helper in text_overlay.py is the safety net for anything longer.
    #
    # We can't use setdefault on `base` because _common_overlay_fields does
    # not read `text_size_px` / `position_y_frac` from cfg — base never holds
    # the caller's override, so setdefault would clobber it. Read cfg here.
    base["text_size_px"] = int(cfg["text_size_px"]) if cfg.get("text_size_px") is not None else 56
    # Default vertical position clears the social-UI safe area at the bottom
    # of TikTok/Reels (~y=1640 on 1920). 0.85 (the "bottom" keyword) put the
    # baseline at 1632 — fine for one line, but a 2-line wrap dropped the
    # second line under the platform controls. 0.80 lifts the block enough
    # that two wrapped lines still sit above the safe boundary.
    base["position_y_frac"] = (
        float(cfg["position_y_frac"]) if cfg.get("position_y_frac") is not None else 0.80
    )
    pre_roll = float(cfg.get("pre_roll_s", _LINE_PRE_ROLL_S))
    post_dwell = float(cfg.get("post_dwell_s", _LINE_POST_DWELL_S))
    next_line_gap_s = max(0.0, float(cfg.get("next_line_gap_s", _LINE_NEXT_LINE_GAP_S)))
    fade_in_ms = _resolve_fade_ms(
        cfg, s_key="fade_in_s", ms_key="fade_in_ms", default_ms=_LINE_FADE_IN_MS
    )
    fade_out_ms = _resolve_fade_ms(
        cfg, s_key="fade_out_s", ms_key="fade_out_ms", default_ms=_LINE_FADE_OUT_MS
    )

    fade_in_s = fade_in_ms / 1000.0
    fade_out_s = fade_out_ms / 1000.0

    max_overlap_s = max(0.0, float(cfg.get("max_overlap_s", _LINE_MAX_OVERLAP_S)))

    # Bound visual overlap with the next line by the available cross-fade
    # duration. When a caller explicitly passes fade_in_ms=0 / fade_out_ms=0,
    # this collapses to 0 → no overlap (intended kill switch). Missing fade
    # keys fall back to _LINE_FADE_*_MS defaults; missing keys must NOT
    # silently disable the overlap behavior.
    dynamic_max_overlap = min(max_overlap_s, fade_in_s + fade_out_s)

    n = len(section_lines)
    line_windows: list[_LineOverlayWindow] = []

    for i, line in enumerate(section_lines):
        # Expand the visible window. Caps only erode added post-dwell; they
        # never cut the line's own audio span. next_line_gap_s is measured
        # against the next line's audio start, while visual overlap is bounded
        # separately by max_overlap_s and the active fade durations.
        line_start = float(line["start_s"])
        line_end = float(line["end_s"])
        section_start = max(0.0, line_start - pre_roll)
        natural_end = line_end + post_dwell
        if i + 1 < n:
            next_audio_start = float(section_lines[i + 1]["start_s"])
            next_visual_start = max(0.0, next_audio_start - pre_roll)
            overlap_cap = next_visual_start + dynamic_max_overlap
            gap_cap = next_audio_start - next_line_gap_s
            section_end = min(natural_end, overlap_cap, gap_cap)
            section_end = max(section_end, line_end)
        else:
            section_end = natural_end

        if section_end <= section_start:
            continue

        line_windows.append(
            _LineOverlayWindow(
                lyric_line_id=f"line:{i}:{line_start:.3f}:{line_end:.3f}",
                text=line["text"],
                line_start_s=line_start,
                line_end_s=line_end,
                section_start_s=section_start,
                section_end_s=section_end,
                fade_in_ms=int(fade_in_ms),
                fade_out_ms=int(fade_out_ms),
            )
        )

    injected = 0
    for line in line_windows:
        if line.section_end_s <= line.section_start_s:
            continue

        # Music jobs cut the rendered video into independent slots. A lyric
        # line can start near the end of one short beat-synced slot and keep
        # singing through the next clip. Emit a segment for every slot the line
        # overlaps so video cuts do not truncate the vocal span.
        segments: list[tuple[_SlotWindow, float, float]] = []
        for slot_win in windows:
            overlap_start = max(line.section_start_s, slot_win.start_s)
            overlap_end = min(line.section_end_s, slot_win.end_s)
            if overlap_end <= overlap_start:
                continue

            rel_start = max(0.0, overlap_start - slot_win.start_s)
            slot_dur = slot_win.end_s - slot_win.start_s
            rel_end = min(slot_dur, overlap_end - slot_win.start_s)
            rel_end = max(rel_start + _MIN_OVERLAY_DURATION_S, rel_end)
            rel_end = min(rel_end, slot_dur)
            if rel_end <= rel_start:
                continue
            segments.append((slot_win, rel_start, rel_end))

        for segment_idx, (slot_win, rel_start, rel_end) in enumerate(segments):
            overlay = dict(base)
            overlay.update(
                {
                    "text": line.text,
                    "effect": "lyric-line",
                    "start_s": round(rel_start, 3),
                    "end_s": round(rel_end, 3),
                    "fade_in_ms": line.fade_in_ms if segment_idx == 0 else 0,
                    "fade_out_ms": line.fade_out_ms if segment_idx == len(segments) - 1 else 0,
                    "lyric_line_id": line.lyric_line_id,
                    "lyric_segment_index": segment_idx,
                    "lyric_segment_count": len(segments),
                }
            )
            _ensure_overlay_list(slots[slot_win.index]).append(overlay)
            injected += 1

    return injected
