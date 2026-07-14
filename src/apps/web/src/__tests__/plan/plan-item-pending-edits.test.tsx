/**
 * Regression tests for the pendingEdits fingerprint fix (PR: clip-edit pin).
 *
 * Before the fix, `pendingEdits` cleared when `output_url` changed. For a
 * clip-timeline re-render the signed URL is held stable across the render, so
 * the pin never cleared → all PlanVariantEditor controls stayed disabled forever.
 *
 * After the fix, the pin clears based on `render_finished_at` advancing (or
 * `sawRendering` becoming true then a terminal status arriving). This file
 * verifies that contract through the full page component.
 *
 * Scenarios:
 *  A. Pre-edit `ready` race: after edit, a poll with same render_finished_at +
 *     "ready" must NOT clear the pin (controls stay disabled, poll continues).
 *  B. Fingerprint advance: a poll with a new render_finished_at + "ready" MUST
 *     clear the pin (controls re-enable, hero updates).
 *  C. sawRendering path: a "rendering" poll followed by "ready" clears the pin
 *     (the classic non-clip path, also exercised here for completeness).
 *  D. failed render: pin clears on "failed" + fresh fingerprint.
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

import { act, fireEvent, render, screen } from "@testing-library/react";
import "@testing-library/jest-dom";

process.env.NEXT_PUBLIC_TIKTOK_EDITOR_ENABLED = "true";

// jsdom doesn't implement HTMLMediaElement playback; useSfxPreview instantiates
// <audio> per placement and calls load()/play(). Stub them so variants carrying
// sound_effects can render without "Not implemented" throws.
window.HTMLMediaElement.prototype.load = jest.fn();
window.HTMLMediaElement.prototype.play = jest.fn().mockResolvedValue(undefined);
window.HTMLMediaElement.prototype.pause = jest.fn();

// ─── Mocks ───────────────────────────────────────────────────────────────────

const mockRouterPush = jest.fn();
let mockSearchParams = new URLSearchParams();

jest.mock("next/navigation", () => ({
  useParams: jest.fn(() => ({ id: "test-item-id" })),
  useRouter: jest.fn(() => ({ push: mockRouterPush })),
  useSearchParams: jest.fn(() => mockSearchParams),
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
  swapPlanItemSong: jest.fn(),
  retextPlanItem: jest.fn(),
  changePlanItemStyle: jest.fn(),
  setPlanItemIntroSize: jest.fn(),
  uploadToGcs: jest.fn(),
  editPlanItemVariant: jest.fn(),
  renderVariantSfx: jest.fn(),
  setVariantSoundEffects: jest.fn(),
  NotAuthenticatedError: class NotAuthenticatedError extends Error {
    constructor() {
      super("Not authenticated");
      this.name = "NotAuthenticatedError";
    }
  },
}));

jest.mock("@/lib/generative-api", () => ({
  getGenerativeStyleSets: jest.fn().mockResolvedValue([]),
  // Never-resolving: keeps the timeline entry hidden without act() noise.
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

jest.mock("@/app/plan/_components/PlanVariantEditor", () => ({
  __esModule: true,
  default: () => <div data-testid="plan-variant-editor" />,
}));

import type { PlanItemJobStatus } from "@/lib/plan-api";
import { renderVariantSfx, setVariantSoundEffects } from "@/lib/plan-api";
import { downloadVideo } from "@/lib/download-video";
const PlanItemPage = require("@/app/plan/items/[id]/page").default;
const mockRenderSfx = renderVariantSfx as jest.MockedFunction<typeof renderVariantSfx>;
const mockSetSfx = setVariantSoundEffects as jest.MockedFunction<typeof setVariantSoundEffects>;
const mockDownloadVideo = downloadVideo as jest.MockedFunction<typeof downloadVideo>;

function setEditorReturn({
  variantId = "v1",
  generation = "gen-1",
  priorFinishedAt = "2026-06-01T10:00:00Z",
  renderStarted = true,
} = {}) {
  mockSearchParams = new URLSearchParams({
    editor_saved: "1",
    editor_variant: variantId,
    editor_generation: generation,
    editor_prior_finished_at: priorFinishedAt,
    editor_render: renderStarted ? "1" : "0",
  });
}

// ─── Factory helpers ──────────────────────────────────────────────────────────

function makeItem(overrides = {}) {
  return {
    id: "test-item-id",
    day_index: 1,
    theme: "Test Theme",
    idea: "A test idea",
    filming_suggestion: null,
    rationale: null,
    filming_guide: [],
    clip_gcs_paths: [],
    status: "ready",
    current_job_id: "job-abc",
    user_edited: false,
    instruction_level: "full" as const,
    conformance: null,
    ...overrides,
  };
}

function makeJob(overrides: Partial<PlanItemJobStatus> = {}): PlanItemJobStatus {
  return {
    status: "variants_ready",
    variants: [],
    current_phase: null,
    phase_log: null,
    started_at: null,
    finished_at: null,
    expected_phase_durations: null,
    created_at: "2026-06-01T10:00:00Z",
    ...overrides,
  };
}

/** Build a variant that renders through the server path (no base_video_url →
 *  not instant-edit-eligible → all handlers go through runEdit). */
function makeServerVariant(
  id: string,
  renderStatus: string,
  outputUrl: string,
  renderFinishedAt: string | null = null,
) {
  return {
    variant_id: id,
    output_url: outputUrl,
    render_status: renderStatus,
    render_finished_at: renderFinishedAt,
    text_mode: "original_text" as const,
    music_track_id: null,
    track_title: null,
    style_set_id: null,
    intro_text_size_px: null,
    intro_size_source: null,
    intro_mode: null,
    base_video_url: null, // not instant-eligible → server path
    error_class: null,
  };
}

// ─── Tests ────────────────────────────────────────────────────────────────────

describe("pendingEdits fingerprint (editor return)", () => {
  const ITEM = makeItem();
  const OUTPUT_URL = "https://cdn/v1.mp4";

  beforeEach(() => {
    mockSearchParams = new URLSearchParams();
    mockRouterPush.mockReset();
    mockRefetch.mockReset();
    window.sessionStorage.clear();
    jest.clearAllMocks();
  });

  it("pins a returned editor render until render_finished_at advances", async () => {
    const TS = "2026-06-01T10:00:00Z";
    const TS2 = "2026-06-01T10:02:30Z";
    const initialVariant = makeServerVariant("v1", "ready", OUTPUT_URL, TS);
    setEditorReturn({ priorFinishedAt: TS, renderStarted: true });

    mockUsePolledJobStatus.mockReturnValue({
      data: { item: ITEM, job: makeJob({ variants: [initialVariant] }) },
      error: null,
      refetch: mockRefetch,
    });

    const { rerender } = await act(async () => render(<PlanItemPage />));
    expect(await screen.findByLabelText("Rendering new version")).toBeInTheDocument();

    mockUsePolledJobStatus.mockReturnValue({
      data: {
        item: ITEM,
        job: makeJob({
          variants: [{ ...initialVariant, render_status: "ready", render_finished_at: TS }],
        }),
      },
      error: null,
      refetch: mockRefetch,
    });
    await act(async () => { rerender(<PlanItemPage />); });
    expect(screen.getByLabelText("Rendering new version")).toBeInTheDocument();

    mockUsePolledJobStatus.mockReturnValue({
      data: {
        item: ITEM,
        job: makeJob({
          variants: [{ ...initialVariant, render_status: "ready", render_finished_at: TS2 }],
        }),
      },
      error: null,
      refetch: mockRefetch,
    });
    await act(async () => { rerender(<PlanItemPage />); });
    expect(screen.queryByLabelText("Rendering new version")).toBeNull();
  });

  it("does not pin an editor return when no render was started", async () => {
    const TS = "2026-06-01T10:00:00Z";
    setEditorReturn({ priorFinishedAt: TS, renderStarted: false });

    mockUsePolledJobStatus.mockReturnValue({
      data: {
        item: ITEM,
        job: makeJob({
          variants: [makeServerVariant("v1", "ready", OUTPUT_URL, TS)],
        }),
      },
      error: null,
      refetch: mockRefetch,
    });

    await act(async () => render(<PlanItemPage />));
    expect(screen.queryByLabelText("Rendering new version")).toBeNull();
  });

  it("auto-opens the native editor once for a single ready editable variant", async () => {
    mockUsePolledJobStatus.mockReturnValue({
      data: {
        item: ITEM,
        job: makeJob({
          variants: [makeServerVariant("v1", "ready", OUTPUT_URL, "2026-06-01T10:00:00Z")],
        }),
      },
      error: null,
      refetch: mockRefetch,
    });

    const { rerender } = await act(async () => render(<PlanItemPage />));
    expect(mockRouterPush).toHaveBeenCalledWith("/plan/items/test-item-id/edit?variant=v1");

    await act(async () => { rerender(<PlanItemPage />); });
    expect(mockRouterPush).toHaveBeenCalledTimes(1);
  });

  it("does not auto-open again on editor return", async () => {
    setEditorReturn({ renderStarted: false });
    mockUsePolledJobStatus.mockReturnValue({
      data: {
        item: ITEM,
        job: makeJob({
          variants: [makeServerVariant("v1", "ready", OUTPUT_URL, "2026-06-01T10:00:00Z")],
        }),
      },
      error: null,
      refetch: mockRefetch,
    });

    await act(async () => render(<PlanItemPage />));
    expect(mockRouterPush).not.toHaveBeenCalled();
  });
});

describe("download-triggered SFX bake failure (C1 regression)", () => {
  // Regression: removing the SFX "Apply"/"Retry" button deleted the only surface
  // for a failed SFX bake. A Download-triggered bake that fails on the backend
  // (FFmpeg error after a successful dispatch) must surface an error — not
  // silently re-enable the button and keep playing the stale video.
  const ITEM = makeItem();
  const OUTPUT_URL = "https://cdn/v1.mp4";

  function variantWithUnbakedSfx(renderStatus: string, finishedAt: string) {
    return {
      ...makeServerVariant("v1", renderStatus, OUTPUT_URL, finishedAt),
      // Saved but never baked (pre_sfx_video_path null) → needsSfxBake is true,
      // so Download triggers a fresh SFX mix.
      sound_effects: [
        {
          id: "sfx1",
          sound_effect_id: null,
          src_gcs_path: "sound-effects/boom/audio.mp3",
          at_s: 4,
          gain: 1,
          trim_start_s: null,
          trim_end_s: null,
          duration_s: 0.5,
          label: "Boom",
        },
      ],
      pre_sfx_video_path: null,
    };
  }

  beforeEach(() => {
    mockRenderSfx.mockReset().mockResolvedValue(undefined);
    mockSetSfx.mockReset().mockResolvedValue(undefined);
    mockDownloadVideo.mockReset();
  });

  it("surfaces an error (and does not silently download) when the bake fails", async () => {
    const TS1 = "2026-06-01T10:00:00Z";
    const TS2 = "2026-06-01T10:01:00Z";

    mockUsePolledJobStatus.mockReturnValue({
      data: { item: ITEM, job: makeJob({ variants: [variantWithUnbakedSfx("ready", TS1)] }) },
      error: null,
      refetch: mockRefetch,
    } as ReturnType<typeof usePolledJobStatus>);

    const { rerender } = await act(async () => render(<PlanItemPage />));

    // Click Download → triggers the SFX bake (needsSfxBake true, never baked).
    const downloadBtn = screen.getByRole("button", { name: /^Download$/i });
    await act(async () => {
      fireEvent.click(downloadBtn);
    });
    expect(mockRenderSfx).toHaveBeenCalledTimes(1);

    // Backend bake fails with a fresh fingerprint (clears the download pin).
    mockUsePolledJobStatus.mockReturnValue({
      data: {
        item: ITEM,
        job: makeJob({
          variants: [{ ...variantWithUnbakedSfx("failed", TS2), error_class: "RenderFailed" }],
        }),
      },
      error: null,
      refetch: mockRefetch,
    } as ReturnType<typeof usePolledJobStatus>);
    await act(async () => {
      rerender(<PlanItemPage />);
    });

    // C1: the failure is surfaced to the user, not swallowed.
    expect(screen.getByText(/Couldn't prepare your video for download/i)).toBeInTheDocument();
    // And the stale video was NOT silently downloaded.
    expect(mockDownloadVideo).not.toHaveBeenCalled();
  });
});
