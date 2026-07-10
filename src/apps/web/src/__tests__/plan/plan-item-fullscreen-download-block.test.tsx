/**
 * Plan 009 T4 — page-level wiring for fullscreen cutaways in the live preview
 * (plan/items/[id]/page.tsx). Modeled on plan-item-live-preview.test.tsx.
 *
 * Covers:
 *  - a fullscreen card renders inset full-frame + object-cover through the
 *    page's hero (Hero mounts LiveOverlayCardsLayer with the shared style /
 *    class utils).
 *  - asset load failure lifts to page state: the failed tile renders and the
 *    Download button's overlay-bake path is BLOCKED (no setVariantMediaOverlays
 *    render:true dispatch) with the inline copy
 *    "1 visual couldn't load — refresh or remove it."
 *  - the tile's Remove button clears the card and unblocks Download (which
 *    then serves the burned output directly — no overlay bake needed).
 */

// @ts-nocheck

import React from "react";

Object.defineProperty(window, "matchMedia", {
  writable: true,
  value: jest.fn().mockImplementation((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: jest.fn(),
    removeListener: jest.fn(),
    addEventListener: jest.fn(),
    removeEventListener: jest.fn(),
    dispatchEvent: jest.fn(),
  })),
});

class ResizeObserverMock {
  observe() {}
  unobserve() {}
  disconnect() {}
}
(global as unknown as { ResizeObserver: typeof ResizeObserverMock }).ResizeObserver =
  ResizeObserverMock;

beforeAll(() => {
  window.HTMLMediaElement.prototype.load = jest.fn();
  window.HTMLMediaElement.prototype.pause = jest.fn();
  window.HTMLMediaElement.prototype.play = jest.fn().mockResolvedValue(undefined);
});

import { act, fireEvent, render, screen } from "@testing-library/react";
import "@testing-library/jest-dom";

jest.mock("next/navigation", () => ({
  useParams: jest.fn(() => ({ id: "test-item-id" })),
  useSearchParams: jest.fn(() => new URLSearchParams()),
}));

const mockRefetch = jest.fn();
jest.mock("@/hooks/usePolledJobStatus", () => ({
  usePolledJobStatus: jest.fn(),
}));
import { usePolledJobStatus } from "@/hooks/usePolledJobStatus";
const mockUsePolledJobStatus = usePolledJobStatus as jest.MockedFunction<
  typeof usePolledJobStatus
>;

const mockSetVariantMediaOverlays = jest.fn().mockResolvedValue({});
const mockRenderVariantSfx = jest.fn().mockResolvedValue({});
const mockSetVariantSoundEffects = jest.fn().mockResolvedValue({});
jest.mock("@/lib/plan-api", () => ({
  ...jest.requireActual("@/lib/plan-api"),
  getPlanItem: jest.fn(),
  getPlanItemJobStatus: jest.fn(),
  requestUploadUrls: jest.fn(),
  attachClips: jest.fn(),
  generatePlanItem: jest.fn(),
  swapPlanItemSong: jest.fn(),
  retextPlanItem: jest.fn(),
  uploadToGcs: jest.fn(),
  listPoolAssets: jest.fn().mockResolvedValue([]),
  getSfxAudioUrl: jest.fn().mockResolvedValue("https://signed/sfx.mp3"),
  setVariantMediaOverlays: (...a: unknown[]) => mockSetVariantMediaOverlays(...a),
  renderVariantSfx: (...a: unknown[]) => mockRenderVariantSfx(...a),
  setVariantSoundEffects: (...a: unknown[]) => mockSetVariantSoundEffects(...a),
  NotAuthenticatedError: class NotAuthenticatedError extends Error {},
}));

jest.mock("@/lib/sfx-api", () => ({
  getSoundEffects: jest.fn().mockResolvedValue([]),
}));

jest.mock("@/lib/generative-api", () => ({
  ...jest.requireActual("@/lib/generative-api"),
  getGenerativeStyleSets: jest.fn().mockResolvedValue([]),
  getTimeline: jest.fn(() => new Promise(() => {})),
  TimelineApiError: class TimelineApiError extends Error {
    status = 0;
    code: string | null = null;
  },
  GENERATIVE_TERMINAL_STATUSES: [
    "variants_ready",
    "variants_ready_partial",
    "variants_failed",
    "processing_failed",
  ],
}));

jest.mock("@/lib/music-api", () => ({
  getMusicTracks: jest.fn().mockResolvedValue({ tracks: [] }),
}));

jest.mock("@/lib/font-faces", () => ({ FONT_FACES: "" }));
const mockDownloadVideo = jest.fn();
jest.mock("@/lib/download-video", () => ({
  downloadVideo: (...a: unknown[]) => mockDownloadVideo(...a),
}));
jest.mock("@/lib/plan-text", () => ({ stripRationalePrefix: (s: string) => s }));
jest.mock("@/components/ui/LightShell", () => ({
  LightShell: ({ children }: { children: React.ReactNode }) => (
    <div data-testid="light-shell">{children}</div>
  ),
}));
jest.mock("@/app/plan/_components/SignInPrompt", () => ({
  __esModule: true,
  default: () => <div data-testid="sign-in-prompt" />,
}));
jest.mock("@/app/library/_components/FeedbackButtons", () => ({
  __esModule: true,
  default: () => <div data-testid="feedback-buttons" />,
}));

import PlanItemPage from "@/app/plan/items/[id]/page";
import type { MediaOverlay } from "@/lib/plan-api";

const PRE_OVERLAY_URL = "https://cdn/pre_overlay.mp4?sig=pre";
const OUTPUT_URL = "https://cdn/out.mp4?sig=out";

function makeItem(overrides = {}) {
  return {
    id: "test-item-id",
    day_index: 3,
    theme: "Morning Routine",
    idea: "Film your morning",
    filming_suggestion: null,
    rationale: null,
    filming_guide: [],
    clip_gcs_paths: ["users/u1/plan/item1/clip.mp4"],
    status: "ready",
    current_job_id: "job-1",
    user_edited: false,
    instruction_level: "full",
    conformance: null,
    ...overrides,
  };
}

function makeJob(variants) {
  return {
    status: "variants_ready",
    variants,
    current_phase: null,
    phase_log: null,
    started_at: "2026-06-06T10:00:00Z",
    finished_at: "2026-06-06T10:02:00Z",
    expected_phase_durations: null,
    created_at: "2026-06-06T10:00:00Z",
  };
}

function makeFullscreenCard(overrides: Partial<MediaOverlay> = {}): MediaOverlay {
  return {
    id: "card-fs",
    kind: "image",
    src_gcs_path: "users/u1/plan/item1/overlays/fs.png",
    preview_url: "https://signed/fs.png",
    position: "center",
    x_frac: 0.5,
    y_frac: 0.5,
    scale: 0.35,
    display_mode: "fullscreen",
    start_s: 0,
    end_s: 5,
    z: 0,
    ...overrides,
  };
}

// NOT instant-edit-eligible (no base_video_url) → hero renders through Hero.
function makeVariant(overrides = {}) {
  return {
    variant_id: "v1",
    output_url: OUTPUT_URL,
    render_status: "ready",
    text_mode: "agent_text",
    music_track_id: null,
    track_title: null,
    style_set_id: null,
    intro_text_size_px: null,
    intro_size_source: null,
    render_finished_at: "2026-06-06T10:02:00Z",
    error_class: null,
    ...overrides,
  };
}

function setData(variants) {
  mockUsePolledJobStatus.mockReturnValue({
    data: { item: makeItem(), job: makeJob(variants) },
    error: null,
    refetch: mockRefetch,
  });
}

const liveVariant = () =>
  makeVariant({
    media_overlays: [makeFullscreenCard()],
    pre_media_overlay_video_path: "generative-jobs/j1/v1_pre_overlay.mp4",
    pre_overlay_video_url: PRE_OVERLAY_URL,
  });

beforeEach(() => {
  mockSetVariantMediaOverlays.mockClear();
  mockRenderVariantSfx.mockClear();
  mockSetVariantSoundEffects.mockClear();
  mockDownloadVideo.mockClear();
});

describe("Plan item hero — fullscreen card live preview", () => {
  it("renders the fullscreen card inset full-frame with cover-cropped media", async () => {
    setData([liveVariant()]);
    await act(async () => {
      render(<PlanItemPage />);
    });

    const wrap = document.querySelector<HTMLElement>('[data-overlay-card="card-fs"]');
    expect(wrap).not.toBeNull();
    expect(wrap).toHaveStyle({
      position: "absolute",
      left: "0px",
      top: "0px",
      right: "0px",
      bottom: "0px",
      pointerEvents: "none",
    });
    const img = wrap!.querySelector("img")!;
    expect(img).toHaveAttribute("src", "https://signed/fs.png");
    expect(img).toHaveClass("w-full", "h-full", "object-cover");
    expect(img.className).not.toMatch(/rounded/);
  });
});

describe("Plan item page — Download blocked while a card's media failed", () => {
  it("onError → tile + inline copy; Download does NOT dispatch the overlay bake", async () => {
    setData([liveVariant()]);
    await act(async () => {
      render(<PlanItemPage />);
    });

    // The signed URL expired (routine after 24h) — the img errors out.
    await act(async () => {
      fireEvent.error(document.querySelector('[data-overlay-card="card-fs"] img')!);
    });

    // Full-frame failure tile with the exact copy + Remove.
    expect(screen.getByTestId("overlay-card-failed-card-fs")).toHaveTextContent(
      "This visual couldn't load",
    );
    expect(
      screen.getByText("1 visual couldn't load — refresh or remove it."),
    ).toBeInTheDocument();

    // Download click: the overlay-bake branch is gated — nothing dispatches.
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Download" }));
    });
    expect(mockSetVariantMediaOverlays).not.toHaveBeenCalled();
    expect(mockRenderVariantSfx).not.toHaveBeenCalled();
    expect(mockDownloadVideo).not.toHaveBeenCalled();
  });

  it("Remove on the tile clears the card, hides the copy, and unblocks Download", async () => {
    setData([liveVariant()]);
    await act(async () => {
      render(<PlanItemPage />);
    });
    await act(async () => {
      fireEvent.error(document.querySelector('[data-overlay-card="card-fs"] img')!);
    });

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Remove" }));
    });

    // Card + tile + blocking copy are gone.
    expect(document.querySelector('[data-overlay-card="card-fs"]')).toBeNull();
    expect(screen.queryByTestId("overlay-card-failed-card-fs")).toBeNull();
    expect(
      screen.queryByText("1 visual couldn't load — refresh or remove it."),
    ).toBeNull();

    // Download now proceeds: no cards left → no overlay bake, direct download.
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Download" }));
    });
    expect(mockSetVariantMediaOverlays).not.toHaveBeenCalled();
    expect(mockDownloadVideo).toHaveBeenCalledWith(OUTPUT_URL, expect.any(String));
  });

  it("Download still bakes normally when no card is failed (regression)", async () => {
    setData([liveVariant()]);
    await act(async () => {
      render(<PlanItemPage />);
    });

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Download" }));
    });
    expect(mockSetVariantMediaOverlays).toHaveBeenCalledWith(
      "test-item-id",
      "v1",
      [expect.objectContaining({ id: "card-fs", display_mode: "fullscreen" })],
      { render: true },
    );
  });
});
