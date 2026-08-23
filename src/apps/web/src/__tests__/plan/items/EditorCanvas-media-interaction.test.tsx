import "@testing-library/jest-dom";
import React from "react";
import { fireEvent, render } from "@testing-library/react";

import EditorCanvas from "@/app/plan/items/[id]/_editor/EditorCanvas";
import type { MediaVisualBlock, PlanItemVariant, PoolAsset } from "@/lib/plan-api";

class ResizeObserverMock {
  observe() {}
  unobserve() {}
  disconnect() {}
}
(global as unknown as { ResizeObserver: typeof ResizeObserverMock }).ResizeObserver =
  ResizeObserverMock;

const variant = {
  variant_id: "song_text",
  output_url: "https://example.com/output.mp4",
  render_status: "ready",
  text_mode: "agent_text",
} as unknown as PlanItemVariant;

const asset: PoolAsset = {
  id: "asset-1",
  kind: "image",
  status: "ready",
  source_filename: "frame.jpg",
  duration_s: null,
  aspect: 1,
  subject: "Frame",
  user_context: "",
  nova_description: "Frame",
  nova_on_screen_text: null,
  display_url: "https://example.com/frame.jpg",
  deduped: false,
  gcs_path: "users/u/plan/i/pool/frame.jpg",
};

const media: MediaVisualBlock = {
  version: 1,
  id: "media-1",
  kind: "media",
  start_s: 0,
  end_s: 2,
  timing_mode: "manual",
  origin: "user",
  transition_in: "cut",
  transition_out: "cut",
  audio_policy: { base: "continue", sfx: "continue" },
  asset_id: asset.id,
  src_gcs_path: asset.gcs_path,
  media_kind: "image",
  display_mode: "overlay",
  transform: { fit_mode: "contain", focal_x: 0.5, focal_y: 0.5, zoom: 1 },
  x_frac: 0.5,
  y_frac: 0.5,
  scale: 0.35,
  z: 0,
};

describe("EditorCanvas unified-media interaction stack", () => {
  it("keeps the foreground surface transparent so media receives selection gestures", () => {
    const onSelectVisualBlock = jest.fn();
    const onRecordVisualBlock = jest.fn();
    const { container } = render(
      <EditorCanvas
        variant={variant}
        elements={[]}
        bars={[]}
        visualBlocks={[media]}
        visualAssets={[asset]}
        selectedTextId={null}
        selectedVisualBlockId={null}
        currentTime={1}
        masonryDurationS={8}
        zoomPct={100}
        tool="select"
        videoRef={React.createRef<HTMLVideoElement>()}
        onSelectText={jest.fn()}
        onSelectVisualBlock={onSelectVisualBlock}
        onClearSelection={jest.fn()}
        onPatchBar={jest.fn()}
        onRecordVisualBlock={onRecordVisualBlock}
        onFocusContent={jest.fn()}
        onTimeUpdate={jest.fn()}
        onDuration={jest.fn()}
      />,
    );

    const foreground = container.querySelector(
      '[data-canvas-foreground-layer="true"]',
    );
    const mediaElement = container.querySelector(
      '[data-media-visual-block="true"]',
    );
    expect(foreground).toHaveClass("pointer-events-none");
    expect(mediaElement).toHaveClass("pointer-events-auto");

    fireEvent.pointerDown(mediaElement as Element, {
      button: 0,
      clientX: 100,
      clientY: 100,
    });
    expect(onSelectVisualBlock).toHaveBeenCalledWith("media-1");
    expect(onRecordVisualBlock).toHaveBeenCalledTimes(1);
  });
});
