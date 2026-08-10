/**
 * EditorTimelineBody — carousel-moment block chip (Lane C, carousel-blocks
 * train). Covers: the chip renders at the spliced window with a distinct
 * label, click opens the panel as inspector (`onSelectCarousel`), and
 * dropping it on the video lane resolves a position from WHICH THIRD of the
 * lane it landed in (`onSetCarouselPosition`) — the lane's three "drop
 * targets" are the thirds of the visible track, not three separate widgets.
 */
import "@testing-library/jest-dom";
import { fireEvent, render, screen } from "@testing-library/react";

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

// jsdom's DragEvent doesn't accept `clientX` via the init dict the way
// MouseEvent does (fireEvent.drop(el, {clientX}) silently drops it, leaving
// e.clientX undefined) — build the event and force the property instead.
function dispatchDragEventAt(target: HTMLElement, type: "dragover" | "drop", clientX: number) {
  const event = new Event(type, { bubbles: true, cancelable: true });
  Object.defineProperty(event, "clientX", { value: clientX });
  fireEvent(target, event);
}

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

// Four 2s clips, no-grid (sequential) layout — jsdom's zero viewport width
// clamps the fit scale to MIN_PX_PER_SECOND (4px/s), so the 8s track is a
// deterministic 32px wide regardless of the test runner's layout engine.
const FOUR_SLOTS: DraftSlot[] = [
  slot({ key: "a", clipIndex: 0 }),
  slot({ key: "b", clipIndex: 1 }),
  slot({ key: "c", clipIndex: 2 }),
  slot({ key: "d", clipIndex: 0 }),
];

function baseProps(over: Partial<EditorTimelineBodyProps> = {}): EditorTimelineBodyProps {
  return {
    durationS: 8,
    currentTimeS: 0,
    zoom: 1,
    selection: null,
    onSelect: jest.fn(),
    onClear: jest.fn(),
    textBars: [],
    visualBlocks: [],
    slots: FOUR_SLOTS,
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

describe("EditorTimelineBody — carousel-moment block chip", () => {
  it("renders nothing when no carousel block is staged", () => {
    render(<EditorTimelineBody {...baseProps()} />);
    expect(screen.queryByText(/Carousel ·/)).not.toBeInTheDocument();
  });

  it("renders the chip labeled with the effect name at the resolved window", () => {
    render(
      <EditorTimelineBody
        {...baseProps({
          carouselBlock: {
            id: "carousel-block",
            effectLabel: "cover flow",
            durationS: 3,
            position: "intro",
          },
        })}
      />,
    );
    const chip = screen.getByRole("button", { name: /Carousel block, cover flow/i });
    expect(chip).toBeInTheDocument();
    expect(chip).toHaveAttribute("data-editor-bar-kind", "carousel");
  });

  it("clicking the chip opens the panel as inspector (onSelectCarousel)", () => {
    const onSelectCarousel = jest.fn();
    render(
      <EditorTimelineBody
        {...baseProps({
          carouselBlock: {
            id: "carousel-block",
            effectLabel: "scale sweep",
            durationS: 3,
            position: "middle",
          },
          onSelectCarousel,
        })}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /Carousel block/i }));
    expect(onSelectCarousel).toHaveBeenCalledTimes(1);
  });

  it("shows the selected Carousel with the shared lime timeline treatment", () => {
    render(
      <EditorTimelineBody
        {...baseProps({
          carouselBlock: {
            id: "carousel-block",
            effectLabel: "scale sweep",
            durationS: 3,
            position: "middle",
          },
          selection: { kind: "carousel", id: "carousel-block" },
        })}
      />,
    );

    const chip = screen.getByRole("button", { name: /Carousel block/i });
    expect(chip).toHaveAttribute("aria-pressed", "true");
    expect(chip.className).toContain("outline-lime-500");
  });

  it("Enter/Space on the chip also opens the panel (keyboard access, not just drag)", () => {
    const onSelectCarousel = jest.fn();
    render(
      <EditorTimelineBody
        {...baseProps({
          carouselBlock: {
            id: "carousel-block",
            effectLabel: "scale sweep",
            durationS: 3,
            position: "middle",
          },
          onSelectCarousel,
        })}
      />,
    );
    fireEvent.keyDown(screen.getByRole("button", { name: /Carousel block/i }), { key: "Enter" });
    expect(onSelectCarousel).toHaveBeenCalledTimes(1);
  });

  it.each([
    // 8s track @ 4px/s = 32px total. Thirds: [0,10.67) intro, [10.67,21.33) middle, [21.33,32] outro.
    [2, "intro"],
    [16, "middle"],
    [30, "outro"],
  ] as const)("dropping at clientX=%i on the video lane resolves position=%s", (clientX, expected) => {
    const onSetCarouselPosition = jest.fn();
    render(
      <EditorTimelineBody
        {...baseProps({
          carouselBlock: {
            id: "carousel-block",
            effectLabel: "scale sweep",
            durationS: 3,
            position: "middle",
          },
          onSetCarouselPosition,
        })}
      />,
    );
    const chip = screen.getByRole("button", { name: /Carousel block/i });
    const lane = chip.parentElement as HTMLElement;
    dispatchDragEventAt(lane, "dragover", clientX);
    dispatchDragEventAt(lane, "drop", clientX);
    expect(onSetCarouselPosition).toHaveBeenCalledWith(expected);
  });

  it("does not resolve a drop when no carousel block is staged", () => {
    const onSetCarouselPosition = jest.fn();
    const { container } = render(
      <EditorTimelineBody {...baseProps({ onSetCarouselPosition })} />,
    );
    const lane = container.querySelector('[data-editor-bar-kind="clip"]')
      ?.parentElement as HTMLElement;
    dispatchDragEventAt(lane, "dragover", 16);
    dispatchDragEventAt(lane, "drop", 16);
    expect(onSetCarouselPosition).not.toHaveBeenCalled();
  });

  it("keeps an unavailable Carousel selectable but prevents repositioning", () => {
    const onSelectCarousel = jest.fn();
    const onSetCarouselPosition = jest.fn();
    render(
      <EditorTimelineBody
        {...baseProps({
          carouselBlock: {
            id: "carousel-block",
            effectLabel: "scale sweep",
            durationS: 3,
            position: "middle",
          },
          carouselReadOnly: true,
          carouselDisabledReason: "Carousel is unavailable for this video.",
          onSelectCarousel,
          onSetCarouselPosition,
        })}
      />,
    );

    const chip = screen.getByRole("button", { name: /Carousel block/i });
    expect(chip).toHaveAttribute("draggable", "false");
    expect(chip).toHaveAttribute("title", "Carousel is unavailable for this video.");
    fireEvent.click(chip);
    expect(onSelectCarousel).toHaveBeenCalledTimes(1);

    const lane = chip.parentElement as HTMLElement;
    dispatchDragEventAt(lane, "dragover", 16);
    dispatchDragEventAt(lane, "drop", 16);
    expect(onSetCarouselPosition).not.toHaveBeenCalled();
  });
});
