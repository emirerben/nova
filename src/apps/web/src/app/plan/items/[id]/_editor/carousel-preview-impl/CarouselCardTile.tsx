"use client";

import { useRef } from "react";
import type { ResolvedCardStyle } from "./card-style";
import { useCardVideoSync, type CardVideoTarget } from "./video-sync";

/**
 * One carousel card: an absolutely-positioned, rounded-corner, clipped tile
 * holding a single `<video>` plus an optional black dim overlay. Owns its
 * own `<video>` ref + `useCardVideoSync` — factored out of
 * CarouselBlockPreviewImpl specifically so that hook call lives on a
 * component instance keyed by card index (`nCards` can change between
 * renders when `clips`/`config` change; calling a hook inside a variable-
 * length `.map()` in the PARENT would violate the Rules of Hooks — mounting/
 * unmounting a keyed child component does not).
 */
export default function CarouselCardTile({
  cardIndex,
  src,
  style,
  videoTarget,
  isPlaying,
}: {
  cardIndex: number;
  src: string | null;
  style: ResolvedCardStyle;
  videoTarget: CardVideoTarget;
  /** The editor transport's real play/pause state — see video-sync.ts. */
  isPlaying: boolean;
}) {
  const videoRef = useRef<HTMLVideoElement | null>(null);
  useCardVideoSync(videoRef, videoTarget, isPlaying);

  return (
    <div
      data-carousel-card-index={cardIndex}
      data-carousel-card-focused={style.isFocused ? "true" : undefined}
      style={{
        position: "absolute",
        left: style.left,
        top: style.top,
        width: style.width,
        height: style.height,
        transform: style.transform,
        transformOrigin: "center center",
        zIndex: style.zIndex,
        opacity: style.opacity,
        borderRadius: style.borderRadius,
        overflow: "hidden",
        backfaceVisibility: "hidden",
        boxShadow: style.boxShadow,
        backgroundColor: "#1a1a1e",
      }}
    >
      {src ? (
        <video
          ref={videoRef}
          src={src}
          muted
          playsInline
          style={{ width: "100%", height: "100%", objectFit: "cover", display: "block" }}
        />
      ) : null}
      {style.dim > 0 ? (
        <div
          aria-hidden="true"
          style={{
            position: "absolute",
            inset: 0,
            backgroundColor: "#000",
            opacity: style.dim,
            pointerEvents: "none",
          }}
        />
      ) : null}
    </div>
  );
}
