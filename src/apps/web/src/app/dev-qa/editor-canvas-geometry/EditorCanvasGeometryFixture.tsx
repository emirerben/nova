"use client";

import { useEffect, useRef, useState } from "react";
import { useSearchParams } from "next/navigation";
import EditorCanvas from "@/app/plan/items/[id]/_editor/EditorCanvas";
import type { VirtualPreviewController } from "@/app/plan/items/[id]/_editor/useVirtualPreview";
import type { PlanItemVariant, TextElement } from "@/lib/plan-api";
import type { TextElementBar } from "@/lib/timeline/text-timeline-reducer";
import { createEditorCanvasVirtualPreview } from "./virtual-preview-fixture";

const variant = {
  variant_id: "dev-qa-editor-canvas-geometry",
  output_url: null,
  base_video_url: null,
  render_status: "draft",
  text_mode: "none",
  resolved_archetype: null,
  captions_enabled: false,
} as unknown as PlanItemVariant;

// KRI-8 regression fixture: a text layer parked near the bottom of the frame,
// where the reported overlap (preview text over the "Add text" control) sits.
const TEXT_ID = "kri-8-stacking-probe";
const textElement: TextElement = {
  id: TEXT_ID,
  text: "Michigan Olympics Adventure",
  start_s: 0,
  end_s: 60,
  role: "generative_intro",
  position: "custom",
  x_frac: 0.5,
  y_frac: 0.92,
  size_px: 64,
};
const textBar = {
  id: TEXT_ID,
  role: "generative_intro",
  text: textElement.text,
  start_s: 0,
  end_s: 60,
  x_frac: 0.5,
  y_frac: 0.92,
} as TextElementBar;

function numberParam(params: Pick<URLSearchParams, "get">, name: string, fallback: number) {
  const value = Number(params.get(name));
  return Number.isFinite(value) && value > 0 ? value : fallback;
}

/**
 * Returns a style that parks an element exactly on top of wherever the KRI-8
 * probe text layer (`[data-text-id]`) actually renders, in `originRef`'s
 * coordinate space (its nearest positioned ancestor). The chrome stand-ins
 * mirror real EditorShell classes, but their own geometry (a full-height
 * drawer; a bottom-anchored CTA) has no reliable relationship to where an
 * independently centered/sized stage places its text — so rather than guess
 * at offsets, measure the real overlap the bug needs and reproduce it
 * directly. Apply the returned style to the element via its `style` prop.
 */
function useProbeOnText(
  enabled: boolean,
  originRef: React.RefObject<HTMLElement>,
  deps: unknown[],
) {
  const [style, setStyle] = useState<React.CSSProperties>({});
  useEffect(() => {
    if (!enabled) {
      setStyle({});
      return;
    }
    const raf = requestAnimationFrame(() => {
      const textEl = document.querySelector<HTMLElement>("[data-text-id]");
      const origin = originRef.current;
      if (!textEl || !origin) return;
      const textRect = textEl.getBoundingClientRect();
      const originRect = origin.getBoundingClientRect();
      setStyle({
        position: "absolute",
        left: `${textRect.left - originRect.left + textRect.width / 2}px`,
        top: `${textRect.top - originRect.top + textRect.height / 2}px`,
        transform: "translate(-50%, -50%)",
      });
    });
    return () => cancelAnimationFrame(raf);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled, ...deps]);
  return style;
}

export default function EditorCanvasGeometryFixture() {
  const params = useSearchParams();
  const hostWidth = numberParam(params, "hostWidth", 900);
  const hostHeight = numberParam(params, "hostHeight", 700);
  const zoomPct = numberParam(params, "zoom", 100);
  const canvas = params.get("canvas") === "landscape" ? { w: 1920, h: 1080 } : { w: 1080, h: 1920 };
  const stageHeightCss = params.get("stageHeightCss") || undefined;
  const isVirtual = params.get("preview") === "virtual";
  const mediaUrl = params.get("mediaUrl") || undefined;
  const layoutParam = params.get("layout");
  const layout = layoutParam === "fullscreen" || layoutParam === "supporting_card"
    ? layoutParam
    : undefined;
  const virtual = useRef<VirtualPreviewController | null>(null);
  const videoRef = useRef<HTMLVideoElement>(null);
  if (isVirtual && virtual.current == null) {
    virtual.current = createEditorCanvasVirtualPreview(undefined, { mediaUrl, layout });
  }
  useEffect(() => {
    const video = virtual.current?.videoAProps.ref.current;
    if (!isVirtual || !mediaUrl || !video) return;
    // The production controller assigns deck sources imperatively. Mirror
    // that transport seam so the fixture exercises decoded real media rather
    // than only asserting classes on an empty video element.
    video.src = mediaUrl;
    video.load();
  }, [isVirtual, mediaUrl]);
  const previewHref = (preview: "clean" | "virtual") => {
    const next = new URLSearchParams(params.toString());
    next.set("preview", preview);
    return `?${next.toString()}`;
  };

  // KRI-8: opt-in stand-ins for the real EditorShell chrome this fixture
  // otherwise omits, so a real-Chromium test can assert paint/hit order
  // against preview text without mounting the whole shell. Classes copied
  // verbatim from EditorShell.tsx so they reproduce (or fail to reproduce)
  // the same stacking behavior. "drawer" and "cta" mirror EditorShell's two
  // mutually exclusive layout modes (overlay vs. light) — never both at once,
  // same as the real shell.
  const chromeMode = params.get("chrome"); // "drawer" | "cta"
  const showDrawer = chromeMode === "drawer";
  const showFloatingCta = chromeMode === "cta";
  const textMode = params.get("text"); // "visible" | "selected"
  const showText = textMode === "visible" || textMode === "selected";
  const selected = textMode === "selected";

  const chromeRootRef = useRef<HTMLDivElement>(null);
  const drawerRef = useRef<HTMLDivElement>(null);
  const probeDeps = [showText, textMode, zoomPct, canvas.w, canvas.h, stageHeightCss];
  const drawerProbeStyle = useProbeOnText(showDrawer && showText, drawerRef, probeDeps);
  const ctaProbeStyle = useProbeOnText(showFloatingCta && showText, chromeRootRef, probeDeps);

  return (
    <main
      data-testid="editor-canvas-geometry-fixture"
      data-preview-mode={isVirtual ? "virtual" : "clean"}
      style={{ width: `${hostWidth}px`, height: `${hostHeight}px` }}
      className="overflow-hidden border border-zinc-300 bg-white"
    >
      <div className="flex h-full min-h-0 w-full flex-col">
        <div className="flex items-center gap-3 border-b px-3 py-2 text-xs">
          <span data-testid="geometry-controls">host {hostWidth}px · {canvas.w}:{canvas.h} · zoom {zoomPct}% · {stageHeightCss ?? "fallback"}</span>
          <a href={previewHref("clean")} data-testid="clean-preview-control">Clean</a>
          <a href={previewHref("virtual")} data-testid="virtual-preview-control">Virtual</a>
        </div>
        <div ref={chromeRootRef} className="relative flex min-h-0 flex-1">
          {/* DOM order matters here (paint-order ties resolve by tree order):
              this mirrors EditorShell.tsx's overlay-layout, where the
              ToolDrawer wrapper (~:8183) precedes the canvas-cell (~:8286). */}
          {showDrawer && (
            <div
              ref={drawerRef}
              data-testid="chrome-drawer"
              className="absolute bottom-0 left-[92px] top-0 z-40 w-[360px] border border-zinc-200 bg-white shadow-[18px_0_36px_rgba(12,12,14,0.16)]"
            >
              <button type="button" data-testid="chrome-add-text" style={drawerProbeStyle}>
                Add text
              </button>
            </div>
          )}
          <div
            data-region="canvas-cell"
            className="flex min-h-0 min-w-0 flex-1 items-center justify-center overflow-hidden"
          >
            <EditorCanvas
              variant={variant}
              elements={showText ? [textElement] : []}
              bars={showText ? [textBar] : []}
              selectedTextId={selected ? TEXT_ID : null}
              allowManipulation={selected}
              currentTime={0}
              masonryDurationS={2}
              zoomPct={zoomPct}
              tool="select"
              videoRef={videoRef}
              onSelectText={() => undefined}
              onClearSelection={() => undefined}
              onPatchBar={() => undefined}
              onFocusContent={() => undefined}
              onTimeUpdate={() => undefined}
              onDuration={() => undefined}
              canvas={canvas}
              stageHeightCss={stageHeightCss}
              virtualPreview={virtual.current}
            />
          </div>
          {/* Mirrors EditorShell.tsx's light-layout floating CTA (~:8065),
              which comes AFTER EditorCanvas in that layout's own wrapper —
              no z-index, relying on DOM order + auto stacking. Real position
              (bottom-4, centered) is the starting point; the probe effect
              then nudges it onto the actual text rect, since the two are
              independently sized/positioned and won't reliably coincide at
              every host size this fixture is parameterized for. */}
          {showFloatingCta && (
            <button
              type="button"
              data-testid="chrome-floating-add-text"
              className="absolute bottom-4 left-1/2 -translate-x-1/2 bg-white shadow-[0_8px_24px_rgba(12,12,14,0.18)] ring-1 ring-zinc-200"
              style={ctaProbeStyle}
            >
              Add text
            </button>
          )}
        </div>
      </div>
    </main>
  );
}
