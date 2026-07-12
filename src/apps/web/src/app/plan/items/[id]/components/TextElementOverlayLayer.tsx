"use client";

import type { CSSProperties, ReactNode } from "react";
import type { TextElement } from "@/lib/plan-api";
import {
  CANVAS_H,
  resolveTextElementsLayout,
  type TextElementLayout,
} from "@/lib/overlay-layout";
import { resolveCssFont } from "@/lib/overlay-constants";
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
  return {
    left: `${xFrac * 100}%`,
    top: `${yFrac * 100}%`,
    transform: textElementAnchorTransform(layout.alignment),
    width: `${maxWidthFrac * 100}%`,
    ...(zIndex !== undefined ? { zIndex } : {}),
  };
}

export function TextElementOverlayContent({
  layout,
  fontSize,
  strokeWidth,
  textAlignOverride,
  reserveText,
  children,
}: {
  layout: TextElementLayout;
  fontSize: string;
  strokeWidth?: string | null;
  textAlignOverride?: TextElementLayout["alignment"] | null;
  reserveText?: string | null;
  children?: ReactNode;
}) {
  const { family, weight } = resolveCssFont(layout.fontFamily);
  const textAlign = textAlignOverride ?? layout.alignment;
  const content = children ?? layout.text;
  const sharedStyle: CSSProperties = {
    fontSize,
    fontFamily: family,
    fontWeight: weight,
    color: layout.color,
    textAlign,
    letterSpacing: layout.letterSpacingEm !== 0 ? `${layout.letterSpacingEm}em` : undefined,
    lineHeight: layout.lineSpacing || 1.15,
    whiteSpace: "pre-wrap",
    wordBreak: "break-word",
    WebkitTextStroke: strokeWidth ? `${strokeWidth} #000000` : undefined,
    textShadow:
      !strokeWidth && layout.shadowEnabled
        ? "0 2px 8px rgba(0,0,0,0.55)"
        : undefined,
    padding: "0.08em 0.18em",
  };

  if (reserveText) {
    return (
      <div style={{ ...sharedStyle, position: "relative" }}>
        <span aria-hidden style={{ visibility: "hidden" }}>
          {reserveText}
        </span>
        <span
          style={{
            position: "absolute",
            inset: "0.08em 0.18em",
            padding: 0,
          }}
        >
          {content}
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
