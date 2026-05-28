import { render, screen, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom";

import AdminMusicTrackPage from "@/app/admin/music/[id]/page";
import type { MusicTrackDetail } from "@/lib/music-api";

// Pins the "Last attempt failed: ..." block that surfaces under the amber
// "no agent sections" tag when MusicTrack.section_error_detail is populated.
// This is the load-bearing observability fix for the silent-fail branch of
// _run_song_sections — without it, the admin had no way to tell whether the
// agent never ran, transiently errored, or hit a persistent schema drift.
//
// Two cases the page distinguishes:
//   1. section_version null + section_error_detail null  →  "agent has not
//      run yet" placeholder ONLY (no error block).
//   2. section_version null + section_error_detail set   →  placeholder AND
//      a muted monospace "Last attempt failed: ..." block carrying the reason.

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

function makeUnsectionedTrack(
  sectionError: string | null,
): MusicTrackDetail {
  return {
    id: "track-1",
    title: "Overnight",
    artist: "Parcels",
    source_url: "",
    audio_gcs_path: "music/track-1/audio.m4a",
    duration_s: 219.6,
    beat_count: 523,
    beat_timestamps_s: [0.5, 1.0, 1.5, 2.0, 2.5, 3.0],
    analysis_status: "ready",
    error_detail: null,
    thumbnail_url: null,
    published_at: null,
    archived_at: null,
    track_config: {
      best_start_s: 107.449,
      best_end_s: 152.449,
      slot_every_n_beats: 8,
      required_clips_min: 8,
      required_clips_max: 17,
    },
    best_sections: null,
    section_version: null,
    section_error_detail: sectionError,
    has_ai_labels: true,
    label_version: "2026-05-15",
    generative_matchable: false,
    created_at: "2026-05-28T10:00:00Z",
    lyrics_status: "ready",
    lyrics_source: "lrclib_synced+whisper",
    lyrics_error_detail: null,
    lyrics_extracted_at: "2026-05-28T10:02:00Z",
    lyrics_cached: null,
  };
}

function bootstrapMocks(track: MusicTrackDetail): void {
  const api = jest.requireMock("@/lib/music-api");
  (api.adminGetMusicTrack as jest.Mock).mockResolvedValue(track);
  // Defensive: every mock the page or its children may invoke during render
  // needs to resolve. The page renders several children eagerly; an
  // unresolved Promise on, say, audio-url fetch can mask the assertion
  // we actually care about behind a React act() warning.
  (api.adminGetAudioUrl as jest.Mock).mockResolvedValue(
    "https://example.com/audio.m4a",
  );
  (api.adminUpdateMusicTrack as jest.Mock).mockResolvedValue(track);
  (api.adminReanalyzeMusicTrack as jest.Mock).mockResolvedValue(track);
  (api.adminArchiveMusicTrack as jest.Mock).mockResolvedValue(track);
  (api.adminPatchLyricsConfig as jest.Mock).mockResolvedValue({
    lyrics_config: track.track_config?.lyrics_config ?? null,
  });
  (api.adminCreateLyricsPreview as jest.Mock).mockResolvedValue({
    job_id: "preview-job",
    status: "queued",
    music_track_id: track.id,
  });
  (api.adminGetLyricsPreviewStatus as jest.Mock).mockResolvedValue({
    status: "queued",
  });
  (api.adminExtractLyrics as jest.Mock).mockResolvedValue({
    lyrics_status: "ready",
  });
  (api.adminCreateMusicTestJob as jest.Mock).mockResolvedValue({
    job_id: "test-job",
    status: "queued",
    music_track_id: track.id,
  });
  (api.adminGetMusicJobStatus as jest.Mock).mockResolvedValue({
    status: "queued",
  });
  (api.adminListMusicTestJobs as jest.Mock).mockResolvedValue([]);
  (api.adminRerenderMusicJob as jest.Mock).mockResolvedValue({
    job_id: "rerender-job",
    status: "queued",
    music_track_id: track.id,
  });
  (api.uploadMusicSlot as jest.Mock).mockResolvedValue({
    gcs_path: "music-uploads/test.mp4",
  });
}

describe("AdminMusicTrackPage section_error_detail render", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    currentTab = "config";
  });

  it("renders the 'Last attempt failed' block when section_error_detail is set", async () => {
    const reason =
      "song_sections: invalid JSON — Expecting value: line 1 column 1 (char 0)";
    bootstrapMocks(makeUnsectionedTrack(reason));

    render(<AdminMusicTrackPage params={{ id: "track-1" }} />);

    // The existing placeholder ALWAYS renders when sections are empty,
    // regardless of error_detail. Wait for it so we know hydration is done.
    await waitFor(() =>
      expect(
        screen.getByText(/The agent has not picked any sections/i),
      ).toBeInTheDocument(),
    );

    // The new error-reason block.
    expect(screen.getByText(/Last attempt failed:/i)).toBeInTheDocument();
    expect(screen.getByText(reason)).toBeInTheDocument();
  });

  it("hides the 'Last attempt failed' block when section_error_detail is null", async () => {
    bootstrapMocks(makeUnsectionedTrack(null));

    render(<AdminMusicTrackPage params={{ id: "track-1" }} />);

    await waitFor(() =>
      expect(
        screen.getByText(/The agent has not picked any sections/i),
      ).toBeInTheDocument(),
    );

    // Placeholder renders but the error block does NOT. This distinguishes
    // "agent has not run yet" (NULL error) from "agent ran but failed"
    // (non-NULL error). Without this gate, every unsectioned track would
    // permanently show "Last attempt failed:" with no failure to describe.
    expect(screen.queryByText(/Last attempt failed:/i)).not.toBeInTheDocument();
  });

  it("hides both placeholder AND error block when sections are populated, even if section_error_detail is stale", async () => {
    // Defense against a stale-row invariant violation: backfill_song_sections.py
    // currently does NOT clear section_error_detail on a successful backfill,
    // so a row can end up with valid sections + a stale error reason. The
    // outer render gate (!best_sections || empty) hides both blocks in that
    // case — this test pins that intent so a future refactor of the gate
    // (e.g. dropping the outer guard to "always show error if present")
    // doesn't silently surface stale text on healthy tracks.
    const track = makeUnsectionedTrack("stale reason from prior failure");
    track.best_sections = [
      {
        rank: 1,
        start_s: 30,
        end_s: 48,
        label: "chorus",
        energy: "high",
        suggested_use: "hook",
        rationale: "peak energy chorus.",
      },
    ];
    track.section_version = "2026-05-22";
    bootstrapMocks(track);

    render(<AdminMusicTrackPage params={{ id: "track-1" }} />);

    // Wait for the page to hydrate — the audio block (sections v2026-05-22)
    // renders when sections are populated; use that as the hydration anchor.
    await waitFor(() =>
      expect(screen.getByText(/sections v2026-05-22/i)).toBeInTheDocument(),
    );

    // Outer guard hides BOTH the placeholder and the error reason.
    expect(
      screen.queryByText(/The agent has not picked any sections/i),
    ).not.toBeInTheDocument();
    expect(screen.queryByText(/Last attempt failed:/i)).not.toBeInTheDocument();
  });
});
