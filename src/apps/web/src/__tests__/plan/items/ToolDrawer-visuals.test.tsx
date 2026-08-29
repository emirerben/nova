import "@testing-library/jest-dom";
import type { ComponentProps } from "react";
import { act, fireEvent, render, screen } from "@testing-library/react";
import userEvent, { PointerEventsCheckLevel } from "@testing-library/user-event";
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

  it("exposes fullscreen, overlay, and adjacent sequence media actions", () => {
    const onAddMediaBlock = jest.fn();
    const onAddMediaSequence = jest.fn((ids: string[]) => ids);
    renderVisuals({ onAddMediaBlock, onAddMediaSequence });
    fireEvent.click(screen.getByRole("button", { name: `Select ${assets[0].source_filename}` }));
    fireEvent.click(screen.getByRole("button", { name: "Add full screen" }));
    fireEvent.click(screen.getByRole("button", { name: "Add as overlay" }));
    expect(onAddMediaBlock).toHaveBeenNthCalledWith(1, ["asset-0"], "fullscreen");
    expect(onAddMediaBlock).toHaveBeenNthCalledWith(2, ["asset-0"], "overlay");

    fireEvent.click(screen.getByRole("button", { name: `Select ${assets[1].source_filename}` }));
    fireEvent.click(screen.getByRole("button", { name: "Place selected in sequence" }));
    expect(onAddMediaSequence).toHaveBeenCalledWith(["asset-0", "asset-1"]);
    expect(screen.getByText("0 selected")).toBeInTheDocument();
  });

  it("keeps unplaced assets selected and makes the selected anchor explicit", () => {
    const onAddMediaSequence = jest.fn(() => ["asset-0"]);
    renderVisuals({
      onAddMediaSequence,
      mediaSequenceAfterSelection: true,
    });
    fireEvent.click(screen.getByRole("button", { name: `Select ${assets[0].source_filename}` }));
    fireEvent.click(screen.getByRole("button", { name: `Select ${assets[1].source_filename}` }));
    fireEvent.click(screen.getByRole("button", { name: "Place sequence after selected" }));

    expect(onAddMediaSequence).toHaveBeenCalledWith(["asset-0", "asset-1"]);
    expect(screen.getByText("1 selected")).toBeInTheDocument();
  });

  it("explains that one photo can be used without creating a montage", async () => {
    renderVisuals();

    expect(screen.getByText("Photos & video")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "About Photos and video" }));
    expect(
      await screen.findByText(
        /Select one or more for full screen, overlay, or a sequence\. Montages use 3–12\./,
      ),
    ).toBeInTheDocument();
    expect(screen.getByText("0 selected")).toBeInTheDocument();
    expect(
      screen.getByText(
        "Upload a photo or video, then add it full screen, as an overlay, or in a sequence.",
      ),
    ).toBeInTheDocument();
  });

  it("labels photo and video timeline blocks as media instead of text cards", () => {
    renderVisuals({
      visualBlocks: [
        {
          version: 1,
          id: "photo-1",
          kind: "media",
          start_s: 0,
          end_s: 2,
          timing_mode: "manual",
          origin: "user",
          transition_in: "cut",
          transition_out: "cut",
          audio_policy: { base: "continue", sfx: "continue" },
          asset_id: assets[0].id,
          src_gcs_path: assets[0].gcs_path,
          media_kind: "image",
          display_mode: "fullscreen",
          transform: { fit_mode: "contain", focal_x: 0.5, focal_y: 0.5, zoom: 1 },
          x_frac: 0.5,
          y_frac: 0.5,
          scale: 0.35,
          z: 0,
        },
        {
          version: 1,
          id: "video-1",
          kind: "media",
          start_s: 2,
          end_s: 4,
          timing_mode: "manual",
          origin: "user",
          transition_in: "cut",
          transition_out: "cut",
          audio_policy: { base: "continue", sfx: "continue" },
          asset_id: "video-asset",
          src_gcs_path: "users/u/plan/i/pool/clip.mov",
          media_kind: "video",
          display_mode: "overlay",
          transform: { fit_mode: "cover", focal_x: 0.5, focal_y: 0.5, zoom: 1 },
          x_frac: 0.5,
          y_frac: 0.5,
          scale: 0.35,
          z: 1,
          source_duration_s: 2,
          trim_start_s: 0,
          trim_end_s: 2,
        },
      ],
    });

    expect(screen.getByText("Photo · Full screen")).toBeInTheDocument();
    expect(screen.getByText("Video · Overlay")).toBeInTheDocument();
  });

  it("shows source-labeled asset context and saves creator edits", async () => {
    const onSaveVisualAssetContext = jest.fn();
    renderVisuals({ onSaveVisualAssetContext });

    expect(screen.getAllByText("You")[0]).toBeInTheDocument();
    expect(screen.getAllByText("Kria")[0]).toBeInTheDocument();
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

  it("uses the completed analysis state for ready videos without optional copy", () => {
    renderVisuals({
      visualAssets: [
        {
          ...assets[0],
          kind: "video",
          source_filename: "sunset.mov",
          nova_description: "",
          nova_on_screen_text: "",
        },
      ],
    });

    expect(screen.getByText("Analysis complete")).toBeInTheDocument();
    expect(screen.queryByText("Analysis pending")).not.toBeInTheDocument();
  });

  it("identifies a filename-only fallback in the editor", () => {
    renderVisuals({
      visualAssets: [
        {
          ...assets[0],
          nova_description: null,
          nova_on_screen_text: null,
          source_type: "stub",
        },
      ],
    });

    expect(screen.getByText("Basic file details ready")).toBeInTheDocument();
    expect(screen.queryByText("Analysis pending")).not.toBeInTheDocument();
  });

  it("exposes card background, transition, duplication, and audio controls", async () => {
    const user = userEvent.setup({ delay: null, pointerEventsCheck: PointerEventsCheckLevel.Never });
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

    // Radix Select opens on pointerdown, not a native <select> change event —
    // userEvent drives the full click sequence jsdom needs (jest.setup.ts
    // polyfills hasPointerCapture/scrollIntoView/ResizeObserver for it).
    await user.click(screen.getByRole("combobox", { name: "Background type" }));
    await user.click(await screen.findByRole("option", { name: "Gradient" }));

    await user.click(screen.getByRole("combobox", { name: "Entrance" }));
    await user.click(await screen.findByRole("option", { name: "Fade" }));

    await user.click(screen.getByRole("combobox", { name: "Base audio" }));
    await user.click(await screen.findByRole("option", { name: "Mute" }));

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

  it("shows the eight-item catalog flag-off separately from route trace", () => {
    const onAddMotion = jest.fn();
    renderVisuals({
      motionAvailable: true,
      motionRuntimeCompatible: true,
      evolvingTypeEnabled: false,
      onAddMotion,
    });

    expect(screen.getByTestId("creator-block-grid").querySelectorAll("button")).toHaveLength(8);
    fireEvent.click(screen.getAllByRole("button", { name: "Wild Type" }).at(-1)!);
    expect(onAddMotion).toHaveBeenCalledWith("kinetic_word");
    expect(screen.getByText("Existing effect")).toBeInTheDocument();
    expect(screen.getByText("Route trace")).toBeInTheDocument();
  });

  it("shows the ninth Evolving Type insertion only when its exposure flag is on", () => {
    const onAddMotion = jest.fn();
    renderVisuals({
      motionAvailable: true,
      motionRuntimeCompatible: true,
      evolvingTypeEnabled: true,
      onAddMotion,
    });

    expect(screen.getByTestId("creator-block-grid").querySelectorAll("button")).toHaveLength(9);
    fireEvent.click(screen.getByRole("button", { name: "Evolving Type" }));
    expect(onAddMotion).toHaveBeenCalledWith("evolving_type");
  });

  it("keeps a persisted Evolving Type chip selectable when insertion exposure is off", () => {
    const onSelectMotion = jest.fn();
    renderVisuals({
      motionAvailable: true,
      motionRuntimeCompatible: true,
      evolvingTypeEnabled: false,
      onSelectMotion,
      motionScenes: [{
        id: "motion-evolving",
        preset_id: "evolving_type",
        preset_version: 2,
        start_frame: 0,
        end_frame_exclusive: 159,
        palette: { primary: "#0c0c0e", accent: "#c7ff3d" },
        intensity: 0.72,
        motion: { version: 2, speed: 1, easing: "ease-in-out-cubic", hold_frames: 30 },
        params: {
          headline: "EVOLVE THE IDEA", subtitle: "Shape, split, and settle into focus",
          icon_count: 4, icon_style: "organic", text_stagger_ms: 45,
          icon_stagger_ms: 70, morph_amplitude: 0.65, density: "medium",
          layout: "compact", order: "forward", typography_scale: 1,
          backdrop_opacity: 0.7, split_icons: true,
        },
      }],
    });

    expect(screen.queryByTestId("creator-block-grid")?.querySelector('[data-preset="evolving_type"]')).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Evolving Type" }));
    expect(onSelectMotion).toHaveBeenCalledWith("motion-evolving");
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
