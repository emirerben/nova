import { render } from "@testing-library/react";
import VisualBlocksLayer, {
  visualShotPreviewState,
} from "@/app/plan/items/[id]/_editor/VisualBlocksLayer";
import type { PoolAsset, VisualBlock } from "@/lib/plan-api";

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

describe("VisualBlocksLayer", () => {
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
});
