import "@testing-library/jest-dom";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { fireEvent, render } from "@testing-library/react";
import VisualBlocksLayer, {
  visualShotPreviewState,
} from "@/app/plan/items/[id]/_editor/VisualBlocksLayer";
import { mediaPreviewGeometry } from "@/app/plan/items/[id]/_editor/editor-media-visuals";
import type { MediaVisualBlock, PoolAsset, VisualBlock } from "@/lib/plan-api";

const assets: PoolAsset[] = [
  {
    id: "asset-1",
    kind: "image",
    status: "ready",
    source_filename: "frame.jpg",
    duration_s: null,
    aspect: 0.5625,
    subject: "Frame",
    user_context: "",
    nova_description: "Nova sees a frame",
    nova_on_screen_text: null,
    display_url: "https://signed/frame.jpg",
    deduped: false,
    gcs_path: "users/u/plan/i/pool/frame.jpg",
  },
];

const geometryFixture = JSON.parse(
  readFileSync(resolve(process.cwd(), "../../../tests/fixtures/media-geometry/v1.json"), "utf8"),
) as {
  cases: Array<{
    name: string;
    asset_aspect: number;
    fit_mode: "contain" | "cover";
    zoom: number;
    focal_x: number;
    focal_y: number;
    preview: { width_pct: number; height_pct: number; left_pct: number; top_pct: number };
  }>;
};

describe("VisualBlocksLayer", () => {
  function media(overrides: Partial<MediaVisualBlock> = {}): MediaVisualBlock {
    return {
      version: 1,
      id: "media-1",
      kind: "media",
      start_s: 1,
      end_s: 2,
      timing_mode: "manual",
      origin: "user",
      transition_in: "cut",
      transition_out: "cut",
      audio_policy: { base: "continue", sfx: "continue" },
      asset_id: "asset-1",
      src_gcs_path: assets[0].gcs_path,
      media_kind: "image",
      display_mode: "fullscreen",
      transform: { fit_mode: "contain", focal_x: 0.5, focal_y: 0.5, zoom: 1 },
      x_frac: 0.5,
      y_frac: 0.5,
      scale: 0.35,
      z: 1,
      ...overrides,
    };
  }

  it("renders media with contain/cover and keeps z inside the fixed media layer", () => {
    const { container, rerender } = render(
      <VisualBlocksLayer blocks={[media()]} assets={assets} currentTime={1.5} />,
    );
    const layer = container.querySelector('[data-visual-block-layer="true"]') as HTMLElement;
    const image = container.querySelector('[data-media-visual-block="true"] img') as HTMLImageElement;
    expect(layer).toHaveStyle({ zIndex: "10" });
    expect(image).toHaveStyle({ objectFit: "contain" });
    rerender(<VisualBlocksLayer blocks={[media({ transform: { fit_mode: "cover", focal_x: 0.2, focal_y: 0.8, zoom: 2 } })]} assets={assets} currentTime={1.5} />);
    expect(container.querySelector('[data-media-visual-block="true"] img')).toHaveStyle({ objectFit: "cover" });
    expect(container.querySelector('[data-media-visual-block="true"]')).toHaveStyle({
      left: "-20%",
      top: "-80%",
      width: "200%",
      height: "200%",
    });
  });

  it.each(geometryFixture.cases)(
    "matches the shared render geometry fixture for $name",
    ({ asset_aspect, fit_mode, zoom, focal_x, focal_y, preview }) => {
      const geometry = mediaPreviewGeometry(
        media({
          transform: { fit_mode, focal_x, focal_y, zoom },
        }),
        asset_aspect,
      );
      expect(geometry.widthPct).toBeCloseTo(preview.width_pct, 8);
      expect(geometry.heightPct).toBeCloseTo(preview.height_pct, 8);
      expect(geometry.leftPct).toBeCloseTo(preview.left_pct, 8);
      expect(geometry.topPct).toBeCloseTo(preview.top_pct, 8);
    },
  );

  it("preserves source aspect for overlays on a portrait canvas", () => {
    expect(mediaPreviewGeometry(media({ display_mode: "overlay", scale: 0.4 }), 16 / 9))
      .toEqual({ leftPct: 30, topPct: 43.671875, widthPct: 40, heightPct: 12.65625 });
  });

  it("uses half-open windows and renders overlapping media in z order", () => {
    const lower = media({ id: "lower", z: 1 });
    const upper = media({ id: "upper", z: 4, start_s: 1.5 });
    const { container, rerender } = render(<VisualBlocksLayer blocks={[upper, lower]} assets={assets} currentTime={1.5} />);
    expect(container.querySelectorAll('[data-media-visual-block="true"]')).toHaveLength(2);
    expect(container.querySelector('[data-visual-block-id="lower"]')).toHaveStyle({ zIndex: "2" });
    rerender(<VisualBlocksLayer blocks={[lower]} assets={assets} currentTime={2} />);
    expect(container.querySelector('[data-visual-block-layer="true"]')).toBeNull();
  });

  it("records one gesture and commits move/resize through the interaction callbacks", () => {
    const onRecord = jest.fn();
    const onPreview = jest.fn();
    const onPatch = jest.fn();
    const { container } = render(
      <VisualBlocksLayer
        blocks={[media({ display_mode: "overlay" })]}
        assets={assets}
        currentTime={1.5}
        allowManipulation
        selectedMediaBlockId="media-1"
        onSelectMediaBlock={jest.fn()}
        onRecordMediaBlock={onRecord}
        onPreviewMediaBlock={onPreview}
        onPatchMediaBlock={onPatch}
      />,
    );
    const card = container.querySelector('[data-media-visual-block="true"]') as HTMLElement;
    fireEvent.pointerDown(card, { button: 0, clientX: 10, clientY: 10 });
    fireEvent.pointerMove(window, { clientX: 30, clientY: 30 });
    fireEvent.pointerUp(window, { clientX: 30, clientY: 30 });
    expect(onRecord).toHaveBeenCalledTimes(1);
    expect(onPreview).toHaveBeenCalled();
    expect(onPatch).toHaveBeenCalledTimes(1);
    onPreview.mockClear();
    onPatch.mockClear();
    fireEvent.pointerDown(container.querySelector("button[aria-label='Resize media overlay']")!, { button: 0, clientX: 20, clientY: 20 });
    fireEvent.pointerMove(window, { clientX: 80, clientY: 80 });
    fireEvent.pointerUp(window, { clientX: 80, clientY: 80 });
    expect(onRecord).toHaveBeenCalledTimes(2);
    expect(onPreview).toHaveBeenCalledWith("media-1", expect.objectContaining({ scale: expect.any(Number) }));
    expect(onPatch).toHaveBeenCalledWith("media-1", expect.objectContaining({ scale: expect.any(Number) }));
  });

  it("directly drags contained and covered fullscreen media in the pointer direction", () => {
    const onRecord = jest.fn();
    const onPreview = jest.fn();
    const onPatch = jest.fn();
    const bounds = jest.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue({
      width: 100,
      height: 200,
      left: 0,
      top: 0,
      right: 100,
      bottom: 200,
      x: 0,
      y: 0,
      toJSON: () => ({}),
    });
    const landscapeAssets = [{ ...assets[0], aspect: 16 / 9 }];
    const { container, rerender } = render(
      <VisualBlocksLayer
        blocks={[media()]}
        assets={landscapeAssets}
        currentTime={1.5}
        allowManipulation
        onRecordMediaBlock={onRecord}
        onPreviewMediaBlock={onPreview}
        onPatchMediaBlock={onPatch}
      />,
    );
    const card = container.querySelector('[data-media-visual-block="true"]') as HTMLElement;
    const dispatchPointer = (target: Window | HTMLElement, type: string, clientX: number, clientY: number) => {
      const event = new Event(type, { bubbles: true });
      Object.defineProperties(event, {
        button: { value: 0 },
        clientX: { value: clientX },
        clientY: { value: clientY },
      });
      fireEvent(target, event);
    };
    dispatchPointer(card, "pointerdown", 20, 40);
    dispatchPointer(window, "pointermove", 20, 80);
    dispatchPointer(window, "pointerup", 20, 80);

    expect(onRecord).toHaveBeenCalledTimes(1);
    expect(onPatch).toHaveBeenCalledTimes(1);
    const containedPatch = onPatch.mock.calls[0][1] as Partial<MediaVisualBlock>;
    const containedGeometry = mediaPreviewGeometry(
      media({ transform: containedPatch.transform! }),
      16 / 9,
    );
    expect(containedGeometry.topPct).toBeCloseTo(
      mediaPreviewGeometry(media(), 16 / 9).topPct + 20,
      8,
    );

    onRecord.mockClear();
    onPreview.mockClear();
    onPatch.mockClear();
    rerender(
      <VisualBlocksLayer
        blocks={[media({ transform: { fit_mode: "cover", focal_x: 0.5, focal_y: 0.5, zoom: 1 } })]}
        assets={landscapeAssets}
        currentTime={1.5}
        allowManipulation
        onRecordMediaBlock={onRecord}
        onPreviewMediaBlock={onPreview}
        onPatchMediaBlock={onPatch}
      />,
    );
    const coveredCard = container.querySelector('[data-media-visual-block="true"]') as HTMLElement;
    dispatchPointer(coveredCard, "pointerdown", 20, 40);
    dispatchPointer(window, "pointermove", 40, 40);
    dispatchPointer(window, "pointerup", 40, 40);
    const coveredPatch = onPatch.mock.calls[0][1] as Partial<MediaVisualBlock>;
    const coveredGeometry = mediaPreviewGeometry(
      media({ transform: coveredPatch.transform! }),
      16 / 9,
    );
    expect(coveredGeometry.leftPct).toBeCloseTo(
      mediaPreviewGeometry(
        media({ transform: { fit_mode: "cover", focal_x: 0.5, focal_y: 0.5, zoom: 1 } }),
        16 / 9,
      ).leftPct + 20,
      8,
    );
    expect(onRecord).toHaveBeenCalledTimes(1);
    bounds.mockRestore();
  });

  it("matches the renderer by skipping fades for media windows at or below 0.3 seconds", () => {
    const { container, rerender } = render(
      <VisualBlocksLayer
        blocks={[media({ start_s: 1, end_s: 1.2, transition_in: "fade", transition_out: "fade" })]}
        assets={assets}
        currentTime={1.01}
      />,
    );
    expect(container.querySelector('[data-media-visual-block="true"]')).toHaveStyle({ opacity: "1" });
    rerender(
      <VisualBlocksLayer
        blocks={[media({ start_s: 1, end_s: 2, transition_in: "fade" })]}
        assets={assets}
        currentTime={1.01}
      />,
    );
    expect(container.querySelector('[data-media-visual-block="true"]')).not.toHaveStyle({ opacity: "1" });
  });

  it("renders a full-frame montage shot only inside its concrete window", () => {
    const block: VisualBlock = {
      version: 1,
      id: "montage-1",
      kind: "montage",
      start_s: 1,
      end_s: 2,
      timing_mode: "manual",
      origin: "user",
      transition_in: "cut",
      transition_out: "cut",
      audio_policy: { base: "continue", sfx: "continue" },
      shots: [
        {
          id: "shot-1",
          asset_id: "asset-1",
          src_gcs_path: assets[0].gcs_path,
          kind: "image",
          start_offset_s: 0,
          duration_s: 1,
          crop: { x_frac: 0.5, y_frac: 0.5, scale: 1 },
          motion: "zoom_in",
        },
      ],
    };
    const { container, rerender } = render(
      <VisualBlocksLayer blocks={[block]} assets={assets} currentTime={1.5} frameDriven />,
    );
    expect(container.querySelector('[data-visual-block-id="montage-1"]')).toBeTruthy();
    expect(container.querySelector("img")?.getAttribute("src")).toBe(
      "https://signed/frame.jpg",
    );
    const midTransform = container.querySelector("img")?.style.transform;
    expect(parseFloat(midTransform?.match(/scale\(([^)]+)\)/)?.[1] ?? "NaN")).toBeCloseTo(
      1 + 0.08 * (15 / 29),
      10,
    );

    rerender(<VisualBlocksLayer blocks={[block]} assets={assets} currentTime={1.75} frameDriven />);
    expect(container.querySelector("img")?.style.transform).not.toBe(midTransform);

    rerender(<VisualBlocksLayer blocks={[block]} assets={assets} currentTime={2.1} frameDriven />);
    expect(container.querySelector('[data-visual-block-id="montage-1"]')).toBeNull();
  });

  it("matches the renderer's rounded 30fps linear state at every export sample", () => {
    const shot: Extract<VisualBlock, { kind: "montage" }>["shots"][number] = {
      id: "shot-parity",
      asset_id: "asset-1",
      src_gcs_path: assets[0].gcs_path,
      kind: "image",
      start_offset_s: 0,
      duration_s: 1,
      crop: { x_frac: 0.5, y_frac: 0.5, scale: 1 },
      motion: "pan_right",
    };
    for (let frame = 0; frame < 30; frame += 1) {
      const state = visualShotPreviewState(shot, frame / 30);
      expect(state.progress).toBeCloseTo(frame / 29, 12);
      expect(state.xFrac).toBeCloseTo(0.5 + 0.08 * (frame / 29), 12);
    }
  });

  it.each([
    ["zoom_in", 1, 1.08, 0.5],
    ["zoom_out", 1.08, 1, 0.5],
    ["pan_left", 1, 1, 0.42],
    ["pan_right", 1, 1, 0.58],
    ["none", 1, 1, 0.5],
  ] as const)("pins %s start/end transform state", (motion, startScale, endScale, endX) => {
    const shot: Extract<VisualBlock, { kind: "montage" }>['shots'][number] = {
      id: `shot-${motion}`,
      asset_id: "asset-1",
      src_gcs_path: assets[0].gcs_path,
      kind: "image",
      start_offset_s: 0,
      duration_s: 1,
      crop: { x_frac: 0.5, y_frac: 0.5, scale: 1 },
      motion,
    };
    expect(visualShotPreviewState(shot, 0)).toMatchObject({
      progress: 0,
      scale: startScale,
      xFrac: 0.5,
    });
    expect(visualShotPreviewState(shot, 29 / 30)).toMatchObject({
      progress: 1,
      scale: endScale,
      xFrac: endX,
    });
  });

  it("clamps short shots, crop boundaries, and times outside the shot window", () => {
    const shot: Extract<VisualBlock, { kind: "montage" }>['shots'][number] = {
      id: "shot-boundary",
      asset_id: "asset-1",
      src_gcs_path: assets[0].gcs_path,
      kind: "image",
      start_offset_s: 0,
      duration_s: 0.01,
      crop: { x_frac: 0.98, y_frac: 1.4, scale: 0.5 },
      motion: "pan_right",
    };
    expect(visualShotPreviewState(shot, -1)).toEqual({
      progress: 1,
      scale: 1,
      xFrac: 1,
      yFrac: 1,
    });
    expect(visualShotPreviewState({ ...shot, duration_s: 1.016 }, 99)).toMatchObject({
      progress: 1,
      xFrac: 1,
    });
  });

  it("previews a gradient text-card background beneath editor text", () => {
    const block: VisualBlock = {
      version: 1,
      id: "card-1",
      kind: "text_card",
      start_s: 0,
      end_s: 2,
      timing_mode: "manual",
      origin: "user",
      transition_in: "fade",
      transition_out: "fade",
      audio_policy: { base: "mute", sfx: "continue" },
      background: {
        type: "gradient",
        from: "#111111",
        to: "#26382F",
        angle_deg: 90,
      },
    };
    const { container } = render(
      <VisualBlocksLayer blocks={[block]} assets={[]} currentTime={1} />,
    );
    expect(container.querySelector('[data-visual-block-id="card-1"]')).toBeTruthy();
    expect(container.querySelector('[data-visual-background="gradient"]')).toBeTruthy();
  });

  it("pauses frame-driven visual videos with the editor transport", () => {
    const play = jest
      .spyOn(HTMLMediaElement.prototype, "play")
      .mockImplementation(() => Promise.resolve());
    const pause = jest
      .spyOn(HTMLMediaElement.prototype, "pause")
      .mockImplementation(() => {});
    const videoAssets: PoolAsset[] = [
      { ...assets[0], kind: "video", duration_s: 3, display_url: "https://signed/shot.mp4" },
    ];
    const block: VisualBlock = {
      version: 1,
      id: "video-montage",
      kind: "montage",
      start_s: 0,
      end_s: 2,
      timing_mode: "manual",
      origin: "user",
      transition_in: "cut",
      transition_out: "cut",
      audio_policy: { base: "continue", sfx: "continue" },
      shots: [
        {
          id: "video-shot",
          asset_id: "asset-1",
          src_gcs_path: videoAssets[0].gcs_path,
          kind: "video",
          start_offset_s: 0,
          duration_s: 2,
          crop: { x_frac: 0.5, y_frac: 0.5, scale: 1 },
          motion: "none",
        },
      ],
    };
    const view = render(
      <VisualBlocksLayer
        blocks={[block]}
        assets={videoAssets}
        currentTime={0.5}
        frameDriven
        playing
      />,
    );
    expect(play).toHaveBeenCalled();

    view.rerender(
      <VisualBlocksLayer
        blocks={[block]}
        assets={videoAssets}
        currentTime={0.5}
        frameDriven
        playing={false}
      />,
    );
    expect(pause).toHaveBeenCalled();
    play.mockRestore();
    pause.mockRestore();
  });

  it("does not restart a media video on every frame-driven time sample", () => {
    const play = jest
      .spyOn(HTMLMediaElement.prototype, "play")
      .mockImplementation(() => Promise.resolve());
    const pause = jest
      .spyOn(HTMLMediaElement.prototype, "pause")
      .mockImplementation(() => {});
    const videoAssets: PoolAsset[] = [
      { ...assets[0], kind: "video", duration_s: 3, display_url: "https://signed/shot.mp4" },
    ];
    const block = media({
      media_kind: "video",
      source_duration_s: 3,
      trim_start_s: 0.5,
      trim_end_s: 2.5,
    });
    const view = render(
      <VisualBlocksLayer blocks={[block]} assets={videoAssets} currentTime={1.2} frameDriven playing />,
    );
    expect(play).toHaveBeenCalledTimes(1);
    view.rerender(
      <VisualBlocksLayer blocks={[block]} assets={videoAssets} currentTime={1.3} frameDriven playing />,
    );
    expect(play).toHaveBeenCalledTimes(1);
    play.mockRestore();
    pause.mockRestore();
  });
});
