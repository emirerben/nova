/**
 * CarouselPanel (carousel-as-a-moment) — panel primitive tests.
 *
 * Gating (capable/reason) is exercised through ToolDrawer's entry-point
 * button, not this file — CarouselPanel itself is only ever mounted once the
 * caller has already decided the feature is usable. This suite covers:
 * effect/mode/position/duration/transition controls, prefill from an
 * existing `current` moment, focus-tile selection (including "Let Nova
 * pick"), the exact apply payload shape (add vs update vs remove), and the
 * busy-disables-controls state.
 */

import "@testing-library/jest-dom";
import { fireEvent, render, screen } from "@testing-library/react";
import CarouselPanel, {
  type CarouselClipThumb,
  type CarouselPanelControl,
} from "@/app/plan/items/[id]/_editor/CarouselPanel";
import type { CarouselMoment } from "@/lib/plan-api";

const CLIPS: CarouselClipThumb[] = [
  { clipIndex: 0, label: "Clip 1", signedUrl: "https://cdn.example/clip0.mp4" },
  { clipIndex: 1, label: "Clip 2", signedUrl: null },
  { clipIndex: 2, label: "Clip 3", signedUrl: "https://cdn.example/clip2.mp4" },
];

function makeControl(overrides: Partial<CarouselPanelControl> = {}): CarouselPanelControl {
  return {
    capable: true,
    reason: null,
    current: null,
    clips: CLIPS,
    busy: false,
    onApply: jest.fn(),
    onRemove: jest.fn(),
    ...overrides,
  };
}

describe("CarouselPanel", () => {
  it("defaults to scale_sweep / focus / Let Nova pick / middle / 6s / crossfade when adding fresh", () => {
    const onApply = jest.fn();
    const control = makeControl({ onApply });
    render(<CarouselPanel control={control} onBack={jest.fn()} />);

    expect(screen.getByRole("button", { name: "Add carousel" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Add carousel" }));

    expect(onApply).toHaveBeenCalledWith({
      effect: "scale_sweep",
      mode: "focus",
      focus_clip_index: null,
      position: "middle",
      duration_s: 6,
      transition: "crossfade",
    } satisfies CarouselMoment);
  });

  it("prefills every control from an existing carousel_moment and shows Update", () => {
    const current: CarouselMoment = {
      effect: "cover_flow",
      mode: "rolling",
      focus_clip_index: 1,
      position: "outro",
      duration_s: 9,
      transition: "none",
    };
    render(<CarouselPanel control={makeControl({ current })} onBack={jest.fn()} />);

    expect(screen.getByRole("radio", { name: "Cover flow effect" })).toHaveAttribute(
      "aria-checked",
      "true",
    );
    expect(screen.getByRole("button", { name: "Rolling" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(screen.getByRole("button", { name: "Outro" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: "Hard cut" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(screen.getByLabelText("Carousel length in seconds")).toHaveValue("9");
    expect(screen.getByRole("button", { name: "Update carousel" })).toBeInTheDocument();
    // Rolling mode hides the focus-tile strip entirely.
    expect(screen.queryByRole("radiogroup", { name: "Focus clip" })).not.toBeInTheDocument();
  });

  it("BUG A: prefills the focus tile from the legacy `focus` shape when focus_clip_index is absent", () => {
    // A moment persisted before the backend started writing `focus_clip_index`
    // alongside `focus` (see _merge_carousel_moment_override) only carries
    // `focus: [{card_index}]`. The panel must still prefill the chosen tile,
    // not fall back to "Let Nova pick".
    const current = {
      mode: "focus",
      focus: [{ card_index: 2 }],
    } as unknown as CarouselMoment;
    render(<CarouselPanel control={makeControl({ current })} onBack={jest.fn()} />);

    expect(screen.getByRole("radio", { name: "Clip 3" })).toHaveAttribute("aria-checked", "true");
    expect(screen.getByRole("radio", { name: "Let Nova pick" })).toHaveAttribute(
      "aria-checked",
      "false",
    );
  });

  it("focus mode shows the clip strip; selecting a clip sets focus_clip_index", () => {
    const onApply = jest.fn();
    render(<CarouselPanel control={makeControl({ onApply })} onBack={jest.fn()} />);

    fireEvent.click(screen.getByRole("radio", { name: "Clip 2" }));
    fireEvent.click(screen.getByRole("button", { name: "Add carousel" }));

    expect(onApply).toHaveBeenCalledWith(
      expect.objectContaining({ mode: "focus", focus_clip_index: 1 }),
    );
  });

  it('"Let Nova pick" resets focus_clip_index to null', () => {
    const onApply = jest.fn();
    const current: CarouselMoment = { mode: "focus", focus_clip_index: 2 };
    render(<CarouselPanel control={makeControl({ current, onApply })} onBack={jest.fn()} />);

    expect(screen.getByRole("radio", { name: "Clip 3" })).toHaveAttribute("aria-checked", "true");
    fireEvent.click(screen.getByRole("radio", { name: "Let Nova pick" }));
    fireEvent.click(screen.getByRole("button", { name: "Update carousel" }));

    expect(onApply).toHaveBeenCalledWith(
      expect.objectContaining({ focus_clip_index: null }),
    );
  });

  it("switching to Rolling sends a null focus_clip_index regardless of prior selection", () => {
    const onApply = jest.fn();
    const current: CarouselMoment = { mode: "focus", focus_clip_index: 0 };
    render(<CarouselPanel control={makeControl({ current, onApply })} onBack={jest.fn()} />);

    fireEvent.click(screen.getByRole("button", { name: "Rolling" }));
    fireEvent.click(screen.getByRole("button", { name: "Update carousel" }));

    expect(onApply).toHaveBeenCalledWith(
      expect.objectContaining({ mode: "rolling", focus_clip_index: null }),
    );
  });

  it("position, length, and transition controls update the apply payload", () => {
    const onApply = jest.fn();
    render(<CarouselPanel control={makeControl({ onApply })} onBack={jest.fn()} />);

    fireEvent.click(screen.getByRole("button", { name: "Intro" }));
    fireEvent.change(screen.getByLabelText("Carousel length in seconds"), {
      target: { value: "12" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Hard cut" }));
    fireEvent.click(screen.getByRole("button", { name: "Add carousel" }));

    expect(onApply).toHaveBeenCalledWith(
      expect.objectContaining({ position: "intro", duration_s: 12, transition: "none" }),
    );
  });

  it('prefilled with a legacy "stills" moment: neither mode is pressed, Update is disabled, and a hint explains why', () => {
    const onApply = jest.fn();
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
    render(<CarouselPanel control={makeControl({ current, onApply })} onBack={jest.fn()} />);

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
    expect(screen.getByRole("button", { name: "Update carousel" })).toBeDisabled();

    fireEvent.click(screen.getByRole("button", { name: "Update carousel" }));
    expect(onApply).not.toHaveBeenCalled();

    // Picking a mode clears the gate and lets Update proceed.
    fireEvent.click(screen.getByRole("button", { name: "Focus" }));
    expect(
      screen.queryByText("This moment uses a legacy static style — pick a mode to update it."),
    ).not.toBeInTheDocument();
    const updateButton = screen.getByRole("button", { name: "Update carousel" });
    expect(updateButton).not.toBeDisabled();
    fireEvent.click(updateButton);
    expect(onApply).toHaveBeenCalledWith(
      expect.objectContaining({ mode: "focus", effect: "flipbook" }),
    );
  });

  it("Remove only appears when a moment already exists, and calls onRemove", () => {
    const onRemove = jest.fn();
    const { rerender } = render(
      <CarouselPanel control={makeControl({ onRemove })} onBack={jest.fn()} />,
    );
    expect(screen.queryByRole("button", { name: "Remove carousel" })).not.toBeInTheDocument();

    rerender(
      <CarouselPanel
        control={makeControl({ current: { effect: "flipbook" }, onRemove })}
        onBack={jest.fn()}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Remove carousel" }));
    expect(onRemove).toHaveBeenCalledTimes(1);
  });

  it("busy disables every control and the apply/remove buttons", () => {
    render(
      <CarouselPanel
        control={makeControl({ current: { effect: "cards_stack" }, busy: true })}
        onBack={jest.fn()}
      />,
    );
    expect(screen.getByRole("button", { name: "Updating…" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Remove carousel" })).toBeDisabled();
    expect(screen.getByRole("radio", { name: "Scale sweep effect" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Rolling" })).toBeDisabled();
    expect(screen.getByLabelText("Carousel length in seconds")).toBeDisabled();
  });

  it("onBack returns to the Add-a-block grid", () => {
    const onBack = jest.fn();
    render(<CarouselPanel control={makeControl()} onBack={onBack} />);
    fireEvent.click(screen.getByRole("button", { name: /Add a block/ }));
    expect(onBack).toHaveBeenCalledTimes(1);
  });
});
