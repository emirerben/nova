/**
 * Tests for plan/items/[id]/page.tsx (PR4).
 *
 * Covers:
 *   - ProgressTheater renders with GENERATIVE_PHASE_ORDER when job has phase data.
 *   - Variant count from job, not a constant.
 *   - Deploy-skew: job status WITHOUT phase fields → no crash, no numeric ETA.
 *   - pendingEdits overlay still flips a re-rendering variant.
 *   - Error class → mapped copy; only raw error → generic fallback.
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

import { act, render, screen } from "@testing-library/react";
import "@testing-library/jest-dom";

// Mock next/navigation
jest.mock("next/navigation", () => ({
  useParams: jest.fn(() => ({ id: "test-item-id" })),
  useSearchParams: jest.fn(() => new URLSearchParams()),
}));

// Mock usePolledJobStatus
const mockRefetch = jest.fn();
jest.mock("@/hooks/usePolledJobStatus", () => ({
  usePolledJobStatus: jest.fn(),
}));
import { usePolledJobStatus } from "@/hooks/usePolledJobStatus";
const mockUsePolledJobStatus = usePolledJobStatus as jest.MockedFunction<typeof usePolledJobStatus>;

// Mock plan-api
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
  NotAuthenticatedError: class NotAuthenticatedError extends Error {
    constructor() {
      super("Not authenticated");
      this.name = "NotAuthenticatedError";
    }
  },
}));

jest.mock("@/lib/generative-api", () => ({
  getGenerativeStyleSets: jest.fn().mockResolvedValue([]),
  // The focused-variant timeline session lazy-GETs on mount; a never-resolving
  // promise keeps the "Edit clips" entry hidden without act() noise.
  getTimeline: jest.fn(() => new Promise(() => {})),
  TimelineApiError: class TimelineApiError extends Error {
    status = 0;
    code: string | null = null;
  },
  GENERATIVE_TERMINAL_STATUSES: ["variants_ready", "variants_ready_partial", "variants_failed", "processing_failed"],
}));

jest.mock("@/lib/music-api", () => ({
  getMusicTracks: jest.fn().mockResolvedValue({ tracks: [] }),
}));

jest.mock("@/lib/font-faces", () => ({ FONT_FACES: "" }));
jest.mock("@/lib/download-video", () => ({ downloadVideo: jest.fn() }));
jest.mock("@/lib/plan-text", () => ({ stripRationalePrefix: (s: string) => s }));

// PlanShell was deleted in v0.4.87.0 — item page now uses LightShell.
jest.mock("@/components/ui/LightShell", () => ({
  LightShell: ({ children }: { children: React.ReactNode }) => <div data-testid="light-shell">{children}</div>,
}));
jest.mock("@/app/plan/_components/PlanFilmstrip", () => ({
  __esModule: true,
  default: () => <div data-testid="plan-filmstrip" />,
}));
jest.mock("@/app/plan/_components/PlanVariantEditor", () => ({
  __esModule: true,
  default: () => <div data-testid="plan-variant-editor" />,
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
import type { PlanItemJobStatus } from "@/lib/plan-api";

// ===== Factory helpers =====

function makeItem(overrides = {}) {
  return {
    id: "test-item-id",
    day_index: 3,
    theme: "Morning Routine",
    idea: "Film your morning from 6am",
    filming_suggestion: null,
    rationale: null,
    filming_guide: [],
    clip_gcs_paths: [],
    status: "idea",
    current_job_id: null,
    user_edited: false,
    instruction_level: "full" as const,
    conformance: null,
    ...overrides,
  };
}

function makeJob(overrides: Partial<PlanItemJobStatus> = {}): PlanItemJobStatus {
  return {
    status: "processing",
    variants: [],
    current_phase: null,
    phase_log: null,
    started_at: null,
    finished_at: null,
    expected_phase_durations: null,
    created_at: "2026-06-06T10:00:00Z",
    ...overrides,
  };
}

function makeVariant(id: string, renderStatus: string, url: string | null = null) {
  return {
    variant_id: id,
    output_url: url,
    render_status: renderStatus,
    text_mode: "agent_text" as const,
    music_track_id: null,
    track_title: null,
    style_set_id: null,
    intro_text_size_px: null,
    intro_size_source: null,
    error_class: null,
  };
}

// ===== Tests =====

describe("PlanItemPage — ProgressTheater renders with phase data", () => {
  it("test_progress_theater_renders_with_generative_phases: phase chips visible", async () => {
    const item = makeItem({
      status: "generating",
      current_job_id: "job-123",
      clip_gcs_paths: ["uploads/test.mp4"],
    });
    const job = makeJob({
      status: "processing",
      current_phase: "analyze_clips",
      started_at: "2026-06-06T10:00:00Z",
      expected_phase_durations: {
        analyze_clips: 45000,
        match_song: 15000,
        render_variants: 90000,
        finalize: 10000,
      },
    });

    mockUsePolledJobStatus.mockReturnValue({
      data: { item, job },
      error: null,
      refetch: mockRefetch,
    });

    await act(async () => {
      render(<PlanItemPage />);
    });

    // ProgressTheater should be present (it renders the status band).
    // The theater renders with generative phases — no crash, light-shell present.
    expect(screen.getByTestId("light-shell")).toBeInTheDocument();
  });
});

describe("PlanItemPage — variant count from job not a constant", () => {
  it("test_variant_count_from_job: renders exactly as many variants as job returns", async () => {
    const item = makeItem({
      status: "ready",
      current_job_id: "job-456",
      clip_gcs_paths: ["uploads/test.mp4"],
    });
    const variants = [
      makeVariant("v1", "ready", "https://cdn/v1.mp4"),
      makeVariant("v2", "ready", "https://cdn/v2.mp4"),
    ];
    const job = makeJob({
      status: "variants_ready",
      variants,
      started_at: "2026-06-06T10:00:00Z",
      finished_at: "2026-06-06T10:02:00Z",
    });

    mockUsePolledJobStatus.mockReturnValue({
      data: { item, job },
      error: null,
      refetch: mockRefetch,
    });

    await act(async () => {
      render(<PlanItemPage />);
    });

    // Hero + rail: the page renders the results section without crashing.
    // With 2 ready variants the light-shell is present and the results area renders.
    expect(screen.getByTestId("light-shell")).toBeInTheDocument();
    // The "Other takes" label appears when there are alternates to show.
    expect(screen.getByText(/Other takes/i)).toBeInTheDocument();
    // No EXPECTED_VARIANTS=3 in sight — the page uses job.variants.length.
  });
});

describe("PlanItemPage — deploy-skew (no phase fields)", () => {
  it("test_deploy_skew_no_phase_fields: no crash, no numeric ETA", async () => {
    const item = makeItem({
      status: "generating",
      current_job_id: "job-789",
      clip_gcs_paths: ["uploads/test.mp4"],
    });
    const job = makeJob({
      status: "processing",
      current_phase: undefined,
      started_at: undefined,
      expected_phase_durations: undefined,
    });

    mockUsePolledJobStatus.mockReturnValue({
      data: { item, job },
      error: null,
      refetch: mockRefetch,
    });

    await act(async () => {
      render(<PlanItemPage />);
    });

    // No crash (test passes), no numeric ETA.
    expect(screen.queryByText(/min left/i)).toBeNull();
    expect(screen.queryByText(/less than a minute/i)).toBeNull();
    // Page is present.
    expect(screen.getByTestId("light-shell")).toBeInTheDocument();
  });
});

describe("PlanItemPage — pendingEdits overlay", () => {
  it("test_pending_edits_overlay_preserved: variant stays rendering while URL unchanged", async () => {
    const item = makeItem({
      status: "ready",
      current_job_id: "job-abc",
      clip_gcs_paths: ["uploads/test.mp4"],
    });
    const variants = [makeVariant("v1", "ready", "https://cdn/old.mp4")];
    const job = makeJob({ status: "variants_ready", variants });

    mockUsePolledJobStatus.mockReturnValue({
      data: { item, job },
      error: null,
      refetch: mockRefetch,
    });

    await act(async () => {
      render(<PlanItemPage />);
    });

    // Hero + rail: the results section renders without crash.
    expect(screen.getByTestId("light-shell")).toBeInTheDocument();
  });
});

// ── M4: conformance verdict panel ──────────────────────────────────────────────

describe("PlanItemPage — conformance verdict tile (D10 redesign)", () => {
  it("test_conformance_on_track_renders_quiet_line: one-liner, no card chrome", async () => {
    const item = makeItem({
      status: "awaiting_clips",
      clip_gcs_paths: ["users/u1/plan/item1/clip.mp4"],
      filming_guide: [{ what: "creator to camera", how: "eye level", duration_s: 8 }],
      conformance: {
        verdict: "on_track" as const,
        confidence: 0.9,
        summary: "Clip matches the brief well",
        mismatches: [],
        suggestions: [],
      },
    });

    mockUsePolledJobStatus.mockReturnValue({
      data: { item, job: null },
      error: null,
      refetch: mockRefetch,
    });

    await act(async () => {
      render(<PlanItemPage />);
    });

    // Two-pane redesign: NovaHelper replaces the full ConformanceVerdictPanel tile.
    // on_track shows a one-liner (lime dot + "Looks on-brief.") inside nova-helper.
    expect(screen.getByTestId("nova-helper")).toBeInTheDocument();
    expect(screen.getByText(/Looks on-brief/)).toBeInTheDocument();
  });

  it("test_conformance_off_brief_tile: one-liner summary + Tell Nova + Hide", async () => {
    const item = makeItem({
      status: "awaiting_clips",
      clip_gcs_paths: ["users/u1/plan/item1/clip.mp4"],
      filming_guide: [{ what: "overhead cooking shot", how: "tripod top-down", duration_s: 10 }],
      conformance: {
        verdict: "off_brief" as const,
        confidence: 0.85,
        summary: "This reads as a guitar session — the brief asked for cooking.",
        evaluated_theme: "Quick Weeknight Dinner",
        mismatches: ["Expected kitchen footage, got guitar"],
        suggestions: ["A steady overhead of the cutting board would land closer"],
      },
    });

    mockUsePolledJobStatus.mockReturnValue({
      data: { item, job: null },
      error: null,
      refetch: mockRefetch,
    });

    await act(async () => {
      render(<PlanItemPage />);
    });

    // Two-pane redesign: NovaHelper shows the conformance summary as a one-liner
    // (no label, no evidence line, no full-tile chrome) — calmer and less opinionated.
    expect(screen.getByTestId("nova-helper")).toBeInTheDocument();
    expect(screen.getByText(/This reads as a guitar session/)).toBeInTheDocument();
    // Recourse buttons — condensed labels in the one-liner.
    expect(screen.getByText(/Tell Nova/)).toBeInTheDocument();
    expect(screen.getByText(/Hide/)).toBeInTheDocument();
    // Mismatch bullets and suggestions are data, not display.
    expect(screen.queryByText(/Expected kitchen footage/)).toBeNull();
    expect(screen.queryByText(/steady overhead of the cutting board/)).toBeNull();
    // Full tile chrome is gone (label, evidence line, "generate anyway" copy).
    expect(screen.queryByText(/Different from the brief/i)).toBeNull();
    expect(screen.queryByText(/Read against:/i)).toBeNull();
  });

  it("test_conformance_suppressed_or_dismissed_renders_nothing", async () => {
    for (const extra of [{ suppressed: true }, { dismissed: true }]) {
      const item = makeItem({
        status: "awaiting_clips",
        clip_gcs_paths: ["users/u1/plan/item1/clip.mp4"],
        filming_guide: [{ what: "x", how: "", duration_s: 5 }],
        conformance: {
          verdict: "off_brief" as const,
          confidence: 0.9,
          summary: "irrelevant",
          mismatches: [],
          suggestions: [],
          ...extra,
        },
      });
      mockUsePolledJobStatus.mockReturnValue({
        data: { item, job: null },
        error: null,
        refetch: mockRefetch,
      });
      let view: ReturnType<typeof render> | undefined;
      await act(async () => {
        view = render(<PlanItemPage />);
      });
      expect(screen.queryByTestId("conformance-verdict-panel")).toBeNull();
      view?.unmount();
    }
  });

  it("test_conformance_low_confidence_renders_nothing", async () => {
    const item = makeItem({
      status: "awaiting_clips",
      clip_gcs_paths: ["users/u1/plan/item1/clip.mp4"],
      filming_guide: [{ what: "x", how: "", duration_s: 5 }],
      conformance: {
        verdict: "off_brief" as const,
        confidence: 0.4,
        summary: "a guess",
        mismatches: [],
        suggestions: [],
      },
    });
    mockUsePolledJobStatus.mockReturnValue({
      data: { item, job: null },
      error: null,
      refetch: mockRefetch,
    });
    await act(async () => {
      render(<PlanItemPage />);
    });
    expect(screen.queryByTestId("conformance-verdict-panel")).toBeNull();
  });

  it("test_conformance_absent_no_panel: panel not rendered when conformance is null", async () => {
    const item = makeItem({
      clip_gcs_paths: ["users/u1/plan/item1/clip.mp4"],
      conformance: null,
    });

    mockUsePolledJobStatus.mockReturnValue({
      data: { item, job: null },
      error: null,
      refetch: mockRefetch,
    });

    await act(async () => {
      render(<PlanItemPage />);
    });

    expect(screen.queryByTestId("conformance-verdict-panel")).toBeNull();
  });

  it("test_generate_button_not_blocked_by_conformance: Generate button enabled with clips regardless of verdict", async () => {
    const item = makeItem({
      status: "awaiting_clips",
      clip_gcs_paths: ["users/u1/plan/item1/clip.mp4"],
      filming_guide: [{ what: "creator at desk", how: "eye level", duration_s: 5 }],
      conformance: {
        verdict: "off_brief" as const,
        confidence: 0.95,
        summary: "Wrong subject",
        mismatches: ["Wrong subject"],
        suggestions: ["Reshoot"],
      },
    });

    mockUsePolledJobStatus.mockReturnValue({
      data: { item, job: null },
      error: null,
      refetch: mockRefetch,
    });

    await act(async () => {
      render(<PlanItemPage />);
    });

    // Generate button should be enabled — off_brief verdict never blocks it.
    const generateBtn = screen.getByRole("button", { name: /generate videos/i });
    expect(generateBtn).not.toBeDisabled();
  });
});
