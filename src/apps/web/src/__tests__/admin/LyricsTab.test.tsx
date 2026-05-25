// @ts-nocheck
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom";

import { LyricsTab } from "@/app/admin/music/[id]/components/LyricsTab";
import {
  adminCreateLyricsPreview,
  adminExtractLyrics,
  adminGetLyricsPreviewStatus,
  adminGetMusicTrack,
  adminPatchLyricsConfig,
  adminUpdateMusicTrack,
} from "@/lib/music-api";

// The composed panels and the polling hook each have their own coverage. This
// test sticks to LyricsTab's own contract: header copy, gating, routing
// "Preview lyrics" through adminCreateLyricsPreview, and the render branch
// (status pill, <video>, legacy-output banner, polling error).
//
// `mockPollerState` is mutated per-test to drive the poller into specific
// states without needing to wait on a real interval. Reset between tests.
let mockPollerState: { data: unknown; polling: boolean; error: string | null } = {
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

const baseLineConfig = {
  enabled: true,
  style: "line",
  pre_roll_s: 0.1,
  post_dwell_s: 1,
  next_line_gap_s: 0.1,
  fade_in_ms: 150,
  fade_out_ms: 250,
  hold_to_next_threshold_ms: 500,
};

function makeTrack(overrides: Record<string, unknown> = {}) {
  return {
    id: "track-1",
    title: "Drown",
    artist: "BMTH",
    source_url: "",
    audio_gcs_path: "music/track-1/audio.m4a",
    duration_s: 185,
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
    },
    best_sections: null,
    section_version: null,
    created_at: "2026-05-25T10:00:00Z",
    lyrics_status: "ready",
    lyrics_source: "lrclib_synced+whisper",
    lyrics_error_detail: null,
    lyrics_extracted_at: "2026-05-25T10:02:00Z",
    lyrics_cached: {
      language: "en",
      confidence: 0.92,
      lines: [
        { text: "Save me from the nothing I've become", start_s: 12.4, end_s: 15.2 },
        { text: "I've been here too long living in the wreckage", start_s: 15.3, end_s: 19.8 },
      ],
    },
    ...overrides,
  };
}

describe("LyricsTab", () => {
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

  it("anchors the workflow as Line Lyric Templates", () => {
    render(
      <LyricsTab trackId="track-1" track={makeTrack()} onTrackUpdated={jest.fn()} />,
    );
    expect(
      screen.getByRole("heading", { name: /Line Lyric Templates/i }),
    ).toBeInTheDocument();
    // The copy must surface 20s + black background so the workflow's
    // constraints are obvious from the dashboard alone.
    expect(screen.getByText(/20s black background/i)).toBeInTheDocument();
  });

  it("routes the Preview button through adminCreateLyricsPreview with the live timing override", async () => {
    render(
      <LyricsTab trackId="track-1" track={makeTrack()} onTrackUpdated={jest.fn()} />,
    );

    // Mutate a timing field so the snapshot we expect to be sent is not the
    // default and can't accidentally pass.
    fireEvent.change(screen.getByLabelText("Post-dwell"), {
      target: { value: "1.5" },
    });

    fireEvent.click(screen.getByRole("button", { name: /Preview lyrics only/i }));

    await waitFor(() => {
      expect(adminCreateLyricsPreview).toHaveBeenCalledWith(
        "track-1",
        expect.objectContaining({
          pre_roll_s: 0.1,
          post_dwell_s: 1.5,
          next_line_gap_s: 0.1,
          fade_in_ms: 150,
          fade_out_ms: 250,
          hold_to_next_threshold_ms: 500,
        }),
      );
    });
  });

  it("disables the full-render button and points the admin to the Test tab", () => {
    render(
      <LyricsTab trackId="track-1" track={makeTrack()} onTrackUpdated={jest.fn()} />,
    );
    const fullTestButton = screen.getByRole("button", { name: /Render full test job/i });
    expect(fullTestButton).toBeDisabled();
    expect(screen.getByText(/Open the Test tab/i)).toBeInTheDocument();
  });

  // Rev3 fix: when LyricsConfigPanel saves, it calls onTrackUpdated which
  // bubbles up to a page-level setTrack. The new track prop must re-sync the
  // LyricsTimingPanel's "saved" baseline; otherwise the timing panel keeps
  // showing the original values and reports "Rendering with unsaved overrides"
  // forever. Mirrors the TestTab.tsx:91-93 pattern.
  it("re-syncs savedLyricsConfig when track.track_config.lyrics_config changes", () => {
    const updatedConfig = {
      enabled: true,
      style: "line",
      pre_roll_s: 0.2,
      post_dwell_s: 1.75,
      next_line_gap_s: 0.15,
      fade_in_ms: 175,
      fade_out_ms: 275,
      hold_to_next_threshold_ms: 600,
    };

    const { rerender } = render(
      <LyricsTab trackId="track-1" track={makeTrack()} onTrackUpdated={jest.fn()} />,
    );
    // Baseline: panel shows the original 1.0 post-dwell value.
    expect(screen.getByLabelText("Post-dwell")).toHaveValue(1);

    // Simulate the page-level setTrack flushing an updated track prop after
    // LyricsConfigPanel.onTrackUpdated fired with new lyrics_config values.
    rerender(
      <LyricsTab
        trackId="track-1"
        track={makeTrack({
          track_config: { required_clips_min: 1, required_clips_max: 20, lyrics_config: updatedConfig },
        })}
        onTrackUpdated={jest.fn()}
      />,
    );

    // The timing panel must now reflect the new saved baseline.
    expect(screen.getByLabelText("Post-dwell")).toHaveValue(1.75);
    expect(screen.getByLabelText("Fade in")).toHaveValue(175);
    // And there should NOT be an "unsaved overrides" warning (the working
    // state matches the new saved baseline after the re-sync).
    expect(
      screen.queryByText(/Rendering with unsaved lyric timing overrides/i),
    ).not.toBeInTheDocument();
  });

  it("explains when the track is not analysis-ready", () => {
    render(
      <LyricsTab
        trackId="track-1"
        track={makeTrack({ analysis_status: "analyzing" })}
        onTrackUpdated={jest.fn()}
      />,
    );
    expect(screen.getByText(/not ready yet/i)).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /Preview lyrics only/i }),
    ).not.toBeInTheDocument();
  });

  it("explains when there are no cached lyrics yet and hides the timing panel", () => {
    render(
      <LyricsTab
        trackId="track-1"
        track={makeTrack({
          lyrics_status: "pending",
          lyrics_cached: null,
        })}
        onTrackUpdated={jest.fn()}
      />,
    );
    expect(screen.getByText(/No cached lyrics yet/i)).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /Preview lyrics only/i }),
    ).not.toBeInTheDocument();
  });

  // T7: lyrics_status="ready" but cached.lines=[] is a real state — extraction
  // finished but produced no lines (e.g., LRCLIB miss + Whisper found vocals
  // but the aligner could not segment them). The dashboard should treat this
  // exactly like "no cached lyrics yet" so the admin gets a clear explanation
  // instead of a broken timing panel.
  it("treats ready-status but empty-lines as not-ready", () => {
    render(
      <LyricsTab
        trackId="track-1"
        track={makeTrack({
          lyrics_cached: { language: "en", confidence: 1, lines: [] },
        })}
        onTrackUpdated={jest.fn()}
      />,
    );
    expect(screen.getByText(/No cached lyrics yet/i)).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /Preview lyrics only/i }),
    ).not.toBeInTheDocument();
  });

  // T6: when adminCreateLyricsPreview rejects, the failure path surfaces the
  // error message into a red banner. This is the only UX channel admins have
  // for preview-submit failures, so a regression that swallows the catch
  // would be invisible without this test.
  it("surfaces submit errors in a banner when adminCreateLyricsPreview rejects", async () => {
    (adminCreateLyricsPreview as jest.Mock).mockRejectedValue(
      new Error("boom: 500 from API"),
    );
    render(
      <LyricsTab trackId="track-1" track={makeTrack()} onTrackUpdated={jest.fn()} />,
    );
    fireEvent.click(screen.getByRole("button", { name: /Preview lyrics only/i }));
    await waitFor(() =>
      expect(screen.getByText(/boom: 500 from API/)).toBeInTheDocument(),
    );
  });

  // T2: success branch — poller returns music_ready with a signed https URL,
  // the dashboard renders the <video> element with that URL. Locks the
  // resolveMusicJobOutputUrl integration end-to-end through the render path.
  it("renders <video> when the preview job lands with an https output_url", async () => {
    mockPollerState = {
      data: {
        job_id: "lp-1",
        status: "music_ready",
        output_url: "https://storage.example.com/preview.mp4",
        error_detail: null,
      },
      polling: false,
      error: null,
    };
    render(
      <LyricsTab trackId="track-1" track={makeTrack()} onTrackUpdated={jest.fn()} />,
    );
    fireEvent.click(screen.getByRole("button", { name: /Preview lyrics only/i }));

    await waitFor(() => {
      const video = document.querySelector("video");
      expect(video).not.toBeNull();
      expect(video).toHaveAttribute("src", "https://storage.example.com/preview.mp4");
    });
  });

  // T3: caption — when the preview job carries the resolved audio window
  // (preview_start_s + preview_duration_s), the dashboard renders a
  // "Previewing m:ss – m:ss" line above the <video>. Without it, the
  // auto-anchor change is silent: an admin previewing Billie Jean would hear
  // the song's body and think the wrong track was loaded.
  it("renders the Previewing m:ss – m:ss caption when the window is present", async () => {
    mockPollerState = {
      data: {
        job_id: "lp-1",
        status: "music_ready",
        output_url: "https://storage.example.com/preview.mp4",
        error_detail: null,
        // Billie Jean's empirical window: anchor 28.80s + 20s.
        preview_start_s: 28.80,
        preview_duration_s: 20.0,
      },
      polling: false,
      error: null,
    };
    render(
      <LyricsTab trackId="track-1" track={makeTrack()} onTrackUpdated={jest.fn()} />,
    );
    fireEvent.click(screen.getByRole("button", { name: /Preview lyrics only/i }));

    await waitFor(() => {
      expect(screen.getByText(/Previewing 0:28 – 0:48/)).toBeInTheDocument();
    });
  });

  // T3 (continued): early-vocal track — first lyric < LEAD_IN_S means anchor
  // resolves to exactly 0. The caption must render `Previewing 0:00 – 0:20`,
  // NOT be silently dropped. A regression that swapped the explicit `!== null`
  // guard for a truthy check (`if (currentJob.preview_start_s && ...)`) would
  // drop the caption for every early-vocal song and tests would still pass —
  // this case locks the null vs falsy distinction.
  it("renders the caption when preview_start_s is exactly 0 (early-vocal track)", async () => {
    mockPollerState = {
      data: {
        job_id: "lp-1",
        status: "music_ready",
        output_url: "https://storage.example.com/preview.mp4",
        error_detail: null,
        preview_start_s: 0,
        preview_duration_s: 20.0,
      },
      polling: false,
      error: null,
    };
    render(
      <LyricsTab trackId="track-1" track={makeTrack()} onTrackUpdated={jest.fn()} />,
    );
    fireEvent.click(screen.getByRole("button", { name: /Preview lyrics only/i }));

    await waitFor(() => {
      expect(screen.getByText(/Previewing 0:00 – 0:20/)).toBeInTheDocument();
    });
  });

  // T3 (continued): legacy rows (rendered before the auto-anchor PR) have
  // null preview_start_s / preview_duration_s. The caption must be omitted
  // rather than render "Previewing NaN:NaN".
  it("omits the caption when the preview job lacks the window fields", async () => {
    mockPollerState = {
      data: {
        job_id: "lp-1",
        status: "music_ready",
        output_url: "https://storage.example.com/preview.mp4",
        error_detail: null,
        preview_start_s: null,
        preview_duration_s: null,
      },
      polling: false,
      error: null,
    };
    render(
      <LyricsTab trackId="track-1" track={makeTrack()} onTrackUpdated={jest.fn()} />,
    );
    fireEvent.click(screen.getByRole("button", { name: /Preview lyrics only/i }));

    // Wait for the video so we know the body has rendered, then assert no
    // caption text appeared.
    await waitFor(() => {
      expect(document.querySelector("video")).not.toBeNull();
    });
    expect(screen.queryByText(/Previewing/)).toBeNull();
  });

  // T2 (continued): legacy-row branch — the music orchestrator used to stash
  // a relative GCS path on assembly_plan.output_url instead of a signed URL.
  // resolveMusicJobOutputUrl rejects that and the dashboard shows a "legacy
  // format" banner instead of a broken <video src> that 404s.
  it("shows the legacy-format banner when output_url is a relative GCS path", async () => {
    mockPollerState = {
      data: {
        job_id: "lp-1",
        status: "music_ready",
        output_url: "music-lyrics-previews/track-1/preview.mp4",
        error_detail: null,
      },
      polling: false,
      error: null,
    };
    render(
      <LyricsTab trackId="track-1" track={makeTrack()} onTrackUpdated={jest.fn()} />,
    );
    fireEvent.click(screen.getByRole("button", { name: /Preview lyrics only/i }));

    await waitFor(() => {
      expect(screen.getByText(/legacy format/i)).toBeInTheDocument();
    });
    expect(document.querySelector("video")).toBeNull();
  });

  // T2 (continued): failure branch — when the preview job fails, the
  // error_detail string must surface to the admin (it carries the FFmpeg
  // stderr tail from lyrics_preview_task).
  it("surfaces error_detail when the preview job fails", async () => {
    mockPollerState = {
      data: {
        job_id: "lp-1",
        status: "processing_failed",
        output_url: null,
        error_detail: "ffmpeg rc=1: libass missing font",
      },
      polling: false,
      error: null,
    };
    render(
      <LyricsTab trackId="track-1" track={makeTrack()} onTrackUpdated={jest.fn()} />,
    );
    fireEvent.click(screen.getByRole("button", { name: /Preview lyrics only/i }));

    await waitFor(() => {
      expect(screen.getByText(/libass missing font/)).toBeInTheDocument();
    });
  });
});
