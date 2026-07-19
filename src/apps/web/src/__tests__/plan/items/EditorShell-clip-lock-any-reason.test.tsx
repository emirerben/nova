/**
 * Regression test: EditorShell — the clip lane must lock for ANY server
 * timeline ineligibility, not just voiceover reasons.
 *
 * Before this fix, `clipLockedToVoiceover` only locked the clip lane when
 * `capabilities.reason` was a voiceover reason (or `resolved_archetype ===
 * "narrated"`). A `lyrics_sync` reason (`timeline: false` on a song_lyrics-
 * like variant) fell through as "unlocked" in the UI — drag/split/delete
 * looked live and only 422'd on save. The fix (`clipEditingLocked`) locks on
 * ANY `capabilities.timeline === false`, with `clipDisabledReason` staying
 * reason-driven (`editorReasonCopy`) except the voiceover/narrated case,
 * which keeps its dedicated "locked to your voiceover" copy.
 *
 * Mounted through the REAL shell (house style of EditorShell-text-lock /
 * EditorShell-undo-capability-gating). Unlike those tests — which stub an
 * EMPTY clip timeline (no bars render, so the clip lane's own lock state is
 * unobservable) — this file stubs useClipTimeline with two real, non-gridded
 * DraftSlots so the clip lane actually renders bars and a selectable clip.
 */

import "@testing-library/jest-dom";
import { act, fireEvent, render, screen } from "@testing-library/react";
import type { EditorCapabilities, PlanItem, PlanItemVariant } from "@/lib/plan-api";

class ResizeObserverMock {
  observe() {}
  unobserve() {}
  disconnect() {}
}
(global as unknown as { ResizeObserver: typeof ResizeObserverMock }).ResizeObserver =
  ResizeObserverMock;

Object.defineProperty(window, "matchMedia", {
  writable: true,
  value: jest.fn().mockImplementation((query: string) => ({
    matches: query.includes("min-width"),
    media: query,
    onchange: null,
    addListener: jest.fn(),
    removeListener: jest.fn(),
    addEventListener: jest.fn(),
    removeEventListener: jest.fn(),
    dispatchEvent: jest.fn(),
  })),
});

jest.mock("next/navigation", () => ({
  useRouter: () => ({ push: jest.fn() }),
}));

jest.mock("@/lib/plan-api", () => ({
  ...jest.requireActual("@/lib/plan-api"),
  getPlanItem: jest.fn(),
  getPlanItemJobStatus: jest.fn(),
}));

// Two real, non-gridded clip slots so the clip lane actually renders bars —
// this is what most EditorShell tests stub away (empty timeline), which
// hides the exact affordance this fix touches.
jest.mock("@/app/plan/_components/useClipTimeline", () => ({
  useClipTimeline: () => ({
    state: {
      grid: [],
      clipDurations: {},
      baseline: [],
      slots: [
        {
          key: "slot-1",
          slotId: "slot-1",
          clipIndex: 0,
          inS: 0,
          durationBeats: null,
          durationS: 3,
          removed: false,
          momentDescription: null,
        },
        {
          key: "slot-2",
          slotId: "slot-2",
          clipIndex: 1,
          inS: 0,
          durationBeats: null,
          durationS: 3,
          removed: false,
          momentDescription: null,
        },
      ],
      past: [],
      future: [],
      clampNonce: 0,
      clampedKey: null,
    },
    dispatch: jest.fn(),
    clips: [
      { clip_index: 0, signed_url: null, duration_s: 5, used: true },
      { clip_index: 1, signed_url: null, duration_s: 5, used: true },
    ],
    windows: [],
    totalS: 6,
    loadState: "ready",
    reload: jest.fn(),
  }),
}));

const EditorShell =
  require("@/app/plan/items/[id]/_editor/EditorShell").default as typeof import("@/app/plan/items/[id]/_editor/EditorShell").default;
const { getPlanItem, getPlanItemJobStatus } = require("@/lib/plan-api") as {
  getPlanItem: typeof import("@/lib/plan-api").getPlanItem;
  getPlanItemJobStatus: typeof import("@/lib/plan-api").getPlanItemJobStatus;
};

const mockGetPlanItem = getPlanItem as jest.MockedFunction<typeof getPlanItem>;
const mockGetPlanItemJobStatus = getPlanItemJobStatus as jest.MockedFunction<
  typeof getPlanItemJobStatus
>;

const ITEM = {
  id: "item-1",
  theme: "My video",
  current_job_id: "job-1",
} as unknown as PlanItem;

// song_lyrics-shaped capabilities: timeline locked for a NON-voiceover reason
// (lyrics_sync) — the exact case the old voiceover-only whitelist missed.
const LYRICS_SYNC_CAPABILITIES: EditorCapabilities = {
  text_elements: false,
  timeline: false,
  split_clips: false,
  mix: true,
  sfx: true,
  overlays: true,
  suggestions: false,
  reason: "lyrics_sync",
};

// Narrated/voiceover-shaped capabilities — the ORIGINAL locked case. Must
// keep working exactly as before (no regression).
const VOICEOVER_CAPABILITIES: EditorCapabilities = {
  text_elements: true,
  timeline: false,
  split_clips: false,
  mix: true,
  sfx: true,
  overlays: true,
  suggestions: true,
  reason: "voiceover_bed_fit",
};

const EDITABLE_CAPABILITIES: EditorCapabilities = {
  text_elements: true,
  timeline: true,
  split_clips: true,
  mix: true,
  sfx: true,
  overlays: true,
  suggestions: true,
};

function makeVariant(
  capabilities: EditorCapabilities,
  archetype: string = "montage",
): PlanItemVariant {
  return {
    variant_id: "var-1",
    output_url: "https://storage.example/variant.mp4",
    render_status: "ready",
    text_mode: "none",
    style_set_id: null,
    intro_text_size_px: null,
    text_elements: [],
    resolved_archetype: archetype,
    editor_capabilities: capabilities,
  } as unknown as PlanItemVariant;
}

async function renderShell(variant: PlanItemVariant) {
  mockGetPlanItem.mockResolvedValue(ITEM);
  mockGetPlanItemJobStatus.mockResolvedValue({
    variants: [variant],
  } as unknown as Awaited<ReturnType<typeof getPlanItemJobStatus>>);
  await act(async () => {
    render(<EditorShell itemId="item-1" variantParam="var-1" />);
  });
}

afterEach(() => {
  jest.clearAllMocks();
  window.sessionStorage.clear();
});

describe("EditorShell — clip lane locks for ANY server timeline ineligibility", () => {
  it("locks the clip lane and shows the lyrics_sync reason copy (a non-voiceover reason)", async () => {
    await renderShell(makeVariant(LYRICS_SYNC_CAPABILITIES));

    // Sanity: this is NOT the whole-shell read-only state — sfx/overlays are
    // still live, only the clip lane is locked.
    expect(screen.queryByText(/This version can('|’)t be edited\./)).toBeNull();

    const clipBar = screen.getByRole("button", { name: /^Clip 1,/ });
    expect(clipBar).toHaveAttribute("title", "lyrics are synced to the song");

    // Selecting the locked clip does not unlock the Delete transport control
    // (canDelete gates on !clipEditingLocked). Before the fix, lyrics_sync
    // wasn't in the voiceover whitelist, so this button was enabled.
    fireEvent.click(clipBar);
    expect(screen.getByRole("button", { name: "Delete selected" })).toBeDisabled();
  });

  it("keeps the narrated/voiceover case locked with its dedicated copy (no regression)", async () => {
    await renderShell(makeVariant(VOICEOVER_CAPABILITIES, "narrated"));

    const clipBar = screen.getByRole("button", { name: /^Clip 1,/ });
    expect(clipBar).toHaveAttribute("title", "locked to your voiceover");

    fireEvent.click(clipBar);
    expect(screen.getByRole("button", { name: "Delete selected" })).toBeDisabled();
  });

  it("control: leaves the clip lane unlocked when the timeline capability is true", async () => {
    await renderShell(makeVariant(EDITABLE_CAPABILITIES));

    const clipBar = screen.getByRole("button", { name: /^Clip 1,/ });
    expect(clipBar).not.toHaveAttribute("title");

    fireEvent.click(clipBar);
    expect(screen.getByRole("button", { name: "Delete selected" })).toBeEnabled();
  });
});
