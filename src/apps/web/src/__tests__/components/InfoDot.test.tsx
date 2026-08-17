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
import { fireEvent, render, screen } from "@testing-library/react";
import { InfoDot } from "@/components/ui/InfoDot";

beforeAll(() => {
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
