"use client";

/**
 * Client-side WYSIWYG render of the EDITORIAL word-cluster intro — the cluster
 * counterpart of IntroTextPreview (which handles the LINEAR single-block intro).
 * Draws the SETTLED (hold) state of the cluster over the text-free base video:
 * the hook is laid out as independent, mixed-size word blocks (magazine style)
 * via the pure-TS geometry port `computeClusterBlocks`, which mirrors the
 * server's `intro_cluster.py` EDITORIAL_STYLE path (parity-guarded by
 * overlay-cluster-layout.test.ts).
 *
 * Fidelity:
 * - geometry: each block's position/size/face comes from the TS port, measured
 *   with canvas over the SAME registry TTFs the server burns (the italic accent
 *   face included — see resolveClusterCssFont + the italic @font-face emitted by
 *   font-faces.ts). Canvas-vs-Skia metric drift (~1%) is the only residual gap;
 *   the burned video stays authoritative.
 * - faces: hero → Great Vibes; non-hero blocks alternate Playfair Display
 *   Regular / Italic. Color is the settled fill (solid; no gradient on the
 *   generative intro). Shadow/stroke ≈ the Skia layers.
 * - DECLINE FALLBACK: when the engine declines the hook (empty, word count
 *   outside 3-6, or can't fit), the server renders the LINEAR intro — so this
 *   component falls back to <LinearIntroTextPreview> in exactly that case. This
 *   keeps the preview from going blank or lying about a layout the burn won't
 *   produce (the previous version rendered nothing, assuming a sibling linear
 *   preview mounted alongside — it did not).
 *
 * Editable mode: a single contentEditable hook line (caret/IME) re-clusters live
 * on every keystroke. Editing the whole hook (not per-block) matches the server,
 * which re-derives word roles from the typed words on every edit (stale agent
 * roles are dropped — see generative_build `_resolve_text_for_reburn`).
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { CANVAS_W, resolveClusterCssFont } from "@/lib/overlay-constants";
import {
  resolveFontSizePx,
  settledColor,
  type IntroOverlayParams,
} from "@/lib/overlay-layout";
import {
  computeClusterBlocks,
  EDITORIAL_STYLE,
} from "@/lib/variant-editor/overlay-cluster-layout";
import { ensureClusterFontLoaded, makeCanvasClusterMeasure } from "@/lib/canvas-measure";
import { LinearIntroTextPreview } from "@/components/variant-editor/LinearIntroTextPreview";

export function ClusterTextPreview({
  params,
  editable = false,
  onTextChange,
}: {
  params: IntroOverlayParams;
  editable?: boolean;
  onTextChange?: (text: string) => void;
}) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const editRef = useRef<HTMLDivElement | null>(null);
  const [containerWidth, setContainerWidth] = useState(0);
  const [fontTick, setFontTick] = useState(0);

  const text = (params.text ?? "").trim();
  const baseSizePx = resolveFontSizePx(params);
  const color = settledColor(params);

  // Track the 9:16 well's rendered width — all canvas-px values scale by it.
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    setContainerWidth(el.getBoundingClientRect().width);
    const observer = new ResizeObserver((entries) => {
      setContainerWidth(entries[0]?.contentRect.width ?? 0);
    });
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  const heroFont = params.clusterHeroFont ?? EDITORIAL_STYLE.heroFont;
  const bodyFont = params.clusterBodyFont ?? EDITORIAL_STYLE.bodyFont;
  const accentFont = EDITORIAL_STYLE.accentFont;

  // Re-measure when editorial faces (including any user-overridden fonts) are ready.
  useEffect(() => {
    let cancelled = false;
    void Promise.all([
      ensureClusterFontLoaded(heroFont),
      ensureClusterFontLoaded(bodyFont),
      ensureClusterFontLoaded(accentFont),
    ]).then(() => {
      if (!cancelled) setFontTick((t) => t + 1);
    });
    return () => {
      cancelled = true;
    };
  }, [heroFont, bodyFont, accentFont]);

  // Compute the settled cluster blocks at 1080×1920 canvas scale. revealWindowS=0
  // → all blocks are at the settled hold (no stagger) — the look for ~95% of the
  // video. fontTick re-runs this once the real faces load.
  const blocks = useMemo(() => {
    if (!text) return null;
    const measure = makeCanvasClusterMeasure();
    return computeClusterBlocks(text, measure, {
      baseSizePx,
      revealWindowS: 0,
      fontOverrides: { heroFont, bodyFont, accentFont },
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [text, baseSizePx, heroFont, bodyFont, accentFont, fontTick]);

  // Sync external text into the contentEditable node ONLY when not focused —
  // rewriting children under an active caret would reset it on every keystroke.
  const textIsCurrent = useCallback(
    (el: HTMLDivElement) =>
      (el.innerText ?? el.textContent ?? "").replace(/\s+$/, "") === text,
    [text],
  );
  const attachEditNode = useCallback(
    (el: HTMLDivElement | null) => {
      editRef.current = el;
      if (el && document.activeElement !== el && !textIsCurrent(el)) {
        el.textContent = text;
      }
    },
    [text, textIsCurrent],
  );
  useEffect(() => {
    const el = editRef.current;
    if (!el || document.activeElement === el) return;
    if (!textIsCurrent(el)) el.textContent = text;
  }, [text, textIsCurrent]);

  const handleInput = useCallback(() => {
    const el = editRef.current;
    if (!el) return;
    onTextChange?.(el.innerText ?? el.textContent ?? "");
  }, [onTextChange]);

  const handlePaste = useCallback((e: React.ClipboardEvent<HTMLDivElement>) => {
    e.preventDefault();
    document.execCommand("insertText", false, e.clipboardData.getData("text/plain"));
  }, []);

  const scale = containerWidth > 0 ? containerWidth / CANVAS_W : 0;
  const strokePx = (params.strokeWidth ?? 0) * 2 * scale;

  // DECLINE FALLBACK: the cluster engine returns null when the hook is empty,
  // outside the 3-6 word range, or can't fit — and the server renders the
  // LINEAR intro in exactly those cases (generative_overlays "engine declined →
  // proven linear intro"). Render the linear preview so the overlay never goes
  // blank and never lies about a layout the burn won't produce. (It mounts its
  // own container + ResizeObserver, so this returns before the cluster shell.)
  if (!blocks) {
    return (
      <LinearIntroTextPreview params={params} editable={editable} onTextChange={onTextChange} />
    );
  }

  if (scale === 0 && containerWidth === 0) {
    // First paint before the ResizeObserver fires — render the measuring shell only.
    return <div ref={containerRef} className="pointer-events-none absolute inset-0" />;
  }

  return (
    <div ref={containerRef} className="pointer-events-none absolute inset-0 overflow-hidden">
      {/* Settled cluster blocks (the visual). */}
      {blocks.map((b, i) => {
        const css = resolveClusterCssFont(b.family);
        return (
          <div
            key={`${b.text}-${i}`}
            style={{
              position: "absolute",
              left: `${b.positionXFrac * 100}%`,
              top: `${b.positionYFrac * 100}%`,
              transform: "translate(-50%, -50%)",
              width: "max-content",
              whiteSpace: "nowrap",
              textAlign: "center",
              fontFamily: css.family,
              fontWeight: css.weight,
              fontStyle: css.style,
              fontSize: `${b.textSizePx * scale}px`,
              lineHeight: 1,
              color,
              textShadow: `0 ${4 * scale}px ${36 * scale}px rgba(0,0,0,0.82)`,
              ...(strokePx > 0
                ? { WebkitTextStroke: `${strokePx}px rgba(0,0,0,0.9)`, paintOrder: "stroke fill" }
                : {}),
              pointerEvents: "none",
            }}
          >
            {b.text}
          </div>
        );
      })}

      {/* Editable hook line: a single transparent-ish caret target the user types
          into; the cluster blocks above recompute live from params.text. Kept
          minimal — the cluster IS the visual; this is the input surface. */}
      {editable && (
        <div
          ref={attachEditNode}
          contentEditable
          suppressContentEditableWarning
          onInput={handleInput}
          onPaste={handlePaste}
          data-placeholder="Tap to add text"
          role="textbox"
          aria-label="Intro text"
          style={{
            position: "absolute",
            left: "50%",
            bottom: "6%",
            transform: "translateX(-50%)",
            maxWidth: "86%",
            minWidth: "6ch",
            minHeight: "1.4em",
            textAlign: "center",
            fontFamily: "var(--font-display, 'Playfair Display', serif)",
            fontSize: `${Math.max(14, 22 * scale * 4)}px`,
            lineHeight: 1.3,
            color: "rgba(255,255,255,0.92)",
            background: "rgba(0,0,0,0.35)",
            borderRadius: 6,
            padding: "2px 8px",
            outline: "none",
            caretColor: "#a3e635", // lime accent (DESIGN.md) — never amber
            pointerEvents: "auto",
            whiteSpace: "pre-wrap",
            overflowWrap: "normal",
          }}
        />
      )}
    </div>
  );
}
