"use client";

/**
 * Client-side WYSIWYG render of the generative hero-intro overlay — the
 * 0-latency half of the instant editor. Draws the SETTLED (hold) state of the
 * intro over the text-free base video, in the exact registry typeface the
 * server burns with.
 *
 * Fidelity strategy (committed render stays authoritative):
 * - font: identical TTF via @font-face (byte-identical registry mirror)
 * - size: `shrinkToFit` port of the server's ×0.85 descent (canvas measureText)
 * - wrap: the block is constrained to the same 90%-of-canvas max width with
 *   normal white-space, so the browser's greedy line breaker reproduces the
 *   server's `_wrap_text_to_lines` on the same font metrics
 * - position/anchor: `_resolve_anchor` / `_anchored_left_x` / `_vertical_block_top`
 *   semantics via left/top % + transform
 * - color: settled hold color (karaoke → highlight); solid fill only — the
 *   generative intro burn does not receive text_gradient
 * - layers: text-shadow ≈ Skia shadow (alpha 160, blur σ12, +6px), optional
 *   -webkit-text-stroke ≈ the crisp black stroke (stroke_px × 2, alpha 230)
 *
 * Editable mode keeps the node contentEditable (real caret/IME); React never
 * rewrites its children while focused, so typing is uninterrupted — external
 * text changes are synced into the DOM only when not focused.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { CANVAS_W, resolveCssFont } from "@/lib/overlay-constants";
import {
  MAX_LINE_W_FRAC,
  resolveAnchorFrac,
  resolveFontSizePx,
  settledColor,
  shrinkToFit,
  type IntroOverlayParams,
} from "@/lib/overlay-layout";
import { ensureFontLoaded, fontLineHeight, makeCanvasMeasureAt } from "@/lib/canvas-measure";

export function IntroTextPreview({
  params,
  editable = false,
  onTextChange,
}: {
  params: IntroOverlayParams;
  editable?: boolean;
  onTextChange?: (text: string) => void;
}) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const textRef = useRef<HTMLDivElement | null>(null);
  const [containerWidth, setContainerWidth] = useState(0);
  const [fontTick, setFontTick] = useState(0);

  const font = resolveCssFont(params.fontFamily);

  // Track the 9:16 well's rendered width — all canvas-px values scale by it.
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    // Seed BEFORE observing: an observer that fires synchronously must not be
    // overwritten by the (possibly stale/zero) initial rect measurement.
    setContainerWidth(el.getBoundingClientRect().width);
    const observer = new ResizeObserver((entries) => {
      const w = entries[0]?.contentRect.width ?? 0;
      setContainerWidth(w);
    });
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  // Re-measure once the real face is ready (layout starts on fallback metrics).
  useEffect(() => {
    let cancelled = false;
    void ensureFontLoaded(font.family, font.weight).then(() => {
      if (!cancelled) setFontTick((t) => t + 1);
    });
    return () => {
      cancelled = true;
    };
  }, [font.family, font.weight]);

  const text = (params.text ?? "").trim();

  // Server-parity typography: shrink-to-fit against 90% of the 1080px canvas
  // for the size, and Skia's `int((descent - ascent) × 1.15)` line step for
  // multi-line spacing (CSS `line-height: 1.15` would be em-relative — these
  // faces run ~1.25–1.35 em tall, leaving the preview ~25% too tight).
  // fontTick re-runs this when the @font-face finishes loading.
  const { sizePx, lineStepPx } = useMemo(() => {
    const measureAt = makeCanvasMeasureAt(font.family, font.weight);
    const size = !text
      ? resolveFontSizePx(params)
      : shrinkToFit(text, measureAt, resolveFontSizePx(params), CANVAS_W * MAX_LINE_W_FRAC)
          .sizePx;
    return {
      sizePx: size,
      lineStepPx: Math.trunc(fontLineHeight(font.family, font.weight, size) * 1.15),
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [text, params.textSizePx, params.textSize, font.family, font.weight, fontTick]);

  // Sync external text into the contentEditable node ONLY when not focused —
  // rewriting children under an active caret would reset it on every keystroke.
  // The node mounts LATER than the first text render (it waits for the
  // ResizeObserver width), so initialization must happen in the ref callback —
  // an effect keyed on [text] has already run (and no-oped) by then.
  const textIsCurrent = useCallback(
    (el: HTMLDivElement) =>
      (el.innerText ?? el.textContent ?? "").replace(/\s+$/, "") === text,
    [text],
  );
  const attachTextNode = useCallback(
    (el: HTMLDivElement | null) => {
      textRef.current = el;
      if (el && document.activeElement !== el && !textIsCurrent(el)) {
        el.textContent = text;
      }
    },
    [text, textIsCurrent],
  );
  useEffect(() => {
    const el = textRef.current;
    if (!el) return;
    if (document.activeElement === el) return;
    if (!textIsCurrent(el)) el.textContent = text;
  }, [text, textIsCurrent]);

  const handleInput = useCallback(() => {
    const el = textRef.current;
    if (!el) return;
    // innerText preserves line breaks as \n (the server wraps each segment
    // separately); textContent would glue "hello⏎world" into "helloworld".
    onTextChange?.(el.innerText ?? el.textContent ?? "");
  }, [onTextChange]);

  // Paste as plain text — rich-text paste would inject styled spans into the
  // preview and lie about the burned result.
  const handlePaste = useCallback((e: React.ClipboardEvent<HTMLDivElement>) => {
    e.preventDefault();
    const plain = e.clipboardData.getData("text/plain");
    document.execCommand("insertText", false, plain);
  }, []);

  const scale = containerWidth > 0 ? containerWidth / CANVAS_W : 0;
  const anchor = params.textAnchor ?? "center";
  const { xFrac, yFrac } = resolveAnchorFrac(params);
  const color = settledColor(params);
  const strokePx = (params.strokeWidth ?? 0) * 2 * scale;

  if (scale === 0 && containerWidth === 0) {
    // First paint before the ResizeObserver fires — render the measuring shell only.
    return <div ref={containerRef} className="pointer-events-none absolute inset-0" />;
  }

  const show = text.length > 0 || editable;

  return (
    <div ref={containerRef} className="pointer-events-none absolute inset-0 overflow-hidden">
      {show && (
        <div
          ref={attachTextNode}
          contentEditable={editable}
          suppressContentEditableWarning
          onInput={handleInput}
          onPaste={handlePaste}
          data-placeholder="Tap to add text"
          role={editable ? "textbox" : undefined}
          aria-label={editable ? "Intro text" : undefined}
          style={{
            position: "absolute",
            left: `${xFrac * 100}%`,
            top: `${yFrac * 100}%`,
            // Anchor semantics mirror _anchored_left_x / _vertical_block_top:
            // left pins the block's top-left at (x, y); center/right center
            // vertically on y and pin the line box horizontally.
            transform:
              anchor === "left"
                ? "none"
                : anchor === "right"
                  ? "translate(-100%, -50%)"
                  : "translate(-50%, -50%)",
            maxWidth: `${MAX_LINE_W_FRAC * 100}%`,
            width: "max-content",
            textAlign: anchor === "left" ? "left" : anchor === "right" ? "right" : "center",
            fontFamily: font.family,
            fontWeight: font.weight,
            fontSize: `${sizePx * scale}px`,
            lineHeight: `${lineStepPx * scale}px`,
            color,
            // Empty-state hit target: an empty inline box would be ~0×0 px and
            // untappable (the placeholder ::before rule lives in globals.css).
            ...(editable ? { minWidth: "6ch", minHeight: "1em" } : {}),
            // ≈ Skia shadow: black α160, blur σ12 (CSS radius ~2σ), +6px down.
            textShadow: `0 ${6 * scale}px ${24 * scale}px rgba(0,0,0,0.63)`,
            ...(strokePx > 0
              ? {
                  WebkitTextStroke: `${strokePx}px rgba(0,0,0,0.9)`,
                  paintOrder: "stroke fill",
                }
              : {}),
            pointerEvents: editable ? "auto" : "none",
            outline: "none",
            cursor: editable ? "text" : undefined,
            whiteSpace: "pre-wrap",
            overflowWrap: "normal",
            caretColor: color,
          }}
        />
      )}
    </div>
  );
}
