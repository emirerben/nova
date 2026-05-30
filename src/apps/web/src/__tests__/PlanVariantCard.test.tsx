import "@testing-library/jest-dom";
import { act, fireEvent, render, screen } from "@testing-library/react";
import PlanVariantCard from "@/app/plan/_components/PlanVariantCard";
import type { PlanItemVariant } from "@/lib/plan-api";
import type { GenerativeStyleSet } from "@/lib/generative-api";
import type { MusicTrackSummary } from "@/lib/music-api";

const TRACKS = [
  { id: "t1", title: "Track One" },
  { id: "t2", title: "Track Two" },
] as unknown as MusicTrackSummary[];

const STYLE_SETS: GenerativeStyleSet[] = [
  { id: "default", label: "Default", tags: [] },
  { id: "bold", label: "Bold", tags: [] },
];

const songVariant: PlanItemVariant = {
  variant_id: "song_text",
  output_url: "https://x/song.mp4",
  render_status: "ready",
  text_mode: "agent_text",
  music_track_id: "t1",
  track_title: "Track One",
  style_set_id: "default",
};

const originalVariant: PlanItemVariant = {
  variant_id: "original_text",
  output_url: "https://x/orig.mp4",
  render_status: "ready",
  text_mode: "agent_text",
  music_track_id: null,
  track_title: null,
  style_set_id: "default",
};

function renderCard(variant: PlanItemVariant, overrides = {}) {
  const cbs = {
    onSwap: jest.fn().mockResolvedValue(undefined),
    onRetext: jest.fn().mockResolvedValue(undefined),
    onRemoveText: jest.fn().mockResolvedValue(undefined),
    onChangeStyle: jest.fn().mockResolvedValue(undefined),
    ...overrides,
  };
  render(
    <PlanVariantCard
      variant={variant}
      tracks={TRACKS}
      styleSets={STYLE_SETS}
      onSwap={cbs.onSwap}
      onRetext={cbs.onRetext}
      onRemoveText={cbs.onRemoveText}
      onChangeStyle={cbs.onChangeStyle}
    />,
  );
  return cbs;
}

test("shows all edit controls for a song variant", () => {
  renderCard(songVariant);
  expect(screen.getByText("Edit text")).toBeInTheDocument();
  expect(screen.getByText("Remove text")).toBeInTheDocument();
  expect(screen.getByLabelText("Text style")).toBeInTheDocument();
  expect(screen.getByLabelText("Swap song")).toBeInTheDocument();
});

test("hides swap-song for the original-audio variant (no track)", () => {
  renderCard(originalVariant);
  expect(screen.queryByLabelText("Swap song")).not.toBeInTheDocument();
  // text + style controls still present
  expect(screen.getByText("Edit text")).toBeInTheDocument();
  expect(screen.getByLabelText("Text style")).toBeInTheDocument();
});

test("inline edit submits trimmed text via onRetext", async () => {
  const { onRetext } = renderCard(songVariant);
  fireEvent.click(screen.getByText("Edit text"));
  const input = screen.getByPlaceholderText("New intro text…");
  fireEvent.change(input, { target: { value: "  fresh hook  " } });
  await act(async () => {
    fireEvent.click(screen.getByText("Save"));
  });
  expect(onRetext).toHaveBeenCalledWith("fresh hook");
});

test("remove text fires onRemoveText", async () => {
  const { onRemoveText } = renderCard(songVariant);
  await act(async () => {
    fireEvent.click(screen.getByText("Remove text"));
  });
  expect(onRemoveText).toHaveBeenCalledTimes(1);
});

test("swap-song fires onSwap with the chosen track id", async () => {
  const { onSwap } = renderCard(songVariant);
  await act(async () => {
    fireEvent.change(screen.getByLabelText("Swap song"), { target: { value: "t2" } });
  });
  expect(onSwap).toHaveBeenCalledWith("t2");
});

test("changing style fires onChangeStyle; re-selecting the current style is a no-op", async () => {
  const { onChangeStyle } = renderCard(songVariant);
  const select = screen.getByLabelText("Text style");
  await act(async () => {
    fireEvent.change(select, { target: { value: "bold" } });
  });
  expect(onChangeStyle).toHaveBeenCalledWith("bold");
});

test("controls are disabled while the variant is rendering", () => {
  renderCard({ ...songVariant, render_status: "rendering" });
  expect(screen.getByText("Rendering…")).toBeInTheDocument();
  expect(screen.getByText("Edit text")).toBeDisabled();
  expect(screen.getByLabelText("Swap song")).toBeDisabled();
});
