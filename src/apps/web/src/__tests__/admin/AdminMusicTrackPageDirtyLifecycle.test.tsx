import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom";

import AdminMusicTrackPage from "@/app/admin/music/[id]/page";
import {
  adminGetMusicTrack,
  adminUpdateMusicTrack,
  type LyricsConfig,
  type MusicTrackDetail,
} from "@/lib/music-api";

// Integration test for the section-bounds dirty-state lifecycle introduced
// to fix the Beat It bug (admin music job 616d3e53). The lifecycle spans
// three components — `AdminMusicTrackPage` owns the form state and computes
// `sectionBoundsDirty`, while `LyricsTab` / `TestTab` consume it via prop
// plumbing. The per-component tests (MusicSectionDirtyGate.test.tsx,
// LyricsTab.test.tsx, TestTab.test.tsx) lock each link in the chain in
// isolation. This file locks the chain itself: page state → save → resync.

// Mutable tab state so we can simulate tab switching without a real router.
let currentTab: string = "config";

jest.mock("next/navigation", () => ({
  useRouter: () => ({ replace: jest.fn(), push: jest.fn() }),
  useSearchParams: () => ({
    get: (key: string) => (key === "tab" ? currentTab : null),
  }),
}));

jest.mock("@/hooks/useJobPoller", () => ({
  useJobPoller: () => ({ data: null, polling: false, error: null }),
}));

jest.mock("@/lib/music-api", () => ({
  adminGetMusicTrack: jest.fn(),
  adminUpdateMusicTrack: jest.fn(),
  adminGetAudioUrl: jest.fn(),
  adminReanalyzeMusicTrack: jest.fn(),
  adminArchiveMusicTrack: jest.fn(),
  adminCreateLyricsPreview: jest.fn(),
  adminExtractLyrics: jest.fn(),
  adminGetLyricsPreviewStatus: jest.fn(),
  adminPatchLyricsConfig: jest.fn(),
  adminCreateMusicTestJob: jest.fn(),
  adminGetMusicJobStatus: jest.fn(),
  adminListMusicTestJobs: jest.fn(),
  adminRerenderMusicJob: jest.fn(),
  uploadMusicSlot: jest.fn(),
}));

jest.mock("@/lib/admin-api", () => ({
  adminCreateTemplateFromMusicTrack: jest.fn(),
  adminUpdateTemplateLyricsConfig: jest.fn(),
}));

const baseLyricsConfig: LyricsConfig = {
  enabled: true,
  style: "line",
  pre_roll_s: 0.1,
  post_dwell_s: 1,
  next_line_gap_s: 0.1,
  fade_in_ms: 150,
  fade_out_ms: 250,
  hold_to_next_threshold_ms: 500,
};

function makeTrack(bestStart: number, bestEnd: number): MusicTrackDetail {
  return {
    id: "track-1",
    title: "Beat It",
    artist: "Michael Jackson",
    source_url: "",
    audio_gcs_path: "music/track-1/audio.m4a",
    duration_s: 298,
    beat_count: 580,
    // Dense, uniformly-spaced beats covering 0..298s. The form lets the
    // user retype start/end to any window inside the track, and the Save
    // button is now disabled when countSlotsClient(beats, start, end, n)
    // is 0. The original 3-beat fixture (bestStart+0.5, +1.0, bestEnd-0.5)
    // 0-slotted on any window not centered on those exact beats, blocking
    // Save and breaking this test's dirty-lifecycle assertions. Beats
    // every 0.5s is plenty for any [start, end] window the test types.
    beat_timestamps_s: Array.from({ length: 596 }, (_, i) => (i + 1) * 0.5),
    analysis_status: "ready",
    error_detail: null,
    thumbnail_url: null,
    published_at: null,
    archived_at: null,
    track_config: {
      best_start_s: bestStart,
      best_end_s: bestEnd,
      slot_every_n_beats: 8,
      required_clips_min: 1,
      required_clips_max: 20,
      lyrics_config: baseLyricsConfig,
    },
    best_sections: null,
    section_version: null,
    has_ai_labels: false,
    label_version: null,
    generative_matchable: false,
    created_at: "2026-05-27T10:00:00Z",
    lyrics_status: "ready",
    lyrics_source: "lrclib_synced+whisper",
    lyrics_error_detail: null,
    lyrics_whisper_draft: null,
    lyrics_diagnostic: null,
    lyrics_extraction_version: 0,
    lyrics_extracted_at: "2026-05-27T10:02:00Z",
    section_error_detail: null,
    lyrics_cached: {
      source: "lrclib_synced+whisper",
      language: "en",
      track_title_matched: "Beat It",
      artist_matched: "Michael Jackson",
      genius_url: "",
      confidence: 0.92,
      lines: [
        { text: "intro", start_s: 130.0, end_s: 132.0, words: [] },
      ],
    },
  };
}

describe("AdminMusicTrackPage section-bounds dirty lifecycle", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    currentTab = "config";

    const api = jest.requireMock("@/lib/music-api");
    (api.adminGetMusicTrack as jest.Mock).mockResolvedValue(makeTrack(28, 48));
    (api.adminUpdateMusicTrack as jest.Mock).mockResolvedValue(
      makeTrack(127.2, 141),
    );
    // Defensive: any mock the page or its children may invoke during render
    // needs to return a Promise (jest.fn() default is undefined → `.then`
    // crash). Set safe resolved values for everything in the music-api mock.
    (api.adminGetAudioUrl as jest.Mock).mockResolvedValue(
      "https://example.com/audio.m4a",
    );
    (api.adminReanalyzeMusicTrack as jest.Mock).mockResolvedValue(
      makeTrack(28, 48),
    );
    (api.adminArchiveMusicTrack as jest.Mock).mockResolvedValue(makeTrack(28, 48));
    (api.adminCreateLyricsPreview as jest.Mock).mockResolvedValue({
      job_id: "preview-job",
      status: "queued",
      music_track_id: "track-1",
    });
    (api.adminGetLyricsPreviewStatus as jest.Mock).mockResolvedValue({
      status: "queued",
    });
    (api.adminPatchLyricsConfig as jest.Mock).mockResolvedValue({
      lyrics_config: baseLyricsConfig,
    });
    (api.adminExtractLyrics as jest.Mock).mockResolvedValue({
      lyrics_status: "ready",
    });
    (api.adminCreateMusicTestJob as jest.Mock).mockResolvedValue({
      job_id: "test-job",
      status: "queued",
      music_track_id: "track-1",
    });
    (api.adminGetMusicJobStatus as jest.Mock).mockResolvedValue({
      status: "queued",
    });
    (api.adminListMusicTestJobs as jest.Mock).mockResolvedValue([]);
    (api.adminRerenderMusicJob as jest.Mock).mockResolvedValue({
      job_id: "rerender-job",
      status: "queued",
      music_track_id: "track-1",
    });
    (api.uploadMusicSlot as jest.Mock).mockResolvedValue({
      gcs_path: "music-uploads/test.mp4",
    });

    const adminApi = jest.requireMock("@/lib/admin-api");
    (adminApi.adminCreateTemplateFromMusicTrack as jest.Mock).mockResolvedValue({
      template_id: "tmpl-1",
    });
    (adminApi.adminUpdateTemplateLyricsConfig as jest.Mock).mockResolvedValue({});
  });

  it("tracks dirty state on the Config tab and clears it after Save resyncs the form", async () => {
    render(<AdminMusicTrackPage params={{ id: "track-1" }} />);

    // Initial load completes — the form is synced to the persisted [28, 48].
    // We grep the bestStart input by its display value because that is the
    // observable surface of `syncFormFromTrack(track)`.
    const startInput = await waitFor(() => screen.getByDisplayValue("28"));
    expect(startInput).toBeInTheDocument();
    expect(screen.getByDisplayValue("48")).toBeInTheDocument();

    // Initial state is clean — sectionBoundsDirty = false, no badge.
    expect(screen.queryByText(/Unsaved changes/i)).not.toBeInTheDocument();

    // Simulate the user clicking section #2 (chorus 127.2–141.0). The band
    // click in AudioPlayer calls `onStartChange(s.start_s)` →
    // `setBestStart(s.toString())`. Editing the input directly exercises the
    // same setter without needing to mock the SVG event.
    fireEvent.change(startInput, { target: { value: "127.2" } });
    fireEvent.change(screen.getByDisplayValue("48"), { target: { value: "141" } });

    // sectionBoundsDirty flips true → "Unsaved changes" badge renders.
    expect(screen.getByText(/Unsaved changes/i)).toBeInTheDocument();

    // Save. handleSaveConfig PATCHes the track, then re-syncs the form from
    // the persisted response (the fix for the prior trapped-dirty bug).
    fireEvent.click(screen.getByRole("button", { name: /Save config/i }));

    await waitFor(() => {
      expect(adminUpdateMusicTrack).toHaveBeenCalledWith("track-1", {
        track_config: {
          best_start_s: 127.2,
          best_end_s: 141,
          slot_every_n_beats: 8,
        },
      });
    });

    // Post-Save: form re-synced to persisted [127.2, 141.0] → dirty clears.
    await waitFor(() => {
      expect(screen.queryByText(/Unsaved changes/i)).not.toBeInTheDocument();
    });
    expect(screen.getByDisplayValue("127.2")).toBeInTheDocument();
    expect(screen.getByDisplayValue("141")).toBeInTheDocument();
  });

  it("propagates dirty state to the Lyrics tab preview gate and releases it after Save", async () => {
    const { rerender } = render(
      <AdminMusicTrackPage params={{ id: "track-1" }} />,
    );

    // Initial load on Config tab. Form synced to [28, 48].
    await waitFor(() => screen.getByDisplayValue("28"));

    // Switch to Lyrics tab. Re-render forces the page to re-read the mocked
    // useSearchParams() and pick up the new tab.
    currentTab = "lyrics";
    rerender(<AdminMusicTrackPage params={{ id: "track-1" }} />);

    // Clean state: preview button enabled, no hint.
    await waitFor(() => {
      expect(screen.getByTestId("lyrics-timing-preview-button")).not.toBeDisabled();
    });
    expect(
      screen.queryByTestId("lyrics-timing-preview-hint"),
    ).not.toBeInTheDocument();

    // Go back to Config and dirty the form.
    currentTab = "config";
    rerender(<AdminMusicTrackPage params={{ id: "track-1" }} />);
    const startInput = await waitFor(() => screen.getByDisplayValue("28"));
    fireEvent.change(startInput, { target: { value: "127.2" } });

    // Cross-tab gate check: switching to Lyrics with the page still holding
    // dirty form state must disable the preview button AND show the hint.
    // This is the test the per-component MusicSectionDirtyGate test cannot
    // cover — it proves the page wires the prop through correctly.
    currentTab = "lyrics";
    rerender(<AdminMusicTrackPage params={{ id: "track-1" }} />);

    await waitFor(() => {
      expect(screen.getByTestId("lyrics-timing-preview-button")).toBeDisabled();
    });
    expect(
      screen.getByTestId("lyrics-timing-preview-hint"),
    ).toHaveTextContent(/Save section bounds/i);

    // Switch back to Config and Save.
    currentTab = "config";
    rerender(<AdminMusicTrackPage params={{ id: "track-1" }} />);
    fireEvent.change(screen.getByDisplayValue("48"), { target: { value: "141" } });
    fireEvent.click(screen.getByRole("button", { name: /Save config/i }));

    await waitFor(() => {
      expect(adminUpdateMusicTrack).toHaveBeenCalled();
    });
    await waitFor(() => {
      expect(screen.queryByText(/Unsaved changes/i)).not.toBeInTheDocument();
    });

    // Back to Lyrics — preview button must be enabled again now that the
    // page's bestStart/bestEnd were resynced to the persisted response.
    currentTab = "lyrics";
    rerender(<AdminMusicTrackPage params={{ id: "track-1" }} />);
    await waitFor(() => {
      expect(screen.getByTestId("lyrics-timing-preview-button")).not.toBeDisabled();
    });
    expect(
      screen.queryByTestId("lyrics-timing-preview-hint"),
    ).not.toBeInTheDocument();
  });
});
