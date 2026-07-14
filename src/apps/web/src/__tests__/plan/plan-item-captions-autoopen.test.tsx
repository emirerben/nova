/**
 * Caption-edit discoverability after item-page cleanup:
 * the item page is no longer the inline editor surface. Caption archetypes keep
 * playback/download visible here, while caption editing moves to the full-screen
 * editor entry.
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
// jsdom does not implement scrollIntoView — the auto-open scroll effect calls it.
Element.prototype.scrollIntoView = jest.fn();

import { act, render, screen } from "@testing-library/react";
import "@testing-library/jest-dom";

process.env.NEXT_PUBLIC_SUBTITLED_ENABLED = "true";

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
const mockUsePolledJobStatus = usePolledJobStatus as jest.MockedFunction<typeof usePolledJobStatus>;

jest.mock("@/lib/plan-api", () => ({
  ...jest.requireActual("@/lib/plan-api"),
  getPlanItem: jest.fn(),
  getPlanItemJobStatus: jest.fn(),
  getPlanItemVariants: jest.fn(),
  requestUploadUrls: jest.fn(),
  attachClips: jest.fn(),
  generatePlanItem: jest.fn(),
  expandIdea: jest.fn(),
  updatePlanItem: jest.fn(),
  swapPlanItemSong: jest.fn(),
  retextPlanItem: jest.fn(),
  changePlanItemStyle: jest.fn(),
  setPlanItemIntroSize: jest.fn(),
  setPlanItemCaptionLanguage: jest.fn(),
  uploadToGcs: jest.fn(),
  NotAuthenticatedError: class NotAuthenticatedError extends Error {},
}));

jest.mock("@/lib/generative-api", () => ({
  getGenerativeStyleSets: jest.fn().mockResolvedValue([]),
  getTimeline: jest.fn(() => new Promise(() => {})),
  TimelineApiError: class TimelineApiError extends Error {},
  GENERATIVE_TERMINAL_STATUSES: ["variants_ready", "variants_ready_partial", "variants_failed", "processing_failed"],
}));

jest.mock("@/lib/music-api", () => ({ getMusicTracks: jest.fn().mockResolvedValue({ tracks: [] }) }));
jest.mock("@/lib/font-faces", () => ({ FONT_FACES: "" }));
jest.mock("@/lib/download-video", () => ({ downloadVideo: jest.fn() }));
jest.mock("@/lib/plan-text", () => ({ stripRationalePrefix: (s: string) => s }));
jest.mock("@/components/ui/LightShell", () => ({
  LightShell: ({ children }: { children: React.ReactNode }) => <div data-testid="light-shell">{children}</div>,
}));
jest.mock("@/app/plan/_components/PlanVariantEditor", () => ({ __esModule: true, default: () => <div /> }));
jest.mock("@/app/plan/_components/SignInPrompt", () => ({ __esModule: true, default: () => <div /> }));
jest.mock("@/app/library/_components/FeedbackButtons", () => ({ __esModule: true, default: () => <div /> }));
jest.mock("@/app/plan/_components/AssetPool", () => ({ __esModule: true, default: () => <div /> }));
jest.mock("@/app/plan/_components/SuggestionRail", () => ({ __esModule: true, default: () => <div /> }));
jest.mock("@/app/plan/items/[id]/components/ShotSlotUploader", () => ({
  __esModule: true,
  default: () => <div />,
  ClipNoteControl: () => <div />,
}));
// The real CaptionEditor renders a <video> + fetches — stub it so the test can
// assert "the caption editor is on screen" without its internals.
jest.mock("@/app/plan/_components/CaptionEditor", () => ({
  __esModule: true,
  default: () => <div data-testid="caption-editor" />,
}));

const PlanItemPage = require("@/app/plan/items/[id]/page").default;

function makeItem(overrides = {}) {
  return {
    id: "test-item-id",
    day_index: 3,
    theme: "Talking to camera",
    idea: "You on screen with auto subtitles",
    filming_guide: [],
    clip_gcs_paths: ["uploads/talk.mp4"],
    status: "ready",
    current_job_id: "job-cap",
    content_mode: "create_new",
    conformance: null,
    edit_format: "subtitled",
    ...overrides,
  };
}

function captionVariant(overrides = {}) {
  return {
    variant_id: "cap-1",
    output_url: "https://cdn/cap.mp4",
    base_video_url: "https://cdn/cap_base.mp4",
    render_status: "ready",
    render_finished_at: "2026-07-12T10:00:00Z",
    text_mode: "none",
    resolved_archetype: "subtitled",
    music_track_id: null,
    style_set_id: null,
    intro_text_size_px: null,
    caption_cues: [{ start_s: 0, end_s: 1, text: "hi" }],
    captions_enabled: true,
    ...overrides,
  };
}

function mountWithVariant(variant, item = {}) {
  mockUsePolledJobStatus.mockReturnValue({
    data: {
      item: makeItem(item),
      job: { status: "variants_ready", variants: [variant], current_phase: null, phase_log: null, started_at: null, finished_at: "2026-07-12T10:02:00Z", expected_phase_durations: null, created_at: "2026-07-12T10:00:00Z" },
    },
    error: null,
    refetch: mockRefetch,
  });
}

afterEach(() => jest.clearAllMocks());

describe("Caption archetype item-page cleanup", () => {
  it("does not render the inline Captions tab or CaptionEditor for a ready subtitled variant", async () => {
    mountWithVariant(captionVariant());
    await act(async () => {
      render(<PlanItemPage />);
    });

    expect(screen.queryByTestId("caption-editor")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /captions/i })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^Download$/ })).toBeInTheDocument();
  });

  it("does not expose a captions fallback panel for no-cue variants on the item page", async () => {
    mountWithVariant(captionVariant({ render_status: "ready", caption_cues: null }));
    await act(async () => {
      render(<PlanItemPage />);
    });

    expect(screen.queryByTestId("captions-unavailable")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /open the timeline/i })).not.toBeInTheDocument();
  });
});
