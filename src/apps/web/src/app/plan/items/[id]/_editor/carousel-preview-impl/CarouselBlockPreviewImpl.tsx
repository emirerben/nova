"use client";

import { useMemo } from "react";
import type { CarouselMoment, TimelineClip } from "@/lib/generative-api";
import { CANVAS_H, CANVAS_W, PERSPECTIVE_PX, type EffectName } from "@/lib/carousel-preview";
import {
  DEFAULT_EFFECT,
  DEFAULT_GEOMETRY,
  MAX_CARDS,
  buildMomentTimeline,
  computeFocusStartTimeline,
  resolveEffectiveMode,
  resolveFrameIndex,
} from "./geometry";
import { cardStyleFor } from "./card-style";
import type { CardVideoTarget } from "./video-sync";
import CarouselCardTile from "./CarouselCardTile";

/**
 * Live CSS-3D preview of a carousel moment (`config`) for the editor canvas.
 *
 * ## Props contract (fixed by Lane C's placeholder — do not rename)
 * `{config, clips, currentTimeS, blockStartS, durationS}`. `config` is the
 * wire-shaped `CarouselMoment` (generative-api.ts); `clips` mirrors what
 * `useVirtualPreview` consumes (`Pick<TimelineClip, "clip_index" |
 * "signed_url">[]`) — ARRAY ORDER is the card order (index i -> card i),
 * mirroring `segment.py`'s `clip_paths: tuple[str, ...]` ("one card per
 * clip", enumerated positionally). Capped to MAX_CARDS (5), mirroring
 * `segment.py:MAX_CARDS`.
 *
 * ## Coordinate contract for the parent (Lane C / EditorCanvas)
 * This component renders a single absolutely-positioned root box sized
 * EXACTLY `CANVAS_W x CANVAS_H` (1080x1920 — carousel-preview's card-native
 * canvas space, matching `renderer.py`'s `CANVAS_W`/`CANVAS_H`). It does
 * NOT scale itself to fit any container — the PARENT mount point must apply
 * whatever `transform: scale(...)` (or equivalent) it already uses to fit
 * the rest of the 1080x1920 editor canvas into the on-screen preview area.
 * Mount this component inside a `position: relative` box and let it fill
 * that box via the ancestor's own scale transform, same as any other
 * 1080x1920-native layer in this editor.
 *
 * ## Rendering model
 * Purely props-driven: a `FrameState` timeline (choreography.ts) is built
 * once per (effect, mode, focusClipIndex, durationS, nCards) via
 * `useMemo`, then indexed by `round((currentTimeS - blockStartS) * 30)`
 * clamped to `[0, durationS]` on every render — no internal rAF loop. This
 * matches how `VisualBlocksLayer` (a sibling live-preview layer) is driven:
 * the EDITOR owns the play/scrub clock and re-renders on every tick: see
 * that component if the driving pattern ever needs re-confirming.
 *
 * See `geometry.ts` for the timeline-authoring mirror of
 * `segment.py`/`choreography.py` (incl. one documented divergence: this
 * component always fits the focus timeline to `durationS`, where the
 * backend only does that for an explicit user override), `card-style.ts`
 * for the per-frame CSS pose math (incl. the documented divergence from
 * `renderer.py`'s corner-projection focus lerp), and `video-sync.ts` for
 * how each card's `<video>` is driven off the explicit `isPlaying` prop.
 */
export interface CarouselBlockPreviewImplProps {
  config: CarouselMoment;
  clips: Pick<TimelineClip, "clip_index" | "signed_url">[];
  currentTimeS: number;
  blockStartS: number;
  durationS: number;
  /** The editor transport's real play/pause state (EditorCanvas's own
   *  `playing` prop, threaded straight through) — drives whether each
   *  active card's video runs forward on its native clock or holds paused
   *  at the exact scrub position. See `video-sync.ts`. */
  isPlaying: boolean;
}

export default function CarouselBlockPreviewImpl({
  config,
  clips,
  currentTimeS,
  blockStartS,
  durationS,
  isPlaying,
}: CarouselBlockPreviewImplProps) {
  const effect: EffectName = config.effect ?? DEFAULT_EFFECT;
  const mode = resolveEffectiveMode(config.mode);
  const safeDurationS = Number.isFinite(durationS) && durationS > 0 ? durationS : 0.1;
  const cards = useMemo(() => clips.slice(0, MAX_CARDS), [clips]);
  const nCards = cards.length;
  const timelineConfig = useMemo(() => {
    const cardIndexByClipIndex = new Map(
      cards.map((clip, cardIndex) => [clip.clip_index, cardIndex] as const),
    );
    return {
      mode,
      focus_clip_index:
        config.focus_clip_index == null
          ? config.focus_clip_index
          : cardIndexByClipIndex.get(config.focus_clip_index),
      sequence:
        config.sequence
          ?.map((item) => {
            const cardIndex = cardIndexByClipIndex.get(item.clip_index);
            return cardIndex == null ? null : { ...item, clip_index: cardIndex };
          })
          .filter((item): item is { clip_index: number; hold_s: number } => item != null) ?? null,
      move_duration_s: config.move_duration_s,
      zoom_duration_s: config.zoom_duration_s,
      timing_model: config.timing_model,
    };
  }, [cards, config, mode]);

  const frames = useMemo(
    () => buildMomentTimeline(timelineConfig, nCards, safeDurationS),
    [timelineConfig, nCards, safeDurationS],
  );
  const focusStartTimeline = useMemo(() => computeFocusStartTimeline(frames), [frames]);

  const rawLocalTimeS = currentTimeS - blockStartS;
  const localTimeS = Math.max(
    0,
    Math.min(safeDurationS, Number.isFinite(rawLocalTimeS) ? rawLocalTimeS : 0),
  );
  const frameIdx = resolveFrameIndex(frames.length, localTimeS);
  const fstate = frameIdx >= 0 ? frames[frameIdx] : null;

  const rootStyle = {
    position: "absolute" as const,
    left: 0,
    top: 0,
    width: CANVAS_W,
    height: CANVAS_H,
    overflow: "hidden" as const,
    background: "#0a0a0c",
  };

  if (nCards === 0 || !fstate) {
    return <div data-carousel-preview-empty="true" style={rootStyle} />;
  }

  // Lag split (mirrors renderer.py:lagged_frame_scroll_x — see
  // card-style.ts's cardStyleFor docstring): the view-timeline-driven VISUAL
  // pose reads the PRECEDING frame's scroll position; layout position reads
  // this frame's own.
  const progressScrollX = frameIdx > 0 ? frames[frameIdx - 1].scrollX : fstate.scrollX;
  const positionScrollX = fstate.scrollX;
  const focusStartTS = focusStartTimeline[frameIdx];

  return (
    <div
      data-carousel-preview="true"
      data-carousel-effect={effect}
      data-carousel-mode={mode}
      style={{
        ...rootStyle,
        perspective: `${PERSPECTIVE_PX}px`,
        perspectiveOrigin: "50% 50%",
        transformStyle: "preserve-3d",
      }}
    >
      {cards.map((clip, cardIndex) => {
        const style = cardStyleFor(
          effect,
          cardIndex,
          fstate,
          progressScrollX,
          positionScrollX,
          DEFAULT_GEOMETRY,
          CANVAS_W,
        );

        const isThisCardFocused = fstate.focusCard === cardIndex && fstate.focusT > 0;
        const videoTarget: CardVideoTarget =
          mode === "rolling"
            ? { active: true, timeS: localTimeS }
            : isThisCardFocused
              ? { active: true, timeS: Math.max(0, localTimeS - (focusStartTS ?? localTimeS)) }
              : { active: false, timeS: 0 };

        return (
          <CarouselCardTile
            key={clip.clip_index}
            cardIndex={cardIndex}
            src={clip.signed_url}
            style={style}
            videoTarget={videoTarget}
            isPlaying={isPlaying}
          />
        );
      })}
    </div>
  );
}
