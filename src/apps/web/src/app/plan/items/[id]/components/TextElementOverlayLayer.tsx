"use client";

import type { CSSProperties, ReactNode } from "react";
import type { TextElement } from "@/lib/plan-api";
import {
  CANVAS_H,
  resolveTextElementsLayout,
  type TextElementLayout,
} from "@/lib/overlay-layout";
import { resolveClusterCssFont } from "@/lib/overlay-constants";
import { FONT_FACES } from "@/lib/font-faces";

export function textElementAnchorTransform(alignment: TextElementLayout["alignment"]): string {
  if (alignment === "left") return "translate(0, -50%)";
  if (alignment === "right") return "translate(-100%, -50%)";
  return "translate(-50%, -50%)";
}

export function textElementWrapperStyle({
  layout,
  xFrac = layout.xFrac,
  yFrac = layout.yFrac,
  maxWidthFrac = layout.maxWidthFrac,
  zIndex,
}: {
  layout: TextElementLayout;
  xFrac?: number;
  yFrac?: number;
  maxWidthFrac?: number;
  zIndex?: number;
}): CSSProperties {
  const anchorTransform = textElementAnchorTransform(layout.alignment);
  const rotateTransform = layout.rotationDeg ? ` rotate(${layout.rotationDeg}deg)` : "";
  return {
    left: `${xFrac * 100}%`,
    top: `${yFrac * 100}%`,
    transform: `${anchorTransform}${rotateTransform}`,
    width: `${maxWidthFrac * 100}%`,
    ...(zIndex !== undefined ? { zIndex } : {}),
  };
}

export function TextElementOverlayContent({
  layout,
  fontSize,
  strokeWidth,
  canvasPixelCssSize = `${100 / CANVAS_H}cqh`,
  reserveText,
  showCursor = false,
  children,
}: {
  layout: TextElementLayout;
  fontSize: string;
  strokeWidth?: string | null;
  /** CSS length occupied by one 1080x1920 renderer-canvas pixel. */
  canvasPixelCssSize?: string;
  reserveText?: string | null;
  showCursor?: boolean;
  children?: ReactNode;
}) {
  const { family, weight, style } = resolveClusterCssFont(layout.fontFamily);
  const textAlign = layout.alignment;
  const content = children ?? layout.text;
  const canvasPx = (pixels: number) => `calc(${pixels} * ${canvasPixelCssSize})`;
  const glowRgb = layout.glowColor?.match(/^#([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i);
  const glowShadows =
    glowRgb && layout.glowStrength > 0
      ? [
          `0 0 ${canvasPx(8)} rgba(${Number.parseInt(glowRgb[1], 16)}, ${Number.parseInt(glowRgb[2], 16)}, ${Number.parseInt(glowRgb[3], 16)}, ${(120 / 255) * layout.glowStrength})`,
          `0 0 ${canvasPx(20)} rgba(${Number.parseInt(glowRgb[1], 16)}, ${Number.parseInt(glowRgb[2], 16)}, ${Number.parseInt(glowRgb[3], 16)}, ${(220 / 255) * layout.glowStrength})`,
        ]
      : [];
  const softShadow =
    !strokeWidth && layout.shadowEnabled
      ? `0 ${canvasPx(6)} ${canvasPx(12)} rgba(0, 0, 0, ${160 / 255})`
      : null;
  const sharedStyle: CSSProperties = {
    fontSize,
    fontFamily: family,
    fontWeight: weight,
    fontStyle: style,
    color: layout.color,
    textAlign,
    letterSpacing: layout.letterSpacingEm !== 0 ? `${layout.letterSpacingEm}em` : undefined,
    lineHeight: layout.lineSpacing || 1.15,
    whiteSpace: "pre-wrap",
    wordBreak: "break-word",
    WebkitTextStroke: strokeWidth ? `${strokeWidth} #000000` : undefined,
    // Mirrors `_draw_line_with_layers`: two Skia halo passes followed by the
    // default 6px-down, 12px-blur black shadow, all at renderer-canvas scale.
    textShadow: [...glowShadows, softShadow]
      .filter(Boolean)
      .join(", ") || undefined,
    padding: "0.08em 0.18em",
  };

  if (reserveText != null && typeof content === "string" && reserveText.startsWith(content)) {
    const hiddenRemainder = reserveText.slice(content.length);
    return (
      <div style={sharedStyle}>
        <span>{content}</span>
        {showCursor && (
          <span aria-hidden style={{ position: "relative", display: "inline-block", width: 0 }}>
            <span style={{ position: "absolute", left: "0.2em" }}>|</span>
          </span>
        )}
        <span aria-hidden data-reveal-remainder style={{ visibility: "hidden" }}>
          {hiddenRemainder}
        </span>
      </div>
    );
  }

  return (
    <div
      style={sharedStyle}
    >
      {content}
    </div>
  );
}

export default function TextElementOverlayLayer({
  elements,
  currentTime,
}: {
  elements: TextElement[];
  currentTime?: number;
}) {
  const layouts = resolveTextElementsLayout(elements);
  const visible =
    currentTime === undefined
      ? layouts
      : layouts.filter((layout) => currentTime >= layout.start_s && currentTime < layout.end_s);

  return (
    <div
      className="pointer-events-none absolute inset-0"
      style={{ containerType: "size" } as CSSProperties}
    >
      <style dangerouslySetInnerHTML={{ __html: FONT_FACES }} />
      {visible.map((layout) => (
        <div
          key={layout.id}
          className="absolute select-none"
          style={textElementWrapperStyle({ layout })}
        >
          <TextElementOverlayContent
            layout={layout}
            fontSize={`${(layout.sizePx / CANVAS_H) * 100}cqh`}
            strokeWidth={
              layout.strokeWidth > 0 ? `${(layout.strokeWidth / CANVAS_H) * 100}cqh` : null
            }
          />
        </div>
      ))}
    </div>
  );
}
