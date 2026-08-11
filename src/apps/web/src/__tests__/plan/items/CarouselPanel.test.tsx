/**
 * CarouselPanel (carousel-as-a-staged-block) — panel primitive tests.
 *
 * Lane C (carousel-blocks train) reshaped this panel from a dispatch-on-Apply
 * form (collect fields locally, submit on click, no undo) into an inspector:
 * every control patches the FULL config immediately via `onChange` — no
 * submit button, no confirm dialog. Gating (capable/reason) is exercised
 * through ToolDrawer's entry-point button, not this file — CarouselPanel is
 * mounted by the shared right inspector only after the caller has decided
 * the feature is usable. This suite covers: effect/mode/position/duration/
 * transition controls firing an immediate onChange with the merged config,
 * prefill from an existing `current` moment, focus-tile selection (including
 * "Let Nova pick"), Remove (one click, no confirm), the legacy "stills"
 * gate (every control but Mode disabled until a real mode is picked), and
 * that switching Mode resolves the gate.
 */

import "@testing-library/jest-dom";
import { useState } from "react";
import { fireEvent, render, screen, within } from "@testing-library/react";
import CarouselPanel, {
  createDefaultCarouselMoment,
  type CarouselClipThumb,
  type CarouselPanelControl,
} from "@/app/plan/items/[id]/_editor/CarouselPanel";
import { naturalFocusTimelineLengthS } from "@/app/plan/items/[id]/_editor/carousel-preview-impl/geometry";
import type { CarouselMoment } from "@/lib/plan-api";

const CLIPS: CarouselClipThumb[] = [
  { clipIndex: 0, label: "Clip 1", signedUrl: "https://cdn.example/clip0.mp4" },
  { clipIndex: 1, label: "Clip 2", signedUrl: null },
  { clipIndex: 2, label: "Clip 3", signedUrl: "https://cdn.example/clip2.mp4" },
];

// Same clamp CarouselPanel applies (Math.max(2, Math.min(ceil(natural), 15)))
// to the SAME engine call the panel makes — computed here rather than
// hardcoded so this suite doesn't drift if the choreography engine's pacing
// is ever tuned.
function expectedFocusDefaultDurationS(nCards: number, focusClipIndex: number | null): number {
  const naturalS = naturalFocusTimelineLengthS(nCards, focusClipIndex);
  return Math.max(2, Math.min(Math.ceil(naturalS), 15));
}

function makeControl(overrides: Partial<CarouselPanelControl> = {}): CarouselPanelControl {
  return {
    capable: true,
    reason: null,
    current: null,
    clips: CLIPS,
    onChange: jest.fn(),
    onRemove: jest.fn(),
    ...overrides,
  };
}

describe("CarouselPanel", () => {
  it("authors defaults with sparse active source identities", () => {
    expect(createDefaultCarouselMoment([2, 5]).sequence?.map((item) => item.clip_index)).toEqual([
      2,
      5,
    ]);
  });
  it("a brand-new moment defaults to an ordered ripple sequence with independent boundaries", () => {
    render(<CarouselPanel control={makeControl()} />);

    expect(screen.getByRole("radio", { name: "Scale sweep effect" })).toHaveAttribute(
      "aria-checked",
      "true",
    );
    expect(screen.getByRole("radio", { name: "Scale sweep effect" })).toHaveClass(
      "focus-visible:outline-lime-500",
    );
    expect(screen.getByRole("button", { name: "Focus" })).toHaveAttribute("aria-pressed", "true");
    const sequence = screen.getByLabelText("Carousel video sequence");
    expect(within(sequence).getByText("1. Clip 1")).toBeInTheDocument();
    expect(within(sequence).getByText("2. Clip 2")).toBeInTheDocument();
    expect(within(sequence).getByText("3. Clip 3")).toBeInTheDocument();
    expect(screen.getByLabelText("Clip 1 hold seconds")).toHaveAttribute("step", "0.1");
    expect(screen.getByRole("button", { name: "Middle" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByLabelText("Carousel length in seconds")).toHaveValue(
      String(expectedFocusDefaultDurationS(CLIPS.length, null)),
    );
    expect(within(screen.getByRole("group", { name: "Entry transition" })).getByRole("button", { name: "Crossfade" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(within(screen.getByRole("group", { name: "Exit transition" })).getByRole("button", { name: "Crossfade" })).toHaveAttribute("aria-pressed", "true");
    // No submit step and nothing to remove yet.
    expect(screen.queryByRole("button", { name: "Remove carousel" })).not.toBeInTheDocument();
  });

  it("picking an effect stages the FULL config immediately (no submit button)", () => {
    const onChange = jest.fn();
    render(<CarouselPanel control={makeControl({ onChange })} />);

    fireEvent.click(screen.getByRole("radio", { name: "Cover flow effect" }));

    expect(onChange).toHaveBeenCalledTimes(1);
    expect(onChange).toHaveBeenCalledWith(expect.objectContaining({
      effect: "cover_flow",
      mode: "focus",
      focus_clip_index: null,
      position: "middle",
      duration_s: expectedFocusDefaultDurationS(CLIPS.length, null),
      transition: "crossfade",
      timing_model: "ripple_v1",
      sequence: [
        { clip_index: 0, hold_s: expect.any(Number) },
        { clip_index: 1, hold_s: expect.any(Number) },
        { clip_index: 2, hold_s: expect.any(Number) },
      ],
    } satisfies Partial<CarouselMoment>));
  });

  it("prefills every control from an existing carousel_moment", () => {
    const current: CarouselMoment = {
      effect: "cover_flow",
      mode: "rolling",
      focus_clip_index: 1,
      position: "outro",
      duration_s: 9,
      transition: "none",
    };
    render(<CarouselPanel control={makeControl({ current })} />);

    expect(screen.getByRole("radio", { name: "Cover flow effect" })).toHaveAttribute(
      "aria-checked",
      "true",
    );
    expect(screen.getByRole("button", { name: "Rolling" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(screen.getByRole("button", { name: "Outro" })).toHaveAttribute("aria-pressed", "true");
    expect(within(screen.getByRole("group", { name: "Entry transition" })).getByRole("button", { name: "Hard cut" })).toHaveAttribute("aria-pressed", "true");
    expect(within(screen.getByRole("group", { name: "Exit transition" })).getByRole("button", { name: "Hard cut" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByLabelText("Carousel length in seconds")).toHaveValue("9");
    expect(screen.getByRole("button", { name: "Remove carousel" })).toBeInTheDocument();
    expect(screen.getByLabelText("Carousel video sequence")).toBeInTheDocument();
  });

  it("upgrades the legacy focus shape to an all-video ordered sequence", () => {
    const current = {
      mode: "focus",
      focus: [{ card_index: 2 }],
    } as unknown as CarouselMoment;
    render(<CarouselPanel control={makeControl({ current })} />);

    expect(screen.getByText("1. Clip 1")).toBeInTheDocument();
    expect(screen.getByText("2. Clip 2")).toBeInTheDocument();
    expect(screen.getByText("3. Clip 3")).toBeInTheDocument();
  });

  it("reorders videos and preserves the authored sequence order", () => {
    const onChange = jest.fn();
    render(<CarouselPanel control={makeControl({ onChange })} />);

    fireEvent.click(screen.getByRole("button", { name: "Move Clip 2 earlier" }));

    expect(onChange).toHaveBeenCalledWith(
      expect.objectContaining({
        timing_model: "ripple_v1",
        sequence: [
          expect.objectContaining({ clip_index: 1 }),
          expect.objectContaining({ clip_index: 0 }),
          expect.objectContaining({ clip_index: 2 }),
        ],
      }),
    );
  });

  it("removes and re-adds videos without losing per-video dwell controls", () => {
    const onChange = jest.fn();
    const Controlled = () => {
      const [current, setCurrent] = useState<CarouselMoment | null>(null);
      return <CarouselPanel control={makeControl({ current, onChange: (next) => (onChange(next), setCurrent(next)) })} />;
    };
    render(<Controlled />);
    fireEvent.click(screen.getByRole("button", { name: "Remove Clip 3 from sequence" }));
    fireEvent.click(screen.getByRole("button", { name: "+ Clip 3" }));

    expect(onChange).toHaveBeenLastCalledWith(expect.objectContaining({
      sequence: [
        expect.objectContaining({ clip_index: 0 }),
        expect.objectContaining({ clip_index: 1 }),
        { clip_index: 2, hold_s: 2 },
      ],
    }));
  });

  it("switching to Rolling stages a null focus_clip_index regardless of prior selection", () => {
    const onChange = jest.fn();
    const current: CarouselMoment = { mode: "focus", focus_clip_index: 0 };
    render(<CarouselPanel control={makeControl({ current, onChange })} />);

    fireEvent.click(screen.getByRole("button", { name: "Rolling" }));

    expect(onChange).toHaveBeenCalledWith(
      expect.objectContaining({ mode: "rolling", focus_clip_index: null }),
    );
  });

  it("position, length, and independent boundary controls stage the merged config immediately", () => {
    const onChange = jest.fn();
    render(<CarouselPanel control={makeControl({ onChange })} />);

    fireEvent.click(screen.getByRole("button", { name: "Intro" }));
    expect(onChange).toHaveBeenLastCalledWith(expect.objectContaining({ position: "intro" }));

    fireEvent.change(screen.getByLabelText("Carousel length in seconds"), {
      target: { value: "12" },
    });
    expect(onChange).toHaveBeenLastCalledWith(expect.objectContaining({ duration_s: 12 }));

    fireEvent.click(within(screen.getByRole("group", { name: "Entry transition" })).getByRole("button", { name: "Hard cut" }));
    expect(onChange).toHaveBeenLastCalledWith(expect.objectContaining({ transition_in: "none" }));

    fireEvent.click(within(screen.getByRole("group", { name: "Exit transition" })).getByRole("button", { name: "Hard cut" }));
    expect(onChange).toHaveBeenLastCalledWith(expect.objectContaining({ transition_out: "none" }));

    expect(onChange).toHaveBeenCalledTimes(4);
  });

  it("switching from Rolling to Focus resets Length to the focus arc's natural length, overriding an explicitly customized rolling duration", () => {
    const onChange = jest.fn();
    // A rolling duration deliberately customized away from the 6s default —
    // proves the reset isn't just "duration_s happened to be unset".
    const current: CarouselMoment = { mode: "rolling", duration_s: 10 };
    render(<CarouselPanel control={makeControl({ current, onChange })} />);

    fireEvent.click(screen.getByRole("button", { name: "Focus" }));

    const expected = expectedFocusDefaultDurationS(CLIPS.length, null);
    expect(onChange).toHaveBeenCalledWith(
      expect.objectContaining({ mode: "focus", duration_s: expected }),
    );
  });

  it("rolling mode is untouched: switching Focus -> Rolling keeps rolling's flat 6s default (no natural-length logic applied while rolling), even overriding a customized focus duration", () => {
    const onChange = jest.fn();
    // A focus duration deliberately customized away from its natural
    // default — switching to Rolling should still land on the flat 6s.
    const current: CarouselMoment = { mode: "focus", duration_s: 14 };
    const Controlled = () => {
      const [c, setC] = useState<CarouselMoment | null>(current);
      return (
        <CarouselPanel
          control={makeControl({ current: c, onChange: (next) => (onChange(next), setC(next)) })}
        />
      );
    };
    render(<Controlled />);

    fireEvent.click(screen.getByRole("button", { name: "Rolling" }));

    expect(onChange).toHaveBeenLastCalledWith(
      expect.objectContaining({ mode: "rolling", duration_s: 6 }),
    );
    expect(screen.getByLabelText("Carousel length in seconds")).toHaveValue("6");
  });

  it("Length hint: hidden when the chosen duration already covers the natural focus-arc length, shown when it's shorter", () => {
    const natural = naturalFocusTimelineLengthS(CLIPS.length, null);
    const shortEnough = Math.max(2, Math.floor(natural) - 1); // strictly below natural
    const longEnough = Math.min(15, Math.ceil(natural) + 2); // at/above natural, within DURATION_MAX

    const { rerender } = render(
      <CarouselPanel
        control={makeControl({ current: { mode: "focus", duration_s: longEnough } })}
      />,
    );
    expect(screen.queryByText(/Focus zoom needs/)).not.toBeInTheDocument();

    rerender(
      <CarouselPanel
        control={makeControl({ current: { mode: "focus", duration_s: shortEnough } })}
      />,
    );
    expect(
      screen.getByText(`Focus zoom needs ~${Math.ceil(natural)}s — shorter lengths cut it off`),
    ).toBeInTheDocument();
  });

  it("Length hint never appears in rolling mode, regardless of how short the duration is", () => {
    render(
      <CarouselPanel
        control={makeControl({ current: { mode: "rolling", duration_s: 2 } })}
      />,
    );
    expect(screen.queryByText(/Focus zoom needs/)).not.toBeInTheDocument();
  });

  it('prefilled with a legacy "stills" moment: neither Mode button is pressed, every other control is disabled, and a hint explains why', () => {
    const onChange = jest.fn();
    const current: CarouselMoment = {
      effect: "flipbook",
      position: "middle",
      duration_s: 5,
      // "stills" is a legal *persisted* mode (auto-authored moments can land
      // on it — see director.py) but there's no Focus/Rolling button for it,
      // so CarouselMoment's `mode` union only models the two write-able
      // values. Cast narrowly to simulate the real prefill shape.
      mode: "stills" as unknown as CarouselMoment["mode"],
    };
    render(<CarouselPanel control={makeControl({ current, onChange })} />);

    expect(screen.getByRole("button", { name: "Focus" })).toHaveAttribute(
      "aria-pressed",
      "false",
    );
    expect(screen.getByRole("button", { name: "Rolling" })).toHaveAttribute(
      "aria-pressed",
      "false",
    );
    // No pickable mode yet, so the focus-tile strip (only shown for "focus") stays hidden too.
    expect(screen.queryByRole("radiogroup", { name: "Focus clip" })).not.toBeInTheDocument();
    expect(
      screen.getByText("This moment uses a legacy static style — pick a mode to update it."),
    ).toBeInTheDocument();

    // Every control but Mode is gated until a real mode is picked.
    expect(screen.getByRole("radio", { name: "Flipbook effect" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Middle" })).toBeDisabled();
    expect(screen.getByLabelText("Carousel length in seconds")).toBeDisabled();
    expect(
      within(screen.getByRole("group", { name: "Entry transition" })).getByRole("button", {
        name: "Crossfade",
      }),
    ).toBeDisabled();
    expect(
      within(screen.getByRole("group", { name: "Exit transition" })).getByRole("button", {
        name: "Crossfade",
      }),
    ).toBeDisabled();
    expect(screen.getByRole("button", { name: "Focus" })).not.toBeDisabled();
    expect(screen.getByRole("button", { name: "Rolling" })).not.toBeDisabled();

    fireEvent.click(screen.getByRole("radio", { name: "Flipbook effect" }));
    expect(onChange).not.toHaveBeenCalled();

    // Picking a mode is always live (it's the one control that resolves the
    // gate) and stages the fully-resolved config in one shot. The panel is
    // controlled off `control.current`, so the gate itself only clears once
    // the caller re-renders with the newly-staged current — an EditorShell
    // integration concern, not this component's own state.
    fireEvent.click(screen.getByRole("button", { name: "Focus" }));
    expect(onChange).toHaveBeenCalledWith(
      expect.objectContaining({ mode: "focus", effect: "flipbook" }),
    );
  });

  it("resolving the stills gate (parent re-renders with the newly-staged current) clears every disabled control", () => {
    const Controlled = () => {
      const [current, setCurrent] = useState<CarouselMoment>({
        effect: "flipbook",
        position: "middle",
        duration_s: 5,
        mode: "stills" as unknown as CarouselMoment["mode"],
      });
      return <CarouselPanel control={makeControl({ current, onChange: setCurrent })} />;
    };
    render(<Controlled />);

    expect(screen.getByRole("radio", { name: "Flipbook effect" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Focus" }));

    expect(
      screen.queryByText("This moment uses a legacy static style — pick a mode to update it."),
    ).not.toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "Flipbook effect" })).not.toBeDisabled();
    expect(screen.getByRole("button", { name: "Focus" })).toHaveAttribute("aria-pressed", "true");
  });

  it("Remove only appears when a moment already exists, fires once, with no confirm dialog", () => {
    const onRemove = jest.fn();
    const { rerender } = render(
      <CarouselPanel control={makeControl({ onRemove })} />,
    );
    expect(screen.queryByRole("button", { name: "Remove carousel" })).not.toBeInTheDocument();

    rerender(
      <CarouselPanel control={makeControl({ current: { effect: "flipbook" }, onRemove })} />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Remove carousel" }));
    expect(onRemove).toHaveBeenCalledTimes(1);
  });
});
