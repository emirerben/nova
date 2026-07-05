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

// jsdom doesn't implement HTMLMediaElement playback; useSfxPreview instantiates
// <audio> per placement and calls load()/play(). Stub them so variants carrying
// sound_effects can render without "Not implemented" throws.
window.HTMLMediaElement.prototype.load = jest.fn();
window.HTMLMediaElement.prototype.play = jest.fn().mockResolvedValue(undefined);
window.HTMLMediaElement.prototype.pause = jest.fn();

// ─── Mocks ───────────────────────────────────────────────────────────────────

jest.mock("next/navigation", () => ({
  useParams: jest.fn(() => ({ id: "test-item-id" })),
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
jest.mock("@/app/plan/_components/PlanFilmstrip", () => ({
  __esModule: true,
  default: () => <div data-testid="plan-filmstrip" />,
}));
jest.mock("@/app/plan/_components/SignInPrompt", () => ({
  __esModule: true,
  default: () => <div data-testid="sign-in-prompt" />,
}));
jest.mock("@/app/library/_components/FeedbackButtons", () => ({
  __esModule: true,
  default: () => <div data-testid="feedback-buttons" />,
}));

// PlanVariantEditor spy — captures the `variant` and `onSwap` props each render.
// Remains a dumb div so no child-rendering complexity leaks in.
let spyVariant: any = null;
let spyOnSwap: ((trackId: string) => Promise<void>) | null = null;
jest.mock("@/app/plan/_components/PlanVariantEditor", () => ({
  __esModule: true,
  default: ({ variant, onSwap }: any) => {
    spyVariant = variant;
    spyOnSwap = onSwap;
    return <div data-testid="plan-variant-editor" />;
  },
}));

import PlanItemPage from "@/app/plan/items/[id]/page";
import type { PlanItemJobStatus } from "@/lib/plan-api";
import { swapPlanItemSong, renderVariantSfx, setVariantSoundEffects } from "@/lib/plan-api";
import { downloadVideo } from "@/lib/download-video";
const mockSwap = swapPlanItemSong as jest.MockedFunction<typeof swapPlanItemSong>;
const mockRenderSfx = renderVariantSfx as jest.MockedFunction<typeof renderVariantSfx>;
const mockSetSfx = setVariantSoundEffects as jest.MockedFunction<typeof setVariantSoundEffects>;
const mockDownloadVideo = downloadVideo as jest.MockedFunction<typeof downloadVideo>;

/**
 * Surface PlanVariantEditor by opening the Timeline tab and expanding its Text panel.
 * The makeServerVariant uses text_mode "original_text" (no music_track_id), so the
 * Song tab is hidden. Text editing is now inline in the Timeline Text lane (PR-4).
 */
async function openSongTab() {
  // Click the Timeline tab (▭) — opens showTimelineSection which renders
  // PlanVariantEditor in the text-controls area below UnifiedTimeline (T5).
  // The old "Edit text ▼" expand button is gone (T5 replaced it with interactive bars).
  const timelineTab = screen.queryByRole("button", { name: /▭.*Timeline|Timeline/i });
  if (timelineTab) {
    await act(async () => { fireEvent.click(timelineTab); });
  }
  // PlanVariantEditor is now directly rendered below the timeline for text-mode
  // variants — no additional click needed (T5 architecture change).
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

describe("pendingEdits fingerprint (render_finished_at)", () => {
  const ITEM = makeItem();
  const OUTPUT_URL = "https://cdn/v1.mp4";

  beforeEach(() => {
    spyVariant = null;
    spyOnSwap = null;
    mockRefetch.mockReset();
    mockSwap.mockReset();
    jest.clearAllMocks();
  });

  it("test_pin_stays_on_same_render_finished_at: pre-edit ready race does not clear the pin", async () => {
    // Scenario A: the poll after submission returns "ready" with the SAME
    // render_finished_at as before the edit — the pin must NOT clear.
    const TS = "2026-06-01T10:00:00Z";
    const initialVariant = makeServerVariant("v1", "ready", OUTPUT_URL, TS);

    mockUsePolledJobStatus.mockReturnValue({
      data: { item: ITEM, job: makeJob({ variants: [initialVariant] }) },
      error: null,
      refetch: mockRefetch,
    });

    const { rerender } = await act(async () => render(<PlanItemPage />));

    // Open the Song tab to expose PlanVariantEditor and capture spyOnSwap.
    await openSongTab();

    // Variant is ready; editor shows it as ready.
    expect(spyVariant?.render_status).toBe("ready");
    expect(spyOnSwap).not.toBeNull();

    // Trigger a song-swap edit — goes through runEdit → markVariantRendering.
    mockSwap.mockResolvedValueOnce(undefined);
    await act(async () => {
      await spyOnSwap!("track-99");
    });

    // pendingEdits now has { priorFinishedAt: "TS", sawRendering: false }.
    // The next poll returns "ready" with the SAME render_finished_at —
    // the Celery task hasn't run yet (pre-edit race window).
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

    // Pin must NOT have cleared — variant forced to "rendering".
    expect(spyVariant?.render_status).toBe("rendering");
  });

  it("test_pin_clears_on_advanced_render_finished_at: fresh fingerprint clears the pin", async () => {
    // Scenario B: poll returns "ready" with a NEW render_finished_at — the
    // actual completed render. The pin must clear and controls re-enable.
    const TS1 = "2026-06-01T10:00:00Z";
    const TS2 = "2026-06-01T10:02:30Z"; // advanced
    const initialVariant = makeServerVariant("v1", "ready", OUTPUT_URL, TS1);

    mockUsePolledJobStatus.mockReturnValue({
      data: { item: ITEM, job: makeJob({ variants: [initialVariant] }) },
      error: null,
      refetch: mockRefetch,
    });

    const { rerender } = await act(async () => render(<PlanItemPage />));
    await openSongTab();
    expect(spyVariant?.render_status).toBe("ready");

    mockSwap.mockResolvedValueOnce(undefined);
    await act(async () => { await spyOnSwap!("track-99"); });

    // Poll with advanced timestamp + "ready" — this is the real completed render.
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

    // Pin IS cleared — variant render_status is back to "ready".
    expect(spyVariant?.render_status).toBe("ready");
  });

  it("test_pin_clears_after_saw_rendering: sawRendering path clears on subsequent ready", async () => {
    // Scenario C: the poll first returns "rendering" (sawRendering → true),
    // then "ready" with the same timestamp. The pin must clear.
    const TS = "2026-06-01T10:00:00Z";
    const initialVariant = makeServerVariant("v1", "ready", OUTPUT_URL, TS);

    mockUsePolledJobStatus.mockReturnValue({
      data: { item: ITEM, job: makeJob({ variants: [initialVariant] }) },
      error: null,
      refetch: mockRefetch,
    });

    const { rerender } = await act(async () => render(<PlanItemPage />));
    await openSongTab();
    expect(spyVariant?.render_status).toBe("ready");

    mockSwap.mockResolvedValueOnce(undefined);
    await act(async () => { await spyOnSwap!("track-99"); });

    // Poll 1: variant is "rendering" — sawRendering flips to true.
    mockUsePolledJobStatus.mockReturnValue({
      data: {
        item: ITEM,
        job: makeJob({ variants: [{ ...initialVariant, render_status: "rendering" }] }),
      },
      error: null,
      refetch: mockRefetch,
    });
    await act(async () => { rerender(<PlanItemPage />); });

    // While rendering, variant is still "rendering" (server truth, no override needed).
    expect(spyVariant?.render_status).toBe("rendering");

    // Poll 2: variant is "ready" with same timestamp — but sawRendering = true
    // so isFreshRender is true → pin clears.
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

    // Pin cleared — controls re-enabled.
    expect(spyVariant?.render_status).toBe("ready");
  });

  it("test_pin_clears_on_failed_with_fresh_fingerprint: failed render clears the pin", async () => {
    // Scenario D: render fails with a new render_finished_at. Pin must clear
    // so the standard failure UI (with retry) can take over.
    const TS1 = "2026-06-01T10:00:00Z";
    const TS2 = "2026-06-01T10:01:00Z";
    const initialVariant = makeServerVariant("v1", "ready", OUTPUT_URL, TS1);

    mockUsePolledJobStatus.mockReturnValue({
      data: { item: ITEM, job: makeJob({ variants: [initialVariant] }) },
      error: null,
      refetch: mockRefetch,
    });

    const { rerender } = await act(async () => render(<PlanItemPage />));
    await openSongTab();

    mockSwap.mockResolvedValueOnce(undefined);
    await act(async () => { await spyOnSwap!("track-99"); });

    mockUsePolledJobStatus.mockReturnValue({
      data: {
        item: ITEM,
        job: makeJob({
          variants: [
            {
              ...initialVariant,
              render_status: "failed",
              render_finished_at: TS2,
              error_class: "RenderFailed",
            },
          ],
        }),
      },
      error: null,
      refetch: mockRefetch,
    });
    await act(async () => { rerender(<PlanItemPage />); });

    // Pin cleared on failed + new fingerprint.
    expect(spyVariant?.render_status).toBe("failed");
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
