import { fireEvent, render, screen } from "@testing-library/react";
import "@testing-library/jest-dom";

import { LyricsTab } from "@/app/admin/music/[id]/components/LyricsTab";
import {
  adminCreateLyricsPreview,
  adminGetLyricsPreviewStatus,
  adminPatchLyricsConfig,
  type LyricsConfig,
  type MusicTrackDetail,
} from "@/lib/music-api";

// Gate test for the Beat It bug (admin music job 616d3e53): when the Config
// tab's best_start_s / best_end_s form state diverges from the persisted
// values, the lyric preview button must disable and surface a "Save first"
// hint. The preview endpoint re-reads the DB, so firing while dirty would
// render against the old section bounds (and the user would think the section
// click "didn't work").
//
// Typed end-to-end (no `@ts-nocheck`) — these tests should catch prop drift,
// API contract changes, or fixture/type mismatches at typecheck time, not
// at runtime.

type PollerState = { data: unknown; polling: boolean; error: string | null };

let mockPollerState: PollerState = {
  data: null,
  polling: false,
  error: null,
};

jest.mock("@/hooks/useJobPoller", () => ({
  useJobPoller: () => mockPollerState,
}));

jest.mock("@/lib/music-api", () => ({
  adminCreateLyricsPreview: jest.fn(),
  adminExtractLyrics: jest.fn(),
  adminGetLyricsPreviewStatus: jest.fn(),
  adminGetMusicTrack: jest.fn(),
  adminPatchLyricsConfig: jest.fn(),
  adminUpdateMusicTrack: jest.fn(),
}));

jest.mock("@/lib/admin-api", () => ({
  adminUpdateTemplateLyricsConfig: jest.fn(),
}));

const baseLineConfig: LyricsConfig = {
  enabled: true,
  style: "line",
  pre_roll_s: 0.1,
  post_dwell_s: 1,
  next_line_gap_s: 0.1,
  fade_in_ms: 150,
  fade_out_ms: 250,
  hold_to_next_threshold_ms: 500,
};

function makeTrack(): MusicTrackDetail {
  return {
    id: "track-1",
    title: "Beat It",
    artist: "Michael Jackson",
    source_url: "",
    audio_gcs_path: "music/track-1/audio.m4a",
    duration_s: 298,
    beat_count: 580,
    beat_timestamps_s: null,
    analysis_status: "ready",
    error_detail: null,
    thumbnail_url: null,
    published_at: null,
    archived_at: null,
    track_config: {
      required_clips_min: 1,
      required_clips_max: 20,
      lyrics_config: baseLineConfig,
      best_start_s: 127.2,
      best_end_s: 141.0,
      slot_every_n_beats: 8,
    },
    best_sections: null,
    section_version: null,
    label_version: null,
    has_ai_labels: false,
    generative_matchable: false,
    created_at: "2026-05-27T10:00:00Z",
    lyrics_status: "ready",
    lyrics_source: "lrclib_synced+whisper",
    lyrics_error_detail: null,
    lyrics_extracted_at: "2026-05-27T10:02:00Z",
    lyrics_cached: {
      source: "lrclib_synced+whisper",
      language: "en",
      track_title_matched: "Beat It",
      artist_matched: "Michael Jackson",
      genius_url: "",
      confidence: 0.92,
      lines: [
        {
          text: "They told him don't you ever come around here",
          start_s: 130.0,
          end_s: 132.0,
          words: [],
        },
      ],
    },
  };
}

describe("MusicSectionDirtyGate", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockPollerState = { data: null, polling: false, error: null };
    (adminCreateLyricsPreview as jest.Mock).mockResolvedValue({
      job_id: "lyrics-preview-job",
      status: "queued",
      music_track_id: "track-1",
    });
    (adminGetLyricsPreviewStatus as jest.Mock).mockResolvedValue({ status: "queued" });
    (adminPatchLyricsConfig as jest.Mock).mockResolvedValue({
      lyrics_config: baseLineConfig,
    });
  });

  it("disables the preview button and renders the save-first hint when section bounds are dirty", () => {
    render(
      <LyricsTab
        trackId="track-1"
        track={makeTrack()}
        onTrackUpdated={jest.fn()}
        sectionBoundsDirty
      />,
    );

    const button = screen.getByTestId("lyrics-timing-preview-button");
    expect(button).toBeDisabled();

    const hint = screen.getByTestId("lyrics-timing-preview-hint");
    expect(hint).toBeInTheDocument();
    expect(hint.textContent).toMatch(/Save section bounds/i);
  });

  it("enables the preview button and hides the hint when section bounds match the persisted track", () => {
    render(
      <LyricsTab
        trackId="track-1"
        track={makeTrack()}
        onTrackUpdated={jest.fn()}
        sectionBoundsDirty={false}
      />,
    );

    const button = screen.getByTestId("lyrics-timing-preview-button");
    expect(button).not.toBeDisabled();
    expect(screen.queryByTestId("lyrics-timing-preview-hint")).not.toBeInTheDocument();
  });

  it("blocks the preview from firing while the dirty gate is active even if the click slips through", () => {
    render(
      <LyricsTab
        trackId="track-1"
        track={makeTrack()}
        onTrackUpdated={jest.fn()}
        sectionBoundsDirty
      />,
    );

    // The button is disabled at the DOM level — fireEvent.click on a disabled
    // button still dispatches but React's onClick handler is bypassed. This
    // asserts the user-visible contract: a dirty gate prevents the network
    // call regardless of whether the button receives a synthetic click.
    fireEvent.click(screen.getByTestId("lyrics-timing-preview-button"));
    expect(adminCreateLyricsPreview).not.toHaveBeenCalled();
  });
});
