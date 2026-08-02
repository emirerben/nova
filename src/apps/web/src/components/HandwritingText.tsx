"use client";

import { useId, useMemo, type CSSProperties } from "react";
import {
  handwritingPathD,
  handwritingStrokeLocalProgress,
  layoutHandwritingText,
} from "@/lib/handwriting-strokes";
import {
  textShadowBleedPx,
  textShadowLayers,
  type TextShadowStyle,
} from "@/lib/text-shadow";

export function HandwritingText({
  text,
  revealProgress,
  color,
  maxWidthEm,
  alignment = "center",
  letterSpacingEm = 0,
  lineSpacing = 1.15,
  outlineWidthEm = 0,
  fontSizePx,
  shadowStyle = "standard",
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
  /** Font size on the 1080x1920 renderer canvas; converts px layers to SVG em. */
  fontSizePx: number;
  shadowStyle?: TextShadowStyle;
  glowColor?: string | null;
  glowStrength?: number;
  style?: CSSProperties;
  className?: string;
}) {
  const filterPrefix = useId().replaceAll(":", "");
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
  const safeFontSizePx = Math.max(1, fontSizePx);
  const shadowLayers = textShadowLayers(shadowStyle);
  const shadowBleedPx = textShadowBleedPx(shadowStyle);
  const outlineBleedEm = 0.12 + outlineWidthEm;
  const glowBleedEm = glowStrength > 0 ? 62 / safeFontSizePx : 0;
  const leftBleedEm = Math.max(
    outlineBleedEm,
    shadowBleedPx.left / safeFontSizePx,
    glowBleedEm,
  );
  const topBleedEm = Math.max(
    outlineBleedEm,
    shadowBleedPx.top / safeFontSizePx,
    glowBleedEm,
  );
  const rightBleedEm = Math.max(
    outlineBleedEm,
    shadowBleedPx.right / safeFontSizePx,
    glowBleedEm,
  );
  const bottomBleedEm = Math.max(
    outlineBleedEm,
    shadowBleedPx.bottom / safeFontSizePx,
    glowBleedEm,
  );
  const width = Math.max(0.01, layout.widthEm);
  const height = Math.max(0.01, layout.heightEm);
  const glowLayers =
    glowStrength > 0 && glowColor
      ? [
          { sigmaPx: 8, alpha: (120 / 255) * glowStrength, color: glowColor },
          { sigmaPx: 20, alpha: (220 / 255) * glowStrength, color: glowColor },
        ]
      : [];

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
      viewBox={`0 0 ${width} ${height}`}
      style={{
        display: "block",
        width: `${width}em`,
        height: `${height}em`,
        overflow: "visible",
        ...style,
      }}
    >
      <defs>
        {glowLayers.map((layer, index) => (
          <filter
            key={`glow-filter-${index}`}
            id={`${filterPrefix}-glow-${index}`}
            filterUnits="userSpaceOnUse"
            x={-leftBleedEm}
            y={-topBleedEm}
            width={width + leftBleedEm + rightBleedEm}
            height={height + topBleedEm + bottomBleedEm}
          >
            <feGaussianBlur stdDeviation={layer.sigmaPx / safeFontSizePx} />
          </filter>
        ))}
        {shadowLayers.map((layer, index) => (
            <filter
              key={`shadow-filter-${index}`}
              id={`${filterPrefix}-shadow-${index}`}
              filterUnits="userSpaceOnUse"
              x={-leftBleedEm}
              y={-topBleedEm}
              width={width + leftBleedEm + rightBleedEm}
              height={height + topBleedEm + bottomBleedEm}
            >
              <feGaussianBlur stdDeviation={layer.sigmaPx / safeFontSizePx} />
            </filter>
          ))}
      </defs>
      {glowLayers.map((layer, index) => (
        <g
          key={`glow-${index}`}
          data-handwriting-glow={index}
          fill="none"
          stroke={layer.color}
          strokeLinecap="round"
          strokeLinejoin="round"
          strokeWidth={layout.strokeWidthEm}
          opacity={layer.alpha}
          filter={`url(#${filterPrefix}-glow-${index})`}
        >
          {paths.map((path) => (
            <path
              key={`glow-${index}-${path.key}`}
              d={path.d}
              pathLength={1}
              strokeDasharray="1 1"
              strokeDashoffset={path.dashOffset}
            />
          ))}
        </g>
      ))}
      {shadowLayers.map((layer, index) => (
          <g
            key={`shadow-${index}`}
            data-handwriting-shadow={index}
            fill="none"
            stroke="#000000"
            strokeLinecap="round"
            strokeLinejoin="round"
            strokeWidth={layout.strokeWidthEm}
            opacity={layer.alpha / 255}
            transform={`translate(${layer.dxPx / safeFontSizePx} ${layer.dyPx / safeFontSizePx})`}
            filter={`url(#${filterPrefix}-shadow-${index})`}
          >
            {paths.map((path) => (
              <path
                key={`shadow-${index}-${path.key}`}
                d={path.d}
                pathLength={1}
                strokeDasharray="1 1"
                strokeDashoffset={path.dashOffset}
              />
            ))}
          </g>
        ))}
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
