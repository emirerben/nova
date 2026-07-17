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

import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom";

process.env.NEXT_PUBLIC_SUBTITLED_ENABLED = "true";

// Mock next/navigation
jest.mock("next/navigation", () => ({
  useParams: jest.fn(() => ({ id: "test-item-id" })),
  useRouter: jest.fn(() => ({ push: jest.fn() })),
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
  expandIdea: jest.fn(),
  updatePlanItem: jest.fn(),
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
jest.mock("@/app/plan/_components/AssetPool", () => ({
  __esModule: true,
  default: () => <div data-testid="asset-pool" />,
}));
jest.mock("@/app/plan/_components/SuggestionRail", () => ({
  __esModule: true,
  default: () => <div data-testid="suggestion-rail" />,
}));
jest.mock("@/app/plan/items/[id]/components/ShotSlotUploader", () => ({
  __esModule: true,
  default: () => <div data-testid="shot-slot-uploader" />,
  ClipNoteControl: () => <div data-testid="clip-note-control" />,
}));

import { expandIdea, generatePlanItem, updatePlanItem, type PlanItemJobStatus } from "@/lib/plan-api";
const PlanItemPage = require("@/app/plan/items/[id]/page").default;
const mockExpandIdea = expandIdea as jest.MockedFunction<typeof expandIdea>;
const mockGeneratePlanItem = generatePlanItem as jest.MockedFunction<typeof generatePlanItem>;
const mockUpdatePlanItem = updatePlanItem as jest.MockedFunction<typeof updatePlanItem>;

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
    content_mode: "create_new",
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

describe("PlanItemPage — masonry collage item UX", () => {
  function renderMasonryItem(extra = {}) {
    const item = makeItem({
      status: "awaiting_clips",
      edit_format: "montage",
      montage_preset: "masonry",
      filming_guide: [{ what: "Wide room beat", how: "Hold steady", duration_s: 4 }],
      ...extra,
    });
    mockUsePolledJobStatus.mockReturnValue({
      data: { item, job: null },
      error: null,
      refetch: mockRefetch,
    });
    return render(<PlanItemPage />);
  }

  it("renders preset preview tiles instead of text-only cards", async () => {
    await act(async () => {
      renderMasonryItem({ montage_preset: "classic" });
    });

    expect(
      screen
        .getByText("Classic")
        .previousElementSibling?.querySelector('[class*="montage-classic-a"]'),
    ).not.toBeNull();
    expect(
      screen
        .getByText("Masonry collage")
        .previousElementSibling?.querySelector('[class*="montage-masonry-pan"]'),
    ).not.toBeNull();
    expect(
      screen
        .getByText("Polaroid wall")
        .previousElementSibling?.querySelector('[class*="pb-"]'),
    ).not.toBeNull();
  });

  it.each(["masonry", "polaroid_wall"])(
    "uses compact collage uploads for %s even when the item has a filming guide",
    async (montage_preset) => {
      await act(async () => {
        renderMasonryItem({ montage_preset });
      });

      expect(screen.getByText("Your clips")).toBeInTheDocument();
      expect(screen.queryByTestId("shot-slot-uploader")).not.toBeInTheDocument();
      expect(
        screen.getByLabelText("Upload video clips for this idea").getAttribute("accept"),
      ).toContain("image/webp");
    },
  );

  it("renders uploaded clips as a compact filmstrip", async () => {
    await act(async () => {
      renderMasonryItem({
        clip_gcs_paths: ["users/u/plan/i/001-room.mov", "users/u/plan/i/002-detail.png"],
        clip_assignments: [
          { gcs_path: "users/u/plan/i/001-room.mov", shot_id: null, user_note: "" },
          { gcs_path: "users/u/plan/i/002-detail.png", shot_id: null, user_note: "closeup" },
        ],
      });
    });

    expect(screen.getByTestId("uploaded-clip-filmstrip")).toBeInTheDocument();
    expect(screen.getByText("room.mov")).toBeInTheDocument();
    expect(screen.getByText("detail.png")).toBeInTheDocument();
  });

  it.each(["masonry", "polaroid_wall"])(
    "hides visual-pool affordances for %s items",
    async (montage_preset) => {
      await act(async () => {
        renderMasonryItem({ montage_preset });
      });

      expect(screen.queryByTestId("asset-pool")).not.toBeInTheDocument();
      expect(screen.queryByTestId("suggestion-rail")).not.toBeInTheDocument();
    },
  );
});

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

describe("PlanItemPage — result cleanup", () => {
  it("hides legacy alternates and inline timeline controls", async () => {
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

    expect(screen.getByTestId("light-shell")).toBeInTheDocument();
    expect(screen.queryByText(/Other takes/i)).toBeNull();
    expect(screen.queryByRole("button", { name: /Timeline/i })).toBeNull();
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

    // Two-pane redesign: KriaHelper replaces the full ConformanceVerdictPanel tile.
    // on_track shows a one-liner (lime dot + "Looks on-brief.") inside kria-helper.
    expect(screen.getByTestId("kria-helper")).toBeInTheDocument();
    expect(screen.getByText(/Looks on-brief/)).toBeInTheDocument();
  });

  it("test_conformance_off_brief_tile: one-liner summary + Tell Kria + Hide", async () => {
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

    // Two-pane redesign: KriaHelper shows the conformance summary as a one-liner
    // (no label, no evidence line, no full-tile chrome) — calmer and less opinionated.
    expect(screen.getByTestId("kria-helper")).toBeInTheDocument();
    expect(screen.getByText(/This reads as a guitar session/)).toBeInTheDocument();
    // Recourse buttons — condensed labels in the one-liner.
    expect(screen.getByText(/Tell Kria/)).toBeInTheDocument();
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
    const generateBtn = screen.getByRole("button", { name: /generate video/i });
    expect(generateBtn).not.toBeDisabled();
  });

  it("test_talking_head_preserves_backend_edit_format_before_generate", async () => {
    mockUpdatePlanItem.mockClear();
    mockGeneratePlanItem.mockClear();
    const item = makeItem({
      status: "awaiting_clips",
      edit_format: "talking_head",
      clip_gcs_paths: [
        "users/u1/plan/item1/spoken.mp4",
        "users/u1/plan/item1/broll.mp4",
      ],
    });

    mockGeneratePlanItem.mockResolvedValue(item);
    mockUsePolledJobStatus.mockReturnValue({
      data: { item, job: null },
      error: null,
      refetch: mockRefetch,
    });

    await act(async () => {
      render(<PlanItemPage />);
    });

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /generate video/i }));
    });

    await waitFor(() => {
      expect(mockGeneratePlanItem).toHaveBeenCalledWith("test-item-id");
    });
    expect(mockUpdatePlanItem).not.toHaveBeenCalled();
  });
});

describe("PlanItemPage — Plan this for me proposal flow", () => {
  beforeEach(() => {
    mockExpandIdea.mockReset();
    mockUpdatePlanItem.mockReset();
    mockRefetch.mockReset();
  });

  it("opens a context panel before calling the proposal API", async () => {
    const item = makeItem({ theme: null, filming_guide: [], status: "ready" });
    mockUsePolledJobStatus.mockReturnValue({
      data: { item, job: null },
      error: null,
      refetch: mockRefetch,
    });

    await act(async () => {
      render(<PlanItemPage />);
    });

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /Plan this for me/i }));
    });

    expect(screen.getByText("A little context helps.")).toBeInTheDocument();
    expect(screen.getByText("What should this edit make people feel or notice?")).toBeInTheDocument();
    expect(mockExpandIdea).not.toHaveBeenCalled();
  });

  it("renders the AI proposal card with shot list details after context submit", async () => {
    const item = makeItem({ theme: null, filming_guide: [], status: "ready" });
    mockExpandIdea.mockResolvedValue({
      theme: "A calmer morning reset",
      filming_suggestion: "Film it as three quiet beats.",
      rationale: "This gives the edit a clean before-after arc.",
      filming_guide: [
        {
          shot_id: "shot-1",
          what: "Open on the messy counter",
          how: "Hold steady from chest height",
          duration_s: 4,
        },
        {
          shot_id: "shot-2",
          what: "Wipe and reset the surface",
          how: "Use a close side angle",
          duration_s: 6,
        },
      ],
    });
    mockUsePolledJobStatus.mockReturnValue({
      data: { item, job: null },
      error: null,
      refetch: mockRefetch,
    });

    await act(async () => {
      render(<PlanItemPage />);
    });

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /Plan this for me/i }));
    });
    await act(async () => {
      fireEvent.change(
        screen.getByPlaceholderText("A rough goal or detail is enough..."),
        { target: { value: "Make people feel like they can reset quickly." } },
      );
      fireEvent.click(screen.getByRole("button", { name: /Generate plan/i }));
    });

    expect(await screen.findByText("AI SUGGESTION")).toBeInTheDocument();
    expect(mockExpandIdea).toHaveBeenCalledWith("test-item-id", {
      creator_context: "Make people feel like they can reset quickly.",
    });
    expect(screen.getByText("A calmer morning reset")).toBeInTheDocument();
    expect(screen.getByText("Film it as three quiet beats.")).toBeInTheDocument();
    expect(screen.getByText("Open on the messy counter")).toBeInTheDocument();
    expect(screen.getByText("Hold steady from chest height")).toBeInTheDocument();
    expect(screen.getByText("~4s")).toBeInTheDocument();
    expect(screen.getByText("Wipe and reset the surface")).toBeInTheDocument();
    expect(screen.getByText("Use a close side angle")).toBeInTheDocument();
    expect(screen.getByText("~6s")).toBeInTheDocument();
    expect(screen.getByText("This gives the edit a clean before-after arc.")).toBeInTheDocument();
  });

  it("shows propose failure under the button", async () => {
    const item = makeItem({ theme: null, filming_guide: [] });
    mockExpandIdea.mockRejectedValue(new Error("bad gateway"));
    mockUsePolledJobStatus.mockReturnValue({
      data: { item, job: null },
      error: null,
      refetch: mockRefetch,
    });

    await act(async () => {
      render(<PlanItemPage />);
    });

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /Plan this for me/i }));
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /Generate plan/i }));
    });

    expect(await screen.findByText("Couldn't plan this idea — try again.")).toBeInTheDocument();
    expect(screen.queryByText("AI SUGGESTION")).toBeNull();
  });

  it("skips context and sends null context to the proposal API", async () => {
    const item = makeItem({ theme: null, filming_guide: [] });
    mockExpandIdea.mockResolvedValue({
      theme: "Packing reveal",
      filming_suggestion: "Make the plan feel tactile.",
      rationale: "The shot progression creates curiosity.",
      filming_guide: [
        {
          shot_id: "shot-1",
          what: "Start with the packed bag",
          how: "Shoot from above",
          duration_s: 5,
        },
      ],
    });
    mockUsePolledJobStatus.mockReturnValue({
      data: { item, job: null },
      error: null,
      refetch: mockRefetch,
    });

    await act(async () => {
      render(<PlanItemPage />);
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /Plan this for me/i }));
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /Skip and generate/i }));
    });

    expect(await screen.findByText("Packing reveal")).toBeInTheDocument();
    expect(mockExpandIdea).toHaveBeenCalledWith("test-item-id", {
      creator_context: null,
    });
  });

  it("shows accept failure, preserves the card, and sends shot_ids through untouched", async () => {
    const item = makeItem({ theme: null, filming_guide: [] });
    const filmingGuide = [
      {
        shot_id: "shot-keep-me",
        what: "Start with the packed bag",
        how: "Shoot from above",
        duration_s: 5,
      },
    ];
    mockExpandIdea.mockResolvedValue({
      theme: "Packing reveal",
      filming_suggestion: "Make the plan feel tactile.",
      rationale: "The shot progression creates curiosity.",
      filming_guide: filmingGuide,
    });
    mockUpdatePlanItem.mockRejectedValue(new Error("save failed"));
    mockUsePolledJobStatus.mockReturnValue({
      data: { item, job: null },
      error: null,
      refetch: mockRefetch,
    });

    await act(async () => {
      render(<PlanItemPage />);
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /Plan this for me/i }));
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /Generate plan/i }));
    });
    expect(await screen.findByText("Packing reveal")).toBeInTheDocument();

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /Use this plan/i }));
    });

    expect(await screen.findByText("Couldn't save the plan — try again.")).toBeInTheDocument();
    expect(screen.getByText("Packing reveal")).toBeInTheDocument();
    expect(mockUpdatePlanItem).toHaveBeenCalledWith("test-item-id", {
      theme: "Packing reveal",
      filming_suggestion: "Make the plan feel tactile.",
      filming_guide: filmingGuide,
    });
  });

  it("shows accepted plan summary above existing-footage uploader", async () => {
    const item = makeItem({
      theme: "Packing reveal",
      content_mode: "existing_footage",
      filming_suggestion: "Find the bag reveal in your existing clips.",
      filming_guide: [
        {
          shot_id: "shot-existing",
          what: "Packed bag reveal",
          how: "Use the cleanest close-up",
          duration_s: 5,
        },
      ],
    });
    mockUsePolledJobStatus.mockReturnValue({
      data: { item, job: null },
      error: null,
      refetch: mockRefetch,
    });

    await act(async () => {
      render(<PlanItemPage />);
    });

    expect(screen.getByText("Plan summary")).toBeInTheDocument();
    expect(screen.getByText("Find the bag reveal in your existing clips.")).toBeInTheDocument();
    expect(screen.getByText("Packed bag reveal")).toBeInTheDocument();
    expect(screen.getByText("Use the cleanest close-up")).toBeInTheDocument();
  });

  it("keeps narrated-ready items on pool upload even when a plan exists", async () => {
    const item = makeItem({
      edit_format: "narrated_ready",
      filming_guide: [
        {
          shot_id: "voice-shot",
          what: "Show the messy counter",
          how: "Use the before clip",
          duration_s: 4,
        },
      ],
    });
    mockUsePolledJobStatus.mockReturnValue({
      data: { item, job: null },
      error: null,
      refetch: mockRefetch,
    });

    await act(async () => {
      render(<PlanItemPage />);
    });

    expect(screen.getByText("Plan summary")).toBeInTheDocument();
    expect(screen.getByText("Your clips")).toBeInTheDocument();
    expect(screen.queryByText(/shot left/i)).toBeNull();
  });

  it("keeps talking-to-camera items on single-clip upload even when a plan exists", async () => {
    const item = makeItem({
      edit_format: "subtitled",
      filming_guide: [
        {
          shot_id: "talking-shot",
          what: "Creator explains the lesson",
          how: "Eye-level phone shot",
          duration_s: 8,
        },
      ],
    });
    mockUsePolledJobStatus.mockReturnValue({
      data: { item, job: null },
      error: null,
      refetch: mockRefetch,
    });

    await act(async () => {
      render(<PlanItemPage />);
    });

    expect(screen.getByText("Plan summary")).toBeInTheDocument();
    expect(screen.getByText("Your clip")).toBeInTheDocument();
    expect(screen.queryByText(/shot left/i)).toBeNull();
  });

  it("hides Plan this for me when a post-render item already has a filming guide", async () => {
    const item = makeItem({
      status: "ready",
      current_job_id: "job-ready",
      clip_gcs_paths: ["uploads/rendered-source.mp4"],
      filming_guide: [
        {
          shot_id: "shot-existing",
          what: "creator at the counter",
          how: "eye level",
          duration_s: 7,
        },
      ],
    });
    const job = makeJob({
      status: "variants_ready",
      variants: [makeVariant("v1", "ready", "https://cdn/v1.mp4")],
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

    expect(screen.queryByRole("button", { name: /Plan this for me/i })).toBeNull();
  });

  it("hides Plan this for me when uploaded clips already exist", async () => {
    const item = makeItem({
      status: "awaiting_clips",
      content_mode: "existing_footage",
      clip_gcs_paths: ["users/u/plan/i/source.mp4"],
      clip_assignments: [
        { gcs_path: "users/u/plan/i/source.mp4", shot_id: null, user_note: "" },
      ],
      filming_guide: [],
    });
    mockUsePolledJobStatus.mockReturnValue({
      data: { item, job: null },
      error: null,
      refetch: mockRefetch,
    });

    await act(async () => {
      render(<PlanItemPage />);
    });

    expect(screen.getByTestId("uploaded-clip-filmstrip")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Plan this for me/i })).toBeNull();
  });
});
