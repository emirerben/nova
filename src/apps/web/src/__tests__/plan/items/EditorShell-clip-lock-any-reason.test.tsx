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
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { EditorCapabilities, PlanItem, PlanItemVariant } from "@/lib/plan-api";

class ResizeObserverMock {
  observe() {}
  unobserve() {}
  disconnect() {}
}
(global as unknown as { ResizeObserver: typeof ResizeObserverMock }).ResizeObserver =
  ResizeObserverMock;

let mockDesktopLayout = true;
Object.defineProperty(window, "matchMedia", {
  writable: true,
  value: jest.fn().mockImplementation((query: string) => ({
    matches: query.includes("min-width") && mockDesktopLayout,
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

jest.mock("@/lib/generative-api", () => ({
  ...jest.requireActual("@/lib/generative-api"),
  getGenerativeStyleSets: jest.fn().mockResolvedValue([]),
}));

const mockCommitEditorSession = jest.fn();
let mockTimelineLookPreset: "none" | "stadium_diffusion" = "none";
let mockSecondSlotRemoved = false;
jest.mock("@/lib/editor-commit", () => ({
  ...jest.requireActual("@/lib/editor-commit"),
  commitEditorSession: (...args: unknown[]) => mockCommitEditorSession(...args),
}));

// Two real, non-gridded clip slots so the clip lane actually renders bars —
// this is what most EditorShell tests stub away (empty timeline), which
// hides the exact affordance this fix touches.
jest.mock("@/app/plan/_components/useClipTimeline", () => ({
  useClipTimeline: () => ({
    state: {
      grid: [],
      clipDurations: {},
      baseline: [
        {
          key: "slot-1",
          slotId: "slot-1",
          clipIndex: 0,
          inS: 0,
          durationBeats: null,
          durationS: 3,
          removed: false,
          momentDescription: null,
          lookPreset: mockTimelineLookPreset,
        },
        {
          key: "slot-2",
          slotId: "slot-2",
          clipIndex: 1,
          inS: 0,
          durationBeats: null,
          durationS: 3,
          removed: mockSecondSlotRemoved,
          momentDescription: null,
        },
      ],
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
          lookPreset: mockTimelineLookPreset,
        },
        {
          key: "slot-2",
          slotId: "slot-2",
          clipIndex: 1,
          inS: 0,
          durationBeats: null,
          durationS: 3,
          removed: mockSecondSlotRemoved,
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
      { clip_index: 2, signed_url: null, duration_s: null, used: false },
    ],
    windows: [],
    totalS: 6,
    loadState: "ready",
    editWideLookPresets: ["none", "golden_hour", "faded_analog"],
    lookPresets: [
      "none",
      "stadium_diffusion",
      "olive_film",
      "golden_hour",
      "faded_analog",
    ],
    revisionNumber: null,
    baseGeneration: null,
    tombstones: [],
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
    render_generation_id: "gen-current",
    editor_capabilities: capabilities,
  } as unknown as PlanItemVariant;
}

async function renderShell(variant: PlanItemVariant) {
  mockGetPlanItem.mockResolvedValue(ITEM);
  mockGetPlanItemJobStatus.mockResolvedValue({
    variants: [variant],
  } as unknown as Awaited<ReturnType<typeof getPlanItemJobStatus>>);
  mockCommitEditorSession.mockResolvedValue({
    ok: true,
    generation: "gen-next",
    sections: { timeline: true },
  });
  await act(async () => {
    render(<EditorShell itemId="item-1" variantParam="var-1" />);
  });
}

afterEach(() => {
  jest.clearAllMocks();
  mockDesktopLayout = true;
  mockTimelineLookPreset = "none";
  mockSecondSlotRemoved = false;
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

  it("adds an uploaded source omitted from the rendered cut, with undo and save support", async () => {
    await renderShell(makeVariant(EDITABLE_CAPABILITIES));

    const addClip = screen.getByRole("button", {
      name: "Add source clip 3 to timeline",
    });
    fireEvent.click(addClip);

    expect(screen.queryByRole("button", { name: "Add source clip 3 to timeline" })).toBeNull();
    expect(screen.getByRole("button", { name: /^Clip 3, timeline/ })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Undo" }));
    expect(screen.getByRole("button", { name: "Add source clip 3 to timeline" })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Redo" }));
    expect(screen.getByRole("button", { name: /^Clip 3, timeline/ })).toBeInTheDocument();

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Save" }));
    });
    await waitFor(() => expect(mockCommitEditorSession).toHaveBeenCalledTimes(1));
    expect(mockCommitEditorSession.mock.calls[0][2].timeline_slots).toHaveLength(3);
    expect(mockCommitEditorSession.mock.calls[0][2].timeline_slots[2]).toMatchObject({
      slot_id: null,
      clip_index: 2,
      in_s: 0,
      removed: false,
    });
  });

  it("records, undoes, redoes, and saves a clip look change", async () => {
    await renderShell(makeVariant(EDITABLE_CAPABILITIES));

    fireEvent.click(screen.getByRole("button", { name: /^Clip 1,/ }));
    const original = await screen.findByRole("radio", { name: "Original" });
    const stadium = screen.getByRole("radio", { name: "Stadium Diffusion" });
    expect(original).toBeChecked();

    fireEvent.click(stadium);
    expect(stadium).toBeChecked();

    fireEvent.click(screen.getByRole("button", { name: "Undo" }));
    expect(original).toBeChecked();

    fireEvent.click(screen.getByRole("button", { name: "Redo" }));
    expect(stadium).toBeChecked();

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Save" }));
    });
    await waitFor(() => expect(mockCommitEditorSession).toHaveBeenCalledTimes(1));
    expect(mockCommitEditorSession.mock.calls[0][2]).toMatchObject({
      timeline_slots: [
        expect.objectContaining({
          slot_id: "slot-1",
          look_preset: "stadium_diffusion",
        }),
        expect.objectContaining({
          slot_id: "slot-2",
          look_preset: "none",
        }),
      ],
    });
  });

  it("applies one edit-wide look to every slot in one action", async () => {
    mockSecondSlotRemoved = true;
    await renderShell(makeVariant(EDITABLE_CAPABILITIES));

    fireEvent.click(screen.getByRole("button", { name: "Styles tool" }));
    fireEvent.click(await screen.findByRole("radio", { name: "Faded Analog" }));

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Save" }));
    });
    await waitFor(() => expect(mockCommitEditorSession).toHaveBeenCalledTimes(1));
    const timelineSlots = mockCommitEditorSession.mock.calls[0][2].timeline_slots;
    expect(timelineSlots).toEqual([
      expect.objectContaining({ look_preset: "faded_analog" }),
      expect.objectContaining({ look_preset: "faded_analog", removed: true }),
    ]);
    expect(timelineSlots[0]).not.toHaveProperty("look_adjustments");
    expect(timelineSlots[1]).not.toHaveProperty("look_adjustments");
  });

  it("applies an edit-wide look from the mobile Styles sheet", async () => {
    mockDesktopLayout = false;
    await renderShell(makeVariant(EDITABLE_CAPABILITIES));

    fireEvent.click(screen.getByRole("button", { name: "Styles tool" }));
    fireEvent.click(await screen.findByRole("radio", { name: "Golden Hour" }));

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Save" }));
    });
    await waitFor(() => expect(mockCommitEditorSession).toHaveBeenCalledTimes(1));
    expect(mockCommitEditorSession.mock.calls[0][2].timeline_slots).toEqual([
      expect.objectContaining({ look_preset: "golden_hour" }),
      expect.objectContaining({ look_preset: "golden_hour" }),
    ]);
  });

  it("does not dirty history when the selected edit-wide look is clicked again", async () => {
    await renderShell(makeVariant(EDITABLE_CAPABILITIES));

    fireEvent.click(screen.getByRole("button", { name: "Styles tool" }));
    fireEvent.click(await screen.findByRole("radio", { name: "Original" }));
    expect(screen.getByRole("button", { name: "Undo" })).toBeDisabled();

    fireEvent.click(screen.getByRole("button", { name: "Text tool" }));
    fireEvent.click(screen.getByRole("button", { name: "Add text" }));
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Save" }));
    });
    await waitFor(() => expect(mockCommitEditorSession).toHaveBeenCalledTimes(1));
    expect(mockCommitEditorSession.mock.calls[0][2].timeline_slots).toBeUndefined();
  });

  it("omits timeline after an edit-wide look is undone to baseline", async () => {
    await renderShell(makeVariant(EDITABLE_CAPABILITIES));

    fireEvent.click(screen.getByRole("button", { name: "Styles tool" }));
    fireEvent.click(await screen.findByRole("radio", { name: "Golden Hour" }));

    fireEvent.click(screen.getByRole("button", { name: "Undo" }));
    fireEvent.click(screen.getByRole("button", { name: "Text tool" }));
    fireEvent.click(screen.getByRole("button", { name: "Add text" }));

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Save" }));
    });
    await waitFor(() => expect(mockCommitEditorSession).toHaveBeenCalledTimes(1));
    expect(mockCommitEditorSession.mock.calls[0][2].timeline_slots).toBeUndefined();
    expect(mockCommitEditorSession.mock.calls[0][2].text_elements).toHaveLength(1);
  });

  it("resets an edit-wide look to Original with one-step undo and redo", async () => {
    await renderShell(makeVariant(EDITABLE_CAPABILITIES));

    fireEvent.click(screen.getByRole("button", { name: "Styles tool" }));
    const original = await screen.findByRole("radio", { name: "Original" });
    const golden = screen.getByRole("radio", { name: "Golden Hour" });

    fireEvent.click(golden);
    expect(golden).toBeChecked();
    fireEvent.click(original);
    expect(original).toBeChecked();

    fireEvent.click(screen.getByRole("button", { name: "Undo" }));
    expect(golden).toBeChecked();
    fireEvent.click(screen.getByRole("button", { name: "Redo" }));
    expect(original).toBeChecked();

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Save" }));
    });
    await waitFor(() => expect(mockCommitEditorSession).toHaveBeenCalledTimes(1));
    expect(mockCommitEditorSession.mock.calls[0][2].timeline_slots).toBeUndefined();
  });

  it("records one undo step for an entire look-slider drag", async () => {
    await renderShell(makeVariant(EDITABLE_CAPABILITIES));

    fireEvent.click(screen.getByRole("button", { name: /^Clip 1,/ }));
    fireEvent.click(await screen.findByRole("radio", { name: "Olive Film" }));
    const warmth = screen.getByRole("slider", { name: "Look warmth" });

    fireEvent.pointerDown(warmth);
    fireEvent.change(warmth, { target: { value: "20" } });
    fireEvent.change(warmth, { target: { value: "30" } });
    expect(screen.getByRole("slider", { name: "Look warmth" })).toHaveValue("30");

    fireEvent.click(screen.getByRole("button", { name: "Undo" }));
    expect(screen.getByRole("radio", { name: "Olive Film" })).toBeChecked();
    expect(screen.getByRole("slider", { name: "Look warmth" })).toHaveValue("0");

    fireEvent.click(screen.getByRole("button", { name: "Undo" }));
    expect(screen.getByRole("radio", { name: "Original" })).toBeChecked();
  });

  it("does not double-apply a persisted look over the rendered preview", async () => {
    mockTimelineLookPreset = "stadium_diffusion";
    await renderShell(makeVariant(EDITABLE_CAPABILITIES));

    expect(document.querySelector('[data-look-preview="none"]')).toBeInTheDocument();
    expect(document.querySelector('[data-look-preview="stadium_diffusion"]')).toBeNull();
  });
});
