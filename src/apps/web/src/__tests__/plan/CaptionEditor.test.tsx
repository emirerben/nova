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
  setPlanItemCaptionPosition: jest.fn().mockResolvedValue(undefined),
  setPlanItemCaptionsEnabled: jest.fn().mockResolvedValue(undefined),
  setPlanItemVariantCaptionStyle: jest.fn().mockResolvedValue(undefined),
}));

import CaptionEditor from "@/app/plan/_components/CaptionEditor";
import {
  applyPlanItemCaptions,
  setPlanItemCaptionFont,
  setPlanItemCaptionPosition,
  setPlanItemCaptions,
  setPlanItemCaptionsEnabled,
  setPlanItemVariantCaptionStyle,
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
  delete process.env.NEXT_PUBLIC_CAPTION_POSITION_ENABLED;
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

  it("Apply persists cues + font + style + on/off and triggers the reburn", async () => {
    renderEditor();
    fireEvent.click(screen.getByRole("button", { name: INTRO_FONTS[0].name }));
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /Apply to video/i }));
    });
    expect(setPlanItemCaptions).toHaveBeenCalled();
    expect(setPlanItemCaptionFont).toHaveBeenCalledWith("item-1", "var-1", INTRO_FONTS[0].name);
    expect(setPlanItemVariantCaptionStyle).toHaveBeenCalledWith("item-1", "var-1", "sentence");
    expect(setPlanItemCaptionsEnabled).toHaveBeenCalledWith("item-1", "var-1", true);
    expect(applyPlanItemCaptions).toHaveBeenCalled();
  });
});

describe("CaptionEditor subtitles on/off (independent of cue count)", () => {
  it("toggling off persists immediately and hides the caption preview + cue list, without clearing cues", () => {
    renderEditor();
    const toggle = screen.getByRole("switch", { name: "Subtitles" });
    expect(toggle).toHaveAttribute("aria-checked", "true");
    fireEvent.click(toggle);
    expect(setPlanItemCaptionsEnabled).toHaveBeenCalledWith("item-1", "var-1", false);
    expect(toggle).toHaveAttribute("aria-checked", "false");
    // Cue list + font/style pickers hide — but the cues themselves are untouched
    // client-side (toggling back on needs no re-transcription).
    expect(screen.queryByText("hello world")).not.toBeInTheDocument();
    fireEvent.click(toggle);
    expect(setPlanItemCaptionsEnabled).toHaveBeenLastCalledWith("item-1", "var-1", true);
    expect(screen.getAllByText("hello world").length).toBeGreaterThan(0);
  });
});

describe("CaptionEditor caption style (sentence/word)", () => {
  it("picking word-by-word persists immediately", () => {
    renderEditor();
    fireEvent.click(screen.getByRole("button", { name: /Word-by-word/ }));
    expect(setPlanItemVariantCaptionStyle).toHaveBeenCalledWith("item-1", "var-1", "word");
  });
});

describe("CaptionEditor caption position", () => {
  it("hides the position control unless the feature flag is on", () => {
    renderEditor();
    expect(screen.queryByText("Caption position")).not.toBeInTheDocument();
  });

  it("renders the position control behind the flag and sends y_frac", async () => {
    process.env.NEXT_PUBLIC_CAPTION_POSITION_ENABLED = "true";
    renderEditor();
    expect(screen.getByText("Caption position")).toBeInTheDocument();

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Middle" }));
    });

    expect(setPlanItemCaptionPosition).toHaveBeenCalledWith("item-1", "var-1", 0.66);
  });
});

// D6: subtitled auto-captions are machine-transcribed, and prod ASR can't report
// low confidence — so a review-first notice nudges the creator to scan the cues
// before Apply. Scoped to `reviewFirst` (subtitled); narrated never shows it.
describe("CaptionEditor review-first notice (D6)", () => {
  const NOTICE = /check your captions before applying/i;

  function renderReview(reviewFirst: boolean) {
    return render(
      <CaptionEditor
        itemId="item-1"
        variantId="var-1"
        baseVideoUrl="https://example.com/base.mp4"
        initialCues={CUES}
        reviewFirst={reviewFirst}
      />,
    );
  }

  it("shows only when reviewFirst is set", () => {
    const { unmount } = renderReview(true);
    expect(screen.getByText(NOTICE)).toBeInTheDocument();
    unmount();
    renderReview(false);
    expect(screen.queryByText(NOTICE)).not.toBeInTheDocument();
  });

  it("dismisses once the user taps a caption line", () => {
    renderReview(true);
    expect(screen.getByText(NOTICE)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /second line/ }));
    expect(screen.queryByText(NOTICE)).not.toBeInTheDocument();
  });
});

// D5: the language chip's re-transcribe destructively replaces every caption edit,
// so it MUST be confirm-gated and must send the TOGGLED language.
describe("CaptionEditor language chip (D5)", () => {
  function renderChip(onChangeLanguage = jest.fn(), captionLanguage = "en") {
    render(
      <CaptionEditor
        itemId="item-1"
        variantId="var-1"
        baseVideoUrl="https://example.com/base.mp4"
        initialCues={CUES}
        captionLanguage={captionLanguage}
        onChangeLanguage={onChangeLanguage}
      />,
    );
    return onChangeLanguage;
  }

  it("shows the current language and confirms before re-transcribing", () => {
    const onChangeLanguage = renderChip();
    expect(screen.getByText(/captions in english/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /change caption language/i }));
    // Confirm gate: nothing fires until Re-transcribe is clicked.
    expect(onChangeLanguage).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: /re-transcribe/i }));
    expect(onChangeLanguage).toHaveBeenCalledWith("tr"); // toggled en → tr
  });

  it("cancel closes the confirm without firing", () => {
    const onChangeLanguage = renderChip(jest.fn(), "tr");
    expect(screen.getByText(/captions in türkçe/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /change caption language/i }));
    fireEvent.click(screen.getByRole("button", { name: /cancel/i }));
    expect(onChangeLanguage).not.toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: /re-transcribe/i })).not.toBeInTheDocument();
  });

  it("hides the chip entirely for narrated (no captionLanguage)", () => {
    render(
      <CaptionEditor
        itemId="item-1"
        variantId="var-1"
        baseVideoUrl="https://example.com/base.mp4"
        initialCues={CUES}
      />,
    );
    expect(screen.queryByText(/captions in/i)).not.toBeInTheDocument();
  });
});
