/**
 * InfoDot — the ⓘ popover primitive (DESIGN.md §2).
 *
 * Radix Popover positions via floating-ui, which needs ResizeObserver;
 * jsdom has none, so a minimal polyfill is installed here (test-scoped,
 * not in jest.setup.ts — no other suite needs it yet).
 *
 * Close is animation-gated (Radix Presence waits for animationend, which
 * jsdom never fires), so close assertions read the trigger's aria-expanded
 * state rather than waiting for the content to unmount.
 */
import "@testing-library/jest-dom";
import { act, fireEvent, render, screen } from "@testing-library/react";
import { InfoDot } from "@/components/ui/InfoDot";

beforeAll(() => {
  // jsdom has no PointerEvent, so fireEvent.pointerEnter would drop the
  // pointerType init the hover tests rely on. Minimal constructor polyfill.
  if (typeof globalThis.PointerEvent === "undefined") {
    class PE extends MouseEvent {
      pointerType: string;
      constructor(type: string, init: MouseEventInit & { pointerType?: string } = {}) {
        super(type, init);
        this.pointerType = init.pointerType ?? "";
      }
    }
    (globalThis as unknown as Record<string, unknown>).PointerEvent = PE;
  }
  if (typeof globalThis.ResizeObserver === "undefined") {
    class RO {
      observe() {}
      unobserve() {}
      disconnect() {}
    }
    (globalThis as Record<string, unknown>).ResizeObserver = RO;
  }
});

function renderDot() {
  return render(
    <div>
      <span>Background sound</span>
      <InfoDot label="Background sound">
        How loud your clip audio plays under your voice.
      </InfoDot>
    </div>,
  );
}

describe("InfoDot", () => {
  it("renders a labelled, closed trigger", () => {
    renderDot();
    const trigger = screen.getByRole("button", { name: "About Background sound" });
    expect(trigger).toHaveAttribute("aria-expanded", "false");
    expect(
      screen.queryByText(/How loud your clip audio/),
    ).not.toBeInTheDocument();
  });

  it("opens on click and shows the helper text", () => {
    renderDot();
    const trigger = screen.getByRole("button", { name: "About Background sound" });
    fireEvent.click(trigger);
    expect(trigger).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText(/How loud your clip audio/)).toBeInTheDocument();
  });

  it("closes on Escape and restores collapsed state", () => {
    renderDot();
    const trigger = screen.getByRole("button", { name: "About Background sound" });
    fireEvent.click(trigger);
    expect(trigger).toHaveAttribute("aria-expanded", "true");
    fireEvent.keyDown(screen.getByText(/How loud your clip audio/), { key: "Escape" });
    expect(trigger).toHaveAttribute("aria-expanded", "false");
  });

  it("compact size keeps a 32px hit area for dense rows", () => {
    render(
      <InfoDot label="Bed level" size="compact">
        Balances the background bed against your voiceover.
      </InfoDot>,
    );
    const trigger = screen.getByRole("button", { name: "About Bed level" });
    expect(trigger.className).toContain("h-8");
  });
});


describe("InfoDot — desktop hover intent", () => {
  beforeEach(() => jest.useFakeTimers());
  afterEach(() => jest.useRealTimers());

  const trigger = () => screen.getByRole("button", { name: "About Background sound" });

  it("opens after the 150ms hover delay", () => {
    renderDot();
    fireEvent.pointerEnter(trigger(), { pointerType: "mouse" });
    expect(trigger()).toHaveAttribute("aria-expanded", "false");
    act(() => { jest.advanceTimersByTime(160); });
    expect(trigger()).toHaveAttribute("aria-expanded", "true");
  });

  it("closes 200ms after the pointer leaves an unpinned popover", () => {
    renderDot();
    fireEvent.pointerEnter(trigger(), { pointerType: "mouse" });
    act(() => { jest.advanceTimersByTime(160); });
    expect(trigger()).toHaveAttribute("aria-expanded", "true");
    fireEvent.pointerLeave(trigger(), { pointerType: "mouse" });
    act(() => { jest.advanceTimersByTime(210); });
    expect(trigger()).toHaveAttribute("aria-expanded", "false");
  });

  it("click pins a hover-opened popover so pointer-leave keeps it open", () => {
    renderDot();
    fireEvent.pointerEnter(trigger(), { pointerType: "mouse" });
    act(() => { jest.advanceTimersByTime(160); });
    fireEvent.click(trigger());
    fireEvent.pointerLeave(trigger(), { pointerType: "mouse" });
    act(() => { jest.advanceTimersByTime(300); });
    expect(trigger()).toHaveAttribute("aria-expanded", "true");
  });

  it("touch pointers never hover-open — tap still toggles", () => {
    renderDot();
    fireEvent.pointerEnter(trigger(), { pointerType: "touch" });
    act(() => { jest.advanceTimersByTime(300); });
    expect(trigger()).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(trigger());
    expect(trigger()).toHaveAttribute("aria-expanded", "true");
  });
});
