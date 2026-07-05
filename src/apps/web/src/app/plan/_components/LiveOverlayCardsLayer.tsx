"use client";

/**
 * LiveOverlayCardsLayer — the hero's CSS overlay-card stack.
 *
 * Renders MediaOverlay cards positioned/scaled over the 9:16 hero video with
 * pure DOM (overlayCardStyle) — zero network, zero canvas. The hero uses it in
 * two modes:
 *
 *  • LIVE-EDIT (hero plays the overlay-clean `pre_overlay_video_url`): ALL
 *    cards render here, sourced from the server-signed `card.preview_url`
 *    (applied cards) or a local blob URL (fresh uploads). Timeline lane edits
 *    mutate the `cards` prop and reflect on the very next render — the FFmpeg
 *    bake still only fires on Download.
 *  • LEGACY (hero plays the burned `output_url`): only cards with a local blob
 *    URL render, so already-baked pixels are never doubled.
 *
 * The caller owns that mode split via `resolveCardSrc` + `timeGate`; this
 * layer just filters (src present + inside [start_s, end_s] when gated) and
 * renders. pointer-events-none throughout: applied cards are edited via the
 * timeline lanes; on-video gestures belong to HeroOverlayEditor (suggestions
 * only) — do not add gestures here.
 *
 * Plan 009 T4:
 *  • display_mode "fullscreen" cards render full-frame (overlayCardStyle inset
 *    branch) with cover-crop media (`mediaClassFor`) — CSS parity with the
 *    FFmpeg bake's takeover branch. Fullscreen card videos preload="auto" so
 *    the first frame is ready when the playhead enters the window.
 *  • Media load failure (routine — signed URLs expire in 24h): the failed card
 *    renders a dashed-zinc "This visual couldn't load" tile (full-frame for
 *    fullscreen, pip-sized otherwise) with a Remove button. Failures are
 *    lifted via onCardMediaError so the page can block the Download bake. The
 *    tile's Remove button is the ONE pointer-interactive element here — it is
 *    error recovery, not an editing gesture, so the no-gestures invariant
 *    stands.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import type { MediaOverlay } from "@/lib/plan-api";
import { mediaClassFor } from "./cardMedia";
import { overlayCardStyle } from "./overlayCardStyle";

export default function LiveOverlayCardsLayer({
  cards,
  resolveCardSrc,
  videoTimeS,
  timeGate,
  mainVideoRef,
  onCardMediaError,
  onRemoveCard,
}: {
  cards: MediaOverlay[];
  /** Playable URL for a card (signed preview_url or local blob). undefined → the card is skipped. */
  resolveCardSrc: (card: MediaOverlay) => string | undefined;
  /** Hero playhead in seconds — drives the [start_s, end_s] visibility window. */
  videoTimeS: number;
  /** When true, cards only show inside their timeline window. */
  timeGate: boolean;
  /** Ref to the hero <video> element; card videos play/seek in lockstep with it. */
  mainVideoRef: React.RefObject<HTMLVideoElement | null>;
  /** Plan 009 T4: lifts media load failures (expired signed URL etc.) so the
   *  page can track failed card ids and block the Download bake. */
  onCardMediaError?: (cardId: string) => void;
  /** Plan 009 T4: card-remove path for the failed-load tile's Remove button. */
  onRemoveCard?: (cardId: string) => void;
}) {
  // Failed media loads, keyed by card id — the tile replaces the media element.
  const [failedIds, setFailedIds] = useState<Set<string>>(new Set());
  const markFailed = useCallback(
    (cardId: string) => {
      setFailedIds((prev) => {
        if (prev.has(cardId)) return prev;
        const next = new Set(prev);
        next.add(cardId);
        return next;
      });
      onCardMediaError?.(cardId);
    },
    [onCardMediaError],
  );

  return (
    <>
      {cards.map((card) => {
        const src = resolveCardSrc(card);
        if (!src) return null;
        if (timeGate && (videoTimeS < card.start_s || videoTimeS > card.end_s)) {
          return null;
        }
        const fullscreen = card.display_mode === "fullscreen";
        return (
          <div
            key={card.id}
            data-overlay-card={card.id}
            style={{ ...overlayCardStyle(card), pointerEvents: "none" }}
          >
            {failedIds.has(card.id) ? (
              <FailedCardTile
                cardId={card.id}
                fullscreen={fullscreen}
                onRemove={onRemoveCard ? () => onRemoveCard(card.id) : undefined}
              />
            ) : card.kind === "image" ? (
              // eslint-disable-next-line @next/next/no-img-element
              <img
                src={src}
                alt=""
                className={mediaClassFor(card.display_mode)}
                onError={() => markFailed(card.id)}
              />
            ) : (
              <TrimmedVideoPreview
                src={src}
                trimStart={card.clip_trim_start_s ?? 0}
                trimEnd={card.clip_trim_end_s ?? null}
                mainVideoRef={mainVideoRef}
                cardStartS={card.start_s}
                displayMode={card.display_mode}
                onMediaError={() => markFailed(card.id)}
              />
            )}
          </div>
        );
      })}
    </>
  );
}

/** Dashed-zinc failure tile shown when a card's media fails to load (routine —
 *  signed URLs expire in 24h). Full-frame for fullscreen cards (the wrapper is
 *  already inset-0), pip-sized otherwise. The wrapper is pointer-events-none;
 *  the tile re-enables pointer events ONLY for its Remove button. */
function FailedCardTile({
  cardId,
  fullscreen,
  onRemove,
}: {
  cardId: string;
  fullscreen: boolean;
  onRemove?: () => void;
}) {
  return (
    <div
      data-testid={`overlay-card-failed-${cardId}`}
      className={`flex flex-col items-center justify-center gap-2 border border-dashed border-zinc-400 bg-zinc-100/95 p-3 text-center ${
        fullscreen ? "h-full w-full" : "aspect-video w-full rounded"
      }`}
      style={{ pointerEvents: "auto" }}
    >
      <p className="text-xs text-[#3f3f46]">This visual couldn&apos;t load</p>
      {onRemove && (
        <button
          type="button"
          onClick={onRemove}
          className="rounded border border-zinc-300 bg-white px-2.5 py-1 text-[11px] text-[#3f3f46] transition-colors hover:border-zinc-400 focus-visible:outline focus-visible:outline-2 focus-visible:outline-lime-500"
        >
          Remove
        </button>
      )}
    </div>
  );
}

/** Video overlay card synced to the main edit player.
 *  Seeks to the trim-offset position in lock-step with the edit video and
 *  mirrors play/pause so it never plays independently in a loop. Past the trim
 *  end it freezes on the last in-window frame, mimicking the render's tpad
 *  (stop_mode=clone) behavior. */
export function TrimmedVideoPreview({
  src,
  trimStart,
  trimEnd,
  mainVideoRef,
  cardStartS,
  displayMode,
  onMediaError,
}: {
  src: string;
  trimStart: number;
  trimEnd: number | null;
  /** Ref to the main edit <video> element used for sync. */
  mainVideoRef: React.RefObject<HTMLVideoElement | null>;
  /** The card's start_s on the edit timeline (used to compute card offset). */
  cardStartS: number;
  /** Plan 009 T4: drives the shared media classes; fullscreen also preloads
   *  aggressively for first-frame readiness at window entry. */
  displayMode?: MediaOverlay["display_mode"];
  /** Plan 009 T4: load failure (expired signed URL etc.) — the parent swaps
   *  the media for the failed tile. */
  onMediaError?: () => void;
}) {
  const ref = useRef<HTMLVideoElement>(null);

  useEffect(() => {
    const card = ref.current;
    const main = mainVideoRef.current;
    if (!card) return;
    // No main video (configuration-only mode, no render yet) — just autoplay.
    if (!main) {
      card.currentTime = trimStart;
      card.play().catch(() => {});
      return;
    }

    // Single owner of the card's playback state: seeks the card to its
    // trim-offset position matching the main video's current time, and derives
    // play / pause / freeze from (main state, trim window) so every event
    // handler below can simply re-run it.
    function syncTime() {
      if (!card || !main) return;
      const cardTime = trimStart + Math.max(0, main.currentTime - cardStartS);
      const cappedTime = trimEnd !== null ? Math.min(cardTime, trimEnd) : cardTime;
      // Only seek if the drift exceeds 150ms to avoid thrashing.
      if (Math.abs(card.currentTime - cappedTime) > 0.15) {
        card.currentTime = cappedTime;
      }
      if (trimEnd !== null && cardTime >= trimEnd) {
        // Past the trim end: hold the frame (tpad-style freeze) instead of
        // letting playback drift past trimEnd and re-seek forever.
        if (!card.paused) card.pause();
      } else if (main.paused) {
        if (!card.paused) card.pause();
      } else if (card.paused) {
        card.play().catch(() => {});
      }
    }

    const m = main;
    function onMainEvent() {
      syncTime();
    }

    // Seed initial state (syncTime owns play/pause, incl. the freeze rule).
    syncTime();

    m.addEventListener("play", onMainEvent);
    m.addEventListener("pause", onMainEvent);
    m.addEventListener("timeupdate", onMainEvent);
    m.addEventListener("seeked", onMainEvent);
    return () => {
      m.removeEventListener("play", onMainEvent);
      m.removeEventListener("pause", onMainEvent);
      m.removeEventListener("timeupdate", onMainEvent);
      m.removeEventListener("seeked", onMainEvent);
    };
  }, [src, trimStart, trimEnd, cardStartS, mainVideoRef]);

  return (
    <video
      ref={ref}
      src={src}
      muted
      playsInline
      // Fullscreen cutaways take over the whole frame — preload the full clip
      // so the first frame is ready the moment the playhead enters the window.
      preload={displayMode === "fullscreen" ? "auto" : undefined}
      className={mediaClassFor(displayMode)}
      onError={onMediaError}
    />
  );
}
