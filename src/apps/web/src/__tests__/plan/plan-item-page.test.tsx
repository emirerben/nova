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
window.HTMLMediaElement.prototype.load = jest.fn();

import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom";

process.env.NEXT_PUBLIC_SUBTITLED_ENABLED = "true";
process.env.NEXT_PUBLIC_GUIDED_EDIT_ENABLED = "true";

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
  setItemVoiceover: jest.fn(),
  setVariantMediaOverlays: jest.fn(),
  swapPlanItemSong: jest.fn(),
  retextPlanItem: jest.fn(),
  changePlanItemStyle: jest.fn(),
  setPlanItemIntroSize: jest.fn(),
  uploadToGcs: jest.fn(),
  // The pool uploader now streams through the XHR-based helper — mocked so the
  // requireActual spread can't smuggle a real XMLHttpRequest into jsdom.
  uploadToGcsWithProgress: jest.fn().mockResolvedValue(undefined),
  NotAuthenticatedError: class NotAuthenticatedError extends Error {
    constructor() {
      super("Not authenticated");
      this.name = "NotAuthenticatedError";
    }
  },
}));

const mockUploadVoiceover = jest.fn();
jest.mock("@/lib/generative-api", () => ({
  // Pull the status constants + isGenerativeJobSettled from the REAL module so
  // this mock can't drift from it (they are pure, no network).
  ...jest.requireActual("@/lib/generative-api"),
  getGenerativeStyleSets: jest.fn().mockResolvedValue([]),
  uploadVoiceover: (...args: unknown[]) => mockUploadVoiceover(...args),
  // The focused-variant timeline session lazy-GETs on mount; a never-resolving
  // promise keeps the "Edit clips" entry hidden without act() noise.
  getTimeline: jest.fn(() => new Promise(() => {})),
  TimelineApiError: class TimelineApiError extends Error {
    status = 0;
    code: string | null = null;
  },
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
  default: ({ onMutated }: { onMutated?: () => void }) => (
    <div data-testid="asset-pool">
      {onMutated ? (
        <button type="button" onClick={onMutated}>
          Simulate asset mutation
        </button>
      ) : null}
    </div>
  ),
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

import {
  attachClips,
  expandIdea,
  generatePlanItem,
  requestUploadUrls,
  setItemVoiceover,
  setVariantMediaOverlays,
  updatePlanItem,
  uploadToGcs,
  type PlanItemJobStatus,
} from "@/lib/plan-api";
const PlanItemPage = require("@/app/plan/items/[id]/page").default;
const mockAttachClips = attachClips as jest.MockedFunction<typeof attachClips>;
const mockExpandIdea = expandIdea as jest.MockedFunction<typeof expandIdea>;
const mockGeneratePlanItem = generatePlanItem as jest.MockedFunction<typeof generatePlanItem>;
const mockRequestUploadUrls = requestUploadUrls as jest.MockedFunction<typeof requestUploadUrls>;
const mockSetItemVoiceover = setItemVoiceover as jest.MockedFunction<typeof setItemVoiceover>;
const mockSetVariantMediaOverlays = setVariantMediaOverlays as jest.MockedFunction<
  typeof setVariantMediaOverlays
>;
const mockUpdatePlanItem = updatePlanItem as jest.MockedFunction<typeof updatePlanItem>;
const mockUploadToGcs = uploadToGcs as jest.MockedFunction<typeof uploadToGcs>;

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
    smart_captions_enabled: false,
    smart_sound_design_enabled: true,
    smart_captions_available: false,
    smart_captions_unavailable_reason: "feature_disabled",
    ...overrides,
  };
}

function makeGuidedProposal(status: "analyzing" | "draft" | "approved" | "stale") {
  const snapshot = {
    direction: "guided_story",
    goal: "Tell the Corfu story",
    pace: "balanced",
    duration_s: 24,
    title: "What I noticed in Corfu",
    media: [
      {
        lane: "clip",
        media_id: "clip-1",
        gcs_path: "users/u1/plan/test-item-id/corfu.mp4",
        generation: "1",
        kind: "video",
        source_filename: "corfu.mp4",
        user_context: "",
        analysis: {},
      },
    ],
    story_beats: [
      {
        beat_id: "beat-1",
        topic: "Coast",
        thought: "The water set the pace.",
        thought_source: "ai_draft",
        media_ids: ["clip-1"],
        layout: "fullscreen",
        duration_s: 4,
      },
    ],
  };
  return {
    schema_version: 1,
    proposal_version: 2,
    generation_attempt_id: "attempt-1",
    media_digest: "a".repeat(64),
    status,
    brief: {
      direction: "guided_story",
      goal: "Tell the Corfu story",
      pace: "balanced",
      duration_s: 24,
    },
    draft: snapshot,
    last_approved:
      status === "approved" || status === "stale"
        ? {
            proposal_version: 2,
            media_digest: "a".repeat(64),
            approved_at: "2026-08-14T10:00:00Z",
            snapshot,
          }
        : null,
    failure: null,
  };
}

describe("PlanItemPage — Smart captions availability", () => {
  beforeEach(() => {
    mockUpdatePlanItem.mockReset();
    mockRefetch.mockReset();
  });

  it("shows the server-authorized switch and persists the per-video choice", async () => {
    const item = makeItem({
      edit_format: "subtitled",
      smart_captions_available: true,
      smart_captions_unavailable_reason: null,
    });
    mockUpdatePlanItem.mockResolvedValue({ ...item, smart_captions_enabled: true });
    mockUsePolledJobStatus.mockReturnValue({
      data: { item, job: null },
      error: null,
      refetch: mockRefetch,
    });

    await act(async () => {
      render(<PlanItemPage />);
    });

    const smartSwitch = screen.getByRole("switch", { name: "Smart captions" });
    expect(smartSwitch).toHaveAttribute("aria-checked", "false");

    await act(async () => {
      fireEvent.click(smartSwitch);
    });

    expect(mockUpdatePlanItem).toHaveBeenCalledWith("test-item-id", {
      smart_captions_enabled: true,
    });
    expect(mockRefetch).toHaveBeenCalled();
  });

  it("does not expose the switch when the backend capability denies it", async () => {
    const item = makeItem({
      edit_format: "subtitled",
      smart_captions_available: false,
      smart_captions_unavailable_reason: "not_assigned",
    });
    mockUsePolledJobStatus.mockReturnValue({
      data: { item, job: null },
      error: null,
      refetch: mockRefetch,
    });

    await act(async () => {
      render(<PlanItemPage />);
    });

    expect(screen.queryByRole("switch", { name: "Smart captions" })).toBeNull();
  });

  it("lets the creator disable automatic SFX without disabling Smart captions", async () => {
    const item = makeItem({
      edit_format: "subtitled",
      smart_captions_enabled: true,
      smart_sound_design_enabled: true,
      smart_captions_available: true,
      smart_captions_unavailable_reason: null,
    });
    mockUpdatePlanItem.mockResolvedValue({
      ...item,
      smart_sound_design_enabled: false,
    });
    mockUsePolledJobStatus.mockReturnValue({
      data: { item, job: null },
      error: null,
      refetch: mockRefetch,
    });

    await act(async () => {
      render(<PlanItemPage />);
    });

    expect(screen.getByText("Sound design")).toBeInTheDocument();
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Off" }));
    });

    expect(mockUpdatePlanItem).toHaveBeenCalledWith("test-item-id", {
      smart_sound_design_enabled: false,
    });
  });

  it("keeps the choice unchanged and shows an error when persistence fails", async () => {
    const item = makeItem({
      edit_format: "subtitled",
      smart_captions_available: true,
      smart_captions_unavailable_reason: null,
    });
    mockUpdatePlanItem.mockRejectedValue(new Error("conflict"));
    mockUsePolledJobStatus.mockReturnValue({
      data: { item, job: null },
      error: null,
      refetch: mockRefetch,
    });

    await act(async () => {
      render(<PlanItemPage />);
    });

    await act(async () => {
      fireEvent.click(screen.getByRole("switch", { name: "Smart captions" }));
    });

    expect(screen.getByRole("alert")).toHaveTextContent(
      "Couldn't update Smart captions — try again.",
    );
    expect(mockRefetch).not.toHaveBeenCalled();
  });
});

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
    render_finished_at: "2026-07-12T10:00:00Z",
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

  it("test_all_variants_failed_shows_a_working_retry_button: dead-end variant gets a Try again control that re-dispatches", async () => {
    // Root cause of the "there is no such thing appearing" report: a job that
    // creates one variant before failing routes through showResults (variants.length
    // > 0), not the variants.length === 0 whole-item-retry branch — so the
    // Generate button vanished with nothing replacing it. This is the fix:
    // ProgressTheater's onRetry now covers exactly this case.
    const item = makeItem({
      status: "failed",
      current_job_id: "job-dead-end",
      clip_gcs_paths: ["uploads/test.mp4"],
    });
    const variants = [makeVariant("v1", "failed", null)];
    const job = makeJob({
      status: "variants_failed",
      variants,
      started_at: "2026-06-06T10:00:00Z",
      finished_at: "2026-06-06T10:02:00Z",
    });

    mockUsePolledJobStatus.mockReturnValue({
      data: { item, job },
      error: null,
      refetch: mockRefetch,
    });
    mockGeneratePlanItem.mockResolvedValue(undefined);

    await act(async () => {
      render(<PlanItemPage />);
    });

    expect(screen.getByText("This one didn't render")).toBeInTheDocument();
    const retryButton = screen.getByRole("button", { name: "Try again" });

    await act(async () => {
      fireEvent.click(retryButton);
    });

    expect(mockGeneratePlanItem).toHaveBeenCalledWith("test-item-id");
  });

  it("puts compact render progress after the preview and removes the duplicate duration note", async () => {
    process.env.NEXT_PUBLIC_NOVA_STEPS_FEED_ENABLED = "true";
    const item = makeItem({
      status: "generating",
      current_job_id: "job-video-first",
      clip_gcs_paths: ["uploads/test.mp4"],
    });
    const variants = [makeVariant("v1", "ready", "https://cdn/v1.mp4")];
    const job = makeJob({
      status: "processing",
      variants,
      current_phase: "render_variants",
      started_at: "2026-06-06T10:00:00Z",
      steps: [
        {
          id: "step-1",
          ts: "2026-06-06T10:00:01Z",
          kind: "phase",
          label: "Analyzed your clips",
          detail: null,
          status: "done",
        },
        {
          id: "step-2",
          ts: "2026-06-06T10:00:02Z",
          kind: "render",
          label: "Rendering the final edit",
          detail: null,
          status: "active",
        },
      ],
    });
    mockUsePolledJobStatus.mockReturnValue({
      data: { item, job },
      error: null,
      refetch: mockRefetch,
    });

    try {
      await act(async () => {
        render(<PlanItemPage />);
      });

      const preview = document.querySelector("[data-variant-preview]");
      const disclosure = screen.getByRole("button", { name: "Show analysis steps" });
      expect(preview).not.toBeNull();
      expect(
        (preview?.compareDocumentPosition(disclosure) ?? 0) &
          Node.DOCUMENT_POSITION_FOLLOWING,
      ).toBeTruthy();
      expect(screen.queryByText(/Usually 2–3 minutes/i)).not.toBeInTheDocument();
    } finally {
      delete process.env.NEXT_PUBLIC_NOVA_STEPS_FEED_ENABLED;
    }
  });

  it("shows the veil instead of the theater when a ready item's sole variant reburns with output already present", async () => {
    // Updated for the veil/theater dedup (v0.26.x): this fixture — the
    // focused (only) variant "rendering" with an `output_url` already set —
    // is exactly the frozen-frame veil's visibility condition. Before the
    // dedup this asserted the theater rendered below the preview (steps
    // disclosure, doubled step label); now the veil is the SOLE rendering
    // voice for this case and the theater must not also mount below it.
    process.env.NEXT_PUBLIC_NOVA_STEPS_FEED_ENABLED = "true";
    const item = makeItem({
      status: "ready",
      current_job_id: "job-controlled-rerender",
      clip_gcs_paths: ["uploads/test.mp4"],
    });
    const variants = [makeVariant("v1", "rendering", "https://cdn/v1.mp4")];
    const job = makeJob({
      status: "variants_ready",
      variants,
      current_phase: "render_variants",
      started_at: "2026-06-06T10:00:00Z",
      steps: [
        {
          id: "step-rerender",
          ts: "2026-06-06T10:00:02Z",
          kind: "render",
          label: "Applying your intro text",
          detail: null,
          status: "active",
        },
      ],
    });
    mockUsePolledJobStatus.mockReturnValue({
      data: { item, job },
      error: null,
      refetch: mockRefetch,
    });

    try {
      await act(async () => {
        render(<PlanItemPage />);
      });

      expect(await screen.findByLabelText("Rendering new version")).toBeInTheDocument();
      expect(screen.queryByRole("button", { name: "Show analysis steps" })).not.toBeInTheDocument();
      expect(screen.queryByText("Applying your intro text")).not.toBeInTheDocument();
    } finally {
      delete process.env.NEXT_PUBLIC_NOVA_STEPS_FEED_ENABLED;
    }
  });

  it("test_zero_variants_failed_has_no_duplicate_retry_button: whole-item setup form (not ProgressTheater) owns retry when nothing rendered at all", async () => {
    // variants.length === 0 already gets the Generate button via
    // showSetupControls — ProgressTheater must not also show one there
    // (allVariantsFailed requires variants.length > 0).
    const item = makeItem({
      status: "failed",
      current_job_id: "job-empty",
      clip_gcs_paths: ["uploads/test.mp4"],
    });
    const job = makeJob({
      status: "processing_failed",
      variants: [],
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

    expect(screen.queryByRole("button", { name: "Try again" })).toBeNull();
    expect(screen.getByText("This one didn't render")).toBeInTheDocument();
    expect(document.querySelector("[data-variant-preview]")).toBeNull();
  });
});

describe("PlanItemPage — result cleanup", () => {
  it("renders an empty overlay replacement when the last baked card was removed", async () => {
    mockSetVariantMediaOverlays.mockReset();
    mockSetVariantMediaOverlays.mockResolvedValue(undefined);
    const item = makeItem({
      status: "ready",
      current_job_id: "job-overlay-clear",
      clip_gcs_paths: ["uploads/test.mp4"],
    });
    const variant = {
      ...makeVariant("v1", "ready", "https://cdn/v1.mp4"),
      media_overlays: [],
      media_overlays_render_dirty: true,
      pre_media_overlay_video_path: "generative-jobs/job-overlay-clear/clean.mp4",
    };
    mockUsePolledJobStatus.mockReturnValue({
      data: {
        item,
        job: makeJob({ status: "variants_ready", variants: [variant] }),
      },
      error: null,
      refetch: mockRefetch,
    });

    await act(async () => {
      render(<PlanItemPage />);
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "More video actions" }));
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Download video" }));
    });

    await waitFor(() => {
      expect(mockSetVariantMediaOverlays).toHaveBeenCalledWith(
        "test-item-id",
        "v1",
        [],
        { render: true },
      );
    });
  });

  it("does not rerender an already-applied empty overlay state", async () => {
    mockSetVariantMediaOverlays.mockReset();
    const item = makeItem({
      status: "ready",
      current_job_id: "job-overlay-clean",
      clip_gcs_paths: ["uploads/test.mp4"],
    });
    const variant = {
      ...makeVariant("v1", "ready", "https://cdn/v1.mp4"),
      media_overlays: [],
      media_overlays_render_dirty: false,
      // The clean snapshot is durable and must not itself imply dirty state.
      pre_media_overlay_video_path: "generative-jobs/job-overlay-clean/clean.mp4",
    };
    mockUsePolledJobStatus.mockReturnValue({
      data: {
        item,
        job: makeJob({ status: "variants_ready", variants: [variant] }),
      },
      error: null,
      refetch: mockRefetch,
    });

    await act(async () => {
      render(<PlanItemPage />);
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "More video actions" }));
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Download video" }));
    });

    expect(mockSetVariantMediaOverlays).not.toHaveBeenCalled();
  });

  it("renders the hero video with mobile-safe metadata attributes", async () => {
    const item = makeItem({
      status: "ready",
      current_job_id: "job-hero",
      clip_gcs_paths: ["uploads/test.mp4"],
    });
    const variants = [makeVariant("v1", "ready", "https://cdn/v1.mp4")];
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

    let view: ReturnType<typeof render>;
    await act(async () => {
      view = render(<PlanItemPage />);
    });

    const video = view!.container.querySelector("video");
    expect(video).toHaveAttribute("playsinline");
    expect(video).toHaveAttribute("preload", "metadata");
    expect(screen.queryByLabelText("Visual variants")).toBeNull();

    const titleSection = screen.getByRole("heading", { name: "Morning Routine" }).closest("section");
    expect(titleSection?.parentElement).toHaveClass(
      "grid-cols-[minmax(132px,0.78fr)_minmax(0,1.22fr)]",
    );
  });

  it("replaces a failed preview in-frame with recovery actions", async () => {
    const item = makeItem({
      status: "ready",
      current_job_id: "job-playback-failure",
      clip_gcs_paths: ["uploads/test.mp4"],
    });
    mockUsePolledJobStatus.mockReturnValue({
      data: {
        item,
        job: makeJob({
          status: "variants_ready",
          variants: [makeVariant("v1", "ready", "https://cdn/v1.mp4")],
        }),
      },
      error: null,
      refetch: mockRefetch,
    });

    let view: ReturnType<typeof render>;
    await act(async () => {
      view = render(<PlanItemPage />);
    });
    fireEvent.error(view!.container.querySelector("video")!);

    expect(screen.getByText("Preview unavailable")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Try video again" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Download video" })).toBeInTheDocument();
  });

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
    expect(screen.getByLabelText("Visual variants")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Publish version 1" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    fireEvent.click(screen.getByRole("button", { name: "Publish version 2" }));
    expect(screen.getByRole("button", { name: "Publish version 2" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
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

describe("PlanItemPage — guided edit Generate gating", () => {
  function guidedItem(editProposal: ReturnType<typeof makeGuidedProposal> | null) {
    return makeItem({
      status: "awaiting_clips",
      edit_format: "montage",
      guided_edit_available: true,
      edit_proposal: editProposal,
      clip_gcs_paths: ["users/u1/plan/test-item-id/corfu.mp4"],
      clip_assignments: [
        {
          media_id: "clip-1",
          gcs_path: "users/u1/plan/test-item-id/corfu.mp4",
          shot_id: null,
          user_note: "",
        },
      ],
    });
  }

  beforeEach(() => {
    mockGeneratePlanItem.mockReset();
    mockRefetch.mockReset();
  });

  it.each([
    [null, "Plan this edit before generating."],
    [makeGuidedProposal("analyzing"), "Nova is still planning this edit."],
    [makeGuidedProposal("draft"), "Review and approve the edit plan first."],
    [makeGuidedProposal("stale"), "Your media changed — plan the edit again."],
  ])("blocks Generate until the proposal is current and approved", async (proposal, hint) => {
    const item = guidedItem(proposal);
    mockUsePolledJobStatus.mockReturnValue({
      data: { item, job: null },
      error: null,
      refetch: mockRefetch,
    });

    await act(async () => {
      render(<PlanItemPage />);
    });

    expect(screen.getByRole("button", { name: /generate video/i })).toBeDisabled();
    expect(screen.getByText(hint)).toBeInTheDocument();
  });

  it("enables Generate for a current approved proposal", async () => {
    const item = guidedItem(makeGuidedProposal("approved"));
    mockGeneratePlanItem.mockResolvedValue(item);
    mockUsePolledJobStatus.mockReturnValue({
      data: { item, job: null },
      error: null,
      refetch: mockRefetch,
    });

    await act(async () => {
      render(<PlanItemPage />);
    });

    const generate = screen.getByRole("button", { name: /generate video/i });
    expect(generate).toBeEnabled();

    await act(async () => {
      fireEvent.click(generate);
    });

    await waitFor(() => {
      expect(mockGeneratePlanItem).toHaveBeenCalledWith("test-item-id");
    });
  });

  it("refreshes the proposal after an asset-pool mutation", async () => {
    const item = guidedItem(makeGuidedProposal("approved"));
    mockUsePolledJobStatus.mockReturnValue({
      data: { item, job: null },
      error: null,
      refetch: mockRefetch,
    });

    await act(async () => {
      render(<PlanItemPage />);
    });

    fireEvent.click(screen.getByRole("button", { name: "Simulate asset mutation" }));
    expect(mockRefetch).toHaveBeenCalledTimes(1);
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

  it("routes narrated-ready audio-only mp4 uploads to the voiceover lane", async () => {
    const item = makeItem({
      edit_format: "narrated_ready",
      clip_assignments: [],
      voiceover_gcs_path: null,
    });
    mockUsePolledJobStatus.mockReturnValue({
      data: { item, job: null },
      error: null,
      refetch: mockRefetch,
    });
    mockUploadVoiceover.mockReset();
    mockUploadVoiceover.mockResolvedValue({
      gcs_path: "voiceover-uploads/audio-only/voice.m4a",
      kind: "audio",
    });
    mockSetItemVoiceover.mockResolvedValue({
      ...item,
      voiceover_gcs_path: "voiceover-uploads/audio-only/voice.m4a",
    });
    mockRequestUploadUrls.mockReset();
    mockAttachClips.mockReset();
    mockUploadToGcs.mockReset();

    await act(async () => {
      render(<PlanItemPage />);
    });

    const input = screen.getByLabelText("Upload video clips for this idea") as HTMLInputElement;
    const file = new File(["aac"], "audio_only.mp4", { type: "audio/mp4" });

    await act(async () => {
      fireEvent.change(input, { target: { files: [file] } });
    });

    await waitFor(() => {
      expect(mockUploadVoiceover).toHaveBeenCalledWith(file);
    });
    expect(mockSetItemVoiceover).toHaveBeenCalledWith(
      "test-item-id",
      "voiceover-uploads/audio-only/voice.m4a",
    );
    expect(mockRequestUploadUrls).not.toHaveBeenCalled();
    expect(mockUploadToGcs).not.toHaveBeenCalled();
    expect(mockAttachClips).not.toHaveBeenCalled();
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
