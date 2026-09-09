/**
 * KRI-8 — the canvas root must contain its own z-index scale so preview text
 * can never paint above EditorShell's chrome (drawer, panels, floating
 * controls). jsdom doesn't compute stacking contexts, so this is a
 * declaration-level tripwire only — the behavioral guard against real
 * paint/hit order lives in e2e/editor-canvas-stacking.spec.ts.
 */
import "@testing-library/jest-dom";
import React from "react";
import { render } from "@testing-library/react";

import EditorCanvas from "@/app/plan/items/[id]/_editor/EditorCanvas";
import type { PlanItemVariant } from "@/lib/plan-api";

class ResizeObserverMock {
  observe() {}
  unobserve() {}
  disconnect() {}
}
(global as unknown as { ResizeObserver: typeof ResizeObserverMock }).ResizeObserver =
  ResizeObserverMock;

const variant = {
  variant_id: "song_text",
  output_url: "https://example.com/portrait-output.mp4",
  render_status: "ready",
  text_mode: "agent_text",
} as unknown as PlanItemVariant;

function editorCanvas() {
  return (
    <EditorCanvas
      variant={variant}
      elements={[]}
      bars={[]}
      selectedTextId={null}
      currentTime={0}
      masonryDurationS={8}
      zoomPct={100}
      tool="select"
      videoRef={React.createRef<HTMLVideoElement>()}
      onSelectText={jest.fn()}
      onClearSelection={jest.fn()}
      onPatchBar={jest.fn()}
      onFocusContent={jest.fn()}
      onTimeUpdate={jest.fn()}
      onDuration={jest.fn()}
      canvas={{ w: 1080, h: 1920 }}
    />
  );
}

describe("EditorCanvas stacking containment", () => {
  it("isolates the canvas root so its internal z-index scale can't escape into shell chrome", () => {
    const { container } = render(editorCanvas());
    const region = container.querySelector('[data-region="canvas"]');
    expect(region).not.toBeNull();
    expect(region).toHaveClass("isolate");
  });

  it("still clips content to the stage rectangle", () => {
    const { container } = render(editorCanvas());
    const stage = container.querySelector('[data-testid="editor-canvas-stage"]');
    const clipSurface = stage?.firstElementChild;
    expect(clipSurface).toHaveClass("overflow-hidden");
  });
});
