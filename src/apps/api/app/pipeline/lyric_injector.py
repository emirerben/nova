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
                    "sync_offset_s": -1.0,        # shift cached lyric timing
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
import math
import re
import unicodedata
from dataclasses import dataclass
from typing import Any

import structlog

from app.agents.lyrics import RENDERABLE_CACHED_LYRICS_SOURCES
from app.pipeline.text_reveal import (
    LAST_WORD_DWELL_S as _LAST_WORD_DWELL_S,
)
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


# Sources that the injector will burn into output. Kept as an internal alias so
# older tests/comments that refer to the injector allowlist still point at the
# same renderer policy.
_INJECTOR_ALLOWED_SOURCES = RENDERABLE_CACHED_LYRICS_SOURCES


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
_WORD_POP_LINE_CLEAR_GAP_S = 1.0 / 30.0
_WORD_POP_NESTED_LINE_MAX_WORDS = 2
_WORD_POP_NESTED_LINE_TOLERANCE_S = 0.05
_WORD_POP_SHORT_LINE_OVERLAP_DROP_S = 0.05
_WORD_POP_SHORT_LINE_OVERLAP_DROP_RATIO = 0.30
_WORD_POP_REPAIR_MIN_COLLAPSED_WORDS = 4
_WORD_POP_MALFORMED_LINE_MAX_LEADING_GAP_S = 1.0
_WORD_POP_MALFORMED_LINE_TARGET_GAP_S = 0.6
_WORD_POP_PARTIAL_SENTENCE_PREFIX_MAX_WORDS = 3
_WORD_POP_PARTIAL_SENTENCE_MIN_SUFFIX_WORDS = 3
_WORD_POP_PARTIAL_START_EPS_S = 0.001

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
# These are SOLO-LINE DEFAULTS — used when a line has no successor (last line
# in a section), when consecutive lines are far enough apart that no overlap
# fires, when the user explicitly pinned fade values via cfg, or when the
# kill switch (`settings.lyric_dynamic_crossfade_enabled`) is off. For
# inter-line crossfades with the kill switch on, the §1c post-pass in
# `_inject_line` overrides BOTH sides' fade_in_ms / fade_out_ms with a
# matched per-pair window and tags the outgoing overlay with
# `fade_out_curve="sqrt"` (mirror-symmetric curves → unit-partition crossfade,
# no readable stacked text). See plans/mirea-we-ve-lost-memoized-shannon.md
# §1 for the geometry, §5 for the invariant tests.
_LINE_PRE_ROLL_S = 0.40
_LINE_POST_DWELL_S = 1.0
_LINE_NEXT_LINE_GAP_S = 0.10
_LINE_FADE_IN_MS = 50
_LINE_FADE_OUT_MS = 250
_LINE_HOLD_TO_NEXT_THRESHOLD_MS = 500
# Upper bound on cross-dissolve overlap with the next lyric line. The
# actual cap applied per-line is the minimum of this value and
# (fade_in_s + fade_out_s) when the kill switch is OFF; the kill-switch-on
# path uses just `max_overlap_s` since the §1c post-pass bounds the actual
# emitted window. Bounding by fade duration prevents ghosting when a long
# post_dwell is combined with short fades (in the legacy path).
_LINE_MAX_OVERLAP_S = 0.4
_LINE_DEFAULT_FONT_FAMILY = "Inter Tight"
_MIN_LINE_VISIBLE_S = 0.20

# Trailing-line drop threshold for `_select_section_lines`. A line whose
# clamped start lands in the last `_TRAILING_LINE_DROP_TAIL_S` of the
# section AND whose clamped duration is below `_TRAILING_LINE_DROP_MIN_DUR_S`
# is dropped rather than rendered as a sub-second flash that confuses the
# viewer. Classic case: Instant Crush preview window ends at section 20.0;
# L4 vocal starts at section 19.45 (after the LRC-anchor re-anchor), giving
# only ~0.55s of L4 vocal before the preview ends — better to not show
# any L4 text than to flash it. Distinct from `_MIN_LINE_VISIBLE_S` (which
# only fires on partially-clamped lines and at 0.20s); this rule
# additionally requires the line to be at the TAIL of the section, so a
# legitimately short fully-contained ad-lib mid-section still renders.
_TRAILING_LINE_DROP_TAIL_S = 1.0
_TRAILING_LINE_DROP_MIN_DUR_S = 1.0

# Crossfade duration clamps for the dynamic-scaling post-pass. Below the
# floor, the fade reads as a hard cut and the user perceives a flash rather
# than a transition. Above the ceiling, the fade visibly drags into L_N's
# own audible span — L_N goes translucent while the listener still hears
# its vocal. 30 / 400 ms come from observed thresholds on dense pop tracks.
_LINE_CROSSFADE_MIN_MS = 30
_LINE_CROSSFADE_MAX_MS = 400

# Short-line audible-hold guarantee for the OUTGOING line of a crossfade.
# L_N must hold full opacity for at least this many seconds of its own
# audible window BEFORE its fade-out begins; protects very short lines
# (≤300 ms vocal hits) from being almost-entirely a fade. When the audible
# window can't host MIN_FADE + this hold, §1g policy is hard cut (re-anchor
# nxt.section_start so no visual overlap remains).
_LINE_MIN_AUDIBLE_HOLD_S = 0.10

# Middle-line full-opacity hold for the §1c three-line reconciliation pass.
# If B.fade_in + B.fade_out would consume more than (B_visible − this),
# both adjacent crossfade windows are shrunk proportionally so B still
# reaches its peak alpha for at least this many milliseconds.
_LINE_MIN_MIDDLE_PEAK_HOLD_MS = 80

_FADE_OUT_CURVE_SQRT = "sqrt"


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
    # Song-time originals for downstream finalization. The line-style
    # finalization in template_orchestrate's `_collect_absolute_overlays`
    # recomputes audible_words from `original_words` against the post-snap
    # audio window. These are captured BEFORE any rebase or clamp — never
    # add `best_start_s` to them.
    original_text: str
    original_start_s_song: float
    original_end_s_song: float
    original_words: list[dict]
    # Set by the dynamic-crossfade post-pass to "sqrt" when this line is the
    # OUTGOING side of a `"crossfade"` pair decision. The renderer reads it
    # via overlay.get("fade_out_curve") and switches accel from 2.0 (default
    # lingering `1−p²`) to 0.5 (mirror-symmetric `1−√p`). None for the last
    # line of any section, sparse pairs, hard-cut / solo-demoted pairs,
    # user-override pairs, and the kill-switch-disabled path.
    fade_out_curve: str | None = None


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
    # Layer-1 gate (primary, silent): no publishable extraction → nothing to burn.
    # After 2026-05-27 migration, non-publishable Whisper-only outputs live on
    # `lyrics_whisper_draft` instead and never reach this function.
    if not lyrics_cached:
        log.info("lyric_inject_skipped_no_cache", reason="lyrics_cached missing")
        return recipe_dict

    # Layer-2 gate (defense in depth, loud): even if `lyrics_cached` is populated,
    # the embedded `source` must be in the injector's allowlist. Catches drift
    # cases — a stale `whisper_only` blob sneaking through a half-applied
    # migration, or a future source added to the agent without a paired
    # injector update.
    source = (lyrics_cached.get("source") or "") if isinstance(lyrics_cached, dict) else ""
    if source not in _INJECTOR_ALLOWED_SOURCES:
        log.warning(
            "lyric_injection_skipped_invariant_violated",
            reason="lyrics_cached.source not in injector allowlist",
            source=source or "<empty>",
            config_enabled=bool(cfg.get("enabled")),
        )
        return recipe_dict

    # NOTE: PR #343 introduced a `_caller_key_set` snapshot here to distinguish
    # "operator pinned fade_in_ms via the admin Test tab" from "style-set
    # baseline timings written by _apply_style_set_defaults". Empirically that
    # distinction did not exist in production: the admin Test tab UI submits
    # every form field on every render, including the prefilled defaults
    # `fade_in_ms=150` / `fade_out_ms=250`. `create_admin_lyrics_preview` in
    # routes/admin_music.py merges them into the Job's lyrics_config_effective
    # via effective_lyrics_config(), and the lyrics_preview_task passes that
    # dict to inject_lyric_overlays. The snapshot then saw "user overrides"
    # for every preview/render and silently disabled the dynamic crossfade
    # post-pass — restoring the exact stacking PR #343 was supposed to fix.
    # See plans/mirea-we-ve-lost-memoized-shannon.md §F. The gate is removed:
    # when `LYRIC_DYNAMIC_CROSSFADE_ENABLED` is on, the dynamic post-pass
    # fires for every consecutive pair regardless of what cfg contains.
    # Operators who genuinely want legacy behavior flip the kill switch.

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

    sync_offset_s = _lyrics_sync_offset_s(cfg)
    if sync_offset_s:
        lyrics_cached = _shift_lyrics_cached(lyrics_cached, sync_offset_s)

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
        sync_offset_s=sync_offset_s,
        section_lines=len(section_lines),
        overlays_injected=injected,
    )
    return recipe_dict


# ── Internals ─────────────────────────────────────────────────────────────────


def _lyrics_sync_offset_s(cfg: dict) -> float:
    """Return a bounded whole-track lyrics timing correction.

    Positive values make lyrics render later; negative values make them render
    earlier. Validation normally bounds this at the route/schema layer, but the
    injector is deliberately fail-soft because it runs inside render jobs.
    """
    raw = cfg.get("sync_offset_s")
    if raw is None:
        return 0.0
    try:
        offset_s = float(raw)
    except (TypeError, ValueError):
        log.warning("lyric_sync_offset_ignored_invalid", value=raw)
        return 0.0
    if not math.isfinite(offset_s):
        log.warning("lyric_sync_offset_ignored_non_finite", value=raw)
        return 0.0
    return max(-5.0, min(5.0, offset_s))


def _shift_lyrics_cached(lyrics_cached: dict, offset_s: float) -> dict:
    """Return a shifted lyrics cache without mutating the persisted cache."""
    shifted = copy.deepcopy(lyrics_cached)
    lines = shifted.get("lines")
    if not isinstance(lines, list):
        return shifted
    for line in lines:
        if not isinstance(line, dict):
            continue
        _shift_timing_pair(line, offset_s)
        words = line.get("words")
        if isinstance(words, list):
            for word in words:
                if isinstance(word, dict):
                    _shift_timing_pair(word, offset_s)
    return shifted


def _shift_timing_pair(item: dict, offset_s: float) -> None:
    for key in ("start_s", "end_s"):
        if key not in item:
            continue
        try:
            shifted = float(item[key]) + offset_s
        except (TypeError, ValueError):
            continue
        item[key] = round(max(0.0, shifted), 6)


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
    _default("text_anchor", resolved.get("text_anchor"))
    _default("position_x_frac", resolved.get("position_x_frac"))
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
    """Return lines that overlap [best_start_s, best_end_s], clamped to the section.

    Returned lines have their `start_s`, `end_s`, and per-word `start_s`/`end_s`
    rebased so they're in **section-relative** coordinates (0 = best_start_s).
    Lines that straddle a section boundary are clamped to the section bounds.
    Used to be dropped — job dc33d047 surfaced the consequence: an 11.3s music
    job got exactly one survivor (`(Do think twice, do think twice)`) because
    every other lyric line in the song straddled best_start_s/best_end_s and
    silently dropped.
    Clamped lines whose remaining duration falls below `_MIN_LINE_VISIBLE_S`
    are still dropped to avoid one-word flashes — structured-logged so a
    coverage drift is debuggable from agent_run traces.
    Words that fall entirely outside the clamped window are dropped; words
    that straddle a clamp edge are clamped to the line bounds.
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

        clamped_ls = max(ls, best_start_s)
        clamped_le = min(le, best_end_s)
        partial = ls < best_start_s or le > best_end_s
        # The min-visible guard only applies to CLAMPED lines. A naturally
        # short fully-contained line (e.g. a 0.2s ad-lib like "yeah!") should
        # pass through unchanged — dropping it here would silently strip
        # legitimate one-word lyrics. Float-imprecision near the threshold
        # would also bite fully-contained lines if the guard ran unconditionally
        # (1.2 - 1.0 == 0.19999... < 0.20).
        if partial and clamped_le - clamped_ls < _MIN_LINE_VISIBLE_S:
            log.info(
                "lyric_inject_clamped_below_min_visible",
                line_start_s=ls,
                line_end_s=le,
                clamped_duration_s=round(clamped_le - clamped_ls, 3),
                min_visible_s=_MIN_LINE_VISIBLE_S,
            )
            continue
        if partial:
            log.info(
                "lyric_inject_clamped_partial",
                line_text=str(line.get("text", ""))[:80],
                line_start_s=ls,
                line_end_s=le,
                clamped_start_s=clamped_ls,
                clamped_end_s=clamped_le,
            )

        # Section-clamped rebased words (existing behavior, retained for the
        # karaoke / per-word-pop injectors and for debug only). The line-style
        # finalization pass recomputes audible_words from `original_words`
        # against the post-snap audio window, so it does NOT consume this list.
        rebased_words: list[dict] = []
        for w in line.get("words") or []:
            try:
                ws_abs = float(w.get("start_s", 0.0))
                we_abs = float(w.get("end_s", 0.0))
            except (TypeError, ValueError):
                continue
            if we_abs <= clamped_ls or ws_abs >= clamped_le:
                continue
            ws = max(ws_abs, clamped_ls) - best_start_s
            we = min(we_abs, clamped_le) - best_start_s
            rebased_words.append({"text": str(w.get("text", "")), "start_s": ws, "end_s": we})

        # `original_words` = full pre-rebase word list IN SONG TIME. Carries
        # words that fall outside the clamped section too — the line-style
        # finalization needs the full denominator for coverage_words and the
        # full input for the post-snap audible-word filter. NEVER add
        # best_start_s back to these (they're already song time).
        original_words: list[dict] = []
        for w in line.get("words") or []:
            try:
                ws_abs = float(w.get("start_s", 0.0))
                we_abs = float(w.get("end_s", 0.0))
            except (TypeError, ValueError):
                continue
            original_words.append(
                {"text": str(w.get("text", "")), "start_s_song": ws_abs, "end_s_song": we_abs}
            )

        out.append(
            {
                "text": str(line.get("text", "")),
                "start_s": clamped_ls - best_start_s,
                "end_s": clamped_le - best_start_s,
                "words": rebased_words,
                # Song-time originals for downstream finalization. The line
                # bounds are the values from lyrics_cached as-is (no rebase,
                # no clamp). Pair with `original_words` above. Captured before
                # any clamp so finalization can recompute the audible set
                # against the post-snap audio window.
                "original_text": str(line.get("text", "")),
                "original_start_s_song": ls,
                "original_end_s_song": le,
                "original_words": original_words,
                "clamped_from_start": ls < best_start_s,
                "clamped_from_end": le > best_end_s,
            }
        )

    # Trailing-line drop: a line whose clamped start lands in the last
    # `_TRAILING_LINE_DROP_TAIL_S` of the section AND whose clamped duration
    # is below `_TRAILING_LINE_DROP_MIN_DUR_S` is dropped rather than
    # rendered as a sub-second flash. If at least one word starts inside the
    # renderable section, keep it: lyric previews should show the full word
    # once the listener hears that word begin, even when its midpoint/end lands
    # just beyond the preview tail (Marea: "Marvellous" starts at 14.42s of a
    # 15.3s preview).
    if out:
        last = out[-1]
        section_dur_s = best_end_s - best_start_s
        last_dur_s = last["end_s"] - last["start_s"]
        starts_in_tail = last["start_s"] >= section_dur_s - _TRAILING_LINE_DROP_TAIL_S
        has_started_word = False
        for w in last.get("original_words") or []:
            try:
                word_start_s = float(w.get("start_s_song", -1.0))
            except (TypeError, ValueError):
                continue
            if best_start_s <= word_start_s < best_end_s:
                has_started_word = True
                break
        if starts_in_tail and last_dur_s < _TRAILING_LINE_DROP_MIN_DUR_S and not has_started_word:
            log.info(
                "lyric_inject_dropped_trailing_flash",
                line_text=str(last.get("text", ""))[:80],
                clamped_start_s=round(last["start_s"], 3),
                clamped_end_s=round(last["end_s"], 3),
                clamped_duration_s=round(last_dur_s, 3),
                section_dur_s=round(section_dur_s, 3),
                tail_threshold_s=_TRAILING_LINE_DROP_TAIL_S,
                min_dur_threshold_s=_TRAILING_LINE_DROP_MIN_DUR_S,
            )
            out.pop()
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
    if cfg.get("text_size_px") is not None:
        out["text_size_px"] = int(cfg["text_size_px"])
    if cfg.get("text_anchor"):
        out["text_anchor"] = cfg["text_anchor"]
    if cfg.get("position_x_frac") is not None:
        out["position_x_frac"] = float(cfg["position_x_frac"])
    if cfg.get("position_y_frac") is not None:
        out["position_y_frac"] = float(cfg["position_y_frac"])
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
                # Section-relative anchors capture the line's intended audio
                # position INDEPENDENT of slot windowing. The post-snap
                # re-anchor pass in `_collect_absolute_overlays` reads these
                # to keep the karaoke sweep glued to the vocal even after
                # beat-snap shifts the slot's cumulative offset. Line-style
                # overlays do NOT carry these fields, so the re-anchor pass
                # is a no-op for them — Line's behavior is byte-identical.
                "section_anchor_s": round(line["start_s"], 3),
                "section_end_anchor_s": round(line["end_s"], 3),
            }
        )
        for key in (
            "original_text",
            "original_start_s_song",
            "original_end_s_song",
            "original_words",
        ):
            if line.get(key) is not None:
                overlay[key] = copy.deepcopy(line[key])
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
    # Per-word-pop stages are cumulative: "You" -> "You may" -> ...
    # Center anchoring re-centers every new stage, so earlier words drift left as
    # the line grows. Pin the left edge for this style regardless of the set's
    # generic lyric defaults; font/color/size still come from the chosen set.
    base["text_anchor"] = "left"
    base["position_x_frac"] = float(cfg.get("position_x_frac") or 0.06)
    base["preserve_font_size"] = True
    injected = 0
    section_lines = _drop_nested_word_pop_lines(section_lines)

    for line_idx, line in enumerate(section_lines):
        word_dicts = [w for w in line.get("words", []) if (w.get("text") or "").strip()]
        word_dicts = _drop_leading_partial_sentence_fragment(line, word_dicts)
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
        line_end_s = float(line["end_s"])
        previous_line_end_s = float(section_lines[line_idx - 1]["end_s"]) if line_idx > 0 else None
        words = _repair_word_pop_late_malformed_line_start(
            words,
            previous_line_end_s=previous_line_end_s,
            line_end_s=line_end_s,
            line_text=str(line.get("text", "")),
        )
        words = _repair_word_pop_collapsed_timings(
            words,
            line_end_s=line_end_s,
            line_text=str(line.get("text", "")),
        )
        next_line_start_s = (
            float(section_lines[line_idx + 1]["start_s"])
            if line_idx + 1 < len(section_lines)
            else None
        )
        words, line_end_s = _truncate_word_pop_before_next_line(
            words,
            line_end_s=line_end_s,
            next_line_start_s=next_line_start_s,
            line_text=str(line.get("text", "")),
        )
        if not words:
            continue
        dwell_s = _LAST_WORD_DWELL_S
        if next_line_start_s is not None:
            dwell_budget_s = next_line_start_s - line_end_s
            if dwell_budget_s < dwell_s:
                # First words should pop onto a clean screen. The terminal
                # dwell is only breathing room, so spend it before the next
                # line and leave one video frame for the previous line to
                # clear when there is enough slack.
                dwell_s = max(0.0, dwell_budget_s - _WORD_POP_LINE_CLEAR_GAP_S)

        stages = build_cumulative_stages(
            words,
            line_end_s=line_end_s,
            dwell_s=dwell_s,
        )

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
                    # Section-relative anchors — see karaoke injector for the
                    # full rationale. The post-snap re-anchor pass in
                    # `_collect_absolute_overlays` glues each per-word stage
                    # to its vocal onset even when beat-snap shifts the slot.
                    # Line-style overlays do NOT carry these fields, so the
                    # pass is a no-op for them.
                    "section_anchor_s": round(stage.start_s, 3),
                    "section_end_anchor_s": round(stage.end_s, 3),
                }
            )
            _ensure_overlay_list(slots[slot_win.index]).append(overlay)
            injected += 1

    dropped = 0
    for slot in slots:
        overlays = slot.get("text_overlays")
        if not isinstance(overlays, list):
            continue
        before = _count_word_pop_overlays(overlays)
        slot["text_overlays"] = _enforce_word_pop_no_stacking(overlays)
        after = _count_word_pop_overlays(slot["text_overlays"])
        dropped += before - after

    return max(0, injected - dropped)


def _count_word_pop_overlays(overlays: list[dict]) -> int:
    return sum(
        1 for overlay in overlays if isinstance(overlay, dict) and _is_word_pop_overlay(overlay)
    )


def _coerce_finite_float(value: object) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _is_word_pop_overlay(overlay: dict) -> bool:
    if (
        overlay.get("role") != "lyrics"
        or overlay.get("effect") != "pop-in"
        or not bool(overlay.get("pop_animated_suffix"))
    ):
        return False
    return all(
        _coerce_finite_float(overlay.get(key)) is not None
        for key in ("start_s", "end_s", "section_anchor_s", "section_end_anchor_s")
    )


def _word_pop_screen_slot_key(overlay: dict) -> tuple[object, ...]:
    return (
        overlay.get("position", "bottom"),
        overlay.get("position_x_frac"),
        overlay.get("position_y_frac"),
        overlay.get("text_anchor", "left"),
    )


def _clip_word_pop_overlay_end(overlay: dict, new_end_s: float) -> dict | None:
    start_s = _coerce_finite_float(overlay.get("start_s"))
    current_end_s = _coerce_finite_float(overlay.get("end_s"))
    target_end_s = _coerce_finite_float(new_end_s)
    if start_s is None or current_end_s is None or target_end_s is None:
        return None

    end_s = min(current_end_s, target_end_s)
    if end_s - start_s < _MIN_RENDERABLE_S:
        return None

    out = dict(overlay)
    out["end_s"] = round(end_s, 3)
    section_anchor_s = _coerce_finite_float(out.get("section_anchor_s"))
    if section_anchor_s is not None:
        out["section_end_anchor_s"] = round(
            section_anchor_s + (end_s - start_s),
            3,
        )
    return out


def _enforce_word_pop_no_stacking(overlays: list[dict]) -> list[dict]:
    """Clip or drop same-lane pop-up stages so two lyric lines never stack."""
    word_pop_idxs = [
        idx
        for idx, overlay in enumerate(overlays)
        if isinstance(overlay, dict) and _is_word_pop_overlay(overlay)
    ]
    if len(word_pop_idxs) <= 1:
        return overlays

    out = list(overlays)
    word_pop_idxs.sort(key=lambda idx: (_coerce_finite_float(out[idx].get("start_s")) or 0.0, idx))
    dropped: set[int] = set()
    prev_by_slot: dict[tuple[object, ...], int] = {}
    for next_idx in word_pop_idxs:
        if next_idx in dropped:
            continue
        nxt = out[next_idx]
        slot_key = _word_pop_screen_slot_key(nxt)
        prev_idx = prev_by_slot.get(slot_key)
        prev_by_slot[slot_key] = next_idx
        if prev_idx is None or prev_idx in dropped:
            continue
        prev = out[prev_idx]
        prev_end = _coerce_finite_float(prev.get("end_s"))
        next_start = _coerce_finite_float(nxt.get("start_s"))
        if prev_end is None or next_start is None:
            continue
        if next_start >= prev_end - 1e-3:
            continue

        clipped = _clip_word_pop_overlay_end(prev, next_start)
        if clipped is None:
            dropped.add(prev_idx)
            log.info(
                "lyric_word_pop_overlap_dropped",
                line_text=str(prev.get("text", ""))[:80],
                next_text=str(nxt.get("text", ""))[:80],
                overlap_s=round(prev_end - next_start, 3),
            )
        else:
            out[prev_idx] = clipped
            log.info(
                "lyric_word_pop_overlap_clamped",
                line_text=str(prev.get("text", ""))[:80],
                next_text=str(nxt.get("text", ""))[:80],
                old_end_s=round(prev_end, 3),
                new_end_s=round(float(clipped["end_s"]), 3),
                overlap_s=round(prev_end - next_start, 3),
            )

    return [overlay for idx, overlay in enumerate(out) if idx not in dropped]


def _truncate_word_pop_before_next_line(
    words: list[_RevealWord],
    *,
    line_end_s: float,
    next_line_start_s: float | None,
    line_text: str,
) -> tuple[list[_RevealWord], float]:
    """Cut an outgoing popup line before the next line claims the visual lane."""
    if next_line_start_s is None or next_line_start_s >= line_end_s:
        return words, line_end_s

    cutoff_s = max(0.0, next_line_start_s - _WORD_POP_LINE_CLEAR_GAP_S)
    max_word_start_s = cutoff_s - _MIN_RENDERABLE_S
    kept = [
        _RevealWord(
            text=word.text,
            start_s=word.start_s,
            end_s=min(word.end_s, cutoff_s),
        )
        for word in words
        if word.start_s <= max_word_start_s
    ]
    if not kept:
        log.info(
            "lyric_word_pop_overlapping_line_dropped",
            line_text=line_text[:80],
            line_end_s=round(line_end_s, 3),
            next_line_start_s=round(next_line_start_s, 3),
        )
        return [], line_end_s

    new_line_end_s = min(line_end_s, cutoff_s)
    if len(kept) != len(words) or new_line_end_s != line_end_s:
        log.info(
            "lyric_word_pop_overlapping_line_truncated",
            line_text=line_text[:80],
            old_line_end_s=round(line_end_s, 3),
            new_line_end_s=round(new_line_end_s, 3),
            next_line_start_s=round(next_line_start_s, 3),
            dropped_words=[word.text for word in words[len(kept) :]],
        )
    return kept, new_line_end_s


def _repair_word_pop_collapsed_timings(
    words: list[_RevealWord],
    *,
    line_end_s: float,
    line_text: str,
) -> list[_RevealWord]:
    """Spread a collapsed timing cluster so pop-up reveal stays in word order.

    Whisper occasionally assigns a whole fast phrase to one tiny timestamp
    window, and can even put a later word's start before earlier siblings. For
    karaoke this is a bad highlight sweep; for pop-up it is worse: the
    cumulative builder drops sub-renderable stages and then reveals half the
    line at once. Repair only large clusters so legitimately fast two-word hits
    keep their source timing.
    """
    if len(words) < _WORD_POP_REPAIR_MIN_COLLAPSED_WORDS:
        return words

    repaired = list(words)
    i = 0
    changed = False
    while i < len(repaired) - 1:
        gap_s = repaired[i + 1].start_s - repaired[i].start_s
        if gap_s >= _MIN_RENDERABLE_S:
            i += 1
            continue

        run_start = i
        run_end = i + 1
        while run_end < len(repaired) - 1:
            next_gap_s = repaired[run_end + 1].start_s - repaired[run_end].start_s
            if next_gap_s >= _MIN_RENDERABLE_S:
                break
            run_end += 1

        run_len = run_end - run_start + 1
        if run_len < _WORD_POP_REPAIR_MIN_COLLAPSED_WORDS:
            i = run_end + 1
            continue

        repair_start = run_start
        repair_end = run_end
        span_start_s = repaired[repair_start].start_s
        next_start_s = (
            repaired[repair_end + 1].start_s if repair_end + 1 < len(repaired) else line_end_s
        )
        span_end_s = max(next_start_s, span_start_s)
        repair_words = repaired[repair_start : repair_end + 1]
        min_span_s = _MIN_RENDERABLE_S * len(repair_words)
        if span_end_s - span_start_s < min_span_s:
            i = run_end + 1
            continue

        weights = [1 for _word in repair_words]
        total_weight = float(sum(weights))
        cursor_weight = 0.0
        redistributed: list[_RevealWord] = []
        for idx, word in enumerate(repair_words):
            start_s = span_start_s + ((cursor_weight / total_weight) * (span_end_s - span_start_s))
            cursor_weight += weights[idx]
            if idx + 1 < len(repair_words):
                end_s = span_start_s + (
                    (cursor_weight / total_weight) * (span_end_s - span_start_s)
                )
            else:
                end_s = span_end_s
            redistributed.append(
                _RevealWord(
                    text=word.text,
                    start_s=round(start_s, 3),
                    end_s=round(max(end_s, start_s + _MIN_RENDERABLE_S), 3),
                )
            )

        repaired[repair_start : repair_end + 1] = redistributed
        changed = True
        log.info(
            "lyric_word_pop_collapsed_timing_repaired",
            line_text=line_text[:80],
            words=[w.text for w in repair_words],
            start_s=round(span_start_s, 3),
            end_s=round(span_end_s, 3),
        )
        i = repair_end + 1

    return repaired if changed else words


def _repair_word_pop_late_malformed_line_start(
    words: list[_RevealWord],
    *,
    previous_line_end_s: float | None,
    line_end_s: float,
    line_text: str,
) -> list[_RevealWord]:
    """Pull a malformed pop-up line earlier when its leading gap is implausible.

    This targets LRCLIB/Whisper drift where one line has a collapsed timing
    cluster and also starts far after the previous well-timed line. We only
    repair when both signals are present; real musical rests with clean word
    timings are left alone.
    """
    if previous_line_end_s is None or len(words) < _WORD_POP_REPAIR_MIN_COLLAPSED_WORDS:
        return words
    if not _has_word_pop_collapsed_timing_cluster(words):
        return words

    current_start_s = words[0].start_s
    leading_gap_s = current_start_s - previous_line_end_s
    if leading_gap_s <= _WORD_POP_MALFORMED_LINE_MAX_LEADING_GAP_S:
        return words

    target_start_s = previous_line_end_s + _WORD_POP_MALFORMED_LINE_TARGET_GAP_S
    if target_start_s >= current_start_s:
        return words
    span_s = line_end_s - target_start_s
    min_span_s = _MIN_RENDERABLE_S * len(words)
    if span_s < min_span_s:
        return words

    repaired = _redistribute_word_pop_words(
        words,
        span_start_s=target_start_s,
        span_end_s=line_end_s,
    )
    log.info(
        "lyric_word_pop_late_malformed_line_start_repaired",
        line_text=line_text[:80],
        previous_line_end_s=round(previous_line_end_s, 3),
        old_start_s=round(current_start_s, 3),
        new_start_s=round(target_start_s, 3),
        leading_gap_s=round(leading_gap_s, 3),
    )
    return repaired


def _has_word_pop_collapsed_timing_cluster(words: list[_RevealWord]) -> bool:
    run_len = 1
    for idx in range(len(words) - 1):
        gap_s = words[idx + 1].start_s - words[idx].start_s
        if gap_s < _MIN_RENDERABLE_S:
            run_len += 1
            if run_len >= _WORD_POP_REPAIR_MIN_COLLAPSED_WORDS:
                return True
        else:
            run_len = 1
    return False


def _redistribute_word_pop_words(
    words: list[_RevealWord],
    *,
    span_start_s: float,
    span_end_s: float,
) -> list[_RevealWord]:
    weights = [1 for _word in words]
    total_weight = float(sum(weights))
    cursor_weight = 0.0
    redistributed: list[_RevealWord] = []
    for idx, word in enumerate(words):
        start_s = span_start_s + ((cursor_weight / total_weight) * (span_end_s - span_start_s))
        cursor_weight += weights[idx]
        if idx + 1 < len(words):
            end_s = span_start_s + ((cursor_weight / total_weight) * (span_end_s - span_start_s))
        else:
            end_s = span_end_s
        redistributed.append(
            _RevealWord(
                text=word.text,
                start_s=round(start_s, 3),
                end_s=round(max(end_s, start_s + _MIN_RENDERABLE_S), 3),
            )
        )
    return redistributed


def _drop_leading_partial_sentence_fragment(line: dict, word_dicts: list[dict]) -> list[dict]:
    """Drop a clipped sentence tail from a pop-up line at the section start.

    Preview windows can begin in the middle of a cached lyric line. When the
    surviving words start with a tiny tail from the previous sentence
    (``do you? You men...``), Pop-up renders that tail as the opening scene.
    Keep normal mid-sentence suffixes, and only trim when a sentence boundary
    appears within the first few surviving words with a meaningful phrase after
    it.
    """
    if not word_dicts or not bool(line.get("clamped_from_start")):
        return word_dicts
    try:
        line_start_s = float(line.get("start_s", 0.0))
    except (TypeError, ValueError):
        return word_dicts
    if line_start_s > _WORD_POP_PARTIAL_START_EPS_S:
        return word_dicts

    original_text = str(line.get("original_text") or line.get("text") or "")
    token_matches = list(_LYRIC_TOKEN_RE.finditer(original_text))
    if not token_matches:
        return word_dicts

    token_norms = [_normalize_token(match.group(0)) for match in token_matches]
    word_norms = [_normalize_token(str(w.get("text", ""))) for w in word_dicts]
    word_norms = [norm for norm in word_norms if norm]
    if not word_norms:
        return word_dicts

    best_start = -1
    best_len = 0
    for token_idx, token_norm in enumerate(token_norms):
        if token_norm != word_norms[0]:
            continue
        matched = 0
        while (
            matched < len(word_norms)
            and token_idx + matched < len(token_norms)
            and token_norms[token_idx + matched] == word_norms[matched]
        ):
            matched += 1
        if matched > best_len:
            best_start = token_idx
            best_len = matched

    if best_start < 0 or best_len < 2:
        return word_dicts

    parenthetical_start_idx = _word_pop_parenthetical_suffix_start_index(
        original_text=original_text,
        token_matches=token_matches,
        token_norms=token_norms,
        match_start_idx=best_start,
        match_len=best_len,
    )
    if parenthetical_start_idx is not None:
        log.info(
            "lyric_word_pop_leading_parenthetical_prefix_dropped",
            line_text=original_text[:80],
            dropped_words=[str(w.get("text", "")) for w in word_dicts[:parenthetical_start_idx]],
        )
        return word_dicts[parenthetical_start_idx:]

    max_prefix = min(best_len, _WORD_POP_PARTIAL_SENTENCE_PREFIX_MAX_WORDS)
    for word_idx in range(max_prefix):
        token_idx = best_start + word_idx
        next_token_start = (
            token_matches[token_idx + 1].start()
            if token_idx + 1 < len(token_matches)
            else len(original_text)
        )
        suffix = original_text[token_matches[token_idx].end() : next_token_start]
        if not any(ch in suffix for ch in ".!?…"):
            continue
        suffix_word_count = len(word_dicts) - (word_idx + 1)
        if suffix_word_count < _WORD_POP_PARTIAL_SENTENCE_MIN_SUFFIX_WORDS:
            continue
        log.info(
            "lyric_word_pop_leading_partial_sentence_dropped",
            line_text=original_text[:80],
            dropped_words=[str(w.get("text", "")) for w in word_dicts[: word_idx + 1]],
        )
        return word_dicts[word_idx + 1 :]

    return word_dicts


def _word_pop_parenthetical_suffix_start_index(
    *,
    original_text: str,
    token_matches: list[re.Match[str]],
    token_norms: list[str],
    match_start_idx: int,
    match_len: int,
) -> int | None:
    """Return the word index where a clipped parenthetical hook should begin."""
    if match_start_idx <= 0 or match_len <= 1:
        return None

    match_end_idx = match_start_idx + match_len
    first_start_char = token_matches[match_start_idx].start()
    last_end_char = token_matches[match_end_idx - 1].end()
    open_idx = original_text.find("(", first_start_char, last_end_char)
    if open_idx < 0:
        return None

    close_idx = original_text.find(")", open_idx + 1)
    if close_idx >= 0 and close_idx < last_end_char:
        return None

    matched_indices = list(range(match_start_idx, match_end_idx))
    prefix_indices = [idx for idx in matched_indices if token_matches[idx].start() < open_idx]
    parenthetical_indices = [
        idx for idx in matched_indices if token_matches[idx].start() > open_idx
    ]
    if (
        not prefix_indices
        or len(prefix_indices) > _WORD_POP_PARTIAL_SENTENCE_PREFIX_MAX_WORDS
        or len(parenthetical_indices) < 2
    ):
        return None

    # Only drop the prefix when it looks like a repeated lead-in, e.g.
    # "Day by day (we've lost dancing)" clipped to "by day we've...".
    prior_norms = {norm for norm in token_norms[:match_start_idx] if norm}
    if token_norms[prefix_indices[-1]] not in prior_norms:
        return None

    return len(prefix_indices)


def _drop_nested_word_pop_lines(section_lines: list[dict]) -> list[dict]:
    """Drop short ad-lib lines that collide with a longer pop-up line.

    Per-word-pop has one visual lane: a cumulative main line and a short nested
    line cannot both be readable. Keep the longer line as the canonical lyric
    band and suppress tiny ad-libs like "Ok" / "Ok stop" that would otherwise
    render as a second pop-in event over the main sentence.
    """
    if len(section_lines) <= 1:
        return section_lines

    drop_idxs: set[int] = set()
    windows: list[tuple[int, float, float, int]] = []
    for idx, line in enumerate(section_lines):
        try:
            start_s = float(line.get("start_s", 0.0))
            end_s = float(line.get("end_s", 0.0))
        except (TypeError, ValueError):
            continue
        if end_s <= start_s:
            continue
        word_count = len([w for w in line.get("words", []) if (w.get("text") or "").strip()])
        windows.append((idx, start_s, end_s, word_count))

    for idx, start_s, end_s, word_count in windows:
        if word_count > _WORD_POP_NESTED_LINE_MAX_WORDS:
            continue
        for other_idx, other_start, other_end, other_word_count in windows:
            if idx == other_idx or other_word_count <= word_count:
                continue
            contained = (
                start_s >= other_start - _WORD_POP_NESTED_LINE_TOLERANCE_S
                and end_s <= other_end + _WORD_POP_NESTED_LINE_TOLERANCE_S
            )
            overlap_s = min(end_s, other_end) - max(start_s, other_start)
            duration_s = end_s - start_s
            overlaps_longer_line = (
                overlap_s >= _WORD_POP_SHORT_LINE_OVERLAP_DROP_S
                and overlap_s >= duration_s * _WORD_POP_SHORT_LINE_OVERLAP_DROP_RATIO
            )
            if not contained and not overlaps_longer_line:
                continue
            drop_idxs.add(idx)
            log.info(
                "lyric_word_pop_nested_line_dropped",
                line_text=str(section_lines[idx].get("text", ""))[:80],
                containing_line_text=str(section_lines[other_idx].get("text", ""))[:80],
                line_start_s=round(start_s, 3),
                line_end_s=round(end_s, 3),
            )
            break

    if not drop_idxs:
        return section_lines
    return [line for idx, line in enumerate(section_lines) if idx not in drop_idxs]


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

    # Kill-switch gate. ONE switch, ONE meaning: when
    # `LYRIC_DYNAMIC_CROSSFADE_ENABLED` is True (default), the dynamic
    # post-pass below ALWAYS fires for every consecutive line pair —
    # regardless of what cfg contains for fade_in_ms / fade_out_ms / etc.
    # When False, the scheduler reproduces pre-fix behavior byte-identically:
    # legacy additive `min(max_overlap_s, fade_in_s + fade_out_s)` overlap
    # cap, solo-default fade durations, no `fade_out_curve` key.
    #
    # PR #343 had a second condition here (`and not _any_user_override`)
    # that tried to honor "operator pinned fade values via admin Test tab".
    # That distinction did not exist in production — the admin UI submits
    # every form field with default values on every render, which
    # effective_lyrics_config() merged into the Job row's
    # lyrics_config_effective. The override gate read those defaults as
    # operator intent and silently disabled the dynamic post-pass, exactly
    # restoring the stacking bug PR #343 was supposed to fix. See plan §F.
    from app.config import settings as _app_settings  # noqa: PLC0415

    _dynamic_crossfade_enabled = bool(
        getattr(_app_settings, "lyric_dynamic_crossfade_enabled", True)
    )

    if _dynamic_crossfade_enabled:
        dynamic_max_overlap = max_overlap_s
    else:
        # Legacy additive cap. When a caller explicitly passes
        # fade_in_ms=0 / fade_out_ms=0 this collapses to 0 → no overlap
        # (intended pre-fix kill switch). Preserved exactly so the
        # kill-switch-off path is a true byte-identical rollback.
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

        # Song-time originals from `_select_section_lines`. Required for the
        # finalization pass; the metadata validator in
        # `_finalize_lyric_audible_window` falls back safely if any are
        # missing (logs `lyric_segments_missing_finalization_metadata` and
        # passes the overlay through unchanged).
        original_text = line.get("original_text")
        original_start_s_song = line.get("original_start_s_song")
        original_end_s_song = line.get("original_end_s_song")
        original_words = line.get("original_words")
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
                original_text=(str(original_text) if original_text is not None else line["text"]),
                original_start_s_song=(
                    float(original_start_s_song)
                    if original_start_s_song is not None
                    else float("nan")
                ),
                original_end_s_song=(
                    float(original_end_s_song) if original_end_s_song is not None else float("nan")
                ),
                original_words=list(original_words) if original_words else [],
            )
        )

    # ── Dynamic crossfade post-pass (§1b–§1d in the plan) ─────────────────
    # Gated entirely by `settings.lyric_dynamic_crossfade_enabled`. When off,
    # this block is skipped and the loop below emits line_windows with their
    # solo-default fade durations and no fade_out_curve key — byte-identical
    # to pre-fix output.
    if _dynamic_crossfade_enabled and len(line_windows) >= 2:
        from app.services.pipeline_trace import (  # noqa: PLC0415
            record_pipeline_event,
        )

        # ── Pair candidate assembly (§1b) ─────────────────────────────────
        pair_candidates: list[dict[str, Any]] = []
        for i in range(len(line_windows) - 1):
            cur = line_windows[i]
            nxt = line_windows[i + 1]

            # Defensive: skip if `nxt` isn't temporally after `cur`. Production
            # lyrics arrive in time order from Gemini/Whisper, but a scrambled
            # cache (or a future caller passing lines out of order) would make
            # natural_overlap_s meaningless — it could read as a huge "overlap"
            # that's actually a temporal gap in the wrong direction. The
            # legacy formula is implicitly robust via gap_cap; the dynamic
            # post-pass needs explicit protection.
            if nxt.line_start_s <= cur.line_start_s:
                # Trace the skip so non-monotonic input from a future caller
                # surfaces in the admin job-debug view instead of vanishing
                # silently into a "post-pass didn't run, why is it stacking?"
                # mystery. Cheap, zero-cost when never hit.
                record_pipeline_event(
                    "overlay",
                    "lyric_crossfade_skipped",
                    {
                        "line_idx": i,
                        "reason": "non_monotonic_line_start_s",
                        "cur_line_start_s": cur.line_start_s,
                        "nxt_line_start_s": nxt.line_start_s,
                    },
                )
                continue

            natural_overlap_s = max(0.0, cur.section_end_s - nxt.section_start_s)
            if natural_overlap_s <= 0:
                # Sparse pair — gap exceeded pre_roll + post_dwell. Solo defaults.
                continue

            raw_crossfade_ms = int(round(min(max_overlap_s, natural_overlap_s) * 1000))

            # Below-MIN: refuse to inflate fade metadata above the actual
            # emitted overlap window. A 10 ms emitted window with metadata
            # claiming 30 ms of fade silently breaks the unit-partition
            # invariant — the renderer's curve runs for 30 ms inside a 10 ms
            # frame budget. Fall through to solo for this pair.
            if raw_crossfade_ms < _LINE_CROSSFADE_MIN_MS:
                record_pipeline_event(
                    "overlay",
                    "lyric_crossfade_skipped",
                    {
                        "line_idx": i,
                        "reason": "below_min_overlap",
                        "natural_overlap_ms": int(natural_overlap_s * 1000),
                        "raw_crossfade_ms": raw_crossfade_ms,
                    },
                )
                continue

            crossfade_ms = min(_LINE_CROSSFADE_MAX_MS, raw_crossfade_ms)

            # Short-line safety on the OUTGOING side (cur). Cur's fade-out
            # region runs from section_end backward by crossfade_ms; it must
            # not extend into the first _LINE_MIN_AUDIBLE_HOLD_S of cur's
            # own vocal. If cur's audible window can't host MIN_FADE + this
            # hold, the §1g hard-cut policy applies.
            cur_audible_dur_ms = int((cur.section_end_s - cur.line_start_s) * 1000)
            max_safe_fade_out_ms = cur_audible_dur_ms - int(_LINE_MIN_AUDIBLE_HOLD_S * 1000)
            if max_safe_fade_out_ms < _LINE_CROSSFADE_MIN_MS:
                # Hard cut. Decision is committed in §1d apply; cur keeps
                # solo defaults, nxt.section_start re-anchored to cur.section_end
                # so the two overlays do not visually overlap.
                pair_candidates.append(
                    {
                        "i": i,
                        "cur": cur,
                        "nxt": nxt,
                        "decision": "hard_cut",
                        "natural_overlap_ms": int(natural_overlap_s * 1000),
                    }
                )
                continue

            crossfade_ms = min(crossfade_ms, max_safe_fade_out_ms)

            pair_candidates.append(
                {
                    "i": i,
                    "cur": cur,
                    "nxt": nxt,
                    "decision": "crossfade",
                    "natural_overlap_ms": int(natural_overlap_s * 1000),
                    "raw_crossfade_ms": raw_crossfade_ms,
                    "crossfade_ms": crossfade_ms,
                }
            )

        # ── Three-line conflict reconciliation (§1c) ──────────────────────
        # If middle line B's fade_in (from A→B crossfade) + fade_out (from
        # B→C crossfade) consumes more than (B_visible − MIN_PEAK_HOLD),
        # shrink the adjacent crossfade windows so B still reaches peak
        # alpha for at least MIN_PEAK_HOLD ms. Only operate on pairs whose
        # decision is "crossfade" — a sparse / hard_cut / solo_demoted
        # neighbor isn't a lever the post-pass can pull (its solo fade
        # contributes to budget but can't be shrunk by us).
        def _crossfade_candidate(target_i: int) -> dict | None:
            for _c in pair_candidates:
                if _c["i"] == target_i and _c["decision"] == "crossfade":
                    return _c
            return None

        def _demote_pair_to_solo(prev_index: int) -> None:
            for _c in pair_candidates:
                if _c["i"] == prev_index and _c["decision"] == "crossfade":
                    _c["decision"] = "solo_demoted"
                    return

        for _attempt in range(4):
            conflict_resolved_this_pass = False
            for j in range(1, len(line_windows) - 1):
                B = line_windows[j]
                prev_pair = _crossfade_candidate(j - 1)
                next_pair = _crossfade_candidate(j)
                if prev_pair is None and next_pair is None:
                    # No crossfade lever on either side. Sparse / hard_cut /
                    # solo_demoted neighbors mean B's fades are solo defaults
                    # whose geometry is the legacy behavior. Don't touch.
                    continue

                fi = int(prev_pair["crossfade_ms"]) if prev_pair is not None else int(B.fade_in_ms)
                fo = int(next_pair["crossfade_ms"]) if next_pair is not None else int(B.fade_out_ms)
                B_visible_ms = int((B.section_end_s - B.section_start_s) * 1000)
                budget = B_visible_ms - _LINE_MIN_MIDDLE_PEAK_HOLD_MS
                if fi + fo <= budget:
                    continue

                new_fi, new_fo = fi, fo
                if prev_pair is not None and next_pair is not None:
                    # Both sides shrinkable. Proportional reduction.
                    scale = budget / max(1, fi + fo)
                    new_fi = max(_LINE_CROSSFADE_MIN_MS, int(round(fi * scale)))
                    new_fo = max(_LINE_CROSSFADE_MIN_MS, int(round(fo * scale)))
                    if new_fi + new_fo > budget:
                        # Even with both at MIN_MS, sum exceeds budget.
                        # Demote whichever side has the LARGER crossfade.
                        if fi >= fo:
                            _demote_pair_to_solo(prev_index=j - 1)
                        else:
                            _demote_pair_to_solo(prev_index=j)
                        conflict_resolved_this_pass = True
                        continue
                    prev_pair["crossfade_ms"] = new_fi
                    next_pair["crossfade_ms"] = new_fo
                elif prev_pair is not None:
                    # Only A→B shrinkable; B's outgoing fade is solo.
                    new_fi = budget - fo
                    if new_fi < _LINE_CROSSFADE_MIN_MS:
                        _demote_pair_to_solo(prev_index=j - 1)
                        conflict_resolved_this_pass = True
                        continue
                    prev_pair["crossfade_ms"] = new_fi
                else:
                    # Only B→C shrinkable; B's incoming fade is solo.
                    new_fo = budget - fi
                    if new_fo < _LINE_CROSSFADE_MIN_MS:
                        _demote_pair_to_solo(prev_index=j)
                        conflict_resolved_this_pass = True
                        continue
                    next_pair["crossfade_ms"] = new_fo

                conflict_resolved_this_pass = True
                record_pipeline_event(
                    "overlay",
                    "lyric_crossfade_three_line_reconciled",
                    {
                        "middle_line_idx": j,
                        "B_visible_ms": B_visible_ms,
                        "budget_ms": budget,
                        "fi_before_ms": fi,
                        "fi_after_ms": new_fi,
                        "fo_before_ms": fo,
                        "fo_after_ms": new_fo,
                        "prev_is_crossfade": prev_pair is not None,
                        "next_is_crossfade": next_pair is not None,
                    },
                )

            if not conflict_resolved_this_pass:
                break

        # ── Apply pair_candidates to line_windows (§1d) ───────────────────
        for c in pair_candidates:
            cur = c["cur"]
            nxt = c["nxt"]
            if c["decision"] == "crossfade":
                crossfade_ms = int(c["crossfade_ms"])
                cur.fade_out_ms = crossfade_ms
                cur.fade_out_curve = _FADE_OUT_CURVE_SQRT
                nxt.fade_in_ms = crossfade_ms
                # Re-anchor so the ACTUAL emitted overlap equals
                # crossfade_ms exactly. The unit-partition identity in §2
                # holds geometrically because of this, not just algebraically.
                nxt.section_start_s = max(
                    nxt.section_start_s,
                    cur.section_end_s - crossfade_ms / 1000.0,
                )
                record_pipeline_event(
                    "overlay",
                    "lyric_crossfade_applied",
                    {
                        "line_idx": c["i"],
                        "natural_overlap_ms": c["natural_overlap_ms"],
                        "raw_crossfade_ms": c.get("raw_crossfade_ms"),
                        "applied_crossfade_ms": crossfade_ms,
                        "fade_out_curve": _FADE_OUT_CURVE_SQRT,
                    },
                )
            elif c["decision"] == "hard_cut":
                # No curve tag, no dynamic durations. Anchor nxt at
                # cur.section_end so no visual overlap remains.
                nxt.section_start_s = max(nxt.section_start_s, cur.section_end_s)
                record_pipeline_event(
                    "overlay",
                    "lyric_crossfade_hard_cut",
                    {
                        "line_idx": c["i"],
                        "cur_audible_ms": int((cur.section_end_s - cur.line_start_s) * 1000),
                        "natural_overlap_ms": c["natural_overlap_ms"],
                    },
                )
            elif c["decision"] == "solo_demoted":
                # Same anchor policy as hard_cut — for a different reason.
                nxt.section_start_s = max(nxt.section_start_s, cur.section_end_s)
                record_pipeline_event(
                    "overlay",
                    "lyric_crossfade_solo_demoted",
                    {"line_idx": c["i"]},
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
                    # Song-time originals required by the line-style
                    # finalization pass in `_collect_absolute_overlays`.
                    # Written to EVERY segment so any survivor of merge
                    # carries them. Finalizer recomputes `audible_words`
                    # from `original_words` against the post-snap audio
                    # window; it does NOT consume the section-clamped
                    # `words` list.
                    "original_text": line.original_text,
                    "original_start_s_song": line.original_start_s_song,
                    "original_end_s_song": line.original_end_s_song,
                    "original_words": line.original_words,
                }
            )
            # Emit fade_out_curve ONLY when set and ONLY on the final segment
            # (mid-segments emit fade_out_ms=0 already, so the curve has
            # nothing to act on). Omitting the key when value would be None
            # preserves kill-switch byte-identity with pre-fix output.
            if segment_idx == len(segments) - 1 and line.fade_out_curve is not None:
                overlay["fade_out_curve"] = line.fade_out_curve
            _ensure_overlay_list(slots[slot_win.index]).append(overlay)
            injected += 1

    return injected


# ── Line-style finalization (audible-window text fidelity) ────────────────────
#
# Called by `_collect_absolute_overlays` AFTER Layer 1's
# `_consolidate_lyric_segments` merge. At that point each lyric-line is
# represented by ONE merged overlay carrying:
#
#   - `text`              — current displayed text (may equal original)
#   - `start_s`, `end_s`  — absolute video time
#   - `lyric_line_id`
#   - `original_text`, `original_start_s_song`, `original_end_s_song`,
#     `original_words`   — set by `_inject_line` (song time)
#   - `fade_in_ms`, `fade_out_ms`
#
# Finalization recomputes `audible_words` fresh from `original_words` against
# the post-snap audio window `[audio_mix_song_start_s, audio_mix_song_end_s)`
# using the per-word midpoint rule. It produces `display_text` (renderer-read
# field) and may shrink the overlay's abs window to fit the audible region.
# Dropped lyrics are removed from the output list.
#
# Plan: plans/geli-me-var-ama-hatalar-robust-reddy.md §2.

# Per-word survival: a word is "audible" iff its midpoint lies inside
# [audio_mix_song_start_s, audio_mix_song_end_s). Chosen for determinism,
# perceptual match, and resilience to Whisper edge-jitter (±20 ms typical).
# Document so it's not a magic heuristic.
_AUDIBLE_WORD_MIDPOINT_RULE = (
    "word_audible iff the word midpoint OR word start is in "
    "[audio_mix_song_start_s, audio_mix_song_end_s)"
)

# Quality thresholds (one input among several; not the sole input).
_NEAR_COMPLETE_DURATION = 0.9
_NEAR_COMPLETE_WORDS = 0.9
_INTERIOR_COVERAGE_FLOOR = 0.65
_BASIC_WORD_COUNT_FLOOR = 2
_BASIC_AUDIBLE_SPEECH_S_FLOOR = 0.75
_FINAL_LINE_TAIL_TOLERANCE_S = 0.25  # "final" iff abs_end within this of mix end
_LINE_VS_WORD_BOUNDS_MISMATCH_S = 0.1  # log if line-end differs from last-word-end by more
_FINALIZABLE_LYRIC_EFFECTS = frozenset({"lyric-line", "karaoke-line"})
_EPS = 1e-6


def _finalize_lyric_audible_window(
    overlays: list[dict],
    audio_mix_song_start_s: float,
    audio_mix_song_end_s: float,
) -> list[dict]:
    """Audible-window-aware finalization for lyric overlays.

    Contract (see plan §2i):
      - non-lyric overlays pass through unchanged in their original positions;
      - lyric overlays dropped by the decision procedure are removed;
      - lyric-line overlays kept may receive `display_text`, and karaoke-line
        overlays kept may receive rebuilt `word_timings`;
      - kept lyric overlays have `start_s`/`end_s` conservatively bounded to
        the audible region where needed.
      - The returned list IS what renderers consume.

    Args:
      overlays: full overlay list AFTER Layer 1 consolidation. May contain
        non-lyric overlays; they pass through unchanged.
      audio_mix_song_start_s: song-time start of the rendered audio mix
        (typically `best_start_s`).
      audio_mix_song_end_s: song-time end of the rendered audio mix
        (typically `best_start_s + total_audio_bearing_duration_s`).

    Returns:
      New list with the contract above. Original list is not mutated.
    """
    if audio_mix_song_end_s <= audio_mix_song_start_s:
        return list(overlays)

    audible_end_abs = audio_mix_song_end_s - audio_mix_song_start_s

    # ── final-line detection ─────────────────────────────────────────────
    # The naive picker (max raw `end_s` among all lyric overlays) misclassifies
    # the real audible tail when a LATER lyric line was admitted at config
    # time but sits entirely OUTSIDE the post-snap audible window (e.g. a
    # `best_end_s` that overshoots the rendered audio end, or a lyric line
    # whose audio is fully past `audio_mix_song_end_s`). The naive picker
    # would tag the inaudible config'd line as final and starve the actual
    # last-audible line of the permissive final-line quality floor — silently
    # dropping the tail the user can hear.
    #
    # Correct contract: a line is "final" iff its CLIPPED audible window
    # reaches within _FINAL_LINE_TAIL_TOLERANCE_S of the audible end. Ties
    # are allowed (multiple lines may share the same clipped_end after
    # compression). Fallback: if no line reaches the tolerance band, the
    # latest audible clipped_end wins (covers tracks where the rendered
    # window cuts well before any line tail).
    final_idxs: set[int] = set()
    audible_clipped_ends: list[tuple[int, float]] = []
    for idx, ov in enumerate(overlays):
        if ov.get("effect") not in _FINALIZABLE_LYRIC_EFFECTS:
            continue
        orig_start = ov.get("original_start_s_song")
        orig_end = ov.get("original_end_s_song")
        if orig_start is None or orig_end is None:
            # Missing metadata — `_finalize_one_lyric_line` will pass it
            # through unchanged; finality doesn't matter for that branch.
            continue
        try:
            orig_start_f = float(orig_start)
            orig_end_f = float(orig_end)
        except (TypeError, ValueError):
            continue
        clipped_start = max(orig_start_f, audio_mix_song_start_s)
        clipped_end = min(orig_end_f, audio_mix_song_end_s)
        if clipped_end <= clipped_start:
            # No audible overlap — cannot be final.
            continue
        audible_clipped_ends.append((idx, clipped_end))
        if clipped_end >= audio_mix_song_end_s - _FINAL_LINE_TAIL_TOLERANCE_S:
            final_idxs.add(idx)

    if not final_idxs and audible_clipped_ends:
        # No line reaches the tolerance band — promote the latest audible
        # clipped_end so we never strand the actual last-audible line.
        latest = max(audible_clipped_ends, key=lambda item: item[1])[0]
        final_idxs.add(latest)

    out: list[dict] = []
    for idx, ov in enumerate(overlays):
        effect = ov.get("effect")
        if effect not in _FINALIZABLE_LYRIC_EFFECTS:
            out.append(ov)
            continue
        is_final = idx in final_idxs
        if effect == "karaoke-line":
            finalized = _finalize_one_karaoke_line(
                ov,
                audio_mix_song_start_s=audio_mix_song_start_s,
                audio_mix_song_end_s=audio_mix_song_end_s,
                audible_end_abs=audible_end_abs,
                is_final=is_final,
            )
        else:
            finalized = _finalize_one_lyric_line(
                ov,
                audio_mix_song_start_s=audio_mix_song_start_s,
                audio_mix_song_end_s=audio_mix_song_end_s,
                audible_end_abs=audible_end_abs,
                is_final=is_final,
            )
        if finalized is not None:
            out.append(finalized)
    return _enforce_finalized_karaoke_no_stacking(out)


def _same_karaoke_screen_slot(a: dict, b: dict) -> bool:
    return (
        a.get("effect") == "karaoke-line"
        and b.get("effect") == "karaoke-line"
        and a.get("position", "bottom") == b.get("position", "bottom")
        and a.get("position_x_frac") == b.get("position_x_frac")
        and a.get("position_y_frac") == b.get("position_y_frac")
        and a.get("text_anchor", "center") == b.get("text_anchor", "center")
    )


def _clip_finalized_karaoke_overlay_end(overlay: dict, new_end_s: float) -> dict | None:
    start_s = float(overlay.get("start_s", 0.0))
    end_s = min(float(overlay.get("end_s", 0.0)), float(new_end_s))
    if end_s - start_s < _MIN_OVERLAY_DURATION_S:
        return None

    out = dict(overlay)
    out["end_s"] = end_s
    span_s = end_s - start_s
    if out.get("section_anchor_s") is not None:
        try:
            out["section_end_anchor_s"] = round(float(out["section_anchor_s"]) + span_s, 3)
        except (TypeError, ValueError):
            pass

    raw_timings = overlay.get("word_timings") or []
    if not raw_timings:
        return out

    clipped_timings: list[dict] = []
    prev_end_rel = 0.0
    for wt in raw_timings:
        text = str(wt.get("text", "")).strip()
        if not text:
            continue
        try:
            ws = float(wt.get("start_s", 0.0))
            we = float(wt.get("end_s", 0.0))
        except (TypeError, ValueError):
            continue
        if we <= 0.0 or ws >= span_s:
            continue
        word = dict(wt)
        word["start_s"] = round(max(0.0, ws), 3)
        word["end_s"] = round(min(span_s, we), 3)
        if float(word["end_s"]) <= float(word["start_s"]):
            continue
        dur_s = max(0.05, float(word["end_s"]) - prev_end_rel)
        prev_end_rel = float(word["end_s"])
        word["duration_cs"] = max(5, int(round(dur_s * 100)))
        clipped_timings.append(word)

    if raw_timings and not clipped_timings:
        return None

    out["word_timings"] = clipped_timings
    out["text"] = " ".join(str(w["text"]) for w in clipped_timings).strip() or out.get("text", "")
    return out


def _enforce_finalized_karaoke_no_stacking(overlays: list[dict]) -> list[dict]:
    if len(overlays) <= 1:
        return overlays

    karaoke_idxs = [idx for idx, ov in enumerate(overlays) if ov.get("effect") == "karaoke-line"]
    if len(karaoke_idxs) <= 1:
        return overlays

    out = list(overlays)
    karaoke_idxs.sort(key=lambda idx: (float(out[idx].get("start_s", 0.0)), idx))

    dropped: set[int] = set()
    for prev_idx, next_idx in zip(karaoke_idxs, karaoke_idxs[1:], strict=False):
        if prev_idx in dropped or next_idx in dropped:
            continue
        prev = out[prev_idx]
        nxt = out[next_idx]
        if not _same_karaoke_screen_slot(prev, nxt):
            continue
        prev_end = float(prev.get("end_s", 0.0))
        next_start = float(nxt.get("start_s", 0.0))
        if next_start >= prev_end - 1e-3:
            continue
        clipped = _clip_finalized_karaoke_overlay_end(prev, next_start)
        if clipped is None:
            dropped.add(prev_idx)
            log.info(
                "karaoke_finalize_overlap_dropped",
                line_text=str(prev.get("text", ""))[:80],
                next_text=str(nxt.get("text", ""))[:80],
                overlap_s=round(prev_end - next_start, 3),
            )
        else:
            out[prev_idx] = clipped
            log.info(
                "karaoke_finalize_overlap_clamped",
                line_text=str(prev.get("text", ""))[:80],
                next_text=str(nxt.get("text", ""))[:80],
                old_end_s=round(prev_end, 3),
                new_end_s=round(float(clipped["end_s"]), 3),
                overlap_s=round(prev_end - next_start, 3),
            )

    return [ov for idx, ov in enumerate(out) if idx not in dropped]


def _finalize_one_lyric_line(
    overlay: dict,
    *,
    audio_mix_song_start_s: float,
    audio_mix_song_end_s: float,
    audible_end_abs: float,
    is_final: bool,
) -> dict | None:
    """Apply the decision procedure to one lyric-line overlay.

    Returns the (possibly-modified) overlay dict, or None if dropped.
    """
    line_id = overlay.get("lyric_line_id")
    required = (
        "original_text",
        "original_start_s_song",
        "original_end_s_song",
        "original_words",
    )
    missing = [k for k in required if overlay.get(k) is None]
    # NaN guards on the song-time fields (splitter writes NaN if upstream
    # `_select_section_lines` didn't set them — defense in depth).
    for k in ("original_start_s_song", "original_end_s_song"):
        if k not in missing:
            try:
                v = float(overlay[k])
                if v != v:  # NaN
                    missing.append(k)
            except (TypeError, ValueError):
                missing.append(k)
    if missing:
        log.warning(
            "lyric_segments_missing_finalization_metadata",
            line_id=line_id,
            missing_fields=missing,
        )
        # Safe passthrough: no shrink, no rewrite.
        return overlay

    original_text = str(overlay["original_text"])
    original_start_s_song = float(overlay["original_start_s_song"])
    original_end_s_song = float(overlay["original_end_s_song"])
    original_words = list(overlay.get("original_words") or [])

    # Empty-words short-circuit: tracks sourced from LRCLIB-plain (no Whisper
    # alignment), or lines where the per-word list was dropped upstream, have
    # NO surviving words by construction. Without this guard the decision
    # procedure would compute surviving_word_count=0, fail Step 1 (coverage_words
    # collapses), skip Step 2 (`surviving_word_count >= 2` is False), produce
    # no candidate_text, and DROP the line at Step 3 — silently regressing
    # every plain-lyric track that rendered fine pre-PR. Render the original
    # text when the line is mostly inside the audible window; drop otherwise
    # (line audio entirely outside the rendered window is the only legitimate
    # drop case for plain-lyric lines).
    if not original_words:
        clipped_start = max(original_start_s_song, audio_mix_song_start_s)
        clipped_end = min(original_end_s_song, audio_mix_song_end_s)
        overlap_s = max(0.0, clipped_end - clipped_start)
        original_dur = max(_EPS, original_end_s_song - original_start_s_song)
        cov = overlap_s / original_dur
        if cov < 0.5:
            log.info(
                "lyric_finalize_dropped_empty_words_outside_window",
                line_id=line_id,
                coverage_duration=round(cov, 4),
            )
            return None
        # Mostly audible — keep the original text (no word data to align
        # against), but still clamp the abs window to the audible video
        # window so post_dwell or splitter overhang past the audio mix end
        # doesn't render text after silence falls. Conservative clamp:
        # `min(current_end_s, audible_end_abs)` + `max(0, current_start_s)`,
        # same shape as `_apply_finalized`.
        out = dict(overlay)
        new_end_s = min(float(out.get("end_s", 0.0)), audible_end_abs)
        new_start_s = max(0.0, float(out.get("start_s", 0.0)))
        if new_end_s > new_start_s:
            out["end_s"] = new_end_s
            out["start_s"] = new_start_s
        log.info("lyric_finalize_empty_words_kept_with_clamp", line_id=line_id)
        return out

    # If the line-level bounds disagree materially with word-derived bounds,
    # log and prefer word-derived (user cleanup #5 / plan §2j).
    if original_words:
        first_w = original_words[0]
        last_w = original_words[-1]
        try:
            first_w_start = float(first_w.get("start_s_song", original_start_s_song))
            last_w_end = float(last_w.get("end_s_song", original_end_s_song))
        except (TypeError, ValueError):
            first_w_start = original_start_s_song
            last_w_end = original_end_s_song
        start_mismatch = abs(original_start_s_song - first_w_start)
        end_mismatch = abs(original_end_s_song - last_w_end)
        if (
            start_mismatch > _LINE_VS_WORD_BOUNDS_MISMATCH_S
            or end_mismatch > _LINE_VS_WORD_BOUNDS_MISMATCH_S
        ):
            log.warning(
                "lyric_finalize_line_bounds_word_mismatch",
                line_id=line_id,
                line_start_s_song=original_start_s_song,
                line_end_s_song=original_end_s_song,
                first_word_start_s_song=first_w_start,
                last_word_end_s_song=last_w_end,
                start_mismatch_s=round(start_mismatch, 4),
                end_mismatch_s=round(end_mismatch, 4),
            )
            original_start_s_song = first_w_start
            original_end_s_song = last_w_end

    # Compute audible_words fresh from original_words via midpoint rule.
    audible_words = [
        w for w in original_words if _word_audible(w, audio_mix_song_start_s, audio_mix_song_end_s)
    ]
    surviving_word_count = len(audible_words)
    original_word_count = len(original_words)

    # Per-line overlap (NOT audible section duration).
    clipped_start = max(original_start_s_song, audio_mix_song_start_s)
    clipped_end = min(original_end_s_song, audio_mix_song_end_s)
    overlap_s = max(0.0, clipped_end - clipped_start)
    original_dur = max(_EPS, original_end_s_song - original_start_s_song)
    coverage_duration = overlap_s / original_dur
    coverage_words = surviving_word_count / max(1, original_word_count)

    # Audible speech = sum of per-word audible durations clamped to window.
    audible_speech_s = 0.0
    for w in audible_words:
        try:
            ws = float(w.get("start_s_song", 0.0))
            we = float(w.get("end_s_song", 0.0))
        except (TypeError, ValueError):
            continue
        audible_speech_s += max(
            0.0, min(we, audio_mix_song_end_s) - max(ws, audio_mix_song_start_s)
        )

    # Step 1 — Near-complete: render original text unchanged. No log.
    if coverage_duration >= _NEAR_COMPLETE_DURATION and coverage_words >= _NEAR_COMPLETE_WORDS:
        return overlay

    # Step 2 — Compute candidate_text via alignment, fall back to conservative join.
    candidate_text: str | None = None
    candidate_source: str | None = None
    if surviving_word_count >= 2:
        rebuilt = _align_audible_words_to_original_text(
            original_text=original_text,
            audible_words=audible_words,
            drop_dangling_parenthetical_prefix=True,
        )
        if rebuilt is not None:
            candidate_text = rebuilt
            candidate_source = "alignment"
        else:
            candidate_text = " ".join(str(w.get("text", "")) for w in audible_words).strip()
            candidate_source = "conservative_join"
    elif surviving_word_count == 1:
        candidate_text = str(audible_words[0].get("text", "")).strip()
        candidate_source = "single_started_word"

    # Step 3 — No candidate text: drop unconditionally.
    if not candidate_text or not candidate_text.strip():
        log.info(
            "lyric_finalize_dropped_no_candidate_text",
            line_id=line_id,
            surviving_word_count=surviving_word_count,
        )
        return None

    # Step 4 — Final-partial-line quality floor (basic only, more permissive).
    if is_final:
        single_started_word_ok = (
            surviving_word_count == 1
            and candidate_source == "single_started_word"
            and audible_speech_s >= _BASIC_AUDIBLE_SPEECH_S_FLOOR
        )
        if audible_speech_s < _BASIC_AUDIBLE_SPEECH_S_FLOOR or (
            surviving_word_count < _BASIC_WORD_COUNT_FLOOR and not single_started_word_ok
        ):
            log.info(
                "lyric_finalize_final_line_dropped_fragment_too_short",
                line_id=line_id,
                audible_speech_s=round(audible_speech_s, 4),
                surviving_word_count=surviving_word_count,
            )
            return None
        return _apply_finalized(
            overlay,
            display_text=candidate_text,
            audible_end_abs=audible_end_abs,
            log_event="lyric_finalize_final_line_kept_truncated",
            source=candidate_source,
            line_id=line_id,
        )

    # Step 5 — Interior-partial-line quality floor (basic AND coverage; stricter).
    interior_basic_ok = (
        audible_speech_s >= _BASIC_AUDIBLE_SPEECH_S_FLOOR
        and surviving_word_count >= _BASIC_WORD_COUNT_FLOOR
    )
    interior_coverage_ok = (
        coverage_duration >= _INTERIOR_COVERAGE_FLOOR or coverage_words >= _INTERIOR_COVERAGE_FLOOR
    )
    if not (interior_basic_ok and interior_coverage_ok):
        log.info(
            "lyric_finalize_dropped_interior_partial",
            line_id=line_id,
            audible_speech_s=round(audible_speech_s, 4),
            surviving_word_count=surviving_word_count,
            coverage_duration=round(coverage_duration, 4),
            coverage_words=round(coverage_words, 4),
        )
        return None

    # Step 6 — Interior partial meeting BOTH floors: render candidate.
    return _apply_finalized(
        overlay,
        display_text=candidate_text,
        audible_end_abs=audible_end_abs,
        log_event="lyric_finalize_interior_partial_kept_truncated",
        source=candidate_source,
        line_id=line_id,
    )


def _finalize_one_karaoke_line(
    overlay: dict,
    *,
    audio_mix_song_start_s: float,
    audio_mix_song_end_s: float,
    audible_end_abs: float,
    is_final: bool,
) -> dict | None:
    """Apply audible-window finalization to one karaoke-line overlay.

    Karaoke renderers consume `word_timings` directly, not `display_text`.
    When the selected section extends past the post-snap video duration, this
    function drops inaudible tails or rebuilds `word_timings` to the audible
    word subset so the yellow sweep cannot continue into unheard lyrics.
    """
    line_id = overlay.get("lyric_line_id") or overlay.get("section_anchor_s")
    required = (
        "original_text",
        "original_start_s_song",
        "original_end_s_song",
        "original_words",
    )
    missing = [k for k in required if overlay.get(k) is None]
    for k in ("original_start_s_song", "original_end_s_song"):
        if k not in missing:
            try:
                v = float(overlay[k])
                if v != v:  # NaN
                    missing.append(k)
            except (TypeError, ValueError):
                missing.append(k)
    if missing:
        log.warning(
            "karaoke_segments_missing_finalization_metadata",
            line_id=line_id,
            missing_fields=missing,
        )
        return overlay

    original_text = str(overlay["original_text"])
    original_start_s_song = float(overlay["original_start_s_song"])
    original_end_s_song = float(overlay["original_end_s_song"])
    original_words = list(overlay.get("original_words") or [])

    if not original_words:
        clipped_start = max(original_start_s_song, audio_mix_song_start_s)
        clipped_end = min(original_end_s_song, audio_mix_song_end_s)
        overlap_s = max(0.0, clipped_end - clipped_start)
        original_dur = max(_EPS, original_end_s_song - original_start_s_song)
        cov = overlap_s / original_dur
        if cov < 0.5:
            log.info(
                "karaoke_finalize_dropped_empty_words_outside_window",
                line_id=line_id,
                coverage_duration=round(cov, 4),
            )
            return None
        out = dict(overlay)
        new_end_s = min(float(out.get("end_s", 0.0)), audible_end_abs)
        new_start_s = max(0.0, float(out.get("start_s", 0.0)))
        if new_end_s > new_start_s:
            out["end_s"] = new_end_s
            out["start_s"] = new_start_s
        log.info("karaoke_finalize_empty_words_kept_with_clamp", line_id=line_id)
        return out

    first_w = original_words[0]
    last_w = original_words[-1]
    try:
        first_w_start = float(first_w.get("start_s_song", original_start_s_song))
        last_w_end = float(last_w.get("end_s_song", original_end_s_song))
    except (TypeError, ValueError):
        first_w_start = original_start_s_song
        last_w_end = original_end_s_song
    start_mismatch = abs(original_start_s_song - first_w_start)
    end_mismatch = abs(original_end_s_song - last_w_end)
    if (
        start_mismatch > _LINE_VS_WORD_BOUNDS_MISMATCH_S
        or end_mismatch > _LINE_VS_WORD_BOUNDS_MISMATCH_S
    ):
        log.warning(
            "karaoke_finalize_line_bounds_word_mismatch",
            line_id=line_id,
            line_start_s_song=original_start_s_song,
            line_end_s_song=original_end_s_song,
            first_word_start_s_song=first_w_start,
            last_word_end_s_song=last_w_end,
            start_mismatch_s=round(start_mismatch, 4),
            end_mismatch_s=round(end_mismatch, 4),
        )
        original_start_s_song = first_w_start
        original_end_s_song = last_w_end

    audible_words = [
        w for w in original_words if _word_audible(w, audio_mix_song_start_s, audio_mix_song_end_s)
    ]
    surviving_word_count = len(audible_words)
    original_word_count = len(original_words)

    clipped_start = max(original_start_s_song, audio_mix_song_start_s)
    clipped_end = min(original_end_s_song, audio_mix_song_end_s)
    overlap_s = max(0.0, clipped_end - clipped_start)
    original_dur = max(_EPS, original_end_s_song - original_start_s_song)
    coverage_duration = overlap_s / original_dur
    coverage_words = surviving_word_count / max(1, original_word_count)

    audible_speech_s = 0.0
    for w in audible_words:
        try:
            ws = float(w.get("start_s_song", 0.0))
            we = float(w.get("end_s_song", 0.0))
        except (TypeError, ValueError):
            continue
        audible_speech_s += max(
            0.0, min(we, audio_mix_song_end_s) - max(ws, audio_mix_song_start_s)
        )

    if coverage_duration >= _NEAR_COMPLETE_DURATION and coverage_words >= _NEAR_COMPLETE_WORDS:
        return overlay

    candidate_text: str | None = None
    candidate_source: str | None = None
    if surviving_word_count >= 2:
        rebuilt = _align_audible_words_to_original_text(
            original_text=original_text,
            audible_words=audible_words,
            drop_dangling_parenthetical_prefix=True,
        )
        if rebuilt is not None:
            candidate_text = rebuilt
            candidate_source = "alignment"
        else:
            candidate_text = " ".join(str(w.get("text", "")) for w in audible_words).strip()
            candidate_source = "conservative_join"
    elif surviving_word_count == 1:
        candidate_text = str(audible_words[0].get("text", "")).strip()
        candidate_source = "single_started_word"

    if not candidate_text or not candidate_text.strip():
        log.info(
            "karaoke_finalize_dropped_no_candidate_text",
            line_id=line_id,
            surviving_word_count=surviving_word_count,
        )
        return None

    if is_final:
        single_started_word_ok = (
            surviving_word_count == 1
            and candidate_source == "single_started_word"
            and audible_speech_s >= _BASIC_AUDIBLE_SPEECH_S_FLOOR
        )
        if audible_speech_s < _BASIC_AUDIBLE_SPEECH_S_FLOOR or (
            surviving_word_count < _BASIC_WORD_COUNT_FLOOR and not single_started_word_ok
        ):
            log.info(
                "karaoke_finalize_final_line_dropped_fragment_too_short",
                line_id=line_id,
                audible_speech_s=round(audible_speech_s, 4),
                surviving_word_count=surviving_word_count,
            )
            return None
        return _apply_finalized_karaoke(
            overlay,
            audible_words=audible_words,
            display_text=candidate_text,
            audio_mix_song_start_s=audio_mix_song_start_s,
            audible_end_abs=audible_end_abs,
            line_clipped_from_start=original_start_s_song < audio_mix_song_start_s,
            log_event="karaoke_finalize_final_line_kept_truncated",
            source=candidate_source,
            line_id=line_id,
        )

    interior_basic_ok = (
        audible_speech_s >= _BASIC_AUDIBLE_SPEECH_S_FLOOR
        and surviving_word_count >= _BASIC_WORD_COUNT_FLOOR
    )
    interior_coverage_ok = (
        coverage_duration >= _INTERIOR_COVERAGE_FLOOR or coverage_words >= _INTERIOR_COVERAGE_FLOOR
    )
    if not (interior_basic_ok and interior_coverage_ok):
        log.info(
            "karaoke_finalize_dropped_interior_partial",
            line_id=line_id,
            audible_speech_s=round(audible_speech_s, 4),
            surviving_word_count=surviving_word_count,
            coverage_duration=round(coverage_duration, 4),
            coverage_words=round(coverage_words, 4),
        )
        return None

    return _apply_finalized_karaoke(
        overlay,
        audible_words=audible_words,
        display_text=candidate_text,
        audio_mix_song_start_s=audio_mix_song_start_s,
        audible_end_abs=audible_end_abs,
        line_clipped_from_start=original_start_s_song < audio_mix_song_start_s,
        log_event="karaoke_finalize_interior_partial_kept_truncated",
        source=candidate_source,
        line_id=line_id,
    )


def _word_audible(
    word: dict,
    audio_mix_song_start_s: float,
    audio_mix_song_end_s: float,
) -> bool:
    """Return true when a word is meaningfully audible in the rendered window.

    Midpoint-in-window keeps the existing leading-edge behavior. The
    start-in-window clause handles preview tails: once a word begins with a
    short readable overlap before the audio cut, render the whole word text
    instead of dropping it just because the word's midpoint falls after the cut.
    """
    try:
        ws = float(word.get("start_s_song", 0.0))
        we = float(word.get("end_s_song", 0.0))
    except (TypeError, ValueError):
        return False
    midpoint = (ws + we) / 2.0
    started_in_window = audio_mix_song_start_s <= ws < audio_mix_song_end_s
    audible_overlap_s = max(
        0.0,
        min(we, audio_mix_song_end_s) - max(ws, audio_mix_song_start_s),
    )
    return (audio_mix_song_start_s <= midpoint < audio_mix_song_end_s) or (
        started_in_window and audible_overlap_s >= _MIN_LINE_VISIBLE_S
    )


def _apply_finalized(
    overlay: dict,
    *,
    display_text: str,
    audible_end_abs: float,
    log_event: str,
    source: str | None,
    line_id: str | None,
) -> dict:
    """Write `display_text` + conservative abs window onto a copy of `overlay`.

    Conservative end clamp: `min(current_end_s, audible_end_abs)`. We do NOT
    shrink to `clipped_end - audio_mix_song_start_s` because the splitter's
    `post_dwell` intentionally extends the visual window past the line's
    audio end for the YouTube-lyric-video "settle time" UX (PR #287). We
    only protect against overhang past the audible mix end. Start side: only
    bound to `>= 0` (the splitter never schedules a negative start).
    """
    out = dict(overlay)
    out["display_text"] = display_text
    new_end_s = min(float(out.get("end_s", 0.0)), audible_end_abs)
    new_start_s = max(0.0, float(out.get("start_s", 0.0)))
    if new_end_s > new_start_s:
        out["end_s"] = new_end_s
        out["start_s"] = new_start_s
    log.info(log_event, line_id=line_id, source=source)
    return out


def _apply_finalized_karaoke(
    overlay: dict,
    *,
    audible_words: list[dict],
    display_text: str,
    audio_mix_song_start_s: float,
    audible_end_abs: float,
    line_clipped_from_start: bool = False,
    log_event: str,
    source: str | None,
    line_id: str | None,
) -> dict | None:
    """Rewrite a karaoke overlay to the audible word subset.

    Unlike lyric-line overlays, karaoke has no renderer-level `display_text`
    path. The rendered words come from `word_timings`, so truncation must
    rebuild that payload and keep each word's local timing aligned to the
    overlay's absolute start.
    """
    out = dict(overlay)
    new_start_s = max(0.0, float(out.get("start_s", 0.0)))
    new_end_s = min(float(out.get("end_s", 0.0)), audible_end_abs)
    if new_end_s <= new_start_s:
        log.info("karaoke_finalize_dropped_empty_window", line_id=line_id)
        return None

    span_s = new_end_s - new_start_s
    word_timings: list[dict] = []
    prev_end_rel = 0.0
    render_words = _trim_audible_words_to_display_text(audible_words, display_text)
    for idx, w in enumerate(render_words):
        text = str(w.get("text", "")).strip()
        if not text:
            continue
        try:
            ws_song = float(w.get("start_s_song", 0.0))
            we_song = float(w.get("end_s_song", 0.0))
        except (TypeError, ValueError):
            continue
        abs_word_start_s = max(0.0, ws_song - audio_mix_song_start_s)
        abs_word_end_s = min(audible_end_abs, we_song - audio_mix_song_start_s)
        local_start_s = max(0.0, abs_word_start_s - new_start_s)
        if idx == 0 and line_clipped_from_start and new_start_s <= _EPS:
            local_start_s = 0.0
        if local_start_s >= span_s:
            continue
        local_end_s = min(span_s, abs_word_end_s - new_start_s)
        if local_end_s <= local_start_s:
            local_end_s = min(span_s, local_start_s + 0.05)
        if local_end_s <= local_start_s:
            continue
        dur_s = max(0.05, local_end_s - prev_end_rel)
        prev_end_rel = local_end_s
        word_timings.append(
            {
                "text": text,
                "start_s": round(local_start_s, 3),
                "end_s": round(local_end_s, 3),
                "duration_cs": max(5, int(round(dur_s * 100))),
            }
        )

    if not word_timings:
        log.info("karaoke_finalize_dropped_no_word_timings", line_id=line_id)
        return None

    out["text"] = display_text
    out["word_timings"] = word_timings
    out["start_s"] = new_start_s
    out["end_s"] = new_end_s
    log.info(log_event, line_id=line_id, source=source)
    return out


# ── Punctuation-preserving alignment ──────────────────────────────────────────


# Token regex: Unicode-aware word characters, with apostrophe-containing
# contractions (don't, we'd) and hyphenated compounds (hard-headed) treated
# as single tokens. Leading-apostrophe words ('cause, 'til) are captured by
# the leading `['’]?` alternative (U+2019 = curly apostrophe).
# Parentheses and other punctuation are NOT tokens — they live in the
# original string and survive the substring slice intact.
_LYRIC_TOKEN_RE = re.compile(
    r"['’]?\w+(?:[-'’]\w+)*",
    flags=re.UNICODE,
)


def _normalize_token(text: str) -> str:
    """NFKC normalize, casefold, and collapse curly apostrophe U+2019 → straight."""
    return unicodedata.normalize("NFKC", text).replace("’", "'").casefold()


def _tokenize_lyric_text(text: str) -> list[tuple[int, int, str]]:
    """Return list of (start_char, end_char, normalized_text) tuples.

    Preserves original character positions for substring slicing; normalization
    is used for alignment matching only.
    """
    return [
        (m.start(), m.end(), _normalize_token(m.group(0))) for m in _LYRIC_TOKEN_RE.finditer(text)
    ]


def _align_audible_words_to_original_text(
    *,
    original_text: str,
    audible_words: list[dict],
    drop_dangling_parenthetical_prefix: bool = False,
) -> str | None:
    """Find a contiguous subsequence of tokens in `original_text` that matches
    the normalized `audible_words` text list, and return the original-string
    slice from the first matched token's start to the last matched token's end.

    Contract (tightened per review feedback #6): prefers EXACT full contiguous
    alignment. When the best contiguous run matches every audible word in
    order, return the original-string slice. When it matches only some
    audible words, return None AND log
    `lyric_align_partial_match_omits_word` with the omitted words — the
    caller will fall back to conservative join (`" ".join(audible_words)`),
    which is safer than silently dropping a real audible word into the
    substring slice.

    `drop_dangling_parenthetical_prefix` cleans up clipped backing-vocal
    parentheticals: if a section starts mid-line and the exact slice would
    render a single dangling word before a parenthetical hook
    (`day (we've lost dancing`), return just the parenthetical phrase. Karaoke
    callers must also trim their `word_timings` to the returned display text so
    text and timing rows stay in lockstep.

    Returns None on:
      - audible_words count < 2
      - no contiguous match found at all
      - best contiguous match covers fewer than every audible word

    Plan §2d. Curly apostrophes, leading apostrophes, hyphenated compounds,
    and parenthetical content are all preserved through original-string slicing.
    """
    if len(audible_words) < 2:
        return None

    tokens = _tokenize_lyric_text(original_text)
    if not tokens:
        return None

    audible_norm = [_normalize_token(str(w.get("text", ""))) for w in audible_words]
    audible_norm = [t for t in audible_norm if t]
    if len(audible_norm) < 2:
        return None

    n_aud = len(audible_norm)
    n_tok = len(tokens)
    # Find leftmost contiguous span in `tokens` matching `audible_norm`.
    # Two-pointer scan: anchor on tokens, advance audible-pointer on each
    # successive match; bail on first mismatch and slide anchor forward.
    best_start: int | None = None
    best_end: int | None = None
    best_matched_count = 0
    for anchor in range(n_tok - n_aud + 1):
        ti = anchor
        ai = 0
        while ti < n_tok and ai < n_aud:
            if tokens[ti][2] == audible_norm[ai]:
                ai += 1
                ti += 1
            else:
                break
        matched = ai
        if matched > best_matched_count:
            best_matched_count = matched
            best_start = anchor
            best_end = ti  # exclusive index of last matched token + 1
            if matched == n_aud:
                break  # full match; leftmost wins

    if best_start is None or best_end is None or best_end <= best_start:
        return None

    if best_matched_count < n_aud:
        # Partial contiguous match — would silently drop one or more
        # audible words from the displayed substring. Log the omitted
        # words so coverage drift is debuggable, then return None so
        # the caller uses the conservative join path (every audible word
        # rendered, even if punctuation is lost).
        omitted = audible_norm[best_matched_count:]
        log.info(
            "lyric_align_partial_match_omits_word",
            matched_count=best_matched_count,
            audible_count=n_aud,
            omitted_words=omitted[:10],  # cap to avoid log spam on long lines
        )
        return None

    first_start_char = tokens[best_start][0]
    last_end_char = tokens[best_end - 1][1]
    if drop_dangling_parenthetical_prefix:
        adjusted = _drop_dangling_parenthetical_prefix(
            original_text=original_text,
            tokens=tokens,
            match_start_idx=best_start,
            match_end_idx=best_end,
        )
        if adjusted:
            return adjusted
    return original_text[first_start_char:last_end_char]


def _drop_dangling_parenthetical_prefix(
    *,
    original_text: str,
    tokens: list[tuple[int, int, str]],
    match_start_idx: int,
    match_end_idx: int,
) -> str | None:
    """Return parenthetical text when a clipped line leaves one dangling prefix word.

    Example: original ``Day by day (we've lost dancing)`` clipped to audible
    tokens ``day we've lost dancing`` should display ``we've lost dancing``, not
    ``day (we've lost dancing``. Only fire when the match is already a leading
    truncation (`match_start_idx > 0`) and the text after ``(`` stays inside the
    same parenthetical group.
    """
    if match_start_idx <= 0 or match_end_idx <= match_start_idx:
        return None

    first_start_char = tokens[match_start_idx][0]
    last_end_char = tokens[match_end_idx - 1][1]
    open_idx = original_text.find("(", first_start_char, last_end_char)
    if open_idx < 0:
        return None

    close_idx = original_text.find(")", open_idx + 1)
    if close_idx >= 0 and close_idx < last_end_char:
        return None

    matched_tokens = tokens[match_start_idx:match_end_idx]
    prefix_tokens = [token for token in matched_tokens if token[0] < open_idx]
    parenthetical_tokens = [token for token in matched_tokens if token[0] > open_idx]
    if (
        not prefix_tokens
        or len(prefix_tokens) > _WORD_POP_PARTIAL_SENTENCE_PREFIX_MAX_WORDS
        or len(parenthetical_tokens) < 2
    ):
        return None

    prefix_norm = prefix_tokens[-1][2]
    prior_norms = {token[2] for token in tokens[:match_start_idx]}
    if prefix_norm not in prior_norms:
        return None

    return original_text[parenthetical_tokens[0][0] : last_end_char].strip() or None


def _trim_audible_words_to_display_text(audible_words: list[dict], display_text: str) -> list[dict]:
    """Return the contiguous audible-word subset represented by display_text.

    Karaoke finalization rebuilds renderer-facing `word_timings` from
    `audible_words`. When punctuation-preserving alignment drops a stale prefix
    from `display_text` (for example Marea's dangling pre-parenthetical
    ``day``), the timing rows must drop the same prefix or the renderer will
    still draw it. If we cannot prove a contiguous token match, keep the
    original list; rendering every audible word is safer than silently removing
    a real vocal.
    """
    if not audible_words:
        return audible_words

    display_norm = [token[2] for token in _tokenize_lyric_text(display_text)]
    display_norm = [token for token in display_norm if token]
    if not display_norm or len(display_norm) >= len(audible_words):
        return audible_words

    audible_indexed_norm = [
        (idx, token)
        for idx, w in enumerate(audible_words)
        if (token := _normalize_token(str(w.get("text", ""))))
    ]
    if len(display_norm) > len(audible_indexed_norm):
        return audible_words

    audible_norm = [token for _, token in audible_indexed_norm]
    for start_idx in range(0, len(audible_norm) - len(display_norm) + 1):
        end_idx = start_idx + len(display_norm)
        if audible_norm[start_idx:end_idx] == display_norm:
            first_word_idx = audible_indexed_norm[start_idx][0]
            last_word_idx = audible_indexed_norm[end_idx - 1][0]
            return audible_words[first_word_idx : last_word_idx + 1]

    log.info(
        "karaoke_trim_display_text_no_word_match",
        display_text=display_text[:80],
        audible_words=[str(w.get("text", "")) for w in audible_words[:10]],
    )
    return audible_words
