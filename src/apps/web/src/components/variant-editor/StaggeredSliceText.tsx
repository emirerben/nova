"use client";

import { useEffect, useRef, useState, type CSSProperties } from "react";
import { staggeredSliceSettleS, staggeredSliceStateAt } from "@/lib/overlay-animation";
import { textMotionRendererSettleS, type TextMotionConfigV2 } from "@/lib/text-motion-v2";

function useSmoothPlaybackTime({
  text,
  tLocal,
  durationS,
  playing,
  motion,
}: {
  text: string;
  tLocal: number;
  durationS: number;
  playing: boolean;
  motion?: TextMotionConfigV2 | null;
}) {
  const [frameTime, setFrameTime] = useState(tLocal);
  const anchorRef = useRef({ timeS: tLocal, nowMs: 0 });
  const rafRef = useRef<number | null>(null);
  const settleS = motion?.version === 2
    ? Math.min(durationS, textMotionRendererSettleS("staggered-slice", text, motion))
    : Math.min(durationS, staggeredSliceSettleS(text));

  // Native media `timeupdate` is deliberately coarse. Treat each event as an
  // authoritative sync point, then interpolate only this overlay between them.
  useEffect(() => {
    const nowMs = performance.now();
    anchorRef.current = { timeS: tLocal, nowMs };
    if (!playing) setFrameTime(tLocal);
  }, [playing, tLocal]);

  useEffect(() => {
    if (!playing) return;

    const tick = (nowMs: number) => {
      const anchor = anchorRef.current;
      const nextTime = anchor.timeS + Math.max(0, nowMs - anchor.nowMs) / 1000;
      const displayTime = Math.min(nextTime, settleS);
      setFrameTime(displayTime);
      if (nextTime < settleS) rafRef.current = requestAnimationFrame(tick);
      else rafRef.current = null;
    };

    rafRef.current = requestAnimationFrame(tick);
    return () => {
      if (rafRef.current !== null) cancelAnimationFrame(rafRef.current);
      rafRef.current = null;
    };
  }, [playing, settleS]);

  return playing ? frameTime : tLocal;
}

export function StaggeredSliceText({
  text,
  tLocal,
  durationS,
  playing = false,
  style,
  className,
  motion,
}: {
  text: string;
  tLocal: number;
  durationS: number;
  playing?: boolean;
  style?: CSSProperties;
  className?: string;
  motion?: TextMotionConfigV2 | null;
}) {
  const playbackTime = useSmoothPlaybackTime({ text, tLocal, durationS, playing, motion });
  const state = staggeredSliceStateAt(text, playbackTime, durationS, motion);

  return (
    <div
      aria-hidden="true"
      data-staggered-slice
      className={className}
      style={{ ...style, userSelect: "none", pointerEvents: "none" }}
    >
      {state.lines.map((line, lineIndex) => {
        const geometryText = line.text || "\u00a0";
        return (
          <div
            key={`${lineIndex}:${line.text}`}
            data-staggered-slice-line={line.kind}
            style={{ position: "relative", whiteSpace: "pre-wrap" }}
          >
            <span aria-hidden style={{ visibility: "hidden" }}>
              {geometryText}
            </span>
            <span style={{ position: "absolute", inset: 0, whiteSpace: "pre-wrap" }}>
              {line.glyphs.map((glyph, glyphIndex) => (
                <span
                  key={`${glyphIndex}:${glyph.grapheme}`}
                  data-staggered-slice-glyph
                  style={{
                    display: "inline-block",
                    opacity: glyph.opacity,
                    transform: `translateY(${glyph.translateYEm}em) rotate(${glyph.rotateDeg}deg)`,
                    transformOrigin: "50% 80%",
                  }}
                >
                  {glyph.grapheme}
                </span>
              ))}
            </span>
          </div>
        );
      })}
    </div>
  );
}
