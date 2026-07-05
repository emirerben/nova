"use client";

/**
 * HeroOverlayEditor — on-preview direct manipulation for AI overlay
 * suggestions (plans/007 Fix 2).
 *
 * While pending/staged suggestions exist for the focused variant, each kept
 * suggestion card renders over the hero video at its real position/size (same
 * percent math as the Hero CSS stack + rail mini-preview — overlayCardStyle,
 * one util, zero copies) and the creator can:
 *   - DRAG the card body   → patches x_frac/y_frac (position becomes "custom")
 *   - RESIZE via the bottom-right corner handle (≥44px hit area) → patches
 *     scale around the card CENTER (x/y unchanged), clamped to [0.05, 1.0]
 *   - Keyboard on a focused card: arrows move 1% (shift = 5%), +/- scale ±0.05
 *
 * Every gesture routes through the EXISTING onSuggestionEdit(id, patch) from
 * useOverlaySuggestionState (006 T3) — implicit staging, zero network until
 * the rail's Apply (stage-fires-no-network contract holds).
 *
 * Supersede record (007 tension G4-A): this pre-apply gesture surface formally
 * supersedes 006-tension-A's "zero new editing surface". Time-domain gestures
 * (window drag, clip trim) STAY in the lanes; spatial gestures (position,
 * size) live here — same onSuggestionEdit envelope, one state, two surfaces.
 *
 * Interaction hardening (007 findings 11/12):
 *   - The layer itself is pointer-events-none; only the CARDS (and, during an
 *     active gesture, a transparent backdrop that keeps stray clicks off the
 *     native video controls) are pointer-interactive.
 *   - A drag PAUSES the hero video (found via the shared container — this
 *     layer mounts inside the same aspect-[9/16] box as the <video>) and
 *     resumes playback on release if it was playing. Pausing also guarantees
 *     the card can't unmount mid-gesture; we additionally keep the active
 *     card mounted even if currentTimeS exits its window (belt & braces).
 *   - `touch-action: none` on the layer and every pointer target.
 *   - Mobile note (finding 12): this page has NO bottom-sheet/peek over the
 *     hero today — if 005-7A's sheet ever lands here, it must collapse while
 *     a drag is active so it never covers the hero mid-gesture.
 *
 * Plan 009 T4 — fullscreen cutaway entries (display_mode "fullscreen"):
 *   - NO drag / resize / keyboard-move: spatial gestures are meaningless for a
 *     full-frame takeover (and the mode toggle is NOT this surface — it lives
 *     in the timeline popover, T3).
 *   - Pending suggestions get an INSET dashed-lime outline (outline-offset
 *     -2px, never affecting layout) + the ✦ provenance badge INSIDE the frame;
 *     the resize handle is hidden. Applied/manual fullscreen cards render zero
 *     chrome (they live in LiveOverlayCardsLayer, not here).
 *   - Click-to-edit (E6): during the card's window the frame is ONE click
 *     target ("⛶ Full screen · edit" pill) that pauses the hero video and
 *     requests the timeline popover via onRequestEditCard. The bottom ~15% of
 *     the frame is a pointer PASS-THROUGH band (pointer-events:none stripe) so
 *     clicks over the native video controls reach the video; while the pointer
 *     hovers that band the card visual drops to ~40% opacity (visual cue
 *     only). This keeps 008's "no gestures in LiveOverlayCardsLayer"
 *     invariant: the fullscreen click surface lives HERE.
 *
 * Flag-gated like AssetPool/SuggestionRail:
 *   NEXT_PUBLIC_OVERLAY_AUTOPLACE_ENABLED === "true" AND entries non-empty.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import type { MediaOverlay } from "@/lib/plan-api";
import type { SuggestionLaneEntry } from "./UnifiedTimelineTypes";
import { mediaClassFor } from "./cardMedia";
import { overlayCardStyle } from "./overlayCardStyle";

/** Bottom fraction of a fullscreen card that passes pointer events through to
 *  the native video controls (E6). */
const FULLSCREEN_PASSTHROUGH_FRAC = 0.15;

/** Schema range for MediaOverlay.scale (mirrors the backend validator). */
const SCALE_MIN = 0.05;
const SCALE_MAX = 1.0;
/** Keyboard steps: arrows move 1% (5% with shift), +/- scale by 0.05. */
const MOVE_STEP = 0.01;
const MOVE_STEP_SHIFT = 0.05;
const SCALE_STEP = 0.05;

function clamp01(v: number): number {
  return Math.min(1, Math.max(0, v));
}

function clampScale(v: number): number {
  return Math.min(SCALE_MAX, Math.max(SCALE_MIN, v));
}

function safePlay(video: HTMLVideoElement) {
  try {
    const p = video.play();
    if (p && typeof p.catch === "function") p.catch(() => {});
  } catch {
    // jsdom / autoplay-blocked — resuming is best-effort.
  }
}

interface GestureState {
  entryId: string;
  mode: "move" | "resize";
  pointerId: number;
  startClientX: number;
  startClientY: number;
  origXFrac: number;
  origYFrac: number;
  origScale: number;
  /** Container (== content, see overlayCardStyle) box size at gesture start. */
  boxW: number;
  boxH: number;
  /** The hero <video> we paused on gesture start — resumed on release. */
  resumeVideo: HTMLVideoElement | null;
}

export default function HeroOverlayEditor({
  entries,
  onSuggestionEdit,
  currentTimeS,
  resolveCardUrl,
  onRequestEditCard,
}: {
  /** Kept suggestion entries ONLY (rail working rows via laneEntries) —
   *  manual/applied cards are owned by the lanes, never rendered here. */
  entries: SuggestionLaneEntry[];
  /** 006 T3 envelope-patch callback — implicit staging, no network. */
  onSuggestionEdit: (suggestionId: string, patch: Partial<MediaOverlay>) => void;
  /** Hero playhead (lifted from the video's timeupdate) — time-scopes cards. */
  currentTimeS: number;
  /** overlay → displayable thumbnail URL (signed pool display_url joined by
   *  src_gcs_path, plus any local blob previews). Built by the page. */
  resolveCardUrl?: (overlay: MediaOverlay) => string | undefined;
  /** Plan 009 T4 click-to-edit: a fullscreen card's frame click pauses the
   *  hero and requests the timeline popover for this card. For suggestion
   *  entries the id passed is the ENVELOPE id (the lane chip identity). */
  onRequestEditCard?: (cardId: string) => void;
}) {
  const enabled = process.env.NEXT_PUBLIC_OVERLAY_AUTOPLACE_ENABLED === "true";
  const layerRef = useRef<HTMLDivElement | null>(null);
  const [gesture, setGesture] = useState<GestureState | null>(null);
  /** E6 visual cue: pointer currently hovering the bottom pass-through band. */
  const [bandHover, setBandHover] = useState(false);

  // Dev assert for the container-box == content-box assumption pinned in
  // overlayCardStyle: the layer must span a 9:16 box (1080×1920 output), or
  // the percent math no longer matches render truth.
  useEffect(() => {
    if (process.env.NODE_ENV === "production") return;
    const rect = layerRef.current?.getBoundingClientRect();
    if (rect && rect.width > 0 && rect.height > 0) {
      const ratio = rect.width / rect.height;
      if (Math.abs(ratio - 9 / 16) > 0.02) {
        // eslint-disable-next-line no-console
        console.warn(
          `HeroOverlayEditor: layer aspect ${ratio.toFixed(3)} != 9:16 — ` +
            "overlayCardStyle percent math assumes container-box == content-box.",
        );
      }
    }
  }, [enabled, entries.length]);

  /** The hero video shares this layer's container (the aspect-[9/16] box). */
  const findHeroVideo = useCallback((): HTMLVideoElement | null => {
    return (
      layerRef.current?.parentElement?.querySelector<HTMLVideoElement>(":scope > video") ?? null
    );
  }, []);

  /** Fullscreen click-to-edit (E6): pause the hero (never auto-resume — the
   *  user is heading into the popover) and request the timeline popover. */
  const requestFullscreenEdit = useCallback(
    (cardId: string) => {
      const video = findHeroVideo();
      if (video && !video.paused) video.pause();
      onRequestEditCard?.(cardId);
    },
    [findHeroVideo, onRequestEditCard],
  );

  const beginGesture = useCallback(
    (
      e: React.PointerEvent<HTMLDivElement>,
      entry: SuggestionLaneEntry,
      mode: "move" | "resize",
    ) => {
      // Only primary-button / primary-touch gestures.
      if (e.button !== 0 && e.pointerType === "mouse") return;
      e.preventDefault();
      e.stopPropagation();
      e.currentTarget.setPointerCapture(e.pointerId);
      const rect = layerRef.current?.getBoundingClientRect();
      // Pause playback for the gesture (finding 11): the card can't unmount
      // mid-drag and the drag delta stays anchored to a frozen frame.
      const video = findHeroVideo();
      let resumeVideo: HTMLVideoElement | null = null;
      if (video && !video.paused) {
        resumeVideo = video;
        video.pause();
      }
      setGesture({
        entryId: entry.id,
        mode,
        pointerId: e.pointerId,
        startClientX: e.clientX,
        startClientY: e.clientY,
        origXFrac: entry.overlay.x_frac,
        origYFrac: entry.overlay.y_frac,
        origScale: entry.overlay.scale,
        boxW: rect?.width ?? 0,
        boxH: rect?.height ?? 0,
        resumeVideo,
      });
    },
    [findHeroVideo],
  );

  const moveGesture = useCallback(
    (e: React.PointerEvent<HTMLDivElement>, entryId: string) => {
      if (!gesture || gesture.entryId !== entryId || gesture.pointerId !== e.pointerId) return;
      if (gesture.boxW <= 0 || gesture.boxH <= 0) return;
      if (gesture.mode === "move") {
        onSuggestionEdit(entryId, {
          x_frac: clamp01(gesture.origXFrac + (e.clientX - gesture.startClientX) / gesture.boxW),
          y_frac: clamp01(gesture.origYFrac + (e.clientY - gesture.startClientY) / gesture.boxH),
          // Free-form placement — the enum position no longer applies.
          position: "custom",
        });
      } else {
        // Bottom-right handle, resized around the card CENTER (x/y unchanged):
        // dragging the corner out by dx grows the half-width by dx → width by 2dx.
        onSuggestionEdit(entryId, {
          scale: clampScale(
            gesture.origScale + (2 * (e.clientX - gesture.startClientX)) / gesture.boxW,
          ),
        });
      }
    },
    [gesture, onSuggestionEdit],
  );

  const endGesture = useCallback(
    (e: React.PointerEvent<HTMLDivElement>, entryId: string) => {
      if (!gesture || gesture.entryId !== entryId || gesture.pointerId !== e.pointerId) return;
      try {
        e.currentTarget.releasePointerCapture(e.pointerId);
      } catch {
        // Capture may already be gone (pointercancel) — releasing is best-effort.
      }
      if (gesture.resumeVideo) safePlay(gesture.resumeVideo);
      setGesture(null);
    },
    [gesture],
  );

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLDivElement>, entry: SuggestionLaneEntry) => {
      const o = entry.overlay;
      const step = e.shiftKey ? MOVE_STEP_SHIFT : MOVE_STEP;
      let patch: Partial<MediaOverlay> | null = null;
      switch (e.key) {
        case "ArrowLeft":
          patch = { x_frac: clamp01(o.x_frac - step), position: "custom" };
          break;
        case "ArrowRight":
          patch = { x_frac: clamp01(o.x_frac + step), position: "custom" };
          break;
        case "ArrowUp":
          patch = { y_frac: clamp01(o.y_frac - step), position: "custom" };
          break;
        case "ArrowDown":
          patch = { y_frac: clamp01(o.y_frac + step), position: "custom" };
          break;
        case "+":
        case "=":
          patch = { scale: clampScale(o.scale + SCALE_STEP) };
          break;
        case "-":
        case "_":
          patch = { scale: clampScale(o.scale - SCALE_STEP) };
          break;
        default:
          return;
      }
      e.preventDefault();
      e.stopPropagation();
      onSuggestionEdit(entry.id, patch);
    },
    [onSuggestionEdit],
  );

  // E6 band-hover cue: while a fullscreen card is on screen, watch the shared
  // container's pointer position (the band itself is pointer-events:none, so
  // events land on the video and bubble here). Declared before the early
  // return to keep hook order stable.
  const hasVisibleFullscreen =
    enabled &&
    entries.some(
      (entry) =>
        entry.overlay.display_mode === "fullscreen" &&
        currentTimeS >= entry.overlay.start_s &&
        currentTimeS <= entry.overlay.end_s,
    );
  useEffect(() => {
    if (!hasVisibleFullscreen) {
      setBandHover(false);
      return;
    }
    const container = layerRef.current?.parentElement;
    if (!container) return;
    const onPointerMove = (e: PointerEvent) => {
      const rect = container.getBoundingClientRect();
      if (rect.height <= 0) return;
      setBandHover(
        (e.clientY - rect.top) / rect.height > 1 - FULLSCREEN_PASSTHROUGH_FRAC,
      );
    };
    const onPointerLeave = () => setBandHover(false);
    container.addEventListener("pointermove", onPointerMove);
    container.addEventListener("pointerleave", onPointerLeave);
    return () => {
      container.removeEventListener("pointermove", onPointerMove);
      container.removeEventListener("pointerleave", onPointerLeave);
    };
  }, [hasVisibleFullscreen]);

  if (!enabled || entries.length === 0) return null;

  // Time-scoping: a card is visible/manipulable only while the playhead is in
  // its window (matches render truth). EXCEPTION: the actively-dragged card
  // stays mounted even if playback would exit the window (pause-on-drag makes
  // this moot, but guard anyway — spec req 6).
  const visibleEntries = entries.filter(
    (entry) =>
      (currentTimeS >= entry.overlay.start_s && currentTimeS <= entry.overlay.end_s) ||
      gesture?.entryId === entry.id,
  );

  return (
    <div
      ref={layerRef}
      data-testid="hero-overlay-editor"
      // Layer never blocks video interaction — only cards (and the mid-gesture
      // backdrop) take pointer events.
      className="pointer-events-none absolute inset-0"
      style={{ touchAction: "none" }}
    >
      {/* Transparent backdrop ONLY while a gesture is active: swallows stray
          pointer/click events so they never reach the native video controls
          (finding 11). Unmounts on release — zero interference otherwise. */}
      {gesture && (
        <div
          data-testid="hero-gesture-backdrop"
          aria-hidden
          className="pointer-events-auto absolute inset-0"
          style={{ touchAction: "none" }}
          onPointerDown={(e) => {
            e.preventDefault();
            e.stopPropagation();
          }}
          onClick={(e) => {
            e.preventDefault();
            e.stopPropagation();
          }}
        />
      )}
      {visibleEntries.map((entry) => {
        const url = resolveCardUrl?.(entry.overlay);
        const dragging = gesture?.entryId === entry.id;

        // ── Fullscreen cutaway entry (plan 009 T4) ─────────────────────────
        // Full-frame, zero spatial gestures (no drag / resize / keyboard-move,
        // no resize handle — mode toggle lives in the timeline popover, T3).
        // Pending-suggestion chrome: INSET dashed-lime outline + inside ✦.
        // One click target (top 85%) pauses + requests the popover; the bottom
        // 15% is a pointer pass-through band for the native video controls.
        if (entry.overlay.display_mode === "fullscreen") {
          return (
            <div
              key={entry.id}
              data-testid={`hero-fullscreen-card-${entry.id}`}
              className="pointer-events-none absolute motion-safe:transition-opacity"
              style={{
                ...overlayCardStyle(entry.overlay),
                opacity: bandHover ? 0.4 : 1,
              }}
            >
              {url ? (
                entry.overlay.kind === "video" ? (
                  <video
                    src={url}
                    muted
                    playsInline
                    // First-frame readiness at window entry (plan 009 T4).
                    preload="auto"
                    className={`pointer-events-none select-none ${mediaClassFor("fullscreen")}`}
                    onLoadedMetadata={(e) => {
                      e.currentTarget.currentTime = entry.overlay.clip_trim_start_s ?? 0;
                    }}
                  />
                ) : (
                  // eslint-disable-next-line @next/next/no-img-element -- signed GCS thumbnail
                  <img
                    src={url}
                    alt=""
                    draggable={false}
                    className={`pointer-events-none select-none ${mediaClassFor("fullscreen")}`}
                  />
                )
              ) : (
                <div className="pointer-events-none h-full w-full bg-zinc-800" />
              )}
              {/* Editor chrome ≠ baked chrome: pending suggestions only —
                  inset outline (outline-offset -2px never affects layout) +
                  the ✦ provenance badge INSIDE the frame. */}
              <div
                aria-hidden
                data-testid={`hero-fullscreen-outline-${entry.id}`}
                className={`pointer-events-none absolute inset-0 outline outline-2 outline-lime-600 ${
                  entry.staged ? "" : "outline-dashed"
                }`}
                style={{ outlineOffset: -2 }}
              />
              <span
                aria-hidden
                className="pointer-events-none absolute right-2 top-2 z-10 flex h-4 w-4 items-center justify-center rounded-full bg-lime-600 text-[9px] text-white"
              >
                ✦
              </span>
              {/* ONE click target covering the top 85% of the frame — pauses
                  the hero and requests the timeline popover (E6). */}
              <div
                data-testid={`hero-fullscreen-edit-${entry.id}`}
                role="button"
                tabIndex={0}
                aria-label="Full-screen visual — edit in timeline"
                className="pointer-events-auto absolute inset-x-0 top-0 cursor-pointer focus-visible:outline focus-visible:outline-2 focus-visible:-outline-offset-4 focus-visible:outline-lime-500"
                style={{ bottom: `${FULLSCREEN_PASSTHROUGH_FRAC * 100}%` }}
                onClick={() => requestFullscreenEdit(entry.id)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" || e.key === " ") {
                    e.preventDefault();
                    requestFullscreenEdit(entry.id);
                  }
                }}
              >
                <span className="pointer-events-none absolute left-2 top-2 rounded-full bg-black/60 px-2 py-0.5 text-[10px] font-medium text-white">
                  ⛶ Full screen · edit
                </span>
              </div>
              {/* E6 pass-through band: pointer-events:none so clicks over the
                  native video controls reach the video underneath. Purely a
                  marker element — the ~40% opacity hover cue is driven by the
                  container-level pointermove listener above. */}
              <div
                data-testid={`hero-fullscreen-band-${entry.id}`}
                aria-hidden
                className="pointer-events-none absolute inset-x-0 bottom-0"
                style={{ height: `${FULLSCREEN_PASSTHROUGH_FRAC * 100}%` }}
              />
            </div>
          );
        }

        return (
          <div
            key={entry.id}
            data-testid={`hero-suggestion-card-${entry.id}`}
            role="button"
            tabIndex={0}
            aria-label="Suggested visual — drag to move; arrow keys move, plus and minus resize"
            style={{ ...overlayCardStyle(entry.overlay), touchAction: "none" }}
            // Dashed lime-600 + ✦ while pending, solid when staged (006 tokens).
            className={`pointer-events-auto rounded border-[1.5px] border-lime-600 bg-lime-50/40 focus-visible:outline focus-visible:outline-2 focus-visible:outline-lime-500 motion-safe:transition-[border-style,box-shadow] ${
              entry.staged ? "border-solid" : "border-dashed"
            } ${dragging ? "cursor-grabbing shadow-lg" : "cursor-grab"}`}
            onPointerDown={(e) => beginGesture(e, entry, "move")}
            onPointerMove={(e) => moveGesture(e, entry.id)}
            onPointerUp={(e) => endGesture(e, entry.id)}
            onPointerCancel={(e) => endGesture(e, entry.id)}
            onKeyDown={(e) => handleKeyDown(e, entry)}
          >
            <span
              aria-hidden
              className="absolute -right-1.5 -top-1.5 z-10 flex h-4 w-4 items-center justify-center rounded-full bg-lime-600 text-[9px] text-white"
            >
              ✦
            </span>
            {url ? (
              entry.overlay.kind === "video" ? (
                <video
                  src={url}
                  muted
                  playsInline
                  preload="metadata"
                  className={`pointer-events-none select-none ${mediaClassFor(entry.overlay.display_mode)}`}
                  // Poster at the trimmed-in point so the creator manipulates
                  // the ACTUAL segment they previewed in the rail (006 T3).
                  onLoadedMetadata={(e) => {
                    e.currentTarget.currentTime = entry.overlay.clip_trim_start_s ?? 0;
                  }}
                />
              ) : (
                // eslint-disable-next-line @next/next/no-img-element -- signed GCS thumbnail
                <img
                  src={url}
                  alt=""
                  draggable={false}
                  className={`pointer-events-none select-none ${mediaClassFor(entry.overlay.display_mode)}`}
                />
              )
            ) : (
              <div className="pointer-events-none aspect-video w-full rounded bg-zinc-800" />
            )}
            {/* Bottom-right resize handle: ≥44px hit area centred on the card
                corner, visually a small lime square. Keyboard resize lives on
                the card itself (+/-), so the handle is pointer-only. */}
            <div
              data-testid={`hero-suggestion-resize-${entry.id}`}
              aria-hidden
              className="pointer-events-auto absolute bottom-0 right-0 flex h-11 w-11 translate-x-1/2 translate-y-1/2 cursor-nwse-resize items-center justify-center"
              style={{ touchAction: "none" }}
              onPointerDown={(e) => beginGesture(e, entry, "resize")}
              onPointerMove={(e) => moveGesture(e, entry.id)}
              onPointerUp={(e) => endGesture(e, entry.id)}
              onPointerCancel={(e) => endGesture(e, entry.id)}
            >
              <span className="block h-3 w-3 rounded-[2px] border border-white bg-lime-600 shadow-sm" />
            </div>
          </div>
        );
      })}
    </div>
  );
}
