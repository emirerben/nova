// @ts-nocheck
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom";

import { TestTab } from "@/app/admin/music/[id]/components/TestTab";
import {
  adminCreateLyricsPreview,
  adminCreateMusicTestJob,
  adminGetLyricsPreviewStatus,
  adminGetMusicJobStatus,
  adminListMusicTestJobs,
  adminRerenderMusicJob,
  uploadMusicSlot,
} from "@/lib/music-api";

jest.mock("@/hooks/useJobPoller", () => ({
  useJobPoller: () => ({ data: null, polling: false, error: null }),
}));

jest.mock("@/lib/music-api", () => ({
  adminCreateLyricsPreview: jest.fn(),
  adminCreateMusicTestJob: jest.fn(),
  adminGetLyricsPreviewStatus: jest.fn(),
  adminGetMusicJobStatus: jest.fn(),
  adminListMusicTestJobs: jest.fn(),
  adminRerenderMusicJob: jest.fn(),
  uploadMusicSlot: jest.fn(),
}));

const mockListJobs = adminListMusicTestJobs as jest.MockedFunction<
  typeof adminListMusicTestJobs
>;
const mockRerender = adminRerenderMusicJob as jest.MockedFunction<
  typeof adminRerenderMusicJob
>;

const readyLineTrack = {
  analysis_status: "ready",
  track_config: {
    required_clips_min: 1,
    required_clips_max: 20,
    lyrics_config: {
      enabled: true,
      style: "line",
      pre_roll_s: 0.1,
      post_dwell_s: 1,
      next_line_gap_s: 0.1,
      fade_in_ms: 150,
      fade_out_ms: 250,
      hold_to_next_threshold_ms: 500,
    },
  },
};

describe("TestTab", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockListJobs.mockResolvedValue([
      {
        job_id: "11111111-1111-4111-8111-111111111111",
        status: "music_ready",
        error_detail: null,
        output_url: "https://storage.example.com/output.mp4",
        clip_count: 10,
        created_at: "2026-05-23T18:00:00Z",
        updated_at: "2026-05-23T18:05:00Z",
      },
    ]);
    mockRerender.mockResolvedValue({
      job_id: "22222222-2222-4222-8222-222222222222",
      status: "queued",
      music_track_id: "track-1",
    });
    (adminCreateLyricsPreview as jest.Mock).mockResolvedValue({
      job_id: "lyrics-preview-job",
      status: "queued",
      music_track_id: "track-1",
    });
    (adminCreateMusicTestJob as jest.Mock).mockResolvedValue({
      job_id: "full-test-job",
      status: "queued",
      music_track_id: "track-1",
    });
    (adminGetLyricsPreviewStatus as jest.Mock).mockResolvedValue({ status: "queued" });
    (adminGetMusicJobStatus as jest.Mock).mockResolvedValue({ status: "queued" });
    (uploadMusicSlot as jest.Mock).mockResolvedValue({
      file_name: "clip.mp4",
      gcs_path: "music-uploads/track-1/batch/clip.mp4",
      kind: "video",
    });
  });

  it("passes the current lyric timing snapshot when re-rendering prior clips", async () => {
    render(<TestTab trackId="track-1" track={readyLineTrack} />);

    const rerenderButton = await screen.findByTitle(
      "Re-render using this job's clips against the current track config",
    );

    fireEvent.change(screen.getByLabelText("Post-dwell"), {
      target: { value: "2" },
    });
    fireEvent.change(screen.getByLabelText("Next-line gap"), {
      target: { value: "0.2" },
    });
    await waitFor(() => {
      expect(
        screen.getByText("Rendering with unsaved lyric timing overrides."),
      ).toBeInTheDocument();
    });

    fireEvent.click(rerenderButton);

    await waitFor(() => {
      expect(mockRerender).toHaveBeenCalledWith(
        "track-1",
        "11111111-1111-4111-8111-111111111111",
        {
          pre_roll_s: 0.1,
          post_dwell_s: 2,
          next_line_gap_s: 0.2,
          fade_in_ms: 150,
          fade_out_ms: 250,
          hold_to_next_threshold_ms: 500,
        },
      );
    });
  });
});
