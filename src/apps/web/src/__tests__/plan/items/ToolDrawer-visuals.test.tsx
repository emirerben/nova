import "@testing-library/jest-dom";
import type { ComponentProps } from "react";
import { act, fireEvent, render, screen } from "@testing-library/react";
import ToolDrawer from "@/app/plan/items/[id]/_editor/ToolDrawer";
import type { PoolAsset, VisualBlock } from "@/lib/plan-api";

const assets: PoolAsset[] = [0, 1, 2].map((index) => ({
  id: `asset-${index}`,
  kind: "image",
  status: "ready",
  source_filename: `frame-${index}.jpg`,
  duration_s: null,
  aspect: 0.5625,
  subject: `Frame ${index}`,
  user_context: "",
  nova_description: `Nova frame ${index}`,
  nova_on_screen_text: null,
  display_url: `https://signed/frame-${index}.jpg`,
  deduped: false,
  gcs_path: `users/u/plan/i/pool/frame-${index}.jpg`,
}));

const card: VisualBlock = {
  version: 1,
  id: "card-1",
  kind: "text_card",
  start_s: 1,
  end_s: 3,
  timing_mode: "manual",
  origin: "user",
  transition_in: "cut",
  transition_out: "cut",
  audio_policy: { base: "continue", sfx: "continue" },
  background: { type: "solid", color: "#26382F" },
};

function renderVisuals(overrides: Partial<ComponentProps<typeof ToolDrawer>> = {}) {
  const props: ComponentProps<typeof ToolDrawer> = {
    tool: "visuals",
    sampleWord: null,
    appliedPresetId: null,
    onAddText: jest.fn(),
    onPickPreset: jest.fn(),
    onClose: jest.fn(),
    visualAssets: assets,
    ...overrides,
  };
  return render(<ToolDrawer {...props} />);
}

describe("ToolDrawer visual blocks", () => {
  it("creates a montage from the user's ordered asset selection", () => {
    const onAddMontage = jest.fn();
    renderVisuals({ onAddMontage });

    assets.forEach((asset) => {
      fireEvent.click(screen.getByRole("button", { name: `Select ${asset.source_filename}` }));
    });
    fireEvent.click(screen.getByRole("button", { name: "Add montage (3)" }));

    expect(onAddMontage).toHaveBeenCalledWith(["asset-0", "asset-1", "asset-2"]);
  });

  it("shows source-labeled asset context and saves creator edits", async () => {
    const onSaveVisualAssetContext = jest.fn();
    renderVisuals({ onSaveVisualAssetContext });

    expect(screen.getAllByText("You")[0]).toBeInTheDocument();
    expect(screen.getAllByText("Nova")[0]).toBeInTheDocument();
    expect(screen.getByText("Nova frame 0")).toBeInTheDocument();

    fireEvent.click(screen.getAllByRole("button", { name: "Add" })[0]);
    fireEvent.change(screen.getByPlaceholderText("Context for matching"), {
      target: { value: "Use this when I mention onboarding" },
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Save" }));
    });

    expect(onSaveVisualAssetContext).toHaveBeenCalledWith(
      assets[0],
      "Use this when I mention onboarding",
    );
  });

  it("exposes card background, transition, duplication, and audio controls", () => {
    const onPatchVisualBlock = jest.fn();
    const onDuplicateVisualBlock = jest.fn();
    const onAddVisualBlockText = jest.fn();
    const onSelectVisualBlockText = jest.fn();
    renderVisuals({
      visualBlocks: [card],
      visualTextElements: [
        {
          id: "text-1",
          visual_block_id: "card-1",
          text: "The key idea",
          start_s: 1,
          end_s: 3,
          color: "#FFFFFF",
        },
      ],
      onPatchVisualBlock,
      onDuplicateVisualBlock,
      onAddVisualBlockText,
      onSelectVisualBlockText,
    });

    fireEvent.change(screen.getByLabelText("Background type"), {
      target: { value: "gradient" },
    });
    fireEvent.change(screen.getByLabelText("Entrance"), { target: { value: "fade" } });
    fireEvent.change(screen.getByLabelText("Base audio"), { target: { value: "mute" } });
    fireEvent.click(screen.getByRole("button", { name: "Duplicate" }));
    fireEvent.click(screen.getByRole("button", { name: "Add text" }));
    fireEvent.click(screen.getByRole("button", { name: "The key idea" }));

    expect(onPatchVisualBlock).toHaveBeenCalledWith(
      "card-1",
      expect.objectContaining({
        background: expect.objectContaining({ type: "gradient" }),
      }),
    );
    expect(onPatchVisualBlock).toHaveBeenCalledWith("card-1", { transition_in: "fade" });
    expect(onPatchVisualBlock).toHaveBeenCalledWith(
      "card-1",
      expect.objectContaining({ audio_policy: { base: "mute", sfx: "continue" } }),
    );
    expect(onDuplicateVisualBlock).toHaveBeenCalledWith("card-1");
    expect(onAddVisualBlockText).toHaveBeenCalledWith("card-1");
    expect(onSelectVisualBlockText).toHaveBeenCalledWith("text-1");
  });

  it("warns when linked card copy is dense or lacks contrast", () => {
    renderVisuals({
      visualBlocks: [
        {
          ...card,
          background: { type: "solid", color: "#FFFFFF" },
        },
      ],
      visualTextElements: [
        {
          visual_block_id: "card-1",
          text: "A very long argument ".repeat(12),
          start_s: 1,
          end_s: 2,
          color: "#FFFFFF",
        },
      ],
    });

    expect(screen.getByText(/dense reading load/i)).toBeInTheDocument();
    expect(screen.getByText(/contrast may be too low/i)).toBeInTheDocument();
  });
});
