"""overlay_autoplace — server-side resolution + validation of matcher output (plans/005).

The agent proposes {asset_id, slot, start_s, end_s, tier, reason, sfx_intent};
THIS module owns everything geometric and everything safety-critical:

  - aspect-aware slot resolution (decision 5A + outside-voice finding 4):
    `MediaOverlay.scale` is a WIDTH fraction; height follows source aspect. The
    slot's base scale shrinks until the rendered bbox fits inside the keep-out
    band (caption band = bottom 25%, top margin 3%). Portrait assets at "top"
    would otherwise stand ~88% of the canvas tall.
  - per-item validation with drop-tracing (decision 6A): unknown asset, bad
    timing, overlap (vs FRESH occupied intervals — finding 8), hook window,
    density cap 1/5s ceiling 10 (decision 11A). Valid remainder survives.
  - rule-based sfx mapping (decision 9A): `sfx_intent` → curated glossary
    effect by name preference. The agent never picks audio files.
  - heuristic fallback matcher for keyless machines (finding 10): token-overlap
    between asset text and transcript windows — the whole flow works without
    GEMINI_API_KEY, just with humbler matches.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass

import structlog

log = structlog.get_logger()

# Canvas is 9:16 (1080×1920). h_frac = scale * (1080/1920) / aspect, aspect = w/h.
_CANVAS_RATIO = 1080.0 / 1920.0
# slot → (y_frac preference, base width scale)
SLOTS: dict[str, tuple[float, float]] = {
    "top": (0.24, 0.72),
    "center": (0.50, 0.80),
    "full": (0.50, 1.0),
}
# Keep-out band (decision 5A): captions burn in the bottom 25%; keep 3% top margin.
_BAND_TOP = 0.03
_BAND_BOTTOM = 0.75
_MAX_H_FRAC = 0.60  # a card never dominates more than ~60% of the canvas height

_MIN_ON_SCREEN_S = 1.5
_MAX_ON_SCREEN_S = 10.0
_HOOK_WINDOW_S = 2.5
_HOOK_BURST_WINDOW_S = 12.0
_HOOK_BURST_MIN_STAGGER_S = 0.8
_HOOK_BURST_MAX_CONCURRENT = 5
_DENSITY_WINDOW_S = 5.0  # max 1 suggestion per 5s of runtime (decision 11A)
_CEILING = 10
_SNAP_TOLERANCE_S = 0.75  # snap start to the nearest word start within this window

_TIER_ORDER = {"confident": 0, "likely": 1}

# sfx_intent → glossary-name preference list (decision 9A, rule-based).
SFX_INTENT_PREFERENCES: dict[str, tuple[str, ...]] = {
    "pop_in": ("pop", "bubble", "click", "whoosh"),
    "whoosh": ("whoosh", "swoosh", "swipe", "pop"),
    "click": ("click", "tap", "pop"),
}


@dataclass
class ResolvedPlacement:
    x_frac: float
    y_frac: float
    scale: float


def resolve_slot(slot: str, aspect: float | None) -> ResolvedPlacement:
    """Slot × asset aspect → (x, y, scale) whose rendered bbox fits the keep-out band.

    aspect = width/height. Unknown aspect → assume 1.0 (square, conservative).
    """
    y_pref, base_scale = SLOTS.get(slot, SLOTS["top"])
    a = float(aspect) if aspect and aspect > 0 else 1.0
    scale = base_scale
    if slot == "full" and a < 1.0:
        # Full-screen takeover is landscape-only; portrait falls back to center.
        y_pref, scale = SLOTS["center"]
    h_frac = scale * _CANVAS_RATIO / a
    max_h = min(_MAX_H_FRAC, _BAND_BOTTOM - _BAND_TOP)
    if h_frac > max_h:
        scale = scale * (max_h / h_frac)
        h_frac = max_h
    # Center the card so the whole bbox stays inside the band.
    y = min(max(y_pref, _BAND_TOP + h_frac / 2), _BAND_BOTTOM - h_frac / 2)
    return ResolvedPlacement(x_frac=0.5, y_frac=round(y, 4), scale=round(scale, 4))


def _overlaps(a_start: float, a_end: float, intervals: list[tuple[float, float]]) -> bool:
    return any(a_start < e and s < a_end for s, e in intervals)


def _is_hook_burst_card(*, kind: str, tier: str, start: float, end: float) -> bool:
    return (
        kind == "image"
        and tier == "confident"
        and start >= 0.0
        and start < _HOOK_BURST_WINDOW_S
        and end <= _HOOK_BURST_WINDOW_S
    )


def _hook_burst_reject_reason(
    *,
    start: float,
    end: float,
    taken: list[tuple[float, float, bool]],
) -> str | None:
    concurrent = 0
    for s, e, is_hook in taken:
        if not (start < e and s < end):
            continue
        if not is_hook:
            return "overlap"
        concurrent += 1
    if concurrent >= _HOOK_BURST_MAX_CONCURRENT:
        return "hook_burst_concurrency"
    if any(
        is_hook and abs(start - s) < (_HOOK_BURST_MIN_STAGGER_S - 1e-6) for s, _, is_hook in taken
    ):
        return "hook_burst_stagger"
    return None


def _snap_to_word_start(start_s: float, words: list[dict]) -> float:
    best, best_d = start_s, _SNAP_TOLERANCE_S + 1
    for w in words:
        try:
            ws = float(w.get("start_s", 0.0))
        except (TypeError, ValueError):
            continue
        d = abs(ws - start_s)
        if d < best_d:
            best, best_d = ws, d
    return best if best_d <= _SNAP_TOLERANCE_S else start_s


def map_sfx_intent(
    intent: str, glossary: list[dict], *, allow_fallback: bool = True
) -> dict | None:
    """Rule-based intent → glossary effect (decision 9A). Returns partial
    SoundEffectPlacement fields, or None ("none" intent / empty glossary).

    `glossary` rows: {"id","name","audio_gcs_path","duration_s"} — ready+published only.
    """
    if intent == "none" or not glossary:
        return None
    prefs = SFX_INTENT_PREFERENCES.get(intent, SFX_INTENT_PREFERENCES["pop_in"])
    for pref in prefs:
        for fx in glossary:
            name = str(fx.get("name", "")).lower()
            if pref in name and fx.get("audio_gcs_path"):
                return {
                    "sound_effect_id": str(fx.get("id")),
                    "src_gcs_path": str(fx.get("audio_gcs_path")),
                    "duration_s": fx.get("duration_s"),
                    "label": str(fx.get("name", ""))[:40],
                }
    if not allow_fallback:
        # Smart Captions never substitutes an unrelated/meme/female-voice clip
        # merely because the glossary contains one. Silence is safer than the
        # wrong sound at a numbered heading.
        return None
    # Legacy suggestion path: no name match falls back to the first usable
    # effect. Keep this default byte/behaviour-compatible for existing jobs.
    for fx in glossary:
        if fx.get("audio_gcs_path"):
            return {
                "sound_effect_id": str(fx.get("id")),
                "src_gcs_path": str(fx.get("audio_gcs_path")),
                "duration_s": fx.get("duration_s"),
                "label": str(fx.get("name", ""))[:40],
            }
    return None


def pacing_cap_s(main_duration_s: float) -> float:
    """Window-length pacing (plan 006, decision 2-A): pop-in cap scales with the
    MAIN video's length — clamp(main × 0.15, 4s, 10s). Applies to ALL card kinds
    (rhythm doesn't care about card type)."""
    return min(_MAX_ON_SCREEN_S, max(4.0, float(main_duration_s) * 0.15))


def pick_trim_window(
    moments: list, window_s: float, asset_duration_s: float | None
) -> tuple[float, float] | None:
    """Deterministic content-aware trim for a video card (plan 006, decision B).

    Returns (clip_trim_start_s, clip_trim_end_s) or None (= no trim fields).
    Rules: called with the FINAL clamped window (ordering pinned by decision B);
    highest-energy moment that fits wins, else the highest-energy moment's start,
    else 0; start is clamped into [0, duration − window]; unknown duration ⇒ None
    (never TypeError, never an ffmpeg-killing out-of-range trim).
    """
    if not asset_duration_s or float(asset_duration_s) <= 0:
        return None
    duration = float(asset_duration_s)
    window = min(float(window_s), duration)
    if window <= 0.1:
        return None

    def _m(m: dict) -> tuple[float, float, float]:
        try:
            return (
                float(m.get("start_s", 0.0)),
                float(m.get("end_s", 0.0)),
                float(m.get("energy", 0.0)),
            )
        except (TypeError, ValueError):
            return (0.0, 0.0, -1.0)

    parsed = [_m(m) for m in (moments or []) if isinstance(m, dict)]
    parsed = [(s, e, en) for s, e, en in parsed if en >= 0 and e > s]
    if not parsed:
        return None
    fitting = [(s, e, en) for s, e, en in parsed if (e - s) >= window]
    if fitting:
        start = max(fitting, key=lambda t: t[2])[0]
    else:
        start = max(parsed, key=lambda t: t[2])[0]
    start = min(max(0.0, start), max(0.0, duration - window))
    end = min(start + window, duration)
    if end - start <= 0.1:
        return None
    return round(start, 3), round(end, 3)


# tpad freeze allowance (decision D): a video card's window may exceed the
# asset's real footage by at most this much — a ≤1s freeze reads as a hold,
# a multi-second freeze reads as a broken render.
_FREEZE_ALLOWANCE_S = 1.0

# ── Fullscreen takeover enforcement (plan 009, rules (a)–(h)) ─────────────────
# Mode-aware constants table — these OVERRIDE pacing_cap_s and
# _FREEZE_ALLOWANCE_S for display_mode="fullscreen" ONLY (eng review E11).
_FS_MIN_S = 1.5
_FS_MAX_VIDEO_S = 4.0
_FS_MAX_IMAGE_S = 2.5  # a static full-frame image reads dead past this
_FS_MAX_PER_VIDEO = 2  # rule (b)
_FS_REANCHOR_GAP_S = 1.0  # rule (d): talking-head re-anchor around a takeover
_FS_PANORAMA_ASPECT = 2.2  # rule (f): wider than this never covers well
_FS_MIN_SHORT_SIDE_PX = 720  # rule (g): below this, cover-crop upscale is mush


def _shape_fullscreen_window(
    *,
    start: float,
    end: float,
    asset: dict,
    duration_s: float,
    intro_windows: list[tuple[float, float]],
    caption_cues: list[dict],
    fullscreen_count: int,
    taken: list[tuple[float, float]],
) -> tuple[float, float] | str:
    """Apply rules (a)–(h) to a slot="full" candidate.

    Returns the shaped (start, end) window, or a demote-reason string — the
    caller then renders the card as a centered pip instead (demote, never drop:
    the match itself is still good, only the takeover is disallowed).

    Rule (g) fails CLOSED: assets without persisted pixel dims (legacy uploads
    pre-ANALYSIS_VERSION-3, stub analyses) demote until the backfill heals them.
    """
    if fullscreen_count >= _FS_MAX_PER_VIDEO:
        return "cap"
    aspect = asset.get("aspect")
    if not aspect or float(aspect) <= 0:
        return "no_metadata"
    if float(aspect) > _FS_PANORAMA_ASPECT:
        return "panorama"
    analysis = asset.get("analysis") or {}
    width, height = analysis.get("width"), analysis.get("height")
    if not width or not height:
        return "no_dims"
    if min(int(width), int(height)) < _FS_MIN_SHORT_SIDE_PX:
        return "low_res"

    # Rule (a): never in the hook window or under intro text — shift forward.
    start = max(float(start), _HOOK_WINDOW_S)
    for iw_s, iw_e in sorted(intro_windows):
        if iw_s <= start < iw_e:
            start = iw_e

    # Rule (c) duration clamp + rule (e) zero freeze (video window ≤ footage).
    cap = _FS_MAX_VIDEO_S if asset.get("kind") == "video" else _FS_MAX_IMAGE_S
    window = min(max(float(end) - start, _FS_MIN_S), cap)
    if asset.get("kind") == "video" and asset.get("duration_s"):
        window = min(window, float(asset["duration_s"]))
    end = min(start + window, duration_s)

    # Rule (a) tail check: intro text starting DURING the window shortens it.
    for iw_s, iw_e in sorted(intro_windows):
        if start < iw_e and iw_s < end:
            end = min(end, iw_s)

    # Rule (h): caption-cue avoidance — when cues overlap, shorten toward the
    # 1.5s floor (never toward the ceiling) so muted viewers lose seconds of
    # captions per video, not the 4s worst case.
    def _cue_bounds(c: dict) -> tuple[float, float] | None:
        try:
            return float(c.get("start_s", 0.0)), float(c.get("end_s", 0.0))
        except (TypeError, ValueError):
            return None

    cue_windows = [b for b in (_cue_bounds(c) for c in caption_cues or []) if b]
    overlapping = [(cs, ce) for cs, ce in cue_windows if start < ce and cs < end]
    if overlapping:
        first_cue_start = min(cs for cs, _ in overlapping)
        if first_cue_start - start >= _FS_MIN_S:
            end = min(end, first_cue_start)
        else:
            end = min(end, start + _FS_MIN_S)

    if end - start < _FS_MIN_S:
        return "window_too_short"
    # Rule (d): ≥1s talking-head re-anchor gap around the takeover.
    if _overlaps(start - _FS_REANCHOR_GAP_S, end + _FS_REANCHOR_GAP_S, taken):
        return "gap"
    return round(start, 3), round(end, 3)


def build_suggestions(
    raw_placements: list,
    *,
    assets_by_id: dict[str, dict],
    words: list[dict],
    duration_s: float,
    occupied: list[tuple[float, float]],
    glossary: list[dict],
    trace: callable = None,
    fullscreen_enabled: bool = False,
    intro_windows: list[tuple[float, float]] | None = None,
    caption_cues: list[dict] | None = None,
    stats: dict | None = None,
) -> list[dict]:
    """Validate + resolve raw matcher output into OverlaySuggestion dumps.

    Per-item drop with trace (decision 6A); FRESH occupied intervals must be
    passed by the caller under the same row lock that persists (finding 8).
    `trace(event, **fields)` is optional (record_pipeline_event-shaped).

    Plan 009: when `fullscreen_enabled`, slot "full" becomes a real takeover —
    `_shape_fullscreen_window` enforces rules (a)–(h); an ineligible candidate
    DEMOTES to a centered pip (never dropped for mode reasons alone). When the
    flag is off, slot "full" falls through resolve_slot's legacy band-fit —
    byte-identical to pre-009 output (kill-switch contract).
    `stats` (mutated in place when passed): fullscreen_emitted / fullscreen_demoted
    counters + demote reasons — the zero-click receipt's data source.
    """
    from app.agents._schemas.overlay_suggestion import OverlaySuggestion  # noqa: PLC0415

    def _trace(event: str, **fields) -> None:
        if trace is not None:
            try:
                trace(event, **fields)
            except Exception:  # noqa: BLE001
                pass

    # Density budget (decision 11A).
    budget = min(_CEILING, max(1, int(duration_s / _DENSITY_WINDOW_S)))

    # Highest-confidence first so cap-overflow keeps the best (11A).
    candidates = sorted(
        list(raw_placements or []),
        key=lambda p: (
            _TIER_ORDER.get(getattr(p, "confidence_tier", "likely"), 1),
            float(getattr(p, "start_s", 0.0)),
        ),
    )

    taken: list[tuple[float, float, bool]] = [(s, e, False) for s, e in occupied]
    out: list[dict] = []
    fullscreen_count = 0
    density_count = 0
    for p in candidates:
        asset_id = str(getattr(p, "asset_id", ""))
        asset = assets_by_id.get(asset_id)
        if asset is None:
            _trace("autoplace_item_dropped", reason="unknown_asset", asset_id=asset_id)
            continue
        try:
            start = float(getattr(p, "start_s", 0.0))
            end = float(getattr(p, "end_s", 0.0))
        except (TypeError, ValueError):
            _trace("autoplace_item_dropped", reason="bad_timing", asset_id=asset_id)
            continue
        start = _snap_to_word_start(max(0.0, start), words)
        end = min(end, duration_s)
        asset_dur = asset.get("duration_s")

        # ── Fullscreen branch (plan 009): BEFORE resolve_slot, mode-aware ────
        slot = getattr(p, "slot", "top")
        kind = "video" if asset.get("kind") == "video" else "image"
        is_fs = bool(fullscreen_enabled) and slot == "full"
        if is_fs:
            if bool((asset.get("analysis") or {}).get("has_alpha")):
                shaped = "alpha"
            else:
                shaped = _shape_fullscreen_window(
                    start=start,
                    end=end,
                    asset=asset,
                    duration_s=duration_s,
                    intro_windows=list(intro_windows or []),
                    caption_cues=list(caption_cues or []),
                    fullscreen_count=fullscreen_count,
                    taken=[(s, e) for s, e, _ in taken],
                )
            if isinstance(shaped, str):
                # Demote to a centered pip — the MATCH is good, the takeover isn't.
                is_fs, slot = False, "center"
                _trace("autoplace_fullscreen_demoted", reason=shaped, asset_id=asset_id)
                if stats is not None:
                    stats["fullscreen_demoted"] = stats.get("fullscreen_demoted", 0) + 1
                    stats.setdefault("demote_reasons", []).append(shaped)
            else:
                start, end = shaped

        if not is_fs:
            if end - start < _MIN_ON_SCREEN_S:
                end = min(start + _MIN_ON_SCREEN_S, duration_s)
            max_on_screen = pacing_cap_s(duration_s)  # decision 2-A: all card kinds
            if end - start > max_on_screen:
                end = start + max_on_screen
            # Freeze cap (decision D): a video card never freezes more than
            # _FREEZE_ALLOWANCE_S — the window's tail tightens to the footage.
            if asset.get("kind") == "video" and asset_dur:
                freeze_limit = max(float(asset_dur) + _FREEZE_ALLOWANCE_S, _MIN_ON_SCREEN_S)
                if end - start > freeze_limit:
                    end = start + freeze_limit
            if end - start < _MIN_ON_SCREEN_S:
                _trace("autoplace_item_dropped", reason="too_short", asset_id=asset_id)
                continue
        tier = getattr(p, "confidence_tier", "likely")
        if not is_fs and start < _HOOK_WINDOW_S and tier != "confident":
            _trace("autoplace_item_dropped", reason="hook_window", asset_id=asset_id)
            continue
        if not is_fs and kind == "image" and tier == "confident" and start < _HOOK_BURST_WINDOW_S:
            window_len = max(0.0, end - start)
            for s, _, is_hook in sorted(taken):
                if is_hook and abs(start - s) < _HOOK_BURST_MIN_STAGGER_S:
                    start = round(s + _HOOK_BURST_MIN_STAGGER_S, 3)
                    end = min(start + window_len, duration_s)
        # Fullscreen already checked the PADDED interval inside shaping.
        is_hook_burst = (not is_fs) and _is_hook_burst_card(
            kind=kind, tier=tier, start=start, end=end
        )
        if not is_fs:
            if is_hook_burst:
                reject_reason = _hook_burst_reject_reason(start=start, end=end, taken=taken)
                if reject_reason is not None:
                    _trace("autoplace_item_dropped", reason=reject_reason, asset_id=asset_id)
                    continue
            elif _overlaps(start, end, [(s, e) for s, e, _ in taken]):
                _trace("autoplace_item_dropped", reason="overlap", asset_id=asset_id)
                continue
        if not is_hook_burst and density_count >= budget:
            _trace("autoplace_item_dropped", reason="density_cap", asset_id=asset_id)
            continue

        if is_fs:
            overlay = {
                "id": uuid.uuid4().hex,
                "kind": kind,
                "src_gcs_path": asset["gcs_path"],
                "position": "custom",
                "x_frac": 0.5,
                "y_frac": 0.5,
                "scale": 1.0,
                "display_mode": "fullscreen",
                "start_s": round(start, 3),
                "end_s": round(end, 3),
                "z": 10,
            }
        else:
            resolved = resolve_slot(slot, asset.get("aspect"))
            overlay = {
                "id": uuid.uuid4().hex,
                "kind": kind,
                "src_gcs_path": asset["gcs_path"],
                "position": "custom",
                "x_frac": resolved.x_frac,
                "y_frac": resolved.y_frac,
                "scale": resolved.scale,
                "start_s": round(start, 3),
                "end_s": round(end, 3),
                "z": 10,
            }
        if kind == "video" and asset_dur:
            overlay["clip_duration_s"] = float(asset_dur)
            # Content-aware trim (decision B ordering: computed AFTER the final
            # window clamps above). No best_moments / unknown duration ⇒ no trim
            # (plays from 0:00 — today's behavior, and the 1b backfill path).
            trim = pick_trim_window(
                (asset.get("analysis") or {}).get("best_moments") or [],
                end - start,
                asset_dur,
            )
            if trim is not None:
                overlay["clip_trim_start_s"], overlay["clip_trim_end_s"] = trim

        intent = getattr(p, "sfx_intent", "pop_in")
        if is_fs and intent == "pop_in":
            # Fullscreen default is the takeover's grammar: a whoosh, not a pop.
            intent = "whoosh"
        sfx_fields = map_sfx_intent(intent, glossary)
        sfx = None
        if sfx_fields is not None:
            sfx = {
                "id": uuid.uuid4().hex,
                "at_s": round(start, 3),
                "gain": 1.0,
                **sfx_fields,
            }

        try:
            suggestion = OverlaySuggestion(
                asset_id=asset_id,
                confidence_tier=tier,
                reason=str(getattr(p, "reason", "") or ""),
                transcript_anchor=str(getattr(p, "transcript_anchor", "") or "") or None,
                overlay=overlay,
                sfx=sfx,
            )
        except Exception as exc:  # noqa: BLE001
            _trace("autoplace_item_dropped", reason=f"schema:{exc}"[:120], asset_id=asset_id)
            continue
        if is_fs:
            # Reserve the re-anchor gap (rule d) so later cards keep distance.
            taken.append(
                (
                    max(0.0, start - _FS_REANCHOR_GAP_S),
                    min(duration_s, end + _FS_REANCHOR_GAP_S),
                    False,
                )
            )
            fullscreen_count += 1
            if stats is not None:
                stats["fullscreen_emitted"] = stats.get("fullscreen_emitted", 0) + 1
        else:
            taken.append((start, end, is_hook_burst))
        if not is_hook_burst:
            density_count += 1
        out.append(suggestion.model_dump())

    # Timeline order for the rail (10B was rejected — rows read as the video plays).
    out.sort(key=lambda s: s["overlay"]["start_s"])
    return out


# ── Heuristic fallback matcher (keyless dev machines — finding 10) ───────────

_STOPWORDS = frozenset(
    "the a an and or but of to in on at is are was were be been it this that i you "
    "we they my your our so if then than as for with without do does did not no yes "
    "just very really about into out up down over under again once here there".split()
)


def _tokens(text: str) -> set[str]:
    return {
        t
        for t in re.findall(r"[a-zA-Z0-9']+", (text or "").lower())
        if len(t) > 2 and t not in _STOPWORDS
    }


def _match_tokens(text: str) -> set[str]:
    return {
        t
        for t in re.findall(r"\w+", (text or "").casefold(), flags=re.UNICODE)
        if len(t) > 2 and t not in _STOPWORDS
    }


def filter_wishlist_against_assets(wishlist: list[str], assets: list[dict]) -> list[str]:
    """Drop wishlist rows that are already satisfied by an analyzed pool asset."""
    asset_token_sets: list[tuple[set[str], set[str]]] = []
    for asset in assets:
        analysis = asset.get("analysis") or {}
        brands = analysis.get("brands") if isinstance(analysis.get("brands"), list) else []
        brand_tokens = _match_tokens(" ".join(str(b or "") for b in brands))
        text_tokens = _match_tokens(
            " ".join(
                str(x or "")
                for x in (
                    analysis.get("subject"),
                    analysis.get("description"),
                    analysis.get("on_screen_text"),
                    asset.get("source_filename", "").rsplit(".", 1)[0].replace("-", " "),
                )
            )
        )
        if brand_tokens or text_tokens:
            asset_token_sets.append((brand_tokens, text_tokens))

    out: list[str] = []
    for wish in wishlist or []:
        wish_tokens = _match_tokens(str(wish or ""))
        if not wish_tokens:
            continue
        satisfied = any(
            bool(wish_tokens & brand_tokens) or len(wish_tokens & text_tokens) >= 2
            for brand_tokens, text_tokens in asset_token_sets
        )
        if not satisfied:
            out.append(str(wish).strip())
    return out


def heuristic_match(
    words: list[dict],
    assets: list[dict],
    *,
    duration_s: float,
    window_words: int = 8,
    min_overlap: int = 2,
) -> list:
    """Deterministic token-overlap matcher — the no-LLM fallback.

    Slides a window over the transcript; for each asset, the window with the
    largest token overlap against (subject + description + on_screen_text +
    filename) wins, if it clears `min_overlap`.
    Returns RawPlacement-shaped objects (duck-typed for build_suggestions).
    """
    from app.agents.overlay_placement import RawPlacement  # noqa: PLC0415

    placements: list[RawPlacement] = []
    for asset in assets:
        analysis = asset.get("analysis") or {}
        asset_text = " ".join(
            str(x or "")
            for x in (
                analysis.get("subject"),
                analysis.get("description"),
                analysis.get("on_screen_text"),
                asset.get("source_filename", "").rsplit(".", 1)[0].replace("-", " "),
            )
        )
        asset_tokens = _tokens(asset_text)
        if not asset_tokens:
            continue
        best_score, best_idx, best_anchor = 0, None, ""
        for i in range(len(words)):
            window = words[i : i + window_words]
            window_tokens = _tokens(" ".join(str(w.get("word", "")) for w in window))
            score = len(asset_tokens & window_tokens)
            if score > best_score:
                best_score, best_idx = score, i
                best_anchor = " ".join(str(w.get("word", "")) for w in window[:6])
        if best_idx is None or best_score < min_overlap:
            continue
        start = float(words[best_idx].get("start_s", 0.0))
        placements.append(
            RawPlacement(
                asset_id=str(asset["id"]),
                slot="top",
                start_s=start,
                # Keyless path obeys the pacing rule too (006 decision C).
                end_s=min(start + min(4.0, pacing_cap_s(duration_s)), duration_s),
                confidence_tier="likely",
                reason=f'Might match — your words here overlap "{best_anchor}".',
                transcript_anchor=best_anchor,
                sfx_intent="pop_in",
            )
        )
    return placements
