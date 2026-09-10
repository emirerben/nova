/**
 * EditorTimelineBody — captions lane classification (KRI-18).
 *
 * Guided-story narration captions (role "generative_sequence" +
 * source_params.source === "caption_cue") must render in the single-row
 * Captions lane side by side, not stack one-per-row in the Text lane — the
 * bug this ticket fixes. Their timing is server-pinned, so the lane must
 * also refuse to offer drag/trim on them.
 */
import "@testing-library/jest-dom";
import { render, screen, within } from "@testing-library/react";

// jsdom lacks ResizeObserver (EditorTimelineBody's viewport measure loop).
class ResizeObserverMock {
  observe() {}
  unobserve() {}
  disconnect() {}
}
(global as unknown as { ResizeObserver: typeof ResizeObserverMock }).ResizeObserver =
  ResizeObserverMock;

import EditorTimelineBody, {
  type EditorTimelineBodyProps,
} from "@/app/plan/items/[id]/_editor/EditorTimelineBody";
import type { DraftSlot } from "@/app/generative/timeline-math";
import { buildVirtualTimeline } from "@/app/plan/items/[id]/_editor/virtual-timeline";
import type { TextElementBar } from "@/lib/timeline/text-timeline-reducer";

function slot(over: Partial<DraftSlot> = {}): DraftSlot {
  return {
    key: "s1",
    slotId: "s1",
    clipIndex: 0,
    inS: 0,
    durationBeats: null,
    durationS: 2,
    removed: false,
    momentDescription: null,
    ...over,
  };
}

const TWO_SLOTS: DraftSlot[] = [slot({ key: "a", clipIndex: 0 }), slot({ key: "b", clipIndex: 1 })];

const plainTextBar: TextElementBar = {
  id: "title-1",
  text: "Big title",
  start_s: 0,
  end_s: 1,
  role: "generative_intro",
};

function narrationCaptionBar(index: number): TextElementBar {
  return {
    id: `narration-caption-${index}`,
    text: `word${index}`,
    start_s: index * 0.3,
    end_s: (index + 1) * 0.3,
    role: "generative_sequence",
    source_params: { source: "caption_cue", key: `${index}`, identity: `pinned-narration-caption-${index}` },
  };
}

const cueCaptionBar: TextElementBar = {
  id: "caption-0",
  text: "cue caption",
  start_s: 0,
  end_s: 1,
  role: "narrated_caption",
};

function baseProps(over: Partial<EditorTimelineBodyProps> = {}): EditorTimelineBodyProps {
  const timelineProjection = buildVirtualTimeline(TWO_SLOTS, [], [], null);
  return {
    durationS: 4,
    timelineProjection,
    currentTimeS: 0,
    zoom: 1,
    selection: null,
    onSelect: jest.fn(),
    onClear: jest.fn(),
    textBars: [],
    visualBlocks: [],
    slots: TWO_SLOTS,
    grid: [],
    clipsLoading: false,
    filmstripClips: [],
    sfx: [],
    hasMusic: false,
    videoMuted: false,
    onToggleVideoMute: jest.fn(),
    soundMuted: false,
    onToggleSoundMute: jest.fn(),
    overlays: [],
    onScrub: jest.fn(),
    onScrubStart: jest.fn(),
    ...over,
  };
}

describe("EditorTimelineBody — captions lane classification (KRI-18)", () => {
  it("puts guided-story narration captions in the Captions lane, not the Text lane (AC1, AC6)", () => {
    const bars = [plainTextBar, narrationCaptionBar(0), narrationCaptionBar(1), narrationCaptionBar(2)];
    render(
      <EditorTimelineBody {...baseProps({ textBars: bars, captionsExpanded: true })} />,
    );

    const captionsLane = screen.getByTestId("editor-captions-lane");
    const textLane = screen.getByTestId("editor-text-lane");

    // All three narration captions land in the Captions lane...
    expect(
      within(captionsLane).getAllByRole("button", { name: /^Caption \d/ }),
    ).toHaveLength(3);
    // ...at a single row (top: 0px each) — never one row per bar.
    within(captionsLane)
      .getAllByRole("button", { name: /^Caption \d/ })
      .forEach((button) => {
        expect(button).toHaveStyle({ top: "0px" });
      });

    // ...and are absent from the Text lane, which keeps only the plain bar.
    expect(within(textLane).getByRole("button", { name: /Big title/ })).toBeInTheDocument();
    expect(within(textLane).queryByText(/word0|word1|word2/)).not.toBeInTheDocument();
  });

  it("locks narration-caption timing (no trim handles, server-pinned) while a cue caption stays editable", () => {
    const bars = [narrationCaptionBar(0), cueCaptionBar];
    render(
      <EditorTimelineBody {...baseProps({ textBars: bars, captionsExpanded: true })} />,
    );
    const captionsLane = screen.getByTestId("editor-captions-lane");

    const lockedButton = within(captionsLane).getByRole("button", { name: /^Caption 1,/ });
    expect(lockedButton).toHaveAttribute("title", "Caption timing follows your narration.");
    expect(
      within(captionsLane).getByLabelText("Caption timing locked"),
    ).toBeInTheDocument();

    const editableButton = within(captionsLane).getByRole("button", { name: /^Caption 2,/ });
    expect(editableButton).not.toHaveAttribute("title");
  });

  it("clicking an expanded caption selects it (no drawer collision) — collapsed clicking opens the caption tool", () => {
    const onSelect = jest.fn();
    const onOpenCaptionCue = jest.fn();
    const bars = [narrationCaptionBar(0)];

    const { rerender } = render(
      <EditorTimelineBody
        {...baseProps({ textBars: bars, captionsExpanded: true, onSelect, onOpenCaptionCue })}
      />,
    );
    screen.getByRole("button", { name: /^Caption 1,/ }).click();
    expect(onSelect).toHaveBeenCalledWith("text", "narration-caption-0");
    expect(onOpenCaptionCue).not.toHaveBeenCalled();

    rerender(
      <EditorTimelineBody
        {...baseProps({
          textBars: bars,
          captionsExpanded: false,
          onSelect,
          onOpenCaptionCue,
        })}
      />,
    );
    screen.getByRole("button", { name: /Caption at/ }).click();
    expect(onOpenCaptionCue).toHaveBeenCalledWith("narration-caption-0");
  });
});
