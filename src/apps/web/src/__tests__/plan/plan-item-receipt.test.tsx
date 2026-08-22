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

import { act, fireEvent, render, screen } from "@testing-library/react";
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
  // Pull the status constants + isGenerativeJobSettled from the REAL module so
  // this mock can't drift from it (they are pure, no network).
  ...jest.requireActual("@/lib/generative-api"),
  getGenerativeStyleSets: jest.fn().mockResolvedValue([]),
  getTimeline: jest.fn(() => new Promise(() => {})),
  TimelineApiError: class TimelineApiError extends Error {
    status = 0;
    code: string | null = null;
  },
}));

jest.mock("@/lib/music-api", () => ({
  getMusicTracks: jest.fn().mockResolvedValue({ tracks: [] }),
}));

jest.mock("@/lib/tiktok-api", () => ({
  ...jest.requireActual("@/lib/tiktok-api"),
  getTikTokConnection: jest.fn(),
  getTikTokPublicationReceipt: jest.fn(),
  listTikTokPublications: jest.fn(),
  getTikTokPublication: jest.fn(),
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

import PlanItemPage from "@/app/plan/items/[id]/page";
import { useSearchParams } from "next/navigation";
import { setVariantMediaOverlays } from "@/lib/plan-api";
import {
  getTikTokConnection,
  getTikTokPublication,
  getTikTokPublicationReceipt,
  listTikTokPublications,
} from "@/lib/tiktok-api";

const mockGetTikTokConnection = getTikTokConnection as jest.MockedFunction<typeof getTikTokConnection>;
const mockGetTikTokPublicationReceipt = getTikTokPublicationReceipt as jest.MockedFunction<typeof getTikTokPublicationReceipt>;
const mockListTikTokPublications = listTikTokPublications as jest.MockedFunction<typeof listTikTokPublications>;
const mockGetTikTokPublication = getTikTokPublication as jest.MockedFunction<typeof getTikTokPublication>;
const mockUseSearchParams = useSearchParams as jest.MockedFunction<typeof useSearchParams>;
const mockSetVariantMediaOverlays = setVariantMediaOverlays as jest.MockedFunction<typeof setVariantMediaOverlays>;

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
  mockUseSearchParams.mockReturnValue(new URLSearchParams() as ReturnType<typeof useSearchParams>);
  mockSetVariantMediaOverlays.mockClear();
  mockGetTikTokConnection.mockResolvedValue({
    available: true,
    connected: true,
    status: "connected",
    account: { display_name: "Kria Studio", avatar_url: null },
    granted_scopes: ["video.publish"],
    can_publish: true,
    can_upload_draft: true,
    can_analyze: true,
    audited: true,
    beta: false,
    last_synced_at: null,
    learned_post_count: 0,
  });
  mockListTikTokPublications.mockResolvedValue([]);
  mockGetTikTokPublicationReceipt.mockResolvedValue(null);
  mockGetTikTokPublication.mockReset();
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
    expect(screen.getByRole("button", { name: "More video actions" })).toBeInTheDocument();
  });

  it("selects the active publication and polls it until TikTok reaches a terminal state", async () => {
    jest.useFakeTimers();
    const activePublication = {
      id: "publication-active",
      job_id: "job-1",
      variant_id: "v1",
      title: "Active video caption",
      privacy_level: "SELF_ONLY",
      allow_comment: false,
      allow_duet: false,
      allow_stitch: false,
      creator_nickname: "Kria Studio",
      processing_status: "processing",
      visibility_status: "unknown",
      public_at: null,
      retryable: false,
      failure_code: null,
      failure_detail: null,
      latest_metrics: null,
      metrics_synced_at: null,
      evaluation_metrics: null,
      evaluation_captured_at: null,
      created_at: "2026-08-01T10:00:00Z",
      updated_at: "2026-08-01T10:00:00Z",
    };
    const allPublications = [
      { ...activePublication, id: "wrong-variant", variant_id: "v2", title: "Wrong variant" },
      activePublication,
      {
        ...activePublication,
        id: "publication-previous-attempt",
        title: "Previous attempt",
        processing_status: "failed",
        failure_code: "publish_failed",
        created_at: "2026-07-31T10:00:00Z",
      },
      { ...activePublication, id: "wrong-job", job_id: "job-older", title: "Wrong job" },
    ];
    mockGetTikTokPublicationReceipt.mockResolvedValue(activePublication);
    mockListTikTokPublications.mockResolvedValue(allPublications);
    mockGetTikTokPublication
      .mockRejectedValueOnce(new Error("temporary network failure"))
      .mockResolvedValueOnce({
        ...activePublication,
        processing_status: "complete",
        visibility_status: "private",
        updated_at: "2026-08-01T10:05:00Z",
      });
    setData([makeVariant()]);

    await act(async () => {
      render(<PlanItemPage />);
      await Promise.resolve();
    });
    expect(await screen.findByText("Active video caption")).toBeInTheDocument();
    expect(screen.queryByText("Wrong variant")).toBeNull();
    expect(screen.queryByText("Wrong job")).toBeNull();
    expect(screen.getByRole("button", { name: "TikTok history (2)" })).toBeInTheDocument();

    await act(async () => {
      jest.advanceTimersByTime(5000);
      await Promise.resolve();
    });
    expect(mockGetTikTokPublication).toHaveBeenCalledTimes(1);
    expect(screen.getByText("Active video caption")).toBeInTheDocument();

    await act(async () => {
      jest.advanceTimersByTime(5000);
      await Promise.resolve();
    });
    expect(mockGetTikTokPublication).toHaveBeenCalledTimes(2);
    expect(await screen.findByText("Published privately")).toBeInTheDocument();

    await act(async () => {
      jest.advanceTimersByTime(5000);
      await Promise.resolve();
    });
    expect(mockGetTikTokPublication).toHaveBeenCalledTimes(2);
    jest.useRealTimers();
  });

  it("never shows another variant's receipt when the lookup falls back to job scope", async () => {
    // The receipt endpoint only filters by variant when the param is sent, so a
    // variant-less call returns the job's latest publication across ALL
    // variants. Showing it here would put an unpublished variant into receipt
    // mode and strip its release actions.
    mockGetTikTokPublicationReceipt.mockResolvedValue({
      id: "publication-other-variant",
      job_id: "job-1",
      variant_id: "v2",
      title: "Wrong variant caption",
      privacy_level: "SELF_ONLY",
      allow_comment: false,
      allow_duet: false,
      allow_stitch: false,
      creator_nickname: "Kria Studio",
      processing_status: "complete",
      visibility_status: "private",
      public_at: null,
      retryable: false,
      failure_code: null,
      failure_detail: null,
      latest_metrics: null,
      metrics_synced_at: null,
      evaluation_metrics: null,
      evaluation_captured_at: null,
      created_at: "2026-08-01T10:00:00Z",
      updated_at: "2026-08-01T10:00:00Z",
    });
    mockListTikTokPublications.mockResolvedValue([]);
    setData([makeVariant()]);

    await act(async () => {
      render(<PlanItemPage />);
      await Promise.resolve();
    });

    expect(screen.queryByText("Wrong variant caption")).toBeNull();
    expect(screen.queryByText("Published privately")).toBeNull();
    expect(await screen.findByText("Ready to publish")).toBeInTheDocument();
  });

  it("refetches publications after the same variant re-renders", async () => {
    mockGetTikTokPublicationReceipt.mockResolvedValue(null);
    mockListTikTokPublications.mockResolvedValue([]);
    setData([makeVariant({ render_finished_at: "2026-06-06T10:02:00Z" })]);

    let view;
    await act(async () => {
      view = render(<PlanItemPage />);
      await Promise.resolve();
    });
    const callsBefore = mockGetTikTokPublicationReceipt.mock.calls.length;

    // An edit reburns the SAME variant_id, so render_finished_at is the only
    // signal that the published cut is no longer the cut on screen.
    setData([makeVariant({ render_finished_at: "2026-06-06T10:09:00Z" })]);
    await act(async () => {
      view.rerender(<PlanItemPage />);
      await Promise.resolve();
    });

    expect(mockGetTikTokPublicationReceipt.mock.calls.length).toBeGreaterThan(callsBefore);
  });

  it("fails closed when the canonical receipt lookup fails", async () => {
    mockGetTikTokPublicationReceipt.mockRejectedValue(new Error("temporary receipt lookup failure"));
    mockListTikTokPublications.mockResolvedValue([]);
    setData([makeVariant()]);

    await act(async () => {
      render(<PlanItemPage />);
      await Promise.resolve();
    });

    expect(await screen.findByText(/Publishing stays paused to prevent a duplicate/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Publish to TikTok" })).toBeNull();
    const receiptCallsBeforeRetry = mockGetTikTokPublicationReceipt.mock.calls.length;
    fireEvent.click(screen.getByRole("button", { name: "Check again" }));
    await act(async () => { await Promise.resolve(); });
    expect(mockGetTikTokPublicationReceipt).toHaveBeenCalledTimes(receiptCallsBeforeRetry + 1);
  });

  it("keeps the connected preview account and publish sheet on the same simulated path", async () => {
    mockUseSearchParams.mockReturnValue(
      new URLSearchParams("tiktok_preview=connected") as ReturnType<typeof useSearchParams>,
    );
    mockGetTikTokConnection.mockResolvedValue({
      available: true,
      connected: false,
      status: "disconnected",
      account: null,
      granted_scopes: [],
      can_publish: false,
      can_upload_draft: false,
      can_analyze: false,
      audited: false,
      beta: false,
      last_synced_at: null,
      learned_post_count: 0,
    });
    setData([
      makeVariant({
        media_overlays: [
          {
            id: "preview-card",
            kind: "image",
            src_gcs_path: "users/another-user/preview-card.png",
            start_s: 0,
            end_s: 2,
            x: 0.1,
            y: 0.1,
            width: 0.4,
            height: 0.4,
          },
        ],
      }),
    ]);

    await act(async () => {
      render(<PlanItemPage />);
      await Promise.resolve();
    });

    expect(await screen.findByText("Connected TikTok account")).toBeInTheDocument();
    expect(screen.getByText("Emir")).toBeInTheDocument();
    expect(screen.queryByText("Connect TikTok before publishing.")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Publish to TikTok" }));

    expect(await screen.findByRole("heading", { name: "Preview TikTok delivery" })).toBeInTheDocument();
    expect(screen.getByText("No TikTok API request will be made.")).toBeInTheDocument();
    expect(mockSetVariantMediaOverlays).not.toHaveBeenCalled();
  });
});
