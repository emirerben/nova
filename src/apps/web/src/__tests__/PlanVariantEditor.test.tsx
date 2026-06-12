import "@testing-library/jest-dom";
import { act, fireEvent, render, screen } from "@testing-library/react";
import PlanVariantEditor from "@/app/plan/_components/PlanVariantEditor";
import type { PlanItemVariant } from "@/lib/plan-api";
import type { GenerativeStyleSet } from "@/lib/generative-api";
import type { MusicTrackSummary } from "@/lib/music-api";

const TRACKS = [
  { id: "t1", title: "Track One", artist: "A" },
  { id: "t2", title: "Track Two", artist: "B" },
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

function renderEditor(variant: PlanItemVariant, overrides = {}) {
  const cbs = {
    onSwap: jest.fn().mockResolvedValue(undefined),
    onRetext: jest.fn().mockResolvedValue(undefined),
    onRemoveText: jest.fn().mockResolvedValue(undefined),
    onChangeStyle: jest.fn().mockResolvedValue(undefined),
    onResize: jest.fn().mockResolvedValue(undefined),
    onChangeLayout: jest.fn().mockResolvedValue(undefined),
    ...overrides,
  };
  render(
    <PlanVariantEditor
      variant={variant}
      tracks={TRACKS}
      styleSets={STYLE_SETS}
      onSwap={cbs.onSwap}
      onRetext={cbs.onRetext}
      onRemoveText={cbs.onRemoveText}
      onChangeStyle={cbs.onChangeStyle}
      onResize={cbs.onResize}
      onChangeLayout={cbs.onChangeLayout}
    />,
  );
  return cbs;
}

test("shows all edit controls for a song variant", () => {
  renderEditor(songVariant);
  expect(screen.getByText("Edit text")).toBeInTheDocument();
  expect(screen.getByText("Remove text")).toBeInTheDocument();
  expect(screen.getByRole("radiogroup", { name: "Text style" })).toBeInTheDocument();
  // Song section present → has a "Change" toggle.
  expect(screen.getByRole("button", { name: "Change" })).toBeInTheDocument();
});

test("hides the song picker for the original-audio variant (no track)", () => {
  renderEditor(originalVariant);
  expect(screen.queryByRole("button", { name: "Change" })).not.toBeInTheDocument();
  // text + style controls still present
  expect(screen.getByText("Edit text")).toBeInTheDocument();
  expect(screen.getByRole("radiogroup", { name: "Text style" })).toBeInTheDocument();
});

test("inline edit submits trimmed text via onRetext", async () => {
  const { onRetext } = renderEditor(songVariant);
  fireEvent.click(screen.getByText("Edit text"));
  const input = screen.getByPlaceholderText("New intro text…");
  fireEvent.change(input, { target: { value: "  fresh hook  " } });
  await act(async () => {
    fireEvent.click(screen.getByText("Save"));
  });
  expect(onRetext).toHaveBeenCalledWith("fresh hook");
});

test("remove text fires onRemoveText", async () => {
  const { onRemoveText } = renderEditor(songVariant);
  await act(async () => {
    fireEvent.click(screen.getByText("Remove text"));
  });
  expect(onRemoveText).toHaveBeenCalledTimes(1);
});

test("choosing a track in the picker fires onSwap with its id", async () => {
  const { onSwap } = renderEditor(songVariant);
  fireEvent.click(screen.getByRole("button", { name: "Change" }));
  // Track Two is not current → its "Use" button is enabled.
  await act(async () => {
    fireEvent.click(screen.getByText("Use"));
  });
  expect(onSwap).toHaveBeenCalledWith("t2");
});

test("selecting a different style chip fires onChangeStyle; re-selecting current is a no-op", async () => {
  const { onChangeStyle } = renderEditor(songVariant);
  await act(async () => {
    fireEvent.click(screen.getByRole("radio", { name: "Text style: Bold" }));
  });
  expect(onChangeStyle).toHaveBeenCalledWith("bold");
  // Clicking the already-selected default does nothing.
  fireEvent.click(screen.getByRole("radio", { name: "Text style: Default" }));
  expect(onChangeStyle).toHaveBeenCalledTimes(1);
});

test("controls are disabled while the variant is rendering", () => {
  renderEditor({ ...songVariant, render_status: "rendering" });
  expect(screen.getByText("Edit text")).toBeDisabled();
  expect(screen.getByRole("radio", { name: "Text style: Bold" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "Change" })).toBeDisabled();
});

// ── Text size stepper ──────────────────────────────────────────────────────

test("size stepper hidden until a computed size exists", () => {
  renderEditor(songVariant); // no intro_text_size_px
  expect(screen.queryByRole("button", { name: "Bigger intro text" })).not.toBeInTheDocument();
});

test("A+ / A- nudge the size by the step via onResize", async () => {
  const { onResize } = renderEditor({
    ...songVariant,
    intro_text_size_px: 72,
    intro_size_source: "computed",
  });
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "Bigger intro text" }));
  });
  expect(onResize).toHaveBeenCalledWith(78); // 72 + step(6)
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "Smaller intro text" }));
  });
  expect(onResize).toHaveBeenCalledWith(66); // 72 - step(6)
});

test("size stepper clamps at the envelope bounds", () => {
  renderEditor({ ...songVariant, intro_text_size_px: 80, intro_size_source: "user" });
  expect(screen.getByRole("button", { name: "Bigger intro text" })).toBeDisabled(); // at MAX
  expect(screen.getByRole("button", { name: "Smaller intro text" })).not.toBeDisabled();
});


// ── Layout pick (Classic / Editorial) ─────────────────────────────────────────

test("editorial layout pick fires onChangeLayout for a short hook", async () => {
  const cbs = renderEditor({ ...songVariant, intro_text: "what's your favorite place?" });
  const editorial = screen.getByRole("button", { name: "Editorial" });
  expect(editorial).toBeEnabled();
  await act(async () => {
    fireEvent.click(editorial);
  });
  expect(cbs.onChangeLayout).toHaveBeenCalledWith("cluster");
});

test("editorial chip disables on a wordy hook with a hint", () => {
  renderEditor({
    ...songVariant,
    intro_text: "when they don't even listen to your feelings",
  });
  expect(screen.getByRole("button", { name: "Editorial" })).toBeDisabled();
  expect(screen.getByText(/shorten the caption/i)).toBeInTheDocument();
});

test("cluster variant shows Classic as the way back", async () => {
  const cbs = renderEditor({
    ...songVariant,
    intro_text: "what's your favorite place?",
    intro_layout: "cluster",
  });
  expect(screen.getByRole("button", { name: "Editorial" })).toBeDisabled(); // already active
  const classic = screen.getByRole("button", { name: "Classic" });
  await act(async () => {
    fireEvent.click(classic);
  });
  expect(cbs.onChangeLayout).toHaveBeenCalledWith("linear");
});

test("layout section hidden for lyrics variants", () => {
  renderEditor({ ...songVariant, text_mode: "lyrics", intro_text: "three word hook" });
  expect(screen.queryByRole("radiogroup", { name: "Intro text layout" })).toBeNull();
});
