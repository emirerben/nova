/**
 * Timeline text-lane row marker for AI-authored editorial sequence bars
 * (Lane PR-B). Mirrors the direct-render style already used for the lock /
 * behind-subject markers in this component — role === "generative_sequence"
 * alone is NOT sufficient (the editor's own "split and place" composition
 * tool reuses that role for user-typed text with no source_params), so the
 * marker must only show for bars carrying the backend's "sequence_scene"
 * provenance marker.
 */
import "@testing-library/jest-dom";
import React from "react";
import { render, screen } from "@testing-library/react";

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
import type { TextElementBar } from "@/lib/timeline/text-timeline-reducer";

function baseProps(textBars: TextElementBar[]): EditorTimelineBodyProps {
  return {
    durationS: 10,
    currentTimeS: 0,
    zoom: 1,
    selection: null,
    onSelect: jest.fn(),
    onClear: jest.fn(),
    textBars,
    visualBlocks: [],
    slots: [],
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
  };
}

const AI_SEQUENCE_BAR: TextElementBar = {
  id: "sequence-1",
  text: "edits and I didn't really like CapCut",
  start_s: 0,
  end_s: 2,
  role: "generative_sequence",
  source_params: { source: "sequence_scene", key: "0:1" },
};

const USER_TYPED_SEQUENCE_BAR: TextElementBar = {
  id: "user-sequence-1",
  text: "my own composed beat",
  start_s: 0,
  end_s: 2,
  role: "generative_sequence",
};

const PLAIN_TEXT_BAR: TextElementBar = {
  id: "title-1",
  text: "Big title",
  start_s: 0,
  end_s: 2,
  role: "generative_intro",
};

describe("EditorTimelineBody — AI sequence row marker", () => {
  it("shows the AI sequence marker for a backend-projected sequence_scene bar", () => {
    render(<EditorTimelineBody {...baseProps([AI_SEQUENCE_BAR])} />);
    expect(screen.getByLabelText("AI sequence")).toBeInTheDocument();
  });

  it("hides the marker for a user-typed generative_sequence bar (split & place)", () => {
    render(<EditorTimelineBody {...baseProps([USER_TYPED_SEQUENCE_BAR])} />);
    expect(screen.queryByLabelText("AI sequence")).not.toBeInTheDocument();
  });

  it("hides the marker for a plain text bar", () => {
    render(<EditorTimelineBody {...baseProps([PLAIN_TEXT_BAR])} />);
    expect(screen.queryByLabelText("AI sequence")).not.toBeInTheDocument();
  });
});
