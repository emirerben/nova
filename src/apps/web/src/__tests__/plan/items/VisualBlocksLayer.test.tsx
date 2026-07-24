import { render } from "@testing-library/react";
import VisualBlocksLayer from "@/app/plan/items/[id]/_editor/VisualBlocksLayer";
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
      <VisualBlocksLayer blocks={[block]} assets={assets} currentTime={1.5} />,
    );
    expect(container.querySelector('[data-visual-block-id="montage-1"]')).toBeTruthy();
    expect(container.querySelector("img")?.getAttribute("src")).toBe(
      "https://signed/frame.jpg",
    );

    rerender(<VisualBlocksLayer blocks={[block]} assets={assets} currentTime={2.1} />);
    expect(container.querySelector('[data-visual-block-id="montage-1"]')).toBeNull();
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
});
