"use client";

/**
 * OverlayCardPopover — per-card edit popover for the Overlays lane.
 *
 * Extracted from OverlayLane.tsx (plan 009 T3 — the lane had grown past 600
 * lines and the plan mandates the split). Owns, top→bottom in fullscreen mode:
 *   (1) identity header + time range + ✕ Remove — never below the fold
 *       (suggestions are still removed via the rail's review index, not here)
 *   (2) segmented [PiP | Full screen] — mirrors the role="radiogroup" +
 *       disabled-when-selected pattern from PlanVariantEditor.tsx (same
 *       keyboard/aria semantics; colors adapted to the dark lane surface),
 *       ≥44px touch height
 *   (3) "Fills the whole frame. Your voice keeps playing underneath." +
 *       quiet "Show as small card instead" demote button
 *   (4) trim + timing number fields — the guaranteed editing path for chips
 *       too small to show edge handles
 * plus the non-blocking fullscreen warning stack (zinc tokens, worst first —
 * never amber on this surface) between (3) and (4).
 *
 * Fullscreen mode HIDES Position presets + Scale slider but never clears
 * x_frac/y_frac/scale — toggling back to PiP restores the prior layout
 * (plan 009 "Settled design"). All edits flow through onPatch → patchCard →
 * onUpdateCard/onSuggestionEdit, i.e. the existing 2.5s debounced autosave
 * path in page.tsx. No new persistence path.
 *
 * Keyboard: while the popover is mounted, F toggles display_mode (guarded
 * against input/textarea/select/contenteditable targets, and against events
 * a focused chip already handled via preventDefault).
 */

import { useEffect } from "react";
import type { MediaOverlay } from "@/lib/plan-api";

// ── Constants ─────────────────────────────────────────────────────────────────

export const MIN_SCALE = 0.05;
export const MAX_SCALE = 1.0;

const POSITION_PRESETS = [
  { label: "Top", value: "top" as const },
  { label: "Center", value: "center" as const },
  { label: "Bottom", value: "bottom" as const },
];

/** Minimum window / trim span the number fields will accept (seconds). */
const MIN_WINDOW_S = 0.1;

/** Tolerance before a fullscreen video window counts as outrunning its footage. */
export const OUTRUN_EPS_S = 0.05;

// ── Asset metadata ────────────────────────────────────────────────────────────

/**
 * Pixel/aspect metadata for an overlay's source asset. Overlays only carry
 * src_gcs_path, so the page resolves this from its asset pool and threads it
 * down (UnifiedTimeline → OverlayLane → here). All fields optional — dims
 * arrive with a later backend task; warnings that need a missing field are
 * suppressed, never faked.
 */
export interface OverlayAssetMeta {
  aspect?: number;
  width?: number;
  height?: number;
}

// ── Mode helpers ──────────────────────────────────────────────────────────────

/**
 * Patch that demotes a card to PiP. If the card has a prior pip layout in its
 * fracs those are kept (fracs are never cleared by fullscreen mode, so
 * toggle-back restores them); born-fullscreen cards without fracs fall back
 * to the "center" preset — one rule mirroring resolve_slot("center") on the
 * server path.
 */
export function demotePatch(card: MediaOverlay): Partial<MediaOverlay> {
  const hasPipLayout =
    card.x_frac != null && card.y_frac != null && card.scale != null;
  return hasPipLayout
    ? { display_mode: "pip" }
    : { display_mode: "pip", position: "center", x_frac: 0.5, y_frac: 0.5 };
}

/**
 * If a fullscreen VIDEO card's window outruns its trimmed footage, returns
 * the snapped end_s (start_s + available footage); otherwise null.
 * Fullscreen never freezes (plan 009 rule: snap, not freeze, for manual
 * fullscreen) — pip cards keep the plan-006 freeze behavior and return null.
 */
export function fullscreenOutrunSnapEnd(card: MediaOverlay): number | null {
  if ((card.display_mode ?? "pip") !== "fullscreen" || card.kind !== "video") return null;
  const trimEnd = card.clip_trim_end_s ?? card.clip_duration_s;
  if (trimEnd == null) return null;
  const footage = trimEnd - (card.clip_trim_start_s ?? 0);
  if (footage <= 0) return null;
  if (card.end_s - card.start_s <= footage + OUTRUN_EPS_S) return null;
  return Math.round((card.start_s + footage) * 10) / 10;
}

// ── Warnings ──────────────────────────────────────────────────────────────────

export type FullscreenWarningKey =
  | "hook"
  | "intro"
  | "outrun"
  | "aspect"
  | "lowres"
  | "total";

export interface FullscreenWarning {
  key: FullscreenWarningKey;
  message: string;
}

/**
 * Non-blocking warnings for a FULLSCREEN card, in severity order (worst
 * first — content coverage → quality → render time), exactly the plan-009
 * trigger table. Returns [] for pip cards. Aspect/resolution triggers are
 * suppressed (not faked) while the asset metadata is absent.
 */
export function computeFullscreenWarnings(opts: {
  card: MediaOverlay;
  introTextWindow?: { start_s: number; end_s: number } | null;
  assetMeta?: OverlayAssetMeta;
  /** Total seconds of manual fullscreen coverage across all cards. */
  manualFullscreenTotalS: number;
}): FullscreenWarning[] {
  const { card, introTextWindow, assetMeta, manualFullscreenTotalS } = opts;
  if ((card.display_mode ?? "pip") !== "fullscreen") return [];

  const warnings: FullscreenWarning[] = [];
  if (card.start_s < 2.5) {
    warnings.push({ key: "hook", message: "Covers your hook" });
  }
  if (
    introTextWindow != null &&
    card.start_s < introTextWindow.end_s &&
    card.end_s > introTextWindow.start_s
  ) {
    warnings.push({ key: "intro", message: "Covers your intro text" });
  }
  if (fullscreenOutrunSnapEnd(card) != null) {
    warnings.push({
      key: "outrun",
      message: "Clip ends early — cutaway will be shortened",
    });
  }
  if (assetMeta?.aspect != null && assetMeta.aspect > 1.2) {
    warnings.push({ key: "aspect", message: "Sides will be cropped" });
  }
  if (
    assetMeta?.width != null &&
    assetMeta?.height != null &&
    Math.min(assetMeta.width, assetMeta.height) < 720
  ) {
    warnings.push({
      key: "lowres",
      message: "Low resolution — this may look blurry full screen",
    });
  }
  if (manualFullscreenTotalS > 15) {
    warnings.push({
      key: "total",
      message: "Lots of full-screen time — this render may take longer",
    });
  }
  return warnings;
}

// ── Number field ──────────────────────────────────────────────────────────────

function NumField({
  label,
  value,
  min,
  max,
  onCommit,
}: {
  label: string;
  value: number;
  min: number;
  max: number;
  onCommit: (v: number) => void;
}) {
  function isCoarsePointer() {
    return (
      typeof window !== "undefined" &&
      typeof window.matchMedia === "function" &&
      window.matchMedia("(pointer: coarse)").matches
    );
  }

  function scrollIntoView(target: HTMLInputElement) {
    if (!isCoarsePointer()) return;
    if (typeof target.scrollIntoView !== "function") return;
    window.requestAnimationFrame(() => {
      target.scrollIntoView({ block: "center", inline: "nearest", behavior: "smooth" });
    });
  }

  return (
    <input
      type="number"
      step={0.1}
      min={Math.round(min * 10) / 10}
      max={Math.round(max * 10) / 10}
      aria-label={label}
      value={Number.isFinite(value) ? Math.round(value * 10) / 10 : 0}
      onChange={(e) => {
        const v = Number(e.target.value);
        if (!Number.isFinite(v)) return;
        onCommit(Math.min(max, Math.max(min, Math.round(v * 10) / 10)));
      }}
      onFocus={(e) => scrollIntoView(e.currentTarget)}
      className="h-11 w-20 rounded border border-zinc-600 bg-zinc-900 px-2 py-2 text-base text-white tabular-nums focus:border-amber-400/60 focus:outline-none sm:h-auto sm:w-16 sm:px-1.5 sm:py-1 sm:text-xs"
    />
  );
}

// ── Props ─────────────────────────────────────────────────────────────────────

export interface OverlayCardPopoverProps {
  card: MediaOverlay;
  /** Suggestion cards have no Remove here (the rail owns removal). */
  isSuggestion: boolean;
  totalDurationS: number;
  /** Intro-text window for the "Covers your intro text" warning. */
  introTextWindow?: { start_s: number; end_s: number } | null;
  /** Resolved asset metadata for crop/low-res warnings (optional — degrades). */
  assetMeta?: OverlayAssetMeta;
  /** Total seconds of manual fullscreen coverage (for the >15s warning). */
  manualFullscreenTotalS: number;
  /**
   * Plan 009 D5/E9: when set, the "Full screen" segmented option renders
   * disabled (aria-disabled, non-interactive) with this reason as a small
   * copy line below the control. The page sets it on lyric variants
   * ("Full-screen cutaways aren't available on lyric edits.") — the server's
   * 422 is the contract, this is the honest FE surface. Promote paths
   * (segmented option, F shortcut, max-scale affordance) all respect it;
   * demote stays available for legacy fullscreen cards.
   */
  fullscreenDisabledReason?: string | null;
  /**
   * R2 (review C8): web twin of the api FULLSCREEN_CUTAWAYS_ENABLED. When false
   * (default in Vercel until the Fly deploy carrying display_mode is live), the
   * NEW promote affordances are HIDDEN — the segmented "Full screen" option and
   * the "Make full screen →" max-scale affordance — so a previewed fullscreen
   * can't bake as pip against an old api that strips display_mode. This differs
   * from fullscreenDisabledReason (lyric variants), which DISABLES the option
   * with copy; the flag skew is silent version-management, so it just hides.
   * Demote paths and the rendering of EXISTING fullscreen cards are unaffected.
   * Defaults true (pre-flag behavior).
   */
  fullscreenPromoteEnabled?: boolean;
  /** Routes to onUpdateCard (manual) or onSuggestionEdit (suggestion). */
  onPatch: (patch: Partial<MediaOverlay>) => void;
  /** Present for manual cards only. */
  onRemove?: () => void;
}

// ── Component ─────────────────────────────────────────────────────────────────

export default function OverlayCardPopover({
  card,
  isSuggestion,
  totalDurationS,
  introTextWindow,
  assetMeta,
  manualFullscreenTotalS,
  fullscreenDisabledReason,
  fullscreenPromoteEnabled = true,
  onPatch,
  onRemove,
}: OverlayCardPopoverProps) {
  const isFullscreen = (card.display_mode ?? "pip") === "fullscreen";
  const fullscreenDisabled = fullscreenDisabledReason != null;
  // R2/C8: flag-off hides the NEW promote affordances entirely (a card ALREADY
  // fullscreen still shows its mode + demote — that's not a promote action).
  const showPromoteAffordances = fullscreenPromoteEnabled || isFullscreen;
  const scalePct = Math.round((card.scale ?? 0.35) * 100);
  const atMaxScale = scalePct >= Math.round(MAX_SCALE * 100);
  const clipDur = card.kind === "video" ? (card.clip_duration_s ?? null) : null;
  const trimStart = card.clip_trim_start_s ?? 0;
  const trimEnd = card.clip_trim_end_s ?? clipDur ?? 0;

  const warnings = computeFullscreenWarnings({
    card,
    introTextWindow,
    assetMeta,
    manualFullscreenTotalS,
  });

  // F toggles display_mode while the popover is open. Guarded against typing
  // targets and against events a focused chip already consumed (defaultPrevented).
  // No dep array: re-subscribes per render so the closure is always fresh —
  // only one popover mounts at a time, so this stays cheap.
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key !== "f" && e.key !== "F") return;
      if (e.defaultPrevented) return;
      const t = e.target as HTMLElement | null;
      if (
        t &&
        (t.tagName === "INPUT" ||
          t.tagName === "TEXTAREA" ||
          t.tagName === "SELECT" ||
          t.isContentEditable)
      ) {
        return;
      }
      e.preventDefault();
      // D5/E9: never promote to fullscreen while the mode is disabled
      // (lyric variants). R2/C8: also never promote while the fullscreen flag
      // is off (old-api skew). Demote stays available for legacy data in both.
      if (!isFullscreen && (fullscreenDisabled || !fullscreenPromoteEnabled)) return;
      onPatch(isFullscreen ? demotePatch(card) : { display_mode: "fullscreen" });
    }
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  });

  // Segmented pill — PlanVariantEditor's radiogroup pattern (disabled when
  // selected), restyled for the dark lane surface, ≥44px touch height.
  const pill = (selected: boolean) =>
    `flex-1 min-h-[44px] rounded-full border px-4 py-2 text-sm transition-colors disabled:cursor-not-allowed sm:min-h-0 sm:px-3 sm:py-1 sm:text-xs ${
      selected
        ? "border-white bg-white font-semibold text-[#0c0c0e]"
        : "border-zinc-600 text-zinc-300 hover:border-zinc-400"
    }`;

  return (
    <div
      className="bg-zinc-800 border border-zinc-700 rounded-lg px-3 py-2 mt-1 space-y-2"
      data-testid={`overlay-popover-${card.id}`}
    >
      {/* (1) identity header + time range + ✕ Remove — never below the fold */}
      <div className="flex items-center justify-between">
        <span className="text-xs text-white/40 font-mono">
          {isSuggestion && <span aria-hidden className="text-lime-500">✦ suggested · </span>}
          {card.kind === "video" ? "video" : "image"} · {card.id.slice(0, 8)}
        </span>
        <div className="flex gap-1">
          <span className="text-xs text-zinc-500 tabular-nums">
            {(card.start_s ?? 0).toFixed(1)}s – {(card.end_s ?? 0).toFixed(1)}s
          </span>
          {/* Suggestions are removed via the rail's × (review index), not here. */}
          {!isSuggestion && onRemove && (
            <button
              type="button"
              onClick={onRemove}
              className="ml-2 flex h-11 w-11 items-center justify-center text-xs text-white/30 hover:text-red-400 sm:h-auto sm:w-auto"
              aria-label="Remove card"
            >
              ✕
            </button>
          )}
        </div>
      </div>

      {/* (2) segmented [PiP | Full screen] — R2/C8: the "Full screen" promote
          option is hidden entirely when the flag is off on a pip card (a card
          already fullscreen keeps both so it can demote). */}
      <div role="radiogroup" aria-label="Display mode" className="flex gap-2">
        <button
          type="button"
          disabled={!isFullscreen}
          onClick={() => onPatch(demotePatch(card))}
          className={pill(!isFullscreen)}
        >
          PiP
        </button>
        {showPromoteAffordances && (
          <button
            type="button"
            // D5/E9: disabled both when selected (radiogroup pattern) and when
            // fullscreen is unavailable on this variant (lyrics) — the reason
            // copy below explains the latter.
            disabled={isFullscreen || fullscreenDisabled}
            aria-disabled={fullscreenDisabled || undefined}
            onClick={() => {
              if (fullscreenDisabled) return;
              onPatch({ display_mode: "fullscreen" });
            }}
            className={`${pill(isFullscreen)}${
              fullscreenDisabled && !isFullscreen ? " opacity-50" : ""
            }`}
          >
            Full screen
          </button>
        )}
      </div>
      {fullscreenDisabled && (
        <p
          data-testid="fullscreen-disabled-reason"
          className="text-[11px] text-zinc-400"
        >
          {fullscreenDisabledReason}
        </p>
      )}

      {/* (3) fullscreen explainer + quiet demote */}
      {isFullscreen && (
        <div className="space-y-1">
          <p className="text-xs text-zinc-400">
            Fills the whole frame. Your voice keeps playing underneath.
          </p>
          <button
            type="button"
            onClick={() => onPatch(demotePatch(card))}
            className="flex min-h-11 items-center text-xs text-zinc-400 underline underline-offset-2 transition-colors hover:text-white sm:min-h-0"
          >
            Show as small card instead
          </button>
        </div>
      )}

      {/* Non-blocking warning stack — zinc tokens, worst first, never amber */}
      {warnings.length > 0 && (
        <div className="space-y-1" data-testid="fullscreen-warnings">
          {warnings.map((w) => (
            <p
              key={w.key}
              data-warning={w.key}
              className="rounded border border-zinc-600 bg-zinc-900/70 px-2 py-1 text-[11px] text-zinc-300"
            >
              {w.message}
            </p>
          ))}
        </div>
      )}

      {/* Pip-only: Position presets + Scale slider (hidden in fullscreen —
          fracs are preserved so toggle-back restores the layout) */}
      {!isFullscreen && (
        <>
          <div className="flex gap-1">
            {POSITION_PRESETS.map((p) => (
              <button
                key={p.value}
                type="button"
                onClick={() => onPatch({ position: p.value })}
                className={`min-h-11 flex-1 rounded px-2 py-2 text-xs transition-colors sm:min-h-0 sm:py-1 ${
                  card.position === p.value
                    ? "bg-lime-400 text-black font-semibold"
                    : "bg-white/10 text-white/60 hover:bg-white/20"
                }`}
              >
                {p.label}
              </button>
            ))}
          </div>
          <div className="flex items-center gap-2">
            <span className="text-xs text-white/40 w-10">Scale</span>
            <input
              type="range"
              min={Math.round(MIN_SCALE * 100)}
              max={Math.round(MAX_SCALE * 100)}
              value={scalePct}
              onChange={(e) => onPatch({ scale: Number(e.target.value) / 100 })}
              className="h-11 flex-1 accent-lime-400 sm:h-auto"
            />
            <span className="text-xs text-white/60 w-14 text-right">
              {atMaxScale ? "Full width" : `${scalePct}%`}
            </span>
          </div>
          {/* R2/C8: hidden when the fullscreen flag is off (old-api skew). */}
          {atMaxScale && !fullscreenDisabled && fullscreenPromoteEnabled && (
            <button
              type="button"
              onClick={() => onPatch({ display_mode: "fullscreen" })}
              className="flex min-h-11 items-center text-xs text-zinc-300 underline underline-offset-2 transition-colors hover:text-white sm:min-h-0"
            >
              Make full screen →
            </button>
          )}
        </>
      )}

      {/* (4) trim + timing fields */}
      <div className="flex items-center gap-2">
        <span className="text-xs text-white/40 w-10">Timing</span>
        <NumField
          label="Start time (seconds)"
          value={card.start_s}
          min={0}
          max={Math.max(0, card.end_s - MIN_WINDOW_S)}
          onCommit={(v) => onPatch({ start_s: v })}
        />
        <span className="text-xs text-white/40">to</span>
        <NumField
          label="End time (seconds)"
          value={card.end_s}
          min={card.start_s + MIN_WINDOW_S}
          max={totalDurationS}
          onCommit={(v) => onPatch({ end_s: v })}
        />
        <span className="text-xs text-white/40">s</span>
      </div>
      {clipDur != null && clipDur > 0 && (
        <div className="flex items-center gap-2">
          <span className="text-xs text-white/40 w-10">Trim</span>
          <NumField
            label="Trim in (seconds)"
            value={trimStart}
            min={0}
            max={Math.max(0, trimEnd - MIN_WINDOW_S)}
            onCommit={(v) => onPatch({ clip_trim_start_s: v })}
          />
          <span className="text-xs text-white/40">to</span>
          <NumField
            label="Trim out (seconds)"
            value={trimEnd}
            min={trimStart + MIN_WINDOW_S}
            max={clipDur}
            onCommit={(v) => onPatch({ clip_trim_end_s: v })}
          />
          <span className="text-xs text-white/40">s</span>
        </div>
      )}
    </div>
  );
}
