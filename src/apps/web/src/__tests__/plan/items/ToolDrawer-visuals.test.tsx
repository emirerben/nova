import "@testing-library/jest-dom";
import type { ComponentProps } from "react";
import { act, fireEvent, render, screen } from "@testing-library/react";
import ToolDrawer from "@/app/plan/items/[id]/_editor/ToolDrawer";
import type { PoolAsset, VisualBlock } from "@/lib/plan-api";

/** ToolDrawer's `carousel` prop is `CarouselPanelControl & { onDisabledTap }`
 *  (the gated-entry-point callback lives on the drawer, not the panel).
 *  Derive the type from the real prop instead of importing CarouselPanelControl
 *  alone, so this stays in lockstep with the component. */
type CarouselControlProp = NonNullable<ComponentProps<typeof ToolDrawer>["carousel"]>;

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
  it("keeps upload recovery visible without disabling the picker after a failed file", () => {
    renderVisuals({
      visualUploading: false,
      visualUploadFeedback: (
        <div>
          <p>broken.mov</p>
          <p>Upload interrupted. Check your connection and retry.</p>
          <button type="button">Retry</button>
          <button type="button">Remove</button>
        </div>
      ),
    });

    expect(screen.getByText("broken.mov")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Remove" })).toBeInTheDocument();
    expect(screen.getByLabelText(/upload images or videos/i)).toBeEnabled();
    expect(screen.queryByText("Uploading visuals…")).toBeNull();
  });

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

  function carouselControl(overrides: Partial<CarouselControlProp> = {}) {
    return {
      capable: true,
      reason: null,
      current: null,
      clips: [],
      onChange: jest.fn(),
      onRemove: jest.fn(),
      onDisabledTap: jest.fn(),
      ...overrides,
    };
  }

  it("gated carousel entry stays focusable and reports the honest reason instead of opening", () => {
    const onDisabledTap = jest.fn();
    renderVisuals({
      carousel: carouselControl({ capable: false, reason: "song-synced edits don't support carousels", onDisabledTap }),
    });

    const entry = screen.getByRole("button", { name: "Carousel" });
    expect(entry).toHaveAttribute("aria-disabled", "true");
    fireEvent.click(entry);

    expect(onDisabledTap).toHaveBeenCalledWith("song-synced edits don't support carousels");
    // Gated tap never opens the panel.
    expect(screen.queryByRole("radiogroup", { name: "Carousel effect" })).not.toBeInTheDocument();
  });

  it("selects Carousel for the shared inspector without nesting Effect controls in Visuals", () => {
    const onSelectCarousel = jest.fn();
    const onChange = jest.fn();
    const { rerender } = renderVisuals({
      carousel: carouselControl({ onChange }),
      onSelectCarousel,
    });

    const entry = screen.getByRole("button", { name: "Carousel" });
    expect(entry).not.toHaveAttribute("aria-disabled");
    fireEvent.click(entry);

    expect(onChange).toHaveBeenCalledWith(
      expect.objectContaining({
        effect: "scale_sweep",
        mode: "focus",
        position: "middle",
        transition: "crossfade",
      }),
    );
    expect(onSelectCarousel).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("radiogroup", { name: "Carousel effect" })).not.toBeInTheDocument();

    rerender(
      <ToolDrawer
        tool="visuals"
        sampleWord={null}
        appliedPresetId={null}
        onAddText={jest.fn()}
        onPickPreset={jest.fn()}
        onClose={jest.fn()}
        visualAssets={assets}
        carousel={carouselControl()}
        carouselSelected
        onSelectCarousel={onSelectCarousel}
      />,
    );
    expect(screen.getByRole("button", { name: "Carousel" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });
});

describe("ToolDrawer Creator Blocks", () => {
  const previousFlag = process.env.NEXT_PUBLIC_MOTION_SCENES_ENABLED;

  beforeAll(() => {
    process.env.NEXT_PUBLIC_MOTION_SCENES_ENABLED = "true";
  });

  afterAll(() => {
    process.env.NEXT_PUBLIC_MOTION_SCENES_ENABLED = previousFlag;
  });

  it("shows the eight-item catalog separately from route trace and inserts at the chosen preset", () => {
    const onAddMotion = jest.fn();
    renderVisuals({
      motionAvailable: true,
      motionRuntimeCompatible: true,
      onAddMotion,
    });

    expect(screen.getByTestId("creator-block-grid").querySelectorAll("button")).toHaveLength(8);
    fireEvent.click(screen.getAllByRole("button", { name: "Wild Type" }).at(-1)!);
    expect(onAddMotion).toHaveBeenCalledWith("kinetic_word");
    expect(screen.getByText("Existing effect")).toBeInTheDocument();
    expect(screen.getByText("Route trace")).toBeInTheDocument();
  });

  it("keeps media cards visible with an honest minimum-image requirement", () => {
    renderVisuals({
      motionAvailable: true,
      motionRuntimeCompatible: true,
      onAddMotion: jest.fn(),
      visualAssets: assets.slice(0, 2),
    });

    const filmStrip = screen.getByRole("button", { name: /Film Strip/ });
    expect(filmStrip).toBeDisabled();
    expect(filmStrip).toHaveAttribute(
      "title",
      "Needs 3 ready images",
    );
    expect(screen.getByRole("button", { name: "Card Stack" })).toBeEnabled();
  });

  it("keeps the left Visuals drawer focused on discovery instead of nesting block details", () => {
    renderVisuals({
      motionAvailable: true,
      motionRuntimeCompatible: true,
      onAddMotion: jest.fn(),
      motionScenes: [{
        id: "motion-1",
        preset_id: "kinetic_word",
        preset_version: 1,
        start_frame: 0,
        end_frame_exclusive: 75,
        palette: { primary: "#0c0c0e", accent: "#c7ff3d" },
        intensity: 0.72,
        params: { text: "OLD" },
      }],
    });

    expect(screen.getByTestId("creator-block-grid")).toBeInTheDocument();
    expect(screen.queryByTestId("selected-motion-inspector")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Text")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Intensity")).not.toBeInTheDocument();
    expect(screen.getByTestId("visuals-scroll-container")).toHaveClass("overflow-y-auto");
    expect(screen.getByTestId("visual-blocks-panel")).not.toHaveClass("overflow-y-auto");
  });

  it("links an existing block chip to the shared editor selection", () => {
    const onSelectMotion = jest.fn();
    const scenes: ComponentProps<typeof ToolDrawer>["motionScenes"] = [
      {
        id: "motion-first",
        preset_id: "kinetic_word",
        preset_version: 1,
        start_frame: 0,
        end_frame_exclusive: 75,
        palette: { primary: "#0c0c0e", accent: "#c7ff3d" },
        intensity: 0.72,
        params: { text: "FIRST" },
      },
      {
        id: "motion-second",
        preset_id: "offer_swap",
        preset_version: 1,
        start_frame: 90,
        end_frame_exclusive: 180,
        palette: { primary: "#0c0c0e", accent: "#c7ff3d" },
        intensity: 0.72,
        params: { primary_text: "SECOND", alternate_text: "NOW" },
      },
    ];
    renderVisuals({
      motionAvailable: true,
      motionRuntimeCompatible: true,
      motionScenes: scenes,
      selectedMotionId: "motion-second",
      onSelectMotion,
    });

    expect(screen.getAllByRole("button", { name: "Offer Flip" }).at(-1)).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(screen.getAllByRole("button", { name: "Wild Type" }).at(-1)!);
    expect(onSelectMotion).toHaveBeenCalledWith("motion-first");
  });
});
