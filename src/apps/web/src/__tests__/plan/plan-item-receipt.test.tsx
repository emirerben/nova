/**
 * Plan item page cleanup: the result page no longer renders SuggestionRail as
 * an inline editor surface, so overlay_apply_receipt stays out of this page.
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

import { act, render, screen } from "@testing-library/react";
import "@testing-library/jest-dom";

jest.mock("next/navigation", () => ({
  useParams: jest.fn(() => ({ id: "test-item-id" })),
  useRouter: jest.fn(() => ({ push: jest.fn() })),
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
  // The rail + AssetPool poll these when the autoplace flag is on.
  listPoolAssets: jest.fn().mockResolvedValue({
    assets: [
      {
        id: "asset-1",
        kind: "image",
        status: "ready",
        source_filename: "diagram.png",
        duration_s: null,
        aspect: null,
        width: null,
        height: null,
        subject: null,
        display_url: "https://signed/diagram.png",
        deduped: false,
      },
    ],
    max_assets: 20,
  }),
  getOverlaySuggestions: jest.fn().mockResolvedValue({
    status: null,
    suggestions: [],
    wishlist: [],
    stale_cleared: false,
  }),
  getSfxAudioUrl: jest.fn().mockResolvedValue("https://signed/sfx.mp3"),
  setVariantMediaOverlays: jest.fn().mockResolvedValue({}),
  renderVariantSfx: jest.fn().mockResolvedValue({}),
  setVariantSoundEffects: jest.fn().mockResolvedValue({}),
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
jest.mock("@/lib/download-video", () => ({ downloadVideo: jest.fn() }));
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

const FLAG = "NEXT_PUBLIC_OVERLAY_AUTOPLACE_ENABLED";

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

function makeVariant(overrides = {}) {
  return {
    variant_id: "v1",
    output_url: "https://cdn/out.mp4?sig=out",
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

beforeEach(() => {
  process.env[FLAG] = "true";
});

afterEach(() => {
  delete process.env[FLAG];
});

describe("Plan item page — overlay_apply_receipt cleanup", () => {
  it("does not render the overlay receipt rail on the cleaned-up result page", async () => {
    setData([
      makeVariant({
        overlay_apply_receipt: {
          demoted: 1,
          reason: "intro",
          at: "2026-07-03T00:00:00Z",
        },
      }),
    ]);
    await act(async () => {
      render(<PlanItemPage />);
    });

    expect(screen.queryByRole("button", { name: /place visuals for me/i })).toBeNull();
    expect(screen.queryByTestId("overlay-apply-receipt")).toBeNull();
    expect(screen.getByRole("button", { name: /^Download$/ })).toBeInTheDocument();
  });
});
