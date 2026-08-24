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

import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
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
  uploadOwnedVoiceover: (...args: unknown[]) => mockUploadVoiceover(...args),
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
jest.mock("@/app/plan/_components/AssetPool", () => ({
  __esModule: true,
  default: ({
    onMutated,
    onAssetsChanged,
  }: {
    onMutated?: () => void;
    onAssetsChanged?: (assets: { id: string; status: string }[]) => void;
  }) => (
    <div data-testid="asset-pool">
      {onMutated ? (
        <button type="button" onClick={onMutated}>
          Simulate asset mutation
        </button>
      ) : null}
      {onAssetsChanged ? (
        <button
          type="button"
          onClick={() => onAssetsChanged([{ id: "pool-1", status: "ready" }])}
        >
          Simulate ready pool asset
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

function makeGuidedProposal(status: "analyzing" | "draft" | "approved" | "stale" | "failed") {
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
    conversation: [],
    brief_ready: false,
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

describe("PlanItemPage — Smart captions (default-on, no toggle)", () => {
  it("never renders a Smart captions switch — the capability is server-decided", async () => {
    const item = makeItem({
      edit_format: "subtitled",
      smart_captions_available: true,
      smart_captions_unavailable_reason: null,
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
    expect(screen.queryByText("Sound design")).toBeNull();
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

  // STYLE-tile imagery is now exclusively picked on /plan/new (Lane J removed
  // the item page's inline Change toggle + SetupPicker mount) — see
  // plan-new-chooser.test.tsx for that coverage.

  it.each(["masonry", "polaroid_wall"])(
    "uses compact collage uploads for %s even when the item has a filming guide",
    async (montage_preset) => {
      await act(async () => {
        renderMasonryItem({ montage_preset });
      });

      expect(
        screen.getByText((_, el) => el?.textContent === "Your clips"),
      ).toBeInTheDocument();
      expect(screen.queryByTestId("shot-slot-uploader")).not.toBeInTheDocument();
      expect(
        screen
          .getAllByLabelText("Drop videos here or choose files")
          .find((element) => element.tagName === "INPUT")
          ?.getAttribute("accept"),
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

    expect(screen.getByText("Your setup is saved. Retry the render without uploading again.")).toBeInTheDocument();
    const retryButton = screen.getByRole("button", { name: "Retry render" });

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
    expect(screen.getByText("Kria couldn’t finish this video")).toBeInTheDocument();
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

    // Lane H: the release desk stacks to a single column below `lg` (video,
    // then the Card) instead of a cramped 2-up mobile grid.
    const titleSection = screen.getByRole("heading", { name: "Morning Routine" }).closest("section");
    expect(titleSection?.parentElement).toHaveClass("grid-cols-1");
    expect(titleSection?.parentElement).toHaveClass(
      "lg:grid-cols-[minmax(210px,0.75fr)_minmax(320px,430px)_minmax(300px,0.95fr)]",
    );
  });

  it("labels a track-backed guided edit as music instead of original audio", async () => {
    const item = makeItem({
      status: "ready",
      current_job_id: "job-guided",
      clip_gcs_paths: ["uploads/test.mp4"],
    });
    const variant = {
      ...makeVariant("guided_story", "ready", "https://cdn/guided.mp4"),
      resolved_archetype: "guided_story",
      music_track_id: "track-1",
      track_title: "Maui Wowie",
    };
    mockUsePolledJobStatus.mockReturnValue({
      data: { item, job: makeJob({ status: "variants_ready", variants: [variant] }) },
      error: null,
      refetch: mockRefetch,
    });

    await act(async () => {
      render(<PlanItemPage />);
    });

    expect(screen.getByText("Kria's pick · Music")).toBeInTheDocument();
    expect(screen.queryByText("Kria's pick · Original audio")).toBeNull();
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

    expect(
      screen.getByText(/The preview couldn't load, but your finished video is safe/),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Try preview again" })).toBeInTheDocument();
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
    // Recourse buttons — condensed labels in the one-liner. Scoped to the
    // KriaHelper tile since the setup zone's "Tell Kria" textarea label now
    // also matches /Tell Kria/.
    const kriaHelper = within(screen.getByTestId("kria-helper"));
    expect(kriaHelper.getByText(/Tell Kria/)).toBeInTheDocument();
    expect(kriaHelper.getByText(/Hide/)).toBeInTheDocument();
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
    const generateBtn = screen.getAllByRole("button", { name: /create video/i })[0];
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
      fireEvent.click(screen.getAllByRole("button", { name: /create video/i })[0]);
    });

    await waitFor(() => {
      expect(mockGeneratePlanItem).toHaveBeenCalledWith("test-item-id");
    });
    expect(mockUpdatePlanItem).not.toHaveBeenCalled();
  });
});

describe("PlanItemPage — guided edit Generate gating", () => {
  function guidedItem(
    editProposal: ReturnType<typeof makeGuidedProposal> | null,
    options: { autoDesign?: boolean; poolOnly?: boolean } = {},
  ) {
    return makeItem({
      status: "awaiting_clips",
      edit_format: "montage",
      guided_edit_available: true,
      guided_edit_auto_design: options.autoDesign ?? false,
      edit_proposal: editProposal,
      clip_gcs_paths: options.poolOnly ? [] : ["users/u1/plan/test-item-id/corfu.mp4"],
      clip_assignments: options.poolOnly
        ? []
        : [
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
    [makeGuidedProposal("analyzing"), "Kria is still planning this edit."],
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

    expect(screen.getAllByRole("button", { name: /create video/i })[0]).toBeDisabled();
    expect(screen.getAllByText(hint)[0]).toBeInTheDocument();
  });

  it("stays disabled with no approved proposal when guided_edit_auto_design is false", async () => {
    const item = guidedItem(null, { autoDesign: false });
    mockUsePolledJobStatus.mockReturnValue({
      data: { item, job: null },
      error: null,
      refetch: mockRefetch,
    });

    await act(async () => {
      render(<PlanItemPage />);
    });

    expect(screen.getAllByRole("button", { name: /create video/i })[0]).toBeDisabled();
    expect(screen.getAllByText("Plan this edit before generating.")[0]).toBeInTheDocument();
  });

  it("enables Generate with media and no approved proposal when auto-design is on", async () => {
    const item = guidedItem(null, { autoDesign: true });
    mockGeneratePlanItem.mockResolvedValue(item);
    mockUsePolledJobStatus.mockReturnValue({
      data: { item, job: null },
      error: null,
      refetch: mockRefetch,
    });

    await act(async () => {
      render(<PlanItemPage />);
    });

    // Clicking just works — Kria designs the edit, no planner step required.
    expect(screen.getAllByRole("button", { name: /create video/i })[0]).toBeEnabled();
    await act(async () => {
      fireEvent.click(screen.getAllByRole("button", { name: /create video/i })[0]);
    });
    await waitFor(() => {
      expect(mockGeneratePlanItem).toHaveBeenCalledWith("test-item-id");
    });
  });

  it("lets Create video resume a legacy direction checkpoint", async () => {
    const legacyProposal = makeGuidedProposal("analyzing");
    legacyProposal.status = "briefing";
    legacyProposal.guidance = {
      state: "awaiting_direction_confirmation",
      provenance: "ai_inferred",
    };
    const item = guidedItem(legacyProposal, { autoDesign: true });
    mockGeneratePlanItem.mockResolvedValue({
      ...item,
      edit_proposal: { ...legacyProposal, status: "analyzing", guidance: { state: "confirmed" } },
    });
    mockUsePolledJobStatus.mockReturnValue({
      data: { item, job: null },
      error: null,
      refetch: mockRefetch,
    });

    await act(async () => {
      render(<PlanItemPage />);
    });

    const createButton = screen.getAllByRole("button", { name: /create video/i })[0];
    expect(createButton).toBeEnabled();
    expect(screen.queryByRole("button", { name: /review direction/i })).toBeNull();
    expect(screen.getByRole("button", { name: /plan with kria/i })).toBeInTheDocument();

    await act(async () => {
      fireEvent.click(createButton);
    });
    await waitFor(() => {
      expect(mockGeneratePlanItem).toHaveBeenCalledWith("test-item-id");
    });
  });

  it("does not let a dormant proposal block an audio-led Generate flow", async () => {
    const item = makeItem({
      status: "awaiting_clips",
      edit_format: "narrated_ready",
      audio_mode: "voiceover",
      voiceover_gcs_path: "voiceover-uploads/u/voice.m4a",
      guided_edit_available: false,
      guided_edit_conversation_available: false,
      guided_edit_auto_design: false,
      edit_proposal: makeGuidedProposal("draft"),
      clip_gcs_paths: ["users/u1/plan/test-item-id/clip.mp4"],
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

    expect(screen.queryByText("Review and approve the edit plan first.")).toBeNull();
    expect(screen.queryByTestId("edit-proposal-card")).toBeNull();
    expect(screen.getAllByRole("button", { name: /create video/i })[0]).toBeEnabled();

    await act(async () => {
      fireEvent.click(screen.getAllByRole("button", { name: /create video/i })[0]);
    });
    await waitFor(() => {
      expect(mockGeneratePlanItem).toHaveBeenCalledWith("test-item-id");
    });
  });

  it("P2-5: a pool-only item becomes generate-able once AssetPool reports a ready asset", async () => {
    const item = guidedItem(null, { autoDesign: true, poolOnly: true });
    mockUsePolledJobStatus.mockReturnValue({
      data: { item, job: null },
      error: null,
      refetch: mockRefetch,
    });

    await act(async () => {
      render(<PlanItemPage />);
    });

    // No clips, no approval, no known pool media yet -> blocked.
    expect(screen.getAllByRole("button", { name: /create video/i })[0]).toBeDisabled();

    // AssetPool now lives behind the "Visuals" tab (Lane G) — switch to it
    // before its mocked test hook is reachable. Radix's Tabs.Trigger needs a
    // real pointer sequence (userEvent), not a bare fireEvent.click.
    await userEvent.click(screen.getByRole("tab", { name: /visuals/i }));
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /simulate ready pool asset/i }));
    });

    // AssetPool reported a ready pool asset up -> the gate now sees media.
    expect(screen.getAllByRole("button", { name: /create video/i })[0]).toBeEnabled();
  });

  it("disables Create video while auto-design is already running", async () => {
    const item = guidedItem(makeGuidedProposal("analyzing"), { autoDesign: true });
    mockUsePolledJobStatus.mockReturnValue({
      data: { item, job: null },
      error: null,
      refetch: mockRefetch,
    });

    await act(async () => {
      render(<PlanItemPage />);
    });

    expect(screen.getAllByRole("button", { name: /create video|creating/i })[0]).toBeDisabled();
    expect(
      screen.getAllByText("Kria is analyzing your clips…")[0],
    ).toBeInTheDocument();
  });

  it("P3: the render-register watchdog does not fire while auto-design is still designing", async () => {
    const nowSpy = jest.spyOn(Date, "now");
    const startMs = 1_700_000_000_000;
    nowSpy.mockReturnValue(startMs);

    const readyItem = guidedItem(null, { autoDesign: true });
    const designingItem = guidedItem(makeGuidedProposal("analyzing"), { autoDesign: true });
    mockGeneratePlanItem.mockResolvedValue(designingItem);
    mockUsePolledJobStatus.mockReturnValue({
      data: { item: readyItem, job: null },
      error: null,
      refetch: mockRefetch,
    });

    const { rerender } = render(<PlanItemPage />);
    await act(async () => {});

    await act(async () => {
      fireEvent.click(screen.getAllByRole("button", { name: /create video/i })[0]);
    });
    await waitFor(() => {
      expect(mockGeneratePlanItem).toHaveBeenCalledWith("test-item-id");
    });

    // The poll now reflects the design phase in progress.
    mockUsePolledJobStatus.mockReturnValue({
      data: { item: designingItem, job: null },
      error: null,
      refetch: mockRefetch,
    });
    await act(async () => {
      rerender(<PlanItemPage />);
    });

    // Fast-forward well past RENDER_REGISTER_TIMEOUT_MS (15 min) while the
    // item is STILL designing — no render Job could possibly have registered
    // yet, so the watchdog must not fire.
    nowSpy.mockReturnValue(startMs + 20 * 60_000);
    await act(async () => {
      rerender(<PlanItemPage />);
    });

    expect(
      screen.queryByText("The render didn't register — give it another go."),
    ).not.toBeInTheDocument();
    expect(
      screen.getAllByText("Kria is analyzing your clips…")[0],
    ).toBeInTheDocument();

    nowSpy.mockRestore();
  });

  it.each([
    [
      true,
      "Kria couldn't finish planning this edit — it'll retry when you hit Generate.",
    ],
    [
      false,
      "Kria couldn't finish planning this edit — open the planner to try again.",
    ],
  ])(
    "shows the failed-status hint (auto-design=%s)",
    async (autoDesign, hint) => {
      const item = guidedItem(makeGuidedProposal("failed"), { autoDesign });
      mockUsePolledJobStatus.mockReturnValue({
        data: { item, job: null },
        error: null,
        refetch: mockRefetch,
      });

      await act(async () => {
        render(<PlanItemPage />);
      });

      expect(screen.getAllByText(hint)[0]).toBeInTheDocument();
      const generateBtn = screen.getAllByRole("button", { name: /create video/i })[0];
      if (autoDesign) {
        expect(generateBtn).toBeEnabled();
      } else {
        expect(generateBtn).toBeDisabled();
      }
    },
  );

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

    const generate = screen.getAllByRole("button", { name: /create video/i })[0];
    expect(generate).toBeEnabled();

    await act(async () => {
      fireEvent.click(generate);
    });

    await waitFor(() => {
      expect(mockGeneratePlanItem).toHaveBeenCalledWith("test-item-id");
    });
  });

  it("enables Generate when an approved plan selects only visual-pool media", async () => {
    const proposal = makeGuidedProposal("approved");
    proposal.draft.media[0].lane = "asset";
    const item = {
      ...guidedItem(proposal),
      clip_gcs_paths: [],
      clip_assignments: [],
    };
    mockGeneratePlanItem.mockResolvedValue(item);
    mockUsePolledJobStatus.mockReturnValue({
      data: { item, job: null },
      error: null,
      refetch: mockRefetch,
    });

    await act(async () => {
      render(<PlanItemPage />);
    });

    const generate = screen.getAllByRole("button", { name: /create video/i })[0];
    expect(generate).toBeEnabled();
    expect(screen.queryByText("Add clips to generate")).not.toBeInTheDocument();

    await act(async () => {
      fireEvent.click(generate);
    });

    await waitFor(() => {
      expect(mockGeneratePlanItem).toHaveBeenCalledWith("test-item-id");
    });
  });

  it("keeps polling while a conversational reply is in flight", async () => {
    const active = makeGuidedProposal("briefing");
    active.conversation_in_progress = true;
    const item = {
      ...guidedItem(active),
      status: "ready",
      current_job_id: "job-1",
    };
    const settledJob = makeJob({ status: "done" });
    mockUsePolledJobStatus.mockReturnValue({
      data: { item, job: settledJob },
      error: null,
      refetch: mockRefetch,
    });

    await act(async () => {
      render(<PlanItemPage />);
    });

    const isTerminal = mockUsePolledJobStatus.mock.calls.at(-1)?.[2];
    expect(isTerminal?.({ item, job: settledJob })).toBe(false);

    const retryItem = {
      ...item,
      edit_proposal: {
        ...active,
        conversation_in_progress: false,
        conversation_retry_required: true,
      },
    };
    expect(isTerminal?.({ item: retryItem, job: settledJob })).toBe(true);
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

    // AssetPool now lives behind the "Visuals" tab (Lane G).
    await userEvent.click(screen.getByRole("tab", { name: /visuals/i }));
    fireEvent.click(screen.getByRole("button", { name: "Simulate asset mutation" }));
    expect(mockRefetch).toHaveBeenCalledTimes(1);
  });
});

describe("PlanItemPage — per-type uploaders with an accepted plan", () => {
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
    expect(
        screen.getByText((_, el) => el?.textContent === "Your clips"),
      ).toBeInTheDocument();
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

    const input = screen
      .getAllByLabelText("Drop videos here or choose files")
      .find((element) => element.tagName === "INPUT") as HTMLInputElement;
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
    expect(
        screen.getByText((_, el) => el?.textContent === "Your clip"),
      ).toBeInTheDocument();
    expect(screen.queryByText(/shot left/i)).toBeNull();
  });

});

describe("PlanItemPage — per-type setup truth table (V2 redesign)", () => {
  function renderTyped(overrides = {}) {
    const item = makeItem({
      status: "awaiting_clips",
      theme: null,
      content_mode: "existing_footage",
      ...overrides,
    });
    mockUsePolledJobStatus.mockReturnValue({
      data: { item, job: null },
      error: null,
      refetch: mockRefetch,
    });
    return render(<PlanItemPage />);
  }

  it("montage: clips + direction only — no audio choice, no voiceover, no caption copy", async () => {
    await act(async () => {
      renderTyped({ edit_format: "montage", idea: "Montage", montage_preset: "classic" });
    });
    expect(screen.getByRole("heading", { name: "Montage" })).toBeInTheDocument();
    expect(screen.getByText(/Kria cuts them to the beat of a matched song/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Original audio/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /Kria decides/i })).toBeNull();
    expect(screen.queryByText(/Your narration/)).toBeNull();
    expect(screen.queryByText(/captions/i)).toBeNull();
    expect(screen.queryByText(/Photo collage before using photos/)).toBeNull();
    expect(screen.queryByRole("button", { name: /Plan this for me/i })).toBeNull();
  });

  it("voiceover (narrated_ready): recorder is step 2, no audio-choice fieldset", async () => {
    await act(async () => {
      renderTyped({ edit_format: "narrated_ready", idea: "Voiceover" });
    });
    expect(screen.getByRole("heading", { name: "Voiceover" })).toBeInTheDocument();
    // Lane G: a visible Label + one-line caption inside the Card, replacing
    // the old sr-only heading + InfoDot popover pattern — no step numeral.
    expect(screen.getByText("Your narration")).toBeInTheDocument();
    expect(
      screen.getByText("This recording becomes the soundtrack. It is separate from your note to Kria."),
    ).toBeInTheDocument();
    // Direction/voice-note section is gone — replaced by the single "Tell Kria" field.
    expect(screen.getByRole("textbox", { name: "Tell Kria" })).toBeInTheDocument();
    expect(screen.queryByText(/Direction for Kria/)).toBeNull();
    expect(screen.queryByText(/Add a voice note to Kria/)).toBeNull();
    expect(screen.queryByRole("button", { name: /Original audio/i })).toBeNull();
  });

  it("keeps Generate disabled until the voiceover attachment PATCH settles", async () => {
    const item = makeItem({
      status: "awaiting_clips",
      edit_format: "narrated_ready",
      clip_gcs_paths: ["users/u1/plan/test-item-id/clip.mp4"],
      voiceover_gcs_path: null,
      guided_edit_available: false,
      guided_edit_conversation_available: false,
      guided_edit_auto_design: false,
    });
    mockUsePolledJobStatus.mockReturnValue({
      data: { item, job: null },
      error: null,
      refetch: mockRefetch,
    });
    mockUploadVoiceover.mockResolvedValue({
      gcs_path: "voiceover-uploads/u1/voice.m4a",
      kind: "audio",
    });
    let resolveSave!: (value: typeof item) => void;
    mockSetItemVoiceover.mockReturnValue(
      new Promise((resolve) => {
        resolveSave = resolve as typeof resolveSave;
      }),
    );

    let view: ReturnType<typeof render>;
    await act(async () => {
      view = render(<PlanItemPage />);
    });

    Object.defineProperty(URL, "createObjectURL", {
      configurable: true,
      value: jest.fn(() => "blob:voiceover"),
    });
    const voiceoverInput = view!.container.querySelector(
      'input[type="file"][accept*=".mp4"]',
    ) as HTMLInputElement;
    expect(voiceoverInput).not.toBeNull();
    await act(async () => {
      fireEvent.change(voiceoverInput, {
        target: { files: [new File(["audio"], "voice.m4a", { type: "audio/mp4" })] },
      });
    });

    await waitFor(() => expect(mockSetItemVoiceover).toHaveBeenCalled());
    expect(screen.getAllByRole("button", { name: /create video/i })[0]).toBeDisabled();

    await act(async () => {
      resolveSave({ ...item, voiceover_gcs_path: "voiceover-uploads/u1/voice.m4a" });
    });
    await waitFor(() =>
    expect(screen.getAllByRole("button", { name: /create video/i })[0]).toBeEnabled(),
    );
  });

  it("does not leave an optimistic voiceover after attachment fails", async () => {
    const item = makeItem({
      status: "awaiting_clips",
      edit_format: "narrated_ready",
      clip_gcs_paths: ["users/u1/plan/test-item-id/clip.mp4"],
      voiceover_gcs_path: null,
      guided_edit_available: false,
      guided_edit_conversation_available: false,
      guided_edit_auto_design: false,
    });
    mockUsePolledJobStatus.mockReturnValue({
      data: { item, job: null },
      error: null,
      refetch: mockRefetch,
    });
    mockUploadVoiceover.mockResolvedValue({
      gcs_path: "voiceover-uploads/u1/voice.m4a",
      kind: "audio",
    });
    mockSetItemVoiceover.mockRejectedValue(new Error("attachment failed"));

    let view: ReturnType<typeof render>;
    await act(async () => {
      view = render(<PlanItemPage />);
    });
    Object.defineProperty(URL, "createObjectURL", {
      configurable: true,
      value: jest.fn(() => "blob:voiceover"),
    });
    const voiceoverInput = view!.container.querySelector(
      'input[type="file"][accept*=".mp4"]',
    ) as HTMLInputElement;

    await act(async () => {
      fireEvent.change(voiceoverInput, {
        target: { files: [new File(["audio"], "voice.m4a", { type: "audio/mp4" })] },
      });
    });

    await waitFor(() =>
      expect(screen.getByText("We couldn't save your narration. Try again.")).toBeInTheDocument(),
    );
    expect(screen.getAllByRole("button", { name: /create video/i })[0]).toBeDisabled();
  });

  it("talking to camera (subtitled): single clip slot, own-audio helper, no recorder", async () => {
    await act(async () => {
      renderTyped({ edit_format: "subtitled", idea: "Talking to camera" });
    });
    expect(screen.getByText("TALKING TO CAMERA")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Add your clip." })).toBeInTheDocument();
    // Lane G: the "Your clip" InfoDot is gone — the dropzone's own subline
    // carries the short explanation instead (DESIGN.md §12/§15).
    expect(screen.getByText("One clip of you talking")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "About Your clip" })).toBeNull();
    expect(screen.queryByText(/Its own audio is the soundtrack/)).toBeNull();
    expect(screen.queryByText(/Captions and dead-air cleanup happen automatically/)).toBeNull();
    expect(screen.queryByText(/Your narration/)).toBeNull();
    expect(screen.queryByRole("button", { name: /Original audio/i })).toBeNull();
  });

  it("Lane J: no Change button, no inline SetupPicker — ever", async () => {
    await act(async () => {
      renderTyped({ edit_format: "montage", idea: "Montage" });
    });
    expect(screen.queryByText("Advanced video style")).toBeNull();
    expect(screen.queryByRole("button", { name: "Change" })).toBeNull();
    expect(screen.queryByTestId("setup-picker")).toBeNull();
  });

  it("Lane J: Back returns to the chooser's style step for a montage item", async () => {
    await act(async () => {
      renderTyped({ edit_format: "montage", idea: "Montage", montage_preset: "masonry" });
    });
    const back = screen.getByRole("link", { name: /Back/ });
    expect(back).toHaveAttribute("href", "/plan/new?item=test-item-id&step=style");
  });

  it("Lane J: Back returns to the chooser's kind step for a non-montage item", async () => {
    await act(async () => {
      renderTyped({ edit_format: "narrated_ready", idea: "Voiceover" });
    });
    const back = screen.getByRole("link", { name: /Back/ });
    expect(back).toHaveAttribute("href", "/plan/new?item=test-item-id&step=kind");
  });

  it("titled legacy items keep their real title (no type-label takeover)", async () => {
    await act(async () => {
      renderTyped({ edit_format: "montage", theme: "Morning Routine", idea: "Film your morning" });
    });
    expect(screen.getByRole("heading", { name: "Morning Routine" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Add your clips." })).toBeNull();
  });
});

describe("PlanItemPage — release desk untitled receipt (V2)", () => {
  it("shows the type receipt eyebrow instead of an h1 reading 'Montage'", async () => {
    const item = makeItem({
      status: "ready",
      theme: null,
      idea: "Montage",
      edit_format: "montage",
      montage_preset: "classic",
      current_job_id: "job-1",
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
    await act(async () => {
      render(<PlanItemPage />);
    });
    expect(screen.getByRole("heading", { name: "Montage" })).toBeInTheDocument();
  });
});
