"use client";

import { useMemo, type CSSProperties } from "react";
import {
  handwritingPathD,
  handwritingStrokeLocalProgress,
  layoutHandwritingText,
} from "@/lib/handwriting-strokes";

export function HandwritingText({
  text,
  revealProgress,
  color,
  maxWidthEm,
  alignment = "center",
  letterSpacingEm = 0,
  lineSpacing = 1.15,
  outlineWidthEm = 0,
  shadowEnabled = true,
  glowColor,
  glowStrength = 0,
  style,
  className,
}: {
  text: string;
  revealProgress: number;
  color: string;
  maxWidthEm: number;
  alignment?: "left" | "center" | "right";
  letterSpacingEm?: number;
  lineSpacing?: number;
  outlineWidthEm?: number;
  shadowEnabled?: boolean;
  glowColor?: string | null;
  glowStrength?: number;
  style?: CSSProperties;
  className?: string;
}) {
  const layout = useMemo(
    () =>
      layoutHandwritingText(text, {
        maxWidthEm,
        letterSpacingEm,
        lineSpacing,
      }),
    [letterSpacingEm, lineSpacing, maxWidthEm, text],
  );
  const progress = Math.max(0, Math.min(1, revealProgress));
  const bleedEm = Math.max(
    0.12 + outlineWidthEm,
    shadowEnabled ? 0.42 : 0,
    glowStrength > 0 ? 0.58 : 0,
  );
  const width = Math.max(0.01, layout.widthEm);
  const height = Math.max(0.01, layout.heightEm);
  const filters: string[] = [];
  if (glowStrength > 0 && glowColor) {
    filters.push(
      `drop-shadow(0 0 ${0.08 + 0.12 * glowStrength}em ${glowColor})`,
      `drop-shadow(0 0 ${0.18 + 0.18 * glowStrength}em ${glowColor})`,
    );
  }
  if (shadowEnabled) {
    filters.push("drop-shadow(0 0.11em 0.18em rgba(0, 0, 0, 0.63))");
  }
  const filterStyle = filters.length > 0 ? filters.join(" ") : undefined;

  const paths = layout.strokes.map((stroke, index) => {
    const lineWidth = layout.lineWidthsEm[stroke.lineIndex] ?? width;
    const xOffset =
      alignment === "left"
        ? 0
        : alignment === "right"
          ? width - lineWidth
          : (width - lineWidth) / 2;
    const localProgress = handwritingStrokeLocalProgress(stroke, progress);
    const d = handwritingPathD(
      stroke.points.map(([x, y]) => [x + xOffset, y]),
    );
    return {
      key: `${stroke.lineIndex}-${stroke.glyphIndex}-${index}`,
      d,
      dashOffset: 1 - localProgress,
    };
  });

  return (
    <svg
      aria-hidden="true"
      data-handwriting-strokes=""
      className={className}
      viewBox={`${-bleedEm} ${-bleedEm} ${width + bleedEm * 2} ${height + bleedEm * 2}`}
      style={{
        display: "block",
        width: `${width + bleedEm * 2}em`,
        height: `${height + bleedEm * 2}em`,
        overflow: "visible",
        filter: filterStyle,
        ...style,
      }}
    >
      {outlineWidthEm > 0 && (
        <g
          fill="none"
          stroke="#000000"
          strokeLinecap="round"
          strokeLinejoin="round"
          strokeWidth={layout.strokeWidthEm + outlineWidthEm * 2}
          opacity={0.9}
        >
          {paths.map((path) => (
            <path
              key={`outline-${path.key}`}
              d={path.d}
              pathLength={1}
              strokeDasharray="1 1"
              strokeDashoffset={path.dashOffset}
            />
          ))}
        </g>
      )}
      <g
        fill="none"
        stroke={color}
        strokeLinecap="round"
        strokeLinejoin="round"
        strokeWidth={layout.strokeWidthEm}
      >
        {paths.map((path) => (
          <path
            key={path.key}
            d={path.d}
            pathLength={1}
            strokeDasharray="1 1"
            strokeDashoffset={path.dashOffset}
          />
        ))}
      </g>
    </svg>
  );
}
