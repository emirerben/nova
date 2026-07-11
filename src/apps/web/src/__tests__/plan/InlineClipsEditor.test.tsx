import React, { useEffect, useMemo, useReducer } from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import "@testing-library/jest-dom";

import { InlineClipsEditor } from "@/app/plan/_components/InlineClipsEditor";
import {
  initEditorState,
  timelineReducer,
  type EditorState,
  type EditorAction,
} from "@/app/generative/timeline-reducer";
import type { TimelineResponse } from "@/lib/generative-api";

class PointerEventPolyfill extends MouseEvent {
  pointerId: number;
  pointerType: string;
  isPrimary: boolean;

  constructor(type: string, init: PointerEventInit = {}) {
    super(type, init);
    this.pointerId = init.pointerId ?? 1;
    this.pointerType = init.pointerType ?? "mouse";
    this.isPrimary = init.isPrimary ?? true;
  }
}

beforeAll(() => {
  (window as unknown as Record<string, unknown>).PointerEvent = PointerEventPolyfill;
  Object.defineProperty(HTMLElement.prototype, "setPointerCapture", {
    value: jest.fn(),
    configurable: true,
    writable: true,
  });
});

beforeEach(() => {
  jest.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue({
    width: 300,
    height: 56,
    top: 0,
    left: 0,
    right: 300,
    bottom: 56,
    x: 0,
    y: 0,
    toJSON: () => ({}),
  } as DOMRect);
});

afterEach(() => {
  jest.restoreAllMocks();
});

function timeline(): TimelineResponse {
  return {
    editable: true,
    reason: null,
    beat_grid: [],
    total_duration_s: 4,
    has_user_edits: false,
    slots: [
      {
        slot_id: "s1",
        clip_index: 0,
        source_gcs_path: "music-uploads/a.mp4",
        source_duration_s: 10,
        in_s: 0,
        duration_s: 4,
        duration_beats: null,
        order: 0,
        moment_energy: null,
        moment_description: null,
      },
    ],
    clips: [{ clip_index: 0, signed_url: null, duration_s: 10, used: true }],
  };
}

function gridTimeline(): TimelineResponse {
  return {
    editable: true,
    reason: null,
    beat_grid: [0, 0.5, 1, 1.5, 2],
    total_duration_s: 1,
    has_user_edits: false,
    slots: [
      {
        slot_id: "s1",
        clip_index: 0,
        source_gcs_path: "music-uploads/a.mp4",
        source_duration_s: 10,
        in_s: 0,
        duration_s: 1,
        duration_beats: 2,
        order: 0,
        moment_energy: null,
        moment_description: null,
      },
    ],
    clips: [{ clip_index: 0, signed_url: null, duration_s: 10, used: true }],
  };
}

function oneBeatGridTimeline(): TimelineResponse {
  return {
    ...gridTimeline(),
    beat_grid: [0, 0.5],
    slots: [
      {
        ...gridTimeline().slots[0],
        duration_beats: 1,
      },
    ],
  };
}

function Harness({
  onAction,
  onState,
  data = timeline(),
}: {
  onAction: (action: EditorAction) => void;
  onState?: (state: EditorState) => void;
  data?: TimelineResponse;
}) {
  const initial = useMemo(() => initEditorState(data), [data]);
  const [state, rawDispatch] = useReducer(timelineReducer, initial);
  const dispatch = (action: EditorAction) => {
    onAction(action);
    rawDispatch(action);
  };
  useEffect(() => {
    onState?.(state);
  }, [onState, state]);

  return (
    <InlineClipsEditor
      ownerId="owner-1"
      variantId="variant-1"
      base="generative"
      onRenderEnqueued={jest.fn()}
      externalState={state}
      externalDispatch={dispatch}
      externalClips={data.clips}
    />
  );
}

function leftHandle() {
  return document.querySelector('[data-inline-trim-handle="left-s1"]') as HTMLElement;
}

describe("InlineClipsEditor pointer trim", () => {
  it("pointer drag on a trim handle updates the trim value", () => {
    const actions: EditorAction[] = [];
    render(<Harness onAction={(action) => actions.push(action)} />);

    fireEvent.pointerDown(leftHandle(), {
      clientX: 0,
      clientY: 0,
      pointerId: 1,
      pointerType: "mouse",
      isPrimary: true,
    });
    fireEvent.pointerMove(window, {
      clientX: 30,
      clientY: 0,
      pointerId: 1,
      pointerType: "mouse",
      isPrimary: true,
    });
    fireEvent.pointerUp(window, {
      clientX: 30,
      clientY: 0,
      pointerId: 1,
      pointerType: "mouse",
      isPrimary: true,
    });

    expect(actions).toContainEqual(
      expect.objectContaining({ type: "SET_IN", key: "s1", record: true, inS: 0.4 }),
    );
    expect(actions).toHaveLength(1);
  });

  it("pointercancel mid-drag keeps the one-gesture history protocol", () => {
    const actions: EditorAction[] = [];
    render(<Harness onAction={(action) => actions.push(action)} />);

    fireEvent.pointerDown(leftHandle(), {
      clientX: 0,
      clientY: 0,
      pointerId: 7,
      pointerType: "touch",
      isPrimary: true,
    });
    fireEvent.pointerMove(window, {
      clientX: 45,
      clientY: 3,
      pointerId: 7,
      pointerType: "touch",
      isPrimary: true,
    });
    fireEvent.pointerCancel(window, {
      clientX: 45,
      clientY: 3,
      pointerId: 7,
      pointerType: "touch",
      isPrimary: true,
    });

    expect(actions.at(-1)).toEqual(
      expect.objectContaining({ type: "SET_IN", key: "s1", record: true, inS: 0.6 }),
    );
    expect(actions).toHaveLength(1);
  });

  it("after a drag, undo restores the pre-drag trim from exactly one history entry", () => {
    const actions: EditorAction[] = [];
    const states: EditorState[] = [];
    const latestState = () => {
      const state = states.at(-1);
      if (!state) throw new Error("Harness did not publish state");
      return state;
    };
    render(
      <Harness
        onAction={(action) => actions.push(action)}
        onState={(state) => {
          states.push(state);
        }}
      />,
    );

    fireEvent.pointerDown(leftHandle(), {
      clientX: 0,
      clientY: 0,
      pointerId: 1,
      pointerType: "mouse",
      isPrimary: true,
    });
    fireEvent.pointerMove(window, {
      clientX: 30,
      clientY: 0,
      pointerId: 1,
      pointerType: "mouse",
      isPrimary: true,
    });
    fireEvent.pointerUp(window, {
      clientX: 30,
      clientY: 0,
      pointerId: 1,
      pointerType: "mouse",
      isPrimary: true,
    });

    expect(latestState().past).toHaveLength(1);
    expect(latestState().slots[0].inS).toBeCloseTo(0.4);

    fireEvent.click(screen.getByRole("button", { name: /undo/i }));

    expect(latestState().past).toHaveLength(0);
    expect(latestState().slots[0].inS).toBeCloseTo(0);
    expect(latestState().slots[0].durationS).toBeCloseTo(4);
  });

  it("touch tap under 8px dispatches zero patches", () => {
    const actions: EditorAction[] = [];
    render(<Harness onAction={(action) => actions.push(action)} />);

    fireEvent.pointerDown(leftHandle(), {
      clientX: 0,
      clientY: 0,
      pointerId: 9,
      pointerType: "touch",
      isPrimary: true,
    });
    fireEvent.pointerMove(window, {
      clientX: 4,
      clientY: 3,
      pointerId: 9,
      pointerType: "touch",
      isPrimary: true,
    });
    fireEvent.pointerUp(window, {
      clientX: 4,
      clientY: 3,
      pointerId: 9,
      pointerType: "touch",
      isPrimary: true,
    });

    expect(actions).toEqual([]);
  });

  it("pointercancel without intent dispatches zero patches", () => {
    const actions: EditorAction[] = [];
    render(<Harness onAction={(action) => actions.push(action)} />);

    fireEvent.pointerDown(leftHandle(), {
      clientX: 0,
      clientY: 0,
      pointerId: 9,
      pointerType: "touch",
      isPrimary: true,
    });
    fireEvent.pointerMove(window, {
      clientX: 4,
      clientY: 3,
      pointerId: 9,
      pointerType: "touch",
      isPrimary: true,
    });
    fireEvent.pointerCancel(window, {
      clientX: 4,
      clientY: 3,
      pointerId: 9,
      pointerType: "touch",
      isPrimary: true,
    });

    expect(actions).toEqual([]);
  });

  it("ignores a second pointer while a drag is active", () => {
    const actions: EditorAction[] = [];
    render(<Harness onAction={(action) => actions.push(action)} />);

    fireEvent.pointerDown(leftHandle(), {
      clientX: 0,
      clientY: 0,
      pointerId: 1,
      pointerType: "touch",
      isPrimary: true,
    });
    fireEvent.pointerDown(document.querySelector('[data-inline-trim-handle="right-s1"]') as HTMLElement, {
      clientX: 300,
      clientY: 0,
      pointerId: 2,
      pointerType: "touch",
      isPrimary: false,
    });
    fireEvent.pointerMove(window, {
      clientX: 260,
      clientY: 0,
      pointerId: 2,
      pointerType: "touch",
      isPrimary: false,
    });
    fireEvent.pointerUp(window, {
      clientX: 260,
      clientY: 0,
      pointerId: 2,
      pointerType: "touch",
      isPrimary: false,
    });

    expect(actions).toEqual([]);
  });

  it("completed handle drag does not suppress the next bar-body click", () => {
    const actions: EditorAction[] = [];
    render(<Harness onAction={(action) => actions.push(action)} />);

    fireEvent.pointerDown(leftHandle(), {
      clientX: 0,
      clientY: 0,
      pointerId: 11,
      pointerType: "mouse",
      isPrimary: true,
    });
    fireEvent.pointerMove(window, {
      clientX: 30,
      clientY: 0,
      pointerId: 11,
      pointerType: "mouse",
      isPrimary: true,
    });
    fireEvent.pointerUp(window, {
      clientX: 30,
      clientY: 0,
      pointerId: 11,
      pointerType: "mouse",
      isPrimary: true,
    });

    fireEvent.click(screen.getByText("C0"));

    expect(screen.getByText(/Clip 0 source/)).toBeInTheDocument();
  });

  it("completed handle drag suppresses the synthetic click on the dragged handle", () => {
    const actions: EditorAction[] = [];
    render(<Harness onAction={(action) => actions.push(action)} />);
    const handle = leftHandle();
    const handleClick = jest.fn();
    handle.addEventListener("click", handleClick);

    fireEvent.pointerDown(handle, {
      clientX: 0,
      clientY: 0,
      pointerId: 11,
      pointerType: "mouse",
      isPrimary: true,
    });
    fireEvent.pointerMove(window, {
      clientX: 30,
      clientY: 0,
      pointerId: 11,
      pointerType: "mouse",
      isPrimary: true,
    });
    fireEvent.pointerUp(window, {
      clientX: 30,
      clientY: 0,
      pointerId: 11,
      pointerType: "mouse",
      isPrimary: true,
    });

    fireEvent.click(handle);

    expect(handleClick).not.toHaveBeenCalled();
  });
});

describe("InlineClipsEditor source steppers", () => {
  it("grid-slot out steppers dispatch NUDGE ±1 beat", () => {
    const actions: EditorAction[] = [];
    render(<Harness data={gridTimeline()} onAction={(action) => actions.push(action)} />);

    fireEvent.click(screen.getByText("C0"));
    fireEvent.click(screen.getByRole("button", { name: "Nudge out-point later" }));
    fireEvent.click(screen.getByRole("button", { name: "Nudge out-point earlier" }));

    expect(actions).toContainEqual({ type: "NUDGE", key: "s1", delta: 1 });
    expect(actions).toContainEqual({ type: "NUDGE", key: "s1", delta: -1 });
  });

  it("disables steppers at their bounds", () => {
    const actions: EditorAction[] = [];
    render(<Harness data={oneBeatGridTimeline()} onAction={(action) => actions.push(action)} />);

    fireEvent.click(screen.getByText("C0"));

    expect(screen.getByRole("button", { name: "Nudge in-point earlier" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Nudge out-point earlier" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Nudge out-point later" })).toBeDisabled();
  });
});
