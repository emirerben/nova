/**
 * CaptionEditor on-video editing.
 *
 * Regression for "I can't edit any words in the caption": the cue editor used
 * to clear `editing` on EVERY blur, and rendered two `autoFocus` fields (the
 * on-video textarea + the cue-list input) for the same cue. React focuses both
 * on mount; the second steals focus from the first, firing the first's blur ->
 * setEditing(null) -> both editors unmount before a keystroke can land.
 */

import { act, render, screen, fireEvent } from "@testing-library/react";
import "@testing-library/jest-dom";

jest.mock("@/lib/plan-api", () => ({
  __esModule: true,
  setPlanItemCaptions: jest.fn().mockResolvedValue(undefined),
  applyPlanItemCaptions: jest.fn().mockResolvedValue(undefined),
  setPlanItemCaptionFont: jest.fn().mockResolvedValue(undefined),
}));

import CaptionEditor from "@/app/plan/_components/CaptionEditor";
import {
  applyPlanItemCaptions,
  setPlanItemCaptionFont,
  setPlanItemCaptions,
  type CaptionCue,
} from "@/lib/plan-api";
import { INTRO_FONTS } from "@/lib/overlay-constants";

// jsdom doesn't implement media playback — stub so jumpToCue()'s pause()/seek
// don't throw.
beforeAll(() => {
  window.HTMLMediaElement.prototype.play = jest.fn().mockResolvedValue(undefined);
  window.HTMLMediaElement.prototype.pause = jest.fn();
});

beforeEach(() => {
  jest.clearAllMocks();
});

const CUES: CaptionCue[] = [
  { text: "hello world", start_s: 0, end_s: 5 },
  { text: "second line", start_s: 5, end_s: 10 },
];

function renderEditor() {
  return render(
    <CaptionEditor
      itemId="item-1"
      variantId="var-1"
      baseVideoUrl="https://example.com/base.mp4"
      initialCues={CUES}
    />,
  );
}

// Enter edit on cue 1 (NOT the time-active cue at t=0) via the cue list, so only
// ONE editor mounts — isolates the blur behavior from the autoFocus race.
function editSecondCue() {
  fireEvent.click(screen.getByRole("button", { name: /second line/ }));
  return screen.getByLabelText("Edit caption at 0:05") as HTMLInputElement;
}

describe("CaptionEditor", () => {
  it("keeps edit mode open when focus moves to the other caption editor (no collapse)", () => {
    renderEditor();
    const input = editSecondCue();
    expect(input).toBeInTheDocument();

    // Focus moving to the sibling caption editor must NOT exit edit mode.
    const sibling = document.createElement("input");
    sibling.setAttribute("data-caption-edit", "1");
    document.body.appendChild(sibling);
    act(() => {
      fireEvent.blur(input, { relatedTarget: sibling });
    });

    // Old code cleared editing on ANY blur -> input gone. Fixed code keeps it.
    expect(screen.getByLabelText("Edit caption at 0:05")).toBeInTheDocument();
  });

  it("exits edit mode when focus leaves the editor entirely", () => {
    renderEditor();
    const input = editSecondCue();
    act(() => {
      fireEvent.blur(input, { relatedTarget: document.body });
    });
    expect(screen.queryByLabelText("Edit caption at 0:05")).not.toBeInTheDocument();
  });

  it("accepts typed edits and reflects them in the cue", () => {
    renderEditor();
    const input = editSecondCue();
    act(() => {
      fireEvent.change(input, { target: { value: "second lime" } });
    });
    expect((screen.getByLabelText("Edit caption at 0:05") as HTMLInputElement).value).toBe(
      "second lime",
    );
  });

  it("persists a picked caption font (debounced)", () => {
    jest.useFakeTimers();
    try {
      renderEditor();
      const opt = INTRO_FONTS[0];
      fireEvent.click(screen.getByRole("button", { name: opt.name }));
      act(() => {
        jest.advanceTimersByTime(450); // past the 400ms debounce
      });
      expect(setPlanItemCaptionFont).toHaveBeenCalledWith("item-1", "var-1", opt.name);
    } finally {
      jest.useRealTimers();
    }
  });

  it("Apply persists cues + font and triggers the reburn", async () => {
    renderEditor();
    fireEvent.click(screen.getByRole("button", { name: INTRO_FONTS[0].name }));
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /Apply to video/i }));
    });
    expect(setPlanItemCaptions).toHaveBeenCalled();
    expect(setPlanItemCaptionFont).toHaveBeenCalledWith("item-1", "var-1", INTRO_FONTS[0].name);
    expect(applyPlanItemCaptions).toHaveBeenCalled();
  });
});
