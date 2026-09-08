"use client";

import { useRef } from "react";
import { useSearchParams } from "next/navigation";
import EditorCanvas from "@/app/plan/items/[id]/_editor/EditorCanvas";
import type { VirtualPreviewController } from "@/app/plan/items/[id]/_editor/useVirtualPreview";
import type { PlanItemVariant } from "@/lib/plan-api";
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

function numberParam(params: Pick<URLSearchParams, "get">, name: string, fallback: number) {
  const value = Number(params.get(name));
  return Number.isFinite(value) && value > 0 ? value : fallback;
}

export default function EditorCanvasGeometryFixture() {
  const params = useSearchParams();
  const hostWidth = numberParam(params, "hostWidth", 900);
  const hostHeight = numberParam(params, "hostHeight", 700);
  const zoomPct = numberParam(params, "zoom", 100);
  const canvas = params.get("canvas") === "landscape" ? { w: 1920, h: 1080 } : { w: 1080, h: 1920 };
  const stageHeightCss = params.get("stageHeightCss") || undefined;
  const isVirtual = params.get("preview") === "virtual";
  const virtual = useRef<VirtualPreviewController | null>(null);
  const videoRef = useRef<HTMLVideoElement>(null);
  if (isVirtual && virtual.current == null) virtual.current = createEditorCanvasVirtualPreview();
  const previewHref = (preview: "clean" | "virtual") => {
    const next = new URLSearchParams(params.toString());
    next.set("preview", preview);
    return `?${next.toString()}`;
  };

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
        <div
          data-region="canvas-cell"
          className="flex min-h-0 min-w-0 flex-1 items-center justify-center overflow-hidden"
        >
          <EditorCanvas
            variant={variant}
            elements={[]}
            bars={[]}
            selectedTextId={null}
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
      </div>
    </main>
  );
}
