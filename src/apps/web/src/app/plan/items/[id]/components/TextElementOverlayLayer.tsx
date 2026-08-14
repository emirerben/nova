"use client";

import { useEffect, useMemo, useState, type CSSProperties, type ReactNode } from "react";
import type { TextElement } from "@/lib/plan-api";
import { animationStateAt } from "@/lib/overlay-animation";
import { smoothTypeLineProgresses } from "@/lib/text-motion-v2";
import {
  CANVAS_H,
  resolveTextElementsLayout,
  shrinkToFit,
  type TextElementLayout,
} from "@/lib/overlay-layout";
import { resolveClusterCssFont } from "@/lib/overlay-constants";
import { ensureClusterFontLoaded, makeCanvasMeasureAt } from "@/lib/canvas-measure";
import { textMotionGraphemeCount } from "@/lib/text-motion-v2";
import { FONT_FACES } from "@/lib/font-faces";
import { HandwritingText } from "@/components/HandwritingText";
import { textShadowBleedPx, textShadowCss } from "@/lib/text-shadow";

function firstStrongDirectionIsRtl(text: string): boolean {
  for (const character of text) {
    const codePoint = character.codePointAt(0) ?? 0;
    if ((codePoint >= 0x0590 && codePoint <= 0x08ff) || (codePoint >= 0xfb1d && codePoint <= 0xfdff)) {
      return true;
    }
    if (character.toUpperCase() !== character.toLowerCase()) return false;
  }
  return false;
}

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

export function textElementContentStyle({
  layout,
  fontSize,
  strokeWidth,
  canvasPixelCssSize = `${100 / CANVAS_H}cqh`,
}: {
  layout: TextElementLayout;
  fontSize: string;
  strokeWidth?: string | null;
  /** CSS length occupied by one 1080x1920 renderer-canvas pixel. */
  canvasPixelCssSize?: string;
}): CSSProperties {
  const { family, weight, style } = resolveClusterCssFont(layout.fontFamily);
  const textAlign = layout.alignment;
  const canvasPx = (pixels: number) => `calc(${pixels} * ${canvasPixelCssSize})`;
  const glowRgb = layout.glowColor?.match(/^#([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i);
  const glowShadows =
    glowRgb && layout.glowStrength > 0
      ? [
          `0 0 ${canvasPx(8)} rgba(${Number.parseInt(glowRgb[1], 16)}, ${Number.parseInt(glowRgb[2], 16)}, ${Number.parseInt(glowRgb[3], 16)}, ${(120 / 255) * layout.glowStrength})`,
          `0 0 ${canvasPx(20)} rgba(${Number.parseInt(glowRgb[1], 16)}, ${Number.parseInt(glowRgb[2], 16)}, ${Number.parseInt(glowRgb[3], 16)}, ${(220 / 255) * layout.glowStrength})`,
        ]
      : [];
  const separationShadow = textShadowCss(canvasPx, layout.shadowStyle);
  return {
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
    // CSS paints the first shadow on top: contact, ambient, then optional glow.
    textShadow: [separationShadow, ...glowShadows]
      .filter(Boolean)
      .join(", ") || undefined,
    padding: "0.08em 0.18em",
  };
}

/** Resolve the same greedy visual rows the Skia Smooth Type path masks.
 * Measurement stays at 1080x1920 canvas scale and runs only when layout
 * changes, never on high-frequency playback ticks. */
export function smoothTypePreviewLayout(
  layout: TextElementLayout,
): { lines: string[]; sizePx: number } {
  const font = resolveClusterCssFont(layout.fontFamily);
  const baseMeasureAt = makeCanvasMeasureAt(font.family, font.weight, font.style);
  const measureAt = (sizePx: number) => {
    const measure = baseMeasureAt(sizePx);
    const spacingPx = layout.letterSpacingEm * sizePx;
    return (text: string) =>
      measure(text) + Math.max(0, textMotionGraphemeCount(text) - 1) * spacingPx;
  };
  return shrinkToFit(
    layout.text,
    measureAt,
    Math.trunc(layout.sizePx),
    layout.maxWidthPx,
  );
}

/** Re-measure wrapped Smooth Type rows after their authored fonts finish
 * loading. Without this revision, a cold editor can cache fallback-font rows
 * for the whole session even though the visible text later swaps fonts. */
export function useSmoothTypeFontRevision(layouts: TextElementLayout[]): number {
  const requests = useMemo(
    () => layouts
      .filter((layout) => layout.effect === "smooth-type")
      .map((layout) => ({ fontFamily: layout.fontFamily, sizePx: layout.sizePx })),
    [layouts],
  );
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    if (requests.length === 0) return;
    let active = true;
    void Promise.all(
      requests.map((request) =>
        ensureClusterFontLoaded(request.fontFamily, request.sizePx),
      ),
    ).then(() => {
      if (active) setRevision((current) => current + 1);
    });
    return () => {
      active = false;
    };
  }, [requests]);
  return revision;
}

export function TextElementOverlayContent({
  layout,
  fontSize,
  strokeWidth,
  canvasPixelCssSize = `${100 / CANVAS_H}cqh`,
  reserveText,
  showCursor = false,
  cursorStyle = "bar",
  revealProgress,
  revealOrigin = "forward",
  revealLines,
  lineRevealProgresses,
  children,
}: {
  layout: TextElementLayout;
  fontSize: string;
  strokeWidth?: string | null;
  /** CSS length occupied by one 1080x1920 renderer-canvas pixel. */
  canvasPixelCssSize?: string;
  reserveText?: string | null;
  showCursor?: boolean;
  cursorStyle?: "none" | "bar" | "block" | "underscore";
  /** Apply write-on progress to ink-reveal or centerline handwriting. */
  revealProgress?: number;
  revealOrigin?: "forward" | "reverse" | "center-out";
  revealLines?: string[];
  lineRevealProgresses?: number[];
  children?: ReactNode;
}) {
  const content = children ?? layout.text;
  const sharedStyle = textElementContentStyle({
    layout,
    fontSize,
    strokeWidth,
    canvasPixelCssSize,
  });
  if (lineRevealProgresses) {
    const lines = revealLines ?? layout.text.split("\n");
    return (
      <div style={sharedStyle}>
        {lines.map((line, index) => {
          const progress = lineRevealProgresses[index] ?? 1;
          const rtl = firstStrongDirectionIsRtl(line);
          const physicalOrigin = revealOrigin === "center-out"
            ? "center-out"
            : (revealOrigin === "forward") === rtl
              ? "right"
              : "left";
          const inset = (1 - progress) * 100;
          const clipPath = progress >= 1
            ? undefined
            : physicalOrigin === "center-out"
              ? `inset(-0.4em ${inset / 2}% -0.4em ${inset / 2}%)`
              : physicalOrigin === "right"
                ? `inset(-0.4em 0 -0.4em ${inset}%)`
                : `inset(-0.4em ${inset}% -0.4em 0)`;
          return (
            <span
              key={`${index}:${line}`}
              data-smooth-type-line={index}
              style={{
                display: "block",
                whiteSpace: "pre",
                wordBreak: "normal",
                clipPath,
                willChange: clipPath ? "clip-path" : undefined,
              }}
            >
              {line || "\u00a0"}
            </span>
          );
        })}
      </div>
    );
  }
  if (layout.effect === "handwriting") {
    return (
      <div
        data-handwriting-reveal=""
        style={{
          display: "flex",
          justifyContent:
            layout.alignment === "left"
              ? "flex-start"
              : layout.alignment === "right"
                ? "flex-end"
                : "center",
          fontSize,
        }}
      >
        <HandwritingText
          text={typeof content === "string" ? content : layout.text}
          revealProgress={revealProgress ?? 1}
          color={layout.color}
          maxWidthEm={layout.maxWidthPx / Math.max(1, layout.sizePx)}
          alignment={layout.alignment}
          letterSpacingEm={layout.letterSpacingEm}
          lineSpacing={layout.lineSpacing}
          outlineWidthEm={layout.strokeWidth / Math.max(1, layout.sizePx)}
          fontSizePx={layout.sizePx}
          shadowStyle={layout.shadowStyle}
          glowColor={layout.glowColor}
          glowStrength={layout.glowStrength}
        />
      </div>
    );
  }
  const handwritingStyle: CSSProperties | undefined =
    revealProgress === undefined
      ? undefined
      : (() => {
          const strokeBleed = layout.strokeWidth + 2;
          const glowBleed = layout.glowStrength > 0 ? 62 : 0;
          const shadowBleed = textShadowBleedPx(layout.shadowStyle);
          const leftBleedPx = Math.max(
            strokeBleed,
            shadowBleed.left,
            glowBleed,
          );
          const topBleedPx = Math.max(
            strokeBleed,
            shadowBleed.top,
            glowBleed,
          );
          const rightBleedPx = Math.max(
            strokeBleed,
            shadowBleed.right,
            glowBleed,
          );
          const bottomBleedPx = Math.max(
            strokeBleed,
            shadowBleed.bottom,
            glowBleed,
          );
          const leftBleed = `calc(${-leftBleedPx} * ${canvasPixelCssSize})`;
          const rightBleed = `calc(${-rightBleedPx} * ${canvasPixelCssSize})`;
          const topBleed = `calc(${-topBleedPx} * ${canvasPixelCssSize})`;
          const bottomBleed = `calc(${-bottomBleedPx} * ${canvasPixelCssSize})`;
          const rightInset = `calc(${(1 - revealProgress) * 100}% + ${
            (1 - revealProgress) * leftBleedPx - revealProgress * rightBleedPx
          } * ${canvasPixelCssSize})`;
          const leftInset = `calc(${(1 - revealProgress) * 100}% + ${
            (1 - revealProgress) * rightBleedPx - revealProgress * leftBleedPx
          } * ${canvasPixelCssSize})`;
          const centeredInset = `calc(${(1 - revealProgress) * 50}% - ${
            revealProgress * Math.max(leftBleedPx, rightBleedPx)
          } * ${canvasPixelCssSize})`;
          const horizontalInsets: [string, string] =
            revealOrigin === "reverse"
              ? [rightBleed, leftInset]
              : revealOrigin === "center-out"
                ? [centeredInset, centeredInset]
                : [rightInset, leftBleed];
          return {
            display: "inline-block",
            width: "max-content",
            maxWidth: "100%",
            clipPath:
              revealProgress >= 1
                ? undefined
                : `inset(${topBleed} ${horizontalInsets[0]} ${bottomBleed} ${horizontalInsets[1]})`,
            willChange: revealProgress >= 1 ? undefined : "clip-path",
          };
        })();

  if (reserveText != null && typeof content === "string" && reserveText.startsWith(content)) {
    const hiddenRemainder = reserveText.slice(content.length);
    return (
      <div style={sharedStyle}>
        <span>{content}</span>
        {showCursor && (
          <span aria-hidden style={{ position: "relative", display: "inline-block", width: 0 }}>
            <span style={{ position: "absolute", left: "0.2em" }}>
              {cursorStyle === "block" ? "▮" : cursorStyle === "underscore" ? "_" : "|"}
            </span>
          </span>
        )}
        <span aria-hidden data-reveal-remainder style={{ visibility: "hidden" }}>
          {hiddenRemainder}
        </span>
      </div>
    );
  }

  if (revealProgress === undefined) {
    return <div style={sharedStyle}>{content}</div>;
  }

  return (
    <div
      data-ink-reveal=""
      style={{
        display: "flex",
        justifyContent:
          layout.alignment === "left"
            ? "flex-start"
            : layout.alignment === "right"
              ? "flex-end"
              : "center",
      }}
    >
      <div style={{ ...sharedStyle, ...handwritingStyle }}>{content}</div>
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
  const motionV2Enabled = process.env.NEXT_PUBLIC_TEXT_MOTION_V2_ENABLED === "true";
  const layouts = useMemo(() => resolveTextElementsLayout(elements), [elements]);
  const elementById = useMemo(
    () => new Map(elements.map((element) => [element.id, element])),
    [elements],
  );
  const smoothFontRevision = useSmoothTypeFontRevision(layouts);
  const smoothPreviewById = useMemo(() => {
    void smoothFontRevision;
    return new Map(
      layouts
        .filter((layout) => (elementById.get(layout.id)?.effect ?? layout.effect) === "smooth-type")
        .map((layout) => [layout.id, smoothTypePreviewLayout(layout)]),
    );
  }, [elementById, layouts, smoothFontRevision]);
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
      {visible.map((layout) => {
        const element = elementById.get(layout.id);
        const smoothPreview = smoothPreviewById.get(layout.id);
        const smoothLines = smoothPreview?.lines ?? layout.text.split("\n");
        const effect = element?.effect ?? layout.effect ?? "static";
        const animation = currentTime === undefined
          ? animationStateAt("static", 0, 1, layout.text)
          : animationStateAt(
              effect,
              currentTime - layout.start_s,
              Math.max(0.01, layout.end_s - layout.start_s),
              layout.text,
              {
                motion: element?.motion,
                motionV2Enabled,
              },
            );
        const baseStyle = textElementWrapperStyle({ layout });
        const fixedReveal = effect === "typewriter" || effect === "stream-in";
        const hasTransformMotion =
          animation.xTranslate !== 0 || animation.yTranslate !== 0 || animation.scale !== 1;
        return (
          <div
            key={layout.id}
            className="absolute select-none"
            style={{
              ...baseStyle,
              opacity: animation.alpha,
              filter: animation.blurPx > 0.01
                ? `blur(${(animation.blurPx / CANVAS_H) * 100}cqh)`
                : undefined,
              transform: hasTransformMotion
                ? `${baseStyle.transform ?? ""} translate(${(animation.xTranslate / CANVAS_H) * 100}cqh, ${(animation.yTranslate / CANVAS_H) * 100}cqh) scale(${animation.scale})`
                : baseStyle.transform,
            }}
          >
            <TextElementOverlayContent
              layout={layout}
              fontSize={`${((smoothPreview?.sizePx ?? layout.sizePx) / CANVAS_H) * 100}cqh`}
              strokeWidth={
                layout.strokeWidth > 0 ? `${(layout.strokeWidth / CANVAS_H) * 100}cqh` : null
              }
              reserveText={fixedReveal ? layout.text : null}
              showCursor={animation.showCursor}
              cursorStyle={animation.cursorStyle}
              revealProgress={
                effect === "handwriting" || effect === "ink-reveal" || effect === "smooth-type"
                  ? animation.revealProgress
                  : undefined
              }
              revealOrigin={animation.revealOrigin}
              revealLines={smoothLines}
              lineRevealProgresses={
                effect === "smooth-type" &&
                motionV2Enabled &&
                element?.motion?.version === 2 &&
                currentTime !== undefined
                  ? smoothTypeLineProgresses(
                      smoothLines,
                      currentTime - layout.start_s,
                      element?.motion,
                    )
                  : undefined
              }
            >
              {animation.visibleText}
            </TextElementOverlayContent>
          </div>
        );
      })}
    </div>
  );
}
