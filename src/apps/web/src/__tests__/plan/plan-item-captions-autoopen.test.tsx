/**
 * Caption-edit discoverability (plan/items/[id]/page.tsx → FocusedResults):
 *   - The Captions tab AUTO-OPENS for caption archetypes (narrated/subtitled)
 *     once the variant is ready and has cues, so a talking-to-camera user lands
 *     on caption editing instead of a collapsed tab row.
 *   - Precedence: while the variant is still rendering (cues not yet in), it
 *     does NOT auto-open.
 *   - Cues-null panel body: manually opening the Captions tab on a still-
 *     rendering variant shows an explicit "still processing" state, not a blank
 *     panel that falls through to the non-caption controls.
 *   - A non-caption (montage) variant neither auto-opens nor shows a Captions tab.
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

import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom";

process.env.NEXT_PUBLIC_SUBTITLED_ENABLED = "true";

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

describe("Caption archetype auto-open", () => {
  it("auto-opens the Captions tab (CaptionEditor visible) for a ready subtitled variant with cues", async () => {
    mountWithVariant(captionVariant());
    await act(async () => {
      render(<PlanItemPage />);
    });
    expect(await screen.findByTestId("caption-editor")).toBeInTheDocument();
    // The scroll-into-view effect fired for the opened panel.
    await waitFor(() => expect(Element.prototype.scrollIntoView).toHaveBeenCalled());
  });

  it("does NOT auto-open while the variant is still rendering (cues not in yet)", async () => {
    mountWithVariant(captionVariant({ render_status: "rendering", caption_cues: null }));
    await act(async () => {
      render(<PlanItemPage />);
    });
    expect(screen.queryByTestId("caption-editor")).not.toBeInTheDocument();
    // The Captions tab is still offered (archetype + base video), not hidden.
    expect(screen.getByRole("button", { name: /captions/i })).toBeInTheDocument();
  });

  it("shows an explicit 'still processing' state (not a blank panel) when captions are opened with no cues", async () => {
    mountWithVariant(captionVariant({ render_status: "rendering", caption_cues: null }));
    await act(async () => {
      render(<PlanItemPage />);
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /captions/i }));
    });
    expect(screen.getByTestId("captions-unavailable")).toHaveTextContent(/still processing/i);
    expect(screen.queryByTestId("caption-editor")).not.toBeInTheDocument();
  });

  it("does not auto-open or show a Captions tab for a non-caption (montage) variant", async () => {
    mountWithVariant(
      captionVariant({ resolved_archetype: "original_text", text_mode: "agent_text", caption_cues: null }),
      { edit_format: "montage" },
    );
    await act(async () => {
      render(<PlanItemPage />);
    });
    expect(screen.queryByTestId("caption-editor")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /captions/i })).not.toBeInTheDocument();
  });

  // Adversarial-review P2: auto-open must respect an explicit dismiss. A user who
  // opens then closes the Captions tab during a render window must NOT have it
  // force-reopened when the finishing poll flips the variant to ready.
  it("does not force-reopen the Captions tab after the user dismissed it during a render window", async () => {
    mountWithVariant(captionVariant({ render_status: "rendering" }));
    let view;
    await act(async () => {
      view = render(<PlanItemPage />);
    });
    // Mid-render: auto-open holds off (waits for ready).
    expect(screen.queryByTestId("caption-editor")).not.toBeInTheDocument();

    // User opens the Captions tab (cues present → editor shows), then closes it.
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /captions/i }));
    });
    expect(screen.getByTestId("caption-editor")).toBeInTheDocument();
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /captions/i }));
    });
    expect(screen.queryByTestId("caption-editor")).not.toBeInTheDocument();

    // A later poll delivers the finished render (same variant_id → no remount).
    mountWithVariant(captionVariant({ render_status: "ready" }));
    await act(async () => {
      view.rerender(<PlanItemPage />);
    });
    // The dismiss is honored — captions stay closed.
    expect(screen.queryByTestId("caption-editor")).not.toBeInTheDocument();
  });

  it("control: auto-opens after the render finishes when the user did NOT touch the tabs", async () => {
    mountWithVariant(captionVariant({ render_status: "rendering" }));
    let view;
    await act(async () => {
      view = render(<PlanItemPage />);
    });
    expect(screen.queryByTestId("caption-editor")).not.toBeInTheDocument();
    mountWithVariant(captionVariant({ render_status: "ready" }));
    await act(async () => {
      view.rerender(<PlanItemPage />);
    });
    expect(await screen.findByTestId("caption-editor")).toBeInTheDocument();
  });

  // Adversarial-review P3: a ready, no-speech subtitled variant (text lane flag on)
  // renders with null cues. The Captions panel must not dead-end — route to the
  // Timeline lane where styled text for this variant actually lives.
  it("routes a ready no-speech subtitled variant (text lane on) to the Timeline tab", async () => {
    const prev = process.env.NEXT_PUBLIC_SUBTITLED_TEXT_LANE_ENABLED;
    process.env.NEXT_PUBLIC_SUBTITLED_TEXT_LANE_ENABLED = "true";
    try {
      mountWithVariant(captionVariant({ render_status: "ready", caption_cues: null }));
      await act(async () => {
        render(<PlanItemPage />);
      });
      // No auto-open (no cues), but the Captions tab is still offered.
      await act(async () => {
        fireEvent.click(screen.getByRole("button", { name: /captions/i }));
      });
      const panel = screen.getByTestId("captions-unavailable");
      expect(panel).toHaveTextContent(/no speech detected/i);
      expect(panel).not.toHaveTextContent(/available for this edit yet/i);
      expect(screen.getByRole("button", { name: /open the timeline/i })).toBeInTheDocument();
    } finally {
      process.env.NEXT_PUBLIC_SUBTITLED_TEXT_LANE_ENABLED = prev;
    }
  });

  // Review (testing specialist): the flag-OFF no-cues branch — no Timeline route,
  // plain "no captions" copy (no misleading "yet").
  it("shows plain 'No captions for this edit' (no Timeline route) when the text lane is off", async () => {
    const prev = process.env.NEXT_PUBLIC_SUBTITLED_TEXT_LANE_ENABLED;
    process.env.NEXT_PUBLIC_SUBTITLED_TEXT_LANE_ENABLED = "false";
    try {
      mountWithVariant(captionVariant({ render_status: "ready", caption_cues: null }));
      await act(async () => {
        render(<PlanItemPage />);
      });
      await act(async () => {
        fireEvent.click(screen.getByRole("button", { name: /captions/i }));
      });
      const panel = screen.getByTestId("captions-unavailable");
      expect(panel).toHaveTextContent(/no captions for this edit/i);
      expect(panel).not.toHaveTextContent(/yet/i);
      expect(screen.queryByRole("button", { name: /open the timeline/i })).not.toBeInTheDocument();
    } finally {
      process.env.NEXT_PUBLIC_SUBTITLED_TEXT_LANE_ENABLED = prev;
    }
  });
});
