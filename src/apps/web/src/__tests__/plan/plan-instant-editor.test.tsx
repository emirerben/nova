/**
 * Deferred-burn editor on the /plan content-plan item page.
 *
 * An ELIGIBLE focused variant shows the live base-video + IntroTextPreview
 * overlay on the LEFT, and the NORMAL PlanVariantEditor controls (Caption /
 * Text size / Layout / Style / Song / Clips) on the RIGHT. Changing a control
 * mutates the local edit-session draft with ZERO network — nothing re-renders
 * while editing. The single FFmpeg bake fires ONLY when the user clicks
 * Download: one batched /edit with the accumulated draft, then the download.
 *
 * An INELIGIBLE variant (sequence-synced / lyrics / cluster-without-base / no
 * base_video_url) keeps the legacy per-field server-render controls.
 *
 * This suite does NOT mock PlanVariantEditor — it exercises the real
 * eligibility branch + the real controls in FocusedResults.
 */

// @ts-nocheck

// jsdom lacks ResizeObserver (used by IntroTextPreview) and matchMedia.
class ResizeObserverMock {
  observe() {}
  unobserve() {}
  disconnect() {}
}
(global as unknown as { ResizeObserver: typeof ResizeObserverMock }).ResizeObserver =
  ResizeObserverMock;

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

jest.mock("next/navigation", () => ({
  useParams: jest.fn(() => ({ id: "test-item-id" })),
}));

const mockRefetch = jest.fn();
jest.mock("@/hooks/usePolledJobStatus", () => ({
  usePolledJobStatus: jest.fn(),
}));
import { usePolledJobStatus } from "@/hooks/usePolledJobStatus";
const mockUsePolledJobStatus = usePolledJobStatus as jest.MockedFunction<typeof usePolledJobStatus>;

// Real editPlanItemVariant is spied so we can assert the batched bake payload.
// The legacy per-field endpoints are spied too so we can assert they are NOT
// called while editing an eligible variant (the draft path replaces them).
const mockEditPlanItemVariant = jest.fn().mockResolvedValue({});
const mockRetextPlanItem = jest.fn().mockResolvedValue({});
const mockChangePlanItemStyle = jest.fn().mockResolvedValue({});
const mockSetPlanItemIntroSize = jest.fn().mockResolvedValue({});
jest.mock("@/lib/plan-api", () => ({
  ...jest.requireActual("@/lib/plan-api"),
  getPlanItem: jest.fn(),
  getPlanItemJobStatus: jest.fn(),
  requestUploadUrls: jest.fn(),
  attachClips: jest.fn(),
  generatePlanItem: jest.fn(),
  swapPlanItemSong: jest.fn(),
  retextPlanItem: (...args: unknown[]) => mockRetextPlanItem(...args),
  changePlanItemStyle: (...args: unknown[]) => mockChangePlanItemStyle(...args),
  setPlanItemIntroSize: (...args: unknown[]) => mockSetPlanItemIntroSize(...args),
  editPlanItemVariant: (...args: unknown[]) => mockEditPlanItemVariant(...args),
  uploadToGcs: jest.fn(),
  NotAuthenticatedError: class NotAuthenticatedError extends Error {},
}));

jest.mock("@/lib/generative-api", () => ({
  ...jest.requireActual("@/lib/generative-api"),
  getGenerativeStyleSets: jest.fn().mockResolvedValue([
    {
      id: "travel_editorial",
      label: "Travel",
      tags: [],
      intro: { effect: "karaoke-line", text_color: "#fff", highlight_color: "#FFD24A" },
    },
  ]),
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
const mockDownloadVideo = jest.fn();
jest.mock("@/lib/download-video", () => ({ downloadVideo: (...a: unknown[]) => mockDownloadVideo(...a) }));
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
// IntroTextPreview measures fonts via canvas — stub to a marker so we can assert
// the live overlay mounted without pulling in canvas/measureText in jsdom.
jest.mock("@/components/variant-editor/IntroTextPreview", () => ({
  IntroTextPreview: () => <div data-testid="intro-text-preview" />,
}));

// Capture useSfxPreview calls so we can assert the live SFX <audio> sync is
// wired into WHICHEVER preview renders. Instant-eligible variants render through
// LiveEditPreview (not Hero) on the timeline, so this is the only place that
// proves glossary sound effects reach the preview at all.
const mockUseSfxPreview = jest.fn();
jest.mock("@/app/plan/_components/useSfxPreview", () => ({
  useSfxPreview: (...args: unknown[]) => mockUseSfxPreview(...args),
}));

import PlanItemPage from "@/app/plan/items/[id]/page";

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

const eligibleVariant = {
  variant_id: "song_text",
  output_url: "https://cdn/out.mp4",
  render_status: "ready",
  text_mode: "agent_text",
  music_track_id: "t1",
  track_title: "Track",
  style_set_id: "travel_editorial",
  intro_text_size_px: 56,
  intro_size_source: "computed",
  intro_text: "hello world",
  intro_layout: "linear",
  base_video_url: "https://cdn/base.mp4?sig=1",
  base_video_path: "generative-jobs/j/base.mp4",
};

const sequenceVariant = {
  ...eligibleVariant,
  variant_id: "original_text",
  intro_mode: "sequence",
  intro_layout: "cluster",
  sequence_synced: true,
};

function setData(item, variants) {
  mockUsePolledJobStatus.mockReturnValue({
    data: { item, job: makeJob(variants) },
    error: null,
    refetch: mockRefetch,
  });
}

beforeEach(() => {
  mockEditPlanItemVariant.mockClear();
  mockRetextPlanItem.mockClear();
  mockChangePlanItemStyle.mockClear();
  mockSetPlanItemIntroSize.mockClear();
  mockDownloadVideo.mockClear();
  mockRefetch.mockClear();
  mockUseSfxPreview.mockClear();
});

describe("Plan item page — deferred-burn editor", () => {
  it("eligible variant shows burned output at rest, overlay only while editing", async () => {
    setData(makeItem(), [eligibleVariant]);
    await act(async () => {
      render(<PlanItemPage />);
    });

    // HERO at rest (draft clean = no uncommitted edits): the burned output_url is shown,
    // NOT the live DOM overlay. This is preview-parity: what you see at rest IS the download.
    expect(screen.queryByTestId("intro-text-preview")).toBeNull();

    // Editor row is visible with tab buttons. Text + Font tabs were retired in PR-4.
    expect(screen.queryByRole("button", { name: /T Text/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /Aa Font/i })).toBeNull();

    // Open the Timeline tab → text controls (PlanVariantEditor) render directly below
    // the timeline in the text-controls area (T5: "Edit text ▼" expand button removed).
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /▭|Timeline/i }));
    });
    // PlanVariantEditor shows "Remove text" in the text-controls area below the timeline.
    expect(screen.getAllByRole("button", { name: /^Remove text$/ }).length).toBeGreaterThanOrEqual(1);
    expect(screen.getByRole("radiogroup", { name: /intro text layout/i })).toBeInTheDocument();
    // EditToolbar (font/size) is also inline — slider present, A+ stepper hidden.
    expect(screen.queryByRole("button", { name: /bigger intro text/i })).toBeNull();
    expect(screen.getByRole("slider", { name: /intro text size/i })).toBeInTheDocument();

    // Download is always visible (not behind a tab).
    expect(screen.getByRole("button", { name: /^Download$/ })).toBeInTheDocument();

    // Making a draft edit flips isDirty → overlay switches on for live WYSIWYG feedback.
    // textLaneOpen=true restores LiveEditPreview in the hero for instant-edit variants.
    const slider = screen.getByRole("slider", { name: /intro text size/i });
    fireEvent.change(slider, { target: { value: "62" } });
    expect(screen.getByTestId("intro-text-preview")).toBeInTheDocument();
  });

  it("changing a control updates the draft and does NOT call the server render endpoint", async () => {
    setData(makeItem(), [eligibleVariant]);
    await act(async () => {
      render(<PlanItemPage />);
    });

    // Open Timeline tab → expand Text lane → EditToolbar slider is inline (PR-4).
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: /▭|Timeline/i })); });
    await act(async () => { fireEvent.click(screen.getAllByRole("button", { name: /Edit text/i })[0]); });

    // Bump the text size via the EditToolbar range slider — this is a draft mutation.
    // (The A+ stepper is hidden in the deferred path; the slider replaces it.)
    const slider = screen.getByRole("slider", { name: /intro text size/i });
    fireEvent.change(slider, { target: { value: "62" } });

    // NONE of the render endpoints fired — the bake is deferred to Download.
    expect(mockSetPlanItemIntroSize).not.toHaveBeenCalled();
    expect(mockEditPlanItemVariant).not.toHaveBeenCalled();
    expect(mockRetextPlanItem).not.toHaveBeenCalled();

    // Unsaved hint near Download appears once the draft is dirty.
    expect(screen.getByText(/Unsaved — downloads will include your changes/i)).toBeInTheDocument();
  });

  it("clicking Download triggers exactly one batched editPlanItemVariant bake", async () => {
    setData(makeItem(), [eligibleVariant]);
    await act(async () => {
      render(<PlanItemPage />);
    });

    // Open Timeline → expand Text lane to access both EditToolbar (slider) and PlanVariantEditor (PR-4).
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: /▭|Timeline/i })); });
    await act(async () => { fireEvent.click(screen.getAllByRole("button", { name: /Edit text/i })[0]); });

    // Accumulate two draft edits: size bump + remove text (batched, no network).
    // Size via range slider (A+ stepper is hidden in the deferred path).
    const slider = screen.getByRole("slider", { name: /intro text size/i });
    fireEvent.change(slider, { target: { value: "62" } });

    await act(async () => {
      // Both PlanVariantEditor and EditToolbar are inline in the text panel.
      screen.getAllByRole("button", { name: /^Remove text$/ })[0].click();
    });
    expect(mockEditPlanItemVariant).not.toHaveBeenCalled();

    // Download = the bake: ONE editPlanItemVariant call with the batched payload.
    await act(async () => {
      screen.getByRole("button", { name: /preparing your video|^Download$/i }).click();
    });

    expect(mockEditPlanItemVariant).toHaveBeenCalledTimes(1);
    const [itemId, variantId, payload] = mockEditPlanItemVariant.mock.calls[0];
    expect(itemId).toBe("test-item-id");
    expect(variantId).toBe("song_text");
    // Removing the text wins over the size bump in the batched payload.
    expect(payload).toMatchObject({ remove_text: true });
    // The legacy per-field endpoints were never used.
    expect(mockSetPlanItemIntroSize).not.toHaveBeenCalled();
    expect(mockRetextPlanItem).not.toHaveBeenCalled();
  });

  it("ineligible (sequence-synced) variant keeps the legacy server-render controls", async () => {
    setData(makeItem(), [sequenceVariant]);
    await act(async () => {
      render(<PlanItemPage />);
    });
    // No live overlay preview — ineligible variant always shows Hero.
    expect(screen.queryByTestId("intro-text-preview")).toBeNull();

    // Open Timeline → expand Text lane to expose PlanVariantEditor controls (PR-4).
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: /▭|Timeline/i })); });
    await act(async () => { fireEvent.click(screen.getAllByRole("button", { name: /Edit text/i })[0]); });

    // Legacy PlanVariantEditor renders the synced badge + Remove text control.
    expect(screen.getByText(/Editorial · synced/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^Remove text$/ })).toBeInTheDocument();

    // A legacy size nudge re-renders SERVER-side (not the deferred draft path).
    await act(async () => {
      screen.getByRole("button", { name: /bigger intro text/i }).click();
    });
    expect(mockSetPlanItemIntroSize).toHaveBeenCalledTimes(1);
    expect(mockEditPlanItemVariant).not.toHaveBeenCalled();
  });

  it("ineligible (no base_video_url) variant keeps the legacy controls", async () => {
    const noBase = { ...eligibleVariant, base_video_url: null, base_video_path: null };
    setData(makeItem(), [noBase]);
    await act(async () => {
      render(<PlanItemPage />);
    });
    expect(screen.queryByTestId("intro-text-preview")).toBeNull();

    // Open Timeline → PlanVariantEditor controls are now directly visible below the
    // timeline (T5: the "Edit text ▼" expand button was replaced by the interactive bar lane).
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: /▭|Timeline/i })); });
    // "Remove text" comes from PlanVariantEditor rendered in the text-controls area.
    expect(screen.getByRole("button", { name: /^Remove text$/ })).toBeInTheDocument();
  });

  // Regression for the #576 follow-up: instant-eligible variants (agent_text intro)
  // render the live preview through LiveEditPreview, NOT Hero. #576 wired the
  // glossary-SFX <audio> sync into Hero only, so sound effects were silent in the
  // preview for exactly these variants while the Download bake still included them.
  // Assert the SFX placement reaches useSfxPreview regardless of which preview renders.
  it("instant-eligible variant feeds SFX placements into the live preview audio sync", async () => {
    const sfxPlacement = {
      id: "p1",
      sound_effect_id: "fah-id",
      src_gcs_path: "sound-effects/fah/audio.mp3",
      at_s: 3.9,
      gain: 1.0,
      duration_s: 2.04,
      label: "Fah",
    };
    const variantWithSfx = { ...eligibleVariant, sound_effects: [sfxPlacement] };
    setData(makeItem(), [variantWithSfx]);
    await act(async () => {
      render(<PlanItemPage />);
    });

    // The eligible variant renders LiveEditPreview (instant editor), which must
    // call useSfxPreview with the variant's placements. Before the fix it never
    // did, so the placement never reached an <audio> element.
    const sawPlacement = mockUseSfxPreview.mock.calls.some(
      ([, placements]) =>
        Array.isArray(placements) &&
        placements.some((p: { sound_effect_id?: string }) => p?.sound_effect_id === "fah-id"),
    );
    expect(sawPlacement).toBe(true);
  });
});
