import "@testing-library/jest-dom";
import React from "react";
import { fireEvent, render, screen } from "@testing-library/react";

import InspectorPanel from "@/app/plan/items/[id]/_editor/InspectorPanel";
import type { MediaVisualBlock } from "@/lib/plan-api";

const noop = jest.fn();

const block: MediaVisualBlock = {
  version: 1,
  id: "media-1",
  kind: "media",
  start_s: 1,
  end_s: 3,
  timing_mode: "manual",
  origin: "user",
  transition_in: "cut",
  transition_out: "cut",
  audio_policy: { base: "continue", sfx: "continue" },
  asset_id: "asset-1",
  src_gcs_path: "users/u/plan/i/pool/video.mp4",
  media_kind: "video",
  source_duration_s: 2,
  trim_start_s: 0,
  trim_end_s: 2,
  display_mode: "fullscreen",
  transform: { fit_mode: "contain", focal_x: 0.5, focal_y: 0.5, zoom: 1 },
  x_frac: 0.5,
  y_frac: 0.5,
  scale: 0.35,
  z: 0,
};

function renderInspector() {
  const onPatchVisualBlock = jest.fn();
  const onReorderVisualBlock = jest.fn();
  render(
    <InspectorPanel
      selection={{ kind: "visual", id: block.id }}
      bar={null}
      clipTiming={null}
      sfx={null}
      overlay={null}
      visualBlock={block}
      motionDurationS={8}
      tab="basic"
      sampleWord={null}
      appliedPresetId={null}
      contentRef={React.createRef<HTMLTextAreaElement>()}
      onEditText={noop}
      onPatch={noop}
      onPatchTextTiming={noop}
      onPatchClipTiming={noop}
      onPreviewClipTiming={noop}
      onRecordClipTiming={noop}
      onPatchSfx={noop}
      onDeleteSfx={noop}
      onPatchOverlay={noop}
      onPatchVisualBlock={onPatchVisualBlock}
      onReorderVisualBlock={onReorderVisualBlock}
      onPreviewOverlay={noop}
      onRecordOverlay={noop}
      onDeleteOverlay={noop}
      onClose={noop}
      onPickPreset={noop}
    />,
  );
  return { onPatchVisualBlock, onReorderVisualBlock };
}

describe("InspectorPanel unified media", () => {
  it("edits fullscreen fit, focal point, zoom and ordering", () => {
    const { onPatchVisualBlock, onReorderVisualBlock } = renderInspector();

    fireEvent.click(screen.getByRole("button", { name: "Fill" }));
    fireEvent.change(screen.getByLabelText("Overlay X percent"), { target: { value: "80" } });
    fireEvent.click(screen.getByRole("button", { name: "Bring to front" }));

    expect(screen.getByLabelText("Media zoom")).toBeInTheDocument();
    expect(onPatchVisualBlock).toHaveBeenCalledWith("media-1", {
      transform: { fit_mode: "cover", focal_x: 0.5, focal_y: 0.5, zoom: 1 },
    });
    expect(onPatchVisualBlock).toHaveBeenCalledWith("media-1", {
      transform: { fit_mode: "contain", focal_x: 0.8, focal_y: 0.5, zoom: 1 },
    });
    expect(onReorderVisualBlock).toHaveBeenCalledWith("media-1", "front");
  });

  it("makes the video source limit visible and clamps the timeline end", () => {
    const { onPatchVisualBlock } = renderInspector();

    expect(screen.getByText(/Timeline handles stop at 3.0s/)).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("End seconds"), { target: { value: "7" } });
    expect(onPatchVisualBlock).toHaveBeenCalledWith("media-1", { end_s: 3 });
  });
});
