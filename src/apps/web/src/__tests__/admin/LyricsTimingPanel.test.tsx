// @ts-nocheck
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom";

import { LyricsTimingPanel, normalizeLyricsConfig } from "@/app/admin/music/[id]/components/LyricsTimingPanel";
import { adminPatchLyricsConfig } from "@/lib/music-api";

jest.mock("@/lib/music-api", () => ({
  adminPatchLyricsConfig: jest.fn(),
}));

const mockPatch = adminPatchLyricsConfig as jest.MockedFunction<
  typeof adminPatchLyricsConfig
>;

const savedConfig = {
  enabled: true,
  style: "line" as const,
  pre_roll_s: 0.1,
  post_dwell_s: 1,
  next_line_gap_s: 0.1,
  fade_in_ms: 150,
  fade_out_ms: 250,
  hold_to_next_threshold_ms: 500,
};

describe("LyricsTimingPanel", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockPatch.mockResolvedValue({ lyrics_config: savedConfig });
  });

  it("normalizes equivalent float formatting", () => {
    expect(normalizeLyricsConfig({ post_dwell_s: 1 })).toEqual(
      normalizeLyricsConfig({ post_dwell_s: 1.0 }),
    );
  });

  it("renders all six timing fields from saved config", () => {
    render(
      <LyricsTimingPanel
        trackId="track-1"
        savedConfig={savedConfig}
        onSubmit={jest.fn()}
      />,
    );

    expect(screen.getByLabelText("Pre-roll")).toHaveValue(0.1);
    expect(screen.getByLabelText("Post-dwell")).toHaveValue(1);
    expect(screen.getByLabelText("Next-line gap")).toHaveValue(0.1);
    expect(screen.getByLabelText("Fade in")).toHaveValue(150);
    expect(screen.getByLabelText("Fade out")).toHaveValue(250);
    expect(screen.getByLabelText("Hold-to-next")).toHaveValue(500);
    expect(screen.getByText("Save as track defaults")).toBeDisabled();
  });

  it("enables save and shows the unsaved banner after a genuine change", () => {
    render(
      <LyricsTimingPanel
        trackId="track-1"
        savedConfig={savedConfig}
        onSubmit={jest.fn()}
      />,
    );

    fireEvent.change(screen.getByLabelText("Post-dwell"), {
      target: { value: "0.3" },
    });

    expect(screen.getByText("Save as track defaults")).not.toBeDisabled();
    expect(
      screen.getByText("Rendering with unsaved lyric timing overrides."),
    ).toBeInTheDocument();
  });

  it("keeps save disabled for float-format-only edits", () => {
    render(
      <LyricsTimingPanel
        trackId="track-1"
        savedConfig={savedConfig}
        onSubmit={jest.fn()}
      />,
    );

    fireEvent.change(screen.getByLabelText("Post-dwell"), {
      target: { value: "1.0" },
    });

    expect(screen.getByText("Save as track defaults")).toBeDisabled();
  });

  it("submits preview and full test actions with current values", () => {
    const onSubmit = jest.fn();
    render(
      <LyricsTimingPanel
        trackId="track-1"
        savedConfig={savedConfig}
        onSubmit={onSubmit}
      />,
    );

    fireEvent.change(screen.getByLabelText("Fade in"), {
      target: { value: "50" },
    });
    fireEvent.click(screen.getByText("Preview lyrics only"));
    fireEvent.click(screen.getByText("Render full test job"));

    expect(onSubmit).toHaveBeenNthCalledWith(
      1,
      "preview",
      expect.objectContaining({ fade_in_ms: 50 }),
    );
    expect(onSubmit).toHaveBeenNthCalledWith(
      2,
      "full_test",
      expect.objectContaining({ fade_in_ms: 50 }),
    );
  });

  it("saves track defaults and clears dirty state", async () => {
    mockPatch.mockResolvedValue({
      lyrics_config: { ...savedConfig, post_dwell_s: 0.3 },
    });
    const onSaved = jest.fn();
    render(
      <LyricsTimingPanel
        trackId="track-1"
        savedConfig={savedConfig}
        onSubmit={jest.fn()}
        onSaved={onSaved}
      />,
    );

    fireEvent.change(screen.getByLabelText("Post-dwell"), {
      target: { value: "0.3" },
    });
    fireEvent.click(screen.getByText("Save as track defaults"));

    await waitFor(() => {
      expect(mockPatch).toHaveBeenCalledWith(
        "track-1",
        expect.objectContaining({ post_dwell_s: 0.3 }),
      );
    });
    expect(onSaved).toHaveBeenCalledWith(
      expect.objectContaining({ post_dwell_s: 0.3 }),
    );
  });

  it("reset restores saved config", () => {
    render(
      <LyricsTimingPanel
        trackId="track-1"
        savedConfig={savedConfig}
        onSubmit={jest.fn()}
      />,
    );

    fireEvent.change(screen.getByLabelText("Post-dwell"), {
      target: { value: "0.3" },
    });
    fireEvent.click(screen.getByText("Reset to saved"));

    expect(screen.getByLabelText("Post-dwell")).toHaveValue(1);
    expect(screen.getByText("Save as track defaults")).toBeDisabled();
  });
});
