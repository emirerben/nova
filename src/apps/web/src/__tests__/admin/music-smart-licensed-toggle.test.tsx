// @ts-nocheck
/**
 * /admin/music/[id] Config tab — "Licensed for Smart Captions music bed"
 * toggle (review D3). The backend gate
 * `track_config.smart_captions_licensed is True` decides eligibility for the
 * v2 auto-selected music bed; this toggle is its only write surface.
 *
 *   - Unchecked by default when the track has no licensing flag
 *   - Checking it flips the Unsaved-changes badge on
 *   - Save PATCHes smart_captions_licensed: true through track_config
 */
import "@testing-library/jest-dom";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { act } from "react";

import AdminMusicTrackPage from "@/app/admin/music/[id]/page";
import * as musicApi from "@/lib/music-api";

jest.mock("next/link", () => {
  function MockLink({
    children,
    href,
    ...rest
  }: {
    children: React.ReactNode;
    href: string;
  }) {
    return (
      <a href={href} {...rest}>
        {children}
      </a>
    );
  }
  MockLink.displayName = "MockLink";
  return { __esModule: true, default: MockLink };
});

const replaceMock = jest.fn();
jest.mock("next/navigation", () => ({
  useRouter: () => ({ replace: replaceMock, push: jest.fn() }),
  useSearchParams: () => new URLSearchParams(""),
}));

jest.mock("@/lib/music-api", () => ({
  __esModule: true,
  adminGetMusicTrack: jest.fn(),
  adminGetAudioUrl: jest.fn(),
  adminUpdateMusicTrack: jest.fn(),
  adminReanalyzeMusicTrack: jest.fn(),
  adminArchiveMusicTrack: jest.fn(),
}));

jest.mock("@/lib/admin-api", () => ({
  __esModule: true,
  adminCreateTemplateFromMusicTrack: jest.fn(),
}));

jest.mock("@/app/admin/_shared/LyricsConfigPanel", () => ({
  __esModule: true,
  default: () => <div data-testid="stub-lyrics-panel" />,
}));

const get = musicApi.adminGetMusicTrack as jest.MockedFunction<
  typeof musicApi.adminGetMusicTrack
>;
const update = musicApi.adminUpdateMusicTrack as jest.MockedFunction<
  typeof musicApi.adminUpdateMusicTrack
>;
const audioUrl = musicApi.adminGetAudioUrl as jest.MockedFunction<
  typeof musicApi.adminGetAudioUrl
>;

const BEATS = Array.from({ length: 62 }, (_, i) => (i * 200) / 62);

function makeTrack(
  overrides: Partial<musicApi.MusicTrackDetail> = {},
): musicApi.MusicTrackDetail {
  return {
    id: "t1",
    title: "Licensed fixture",
    artist: "Test Artist",
    source_url: "upload://licensed.mp3",
    audio_gcs_path: "music/licensed.mp3",
    duration_s: 200,
    beat_count: BEATS.length,
    beat_timestamps_s: BEATS,
    analysis_status: "ready",
    error_detail: null,
    thumbnail_url: null,
    published_at: null,
    archived_at: null,
    // N=4 so the slot preview is >0 and Save stays enabled.
    track_config: {
      best_start_s: 56.2,
      best_end_s: 73.4,
      slot_every_n_beats: 4,
    },
    lyrics_status: "none",
    lyrics_source: null,
    lyrics_error_detail: null,
    lyrics_cached: null,
    lyrics_extracted_at: null,
    best_sections: null,
    section_version: null,
    label_version: "2026-05-15",
    has_ai_labels: true,
    generative_matchable: true,
    created_at: "2026-05-01T00:00:00Z",
    ...overrides,
  } as musicApi.MusicTrackDetail;
}

beforeEach(() => {
  get.mockReset();
  update.mockReset();
  audioUrl.mockReset();
  replaceMock.mockReset();
  audioUrl.mockResolvedValue("https://storage.example.com/audio.mp3");
});

async function renderPage() {
  await act(async () => {
    render(<AdminMusicTrackPage params={{ id: "t1" }} />);
  });
  await screen.findByTestId("smart-licensed-checkbox");
}

describe("Admin Config — Smart Captions licensing toggle", () => {
  test("unchecked by default; checking marks the form dirty", async () => {
    get.mockResolvedValue(makeTrack());
    await renderPage();

    const checkbox = screen.getByTestId("smart-licensed-checkbox");
    expect(checkbox).not.toBeChecked();
    expect(screen.queryByText(/Unsaved changes/i)).not.toBeInTheDocument();

    fireEvent.click(checkbox);

    expect(checkbox).toBeChecked();
    expect(screen.getByText(/Unsaved changes/i)).toBeInTheDocument();
  });

  test("reflects a persisted licensed=true flag", async () => {
    get.mockResolvedValue(
      makeTrack({
        track_config: {
          best_start_s: 56.2,
          best_end_s: 73.4,
          slot_every_n_beats: 4,
          smart_captions_licensed: true,
        },
      }),
    );
    await renderPage();

    expect(screen.getByTestId("smart-licensed-checkbox")).toBeChecked();
    expect(screen.queryByText(/Unsaved changes/i)).not.toBeInTheDocument();
  });

  test("Save PATCHes smart_captions_licensed through track_config", async () => {
    const track = makeTrack();
    get.mockResolvedValue(track);
    update.mockResolvedValue(
      makeTrack({
        track_config: { ...track.track_config, smart_captions_licensed: true },
      }),
    );
    await renderPage();

    fireEvent.click(screen.getByTestId("smart-licensed-checkbox"));
    await act(async () => {
      fireEvent.click(screen.getByTestId("save-config-btn"));
    });

    await waitFor(() => expect(update).toHaveBeenCalledTimes(1));
    expect(update.mock.calls[0][1].track_config.smart_captions_licensed).toBe(
      true,
    );
    // Saved response re-syncs the form — dirty badge clears.
    expect(screen.queryByText(/Unsaved changes/i)).not.toBeInTheDocument();
  });
});
