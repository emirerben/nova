import "@testing-library/jest-dom";
import React from "react";
import { fireEvent, render, screen } from "@testing-library/react";

import InspectorPanel from "@/app/plan/items/[id]/_editor/InspectorPanel";
import type { PoolAsset } from "@/lib/plan-api";
import { createCreatorBlockInstance, type MotionPresetInstance } from "@nova/motion-runtime";

const noop = jest.fn();
const scene: MotionPresetInstance = {
  id: "motion-1",
  preset_id: "kinetic_word",
  preset_version: 1,
  start_frame: 0,
  end_frame_exclusive: 75,
  palette: { primary: "#0c0c0e", accent: "#c7ff3d" },
  intensity: 0.72,
  params: { text: "OLD" },
};
const assets: PoolAsset[] = [0, 1, 2].map((index) => ({
  id: `asset-${index}`,
  kind: "image",
  status: "ready",
  source_filename: `frame-${index}.jpg`,
  duration_s: null,
  aspect: 0.5625,
  width: 1080,
  height: 1920,
  subject: `Frame ${index}`,
  user_context: "",
  nova_description: `Frame ${index}`,
  nova_on_screen_text: null,
  display_url: `https://signed/frame-${index}.jpg`,
  deduped: false,
  gcs_path: `users/u/plan/i/pool/frame-${index}.jpg`,
}));

function renderInspector(
  selectedScene: MotionPresetInstance = scene,
  evolvingTypeEnabled = false,
) {
  const onPatchMotion = jest.fn();
  const onPatchMotionControl = jest.fn();
  const onBeginMotionControl = jest.fn();
  const onPreviewMotionControl = jest.fn();
  const onCommitMotionControl = jest.fn();
  const onCancelMotionControl = jest.fn();
  const onRemoveMotion = jest.fn();
  render(
    <InspectorPanel
      selection={{ kind: "motion", id: selectedScene.id }}
      bar={null}
      clipTiming={null}
      sfx={null}
      overlay={null}
      motionScene={selectedScene}
      motionDurationS={10}
      motionAssets={assets}
      evolvingTypeEnabled={evolvingTypeEnabled}
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
      onPreviewOverlay={noop}
      onRecordOverlay={noop}
      onDeleteOverlay={noop}
      onPatchMotion={onPatchMotion}
      onPatchMotionControl={onPatchMotionControl}
      onBeginMotionControl={onBeginMotionControl}
      onPreviewMotionControl={onPreviewMotionControl}
      onCommitMotionControl={onCommitMotionControl}
      onCancelMotionControl={onCancelMotionControl}
      onRemoveMotion={onRemoveMotion}
      onClose={noop}
      onPickPreset={noop}
    />,
  );
  return {
    onPatchMotion,
    onPatchMotionControl,
    onBeginMotionControl,
    onPreviewMotionControl,
    onCommitMotionControl,
    onCancelMotionControl,
    onRemoveMotion,
  };
}

describe("InspectorPanel Creator Blocks", () => {
  it("owns block copy, timing, motion, palette, and removal in the right inspector", () => {
    const {
      onPatchMotion,
      onBeginMotionControl,
      onPreviewMotionControl,
      onCommitMotionControl,
      onRemoveMotion,
    } = renderInspector();

    expect(screen.getByTestId("selected-motion-inspector")).toHaveTextContent("Wild Type");
    fireEvent.change(screen.getByLabelText("Text"), { target: { value: "NEW" } });
    const intensity = screen.getByLabelText("Intensity");
    fireEvent.pointerDown(intensity);
    fireEvent.change(intensity, { target: { value: "50" } });
    fireEvent.pointerUp(intensity);
    fireEvent.change(screen.getByLabelText("Start (seconds)"), { target: { value: "0.5" } });
    fireEvent.change(screen.getByLabelText("primary"), { target: { value: "#112233" } });
    fireEvent.click(screen.getByRole("button", { name: "Remove block" }));

    expect(onPatchMotion).toHaveBeenCalledWith("motion-1", { params: { text: "NEW" } });
    expect(onBeginMotionControl).toHaveBeenCalledTimes(1);
    expect(onPreviewMotionControl).toHaveBeenCalledWith("motion-1", { intensity: 0.5 });
    expect(onCommitMotionControl).toHaveBeenCalledWith("motion-1", { intensity: 0.5 });
    expect(onPatchMotion).toHaveBeenCalledWith("motion-1", { start_frame: 15 });
    expect(onPatchMotion).toHaveBeenCalledWith("motion-1", {
      palette: { primary: "#112233", accent: "#c7ff3d" },
    });
    expect(onRemoveMotion).toHaveBeenCalledWith("motion-1");
  });

  it("restores a cancelled motion slider and starts the next gesture cleanly", () => {
    const {
      onBeginMotionControl,
      onPreviewMotionControl,
      onCommitMotionControl,
      onCancelMotionControl,
    } = renderInspector();
    const intensity = screen.getByLabelText("Intensity");

    fireEvent.pointerDown(intensity);
    fireEvent.change(intensity, { target: { value: "50" } });
    fireEvent.pointerCancel(intensity);

    expect(onBeginMotionControl).toHaveBeenCalledTimes(1);
    expect(onPreviewMotionControl).toHaveBeenNthCalledWith(1, "motion-1", {
      intensity: 0.5,
    });
    expect(onCancelMotionControl).toHaveBeenCalledTimes(1);
    expect(onCommitMotionControl).not.toHaveBeenCalled();

    fireEvent.pointerDown(intensity);
    fireEvent.change(intensity, { target: { value: "60" } });
    fireEvent.pointerUp(intensity);

    expect(onBeginMotionControl).toHaveBeenCalledTimes(2);
    expect(onCommitMotionControl).toHaveBeenCalledWith("motion-1", {
      intensity: 0.6,
    });
  });

  it("cancels a gesture that returns to its initial value", () => {
    const { onCancelMotionControl, onCommitMotionControl } = renderInspector();
    const intensity = screen.getByLabelText("Intensity");

    fireEvent.pointerDown(intensity);
    fireEvent.change(intensity, { target: { value: "50" } });
    fireEvent.change(intensity, { target: { value: "72" } });
    fireEvent.pointerUp(intensity);

    expect(onCancelMotionControl).toHaveBeenCalledTimes(1);
    expect(onCommitMotionControl).not.toHaveBeenCalled();
  });

  it("reorders media assets without allowing the preset to drop below its minimum", () => {
    const mediaScene: MotionPresetInstance = {
      ...scene,
      id: "motion-media",
      preset_id: "film_strip",
      end_frame_exclusive: 120,
      params: {
        assets: assets.map((asset) => ({ asset_id: asset.id, gcs_path: asset.gcs_path })),
      },
    };
    const { onPatchMotion } = renderInspector(mediaScene);

    expect(screen.getByRole("button", { name: "Remove frame-0.jpg" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Move image 3 up" }));
    expect(onPatchMotion).toHaveBeenCalledWith("motion-media", {
      params: {
        assets: [
          { asset_id: "asset-0", gcs_path: assets[0].gcs_path },
          { asset_id: "asset-2", gcs_path: assets[2].gcs_path },
          { asset_id: "asset-1", gcs_path: assets[1].gcs_path },
        ],
      },
    });
  });

  it("keeps a persisted Evolving Type chip removable while flag-off controls stay hidden", () => {
    const evolving = createCreatorBlockInstance({
      id: "motion-evolving",
      presetId: "evolving_type",
      startFrame: 0,
      endFrameExclusive: 159,
    });
    const { onRemoveMotion } = renderInspector(evolving);

    expect(screen.getByText("Evolving Type")).toBeInTheDocument();
    expect(screen.getByText(/saved Evolving Type block is preserved/i)).toBeInTheDocument();
    expect(screen.queryByLabelText("Speed")).toBeNull();
    expect(screen.queryByLabelText("Start (seconds)")).toBeNull();
    expect(screen.queryByLabelText("primary")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Remove block" }));
    expect(onRemoveMotion).toHaveBeenCalledWith("motion-evolving");
  });

  it("renders supported metadata controls for Evolving Type without invented fields", () => {
    renderInspector(createCreatorBlockInstance({
      id: "motion-evolving",
      presetId: "evolving_type",
      startFrame: 0,
      endFrameExclusive: 159,
    }), true);

    expect(screen.getByLabelText("Speed")).toHaveAttribute("min", "0.75");
    expect(screen.getByLabelText("Intensity")).toBeInTheDocument();
    fireEvent.click(screen.getByText("Advanced motion"));
    expect(screen.getByLabelText("Icon count")).toBeInTheDocument();
    expect(screen.getByLabelText("Split icons")).toBeInTheDocument();
    expect(screen.queryByLabelText("Blur")).toBeNull();
  });
});
