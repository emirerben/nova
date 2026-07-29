/**
 * Tests for the pocket-editor Sheet primitive (mobile editor Lane A).
 *
 *  1. resolveSheetGesture decision table — handle vs body drags, 60px
 *     threshold, keyboardOpen disables dismiss, horizontal intent never
 *     changes detent, body drags require scrollTop 0.
 *  2. Half detent: role=dialog WITHOUT aria-modal, no scrim, no focus trap.
 *  3. Full detent: aria-modal + scrim; scrim click demotes to half; Tab is
 *     trapped inside the sheet.
 *  4. transportSlot renders only at full detent.
 *  5. Escape calls onClose (half and full).
 *  6. Keyboard promote via the visualViewport mock: shrink at half →
 *     onDetentChange("full"); restore height → back to "half".
 *  7. Absent visualViewport: promote path no-ops, sheet still renders — pins
 *     the unsupported-browser failure mode.
 *  8. Grabber sr button toggles detent.
 *  9. Body scroll locked while open, restored on unmount.
 * 10. Pointer wiring: grabber drag down closes; horizontal drags are inert.
 */

import "@testing-library/jest-dom";
import React from "react";
import { act, fireEvent, render, screen } from "@testing-library/react";

import Sheet, {
  resolveSheetGesture,
  type SheetDetent,
  type SheetGestureAction,
} from "@/app/plan/items/[id]/_editor/Sheet";
import {
  installPointerEventPolyfill,
  installVisualViewportMock,
} from "@/__tests__/utils/viewport-mocks";

// ── jsdom polyfills ───────────────────────────────────────────────────────────

let restorePointerEvents: () => void;
// useFocusTrap filters focusables on offsetParent, which jsdom always reports
// as null — give it a layout-ish answer so the trap is exercisable.
const originalOffsetParent = Object.getOwnPropertyDescriptor(
  HTMLElement.prototype,
  "offsetParent",
);

beforeAll(() => {
  restorePointerEvents = installPointerEventPolyfill();
  Object.defineProperty(HTMLElement.prototype, "offsetParent", {
    configurable: true,
    get(this: HTMLElement) {
      return this.parentElement;
    },
  });
});

afterAll(() => {
  restorePointerEvents();
  if (originalOffsetParent) {
    Object.defineProperty(HTMLElement.prototype, "offsetParent", originalOffsetParent);
  }
});

// ── Harness ───────────────────────────────────────────────────────────────────

type SheetProps = React.ComponentProps<typeof Sheet>;

function renderSheet(overrides: Partial<SheetProps> = {}) {
  const onDetentChange = jest.fn();
  const onClose = jest.fn();
  const ui = (detent: SheetDetent) => (
    <Sheet
      open
      title="Text"
      onDetentChange={onDetentChange}
      onClose={onClose}
      {...overrides}
      detent={detent}
    >
      <button type="button">inside action</button>
      <p>Body content</p>
    </Sheet>
  );
  const utils = render(ui(overrides.detent ?? "half"));
  return {
    ...utils,
    onDetentChange,
    onClose,
    rerenderDetent: (d: SheetDetent) => utils.rerender(ui(d)),
  };
}

// ── 1. resolveSheetGesture decision table ─────────────────────────────────────

describe("resolveSheetGesture", () => {
  const CASES: Array<
    [
      string,
      Parameters<typeof resolveSheetGesture>[0],
      SheetGestureAction,
    ]
  > = [
    // Handle drags always adjust detent (scrollTop ignored).
    ["handle up past threshold at half promotes",
      { startedOnHandle: true, scrollTop: 0, dx: 0, dy: -80, detent: "half", keyboardOpen: false }, "promote"],
    ["handle up at half promotes even mid-scroll",
      { startedOnHandle: true, scrollTop: 120, dx: 0, dy: -80, detent: "half", keyboardOpen: false }, "promote"],
    ["handle down past threshold at half closes",
      { startedOnHandle: true, scrollTop: 0, dx: 0, dy: 80, detent: "half", keyboardOpen: false }, "close"],
    ["handle down at full demotes",
      { startedOnHandle: true, scrollTop: 0, dx: 0, dy: 80, detent: "full", keyboardOpen: false }, "demote"],
    ["handle up at full is a no-op",
      { startedOnHandle: true, scrollTop: 0, dx: 0, dy: -80, detent: "full", keyboardOpen: false }, "none"],
    // 60px threshold.
    ["under-threshold up is a no-op",
      { startedOnHandle: true, scrollTop: 0, dx: 0, dy: -59, detent: "half", keyboardOpen: false }, "none"],
    ["under-threshold down is a no-op",
      { startedOnHandle: true, scrollTop: 0, dx: 0, dy: 59, detent: "half", keyboardOpen: false }, "none"],
    // Keyboard open disables dismiss but not demote.
    ["keyboard open disables drag-to-dismiss",
      { startedOnHandle: true, scrollTop: 0, dx: 0, dy: 80, detent: "half", keyboardOpen: true }, "none"],
    ["keyboard open still allows full→half demote",
      { startedOnHandle: true, scrollTop: 0, dx: 0, dy: 80, detent: "full", keyboardOpen: true }, "demote"],
    // Horizontal intent never changes detent.
    ["horizontal-intent handle drag is inert",
      { startedOnHandle: true, scrollTop: 0, dx: 90, dy: 80, detent: "half", keyboardOpen: false }, "none"],
    ["horizontal-intent body drag is inert",
      { startedOnHandle: false, scrollTop: 0, dx: -100, dy: -70, detent: "half", keyboardOpen: false }, "none"],
    // Body drags require scrollTop 0.
    ["body drag mid-scroll is inert",
      { startedOnHandle: false, scrollTop: 40, dx: 0, dy: 80, detent: "full", keyboardOpen: false }, "none"],
    ["body up at scroll top at half promotes",
      { startedOnHandle: false, scrollTop: 0, dx: 0, dy: -80, detent: "half", keyboardOpen: false }, "promote"],
    ["body down at scroll top at half closes",
      { startedOnHandle: false, scrollTop: 0, dx: 0, dy: 80, detent: "half", keyboardOpen: false }, "close"],
    ["body down at scroll top at full demotes",
      { startedOnHandle: false, scrollTop: 0, dx: 0, dy: 80, detent: "full", keyboardOpen: false }, "demote"],
  ];

  it.each(CASES)("%s", (_label, input, expected) => {
    expect(resolveSheetGesture(input)).toBe(expected);
  });
});

// ── 2. Half-detent modality ───────────────────────────────────────────────────

describe("Sheet — modality", () => {
  it("half detent: dialog without aria-modal, no scrim, no focus trap", () => {
    render(<button data-testid="outside">outside</button>);
    const outside = screen.getByTestId("outside");
    outside.focus();

    renderSheet({ detent: "half" });
    const dialog = screen.getByRole("dialog");
    expect(dialog).toBeInTheDocument();
    expect(dialog).not.toHaveAttribute("aria-modal");
    expect(screen.queryByTestId("pocket-sheet-scrim")).not.toBeInTheDocument();

    // No trap: a Tab keydown at document level is not intercepted and focus
    // stays where it was.
    fireEvent.keyDown(document, { key: "Tab" });
    expect(outside).toHaveFocus();
  });

  // ── 3. Full-detent modality ─────────────────────────────────────────────────

  it("full detent: aria-modal + scrim; scrim click demotes to half", () => {
    const { onDetentChange } = renderSheet({ detent: "full" });
    expect(screen.getByRole("dialog")).toHaveAttribute("aria-modal", "true");

    const scrim = screen.getByTestId("pocket-sheet-scrim");
    fireEvent.click(scrim);
    expect(onDetentChange).toHaveBeenCalledWith("half");
  });

  it("full detent traps Tab inside the sheet", () => {
    render(<button data-testid="outside">outside</button>);
    screen.getByTestId("outside").focus();

    renderSheet({ detent: "full" });
    fireEvent.keyDown(document, { key: "Tab" });
    // First focusable inside the sheet is the grabber button.
    expect(screen.getByRole("button", { name: "Collapse sheet" })).toHaveFocus();
  });

  // ── 4. transportSlot ────────────────────────────────────────────────────────

  it("transportSlot renders only at full detent", () => {
    const { rerenderDetent } = renderSheet({
      detent: "half",
      transportSlot: <span data-testid="transport">0:04 / 0:12</span>,
    });
    expect(screen.queryByTestId("transport")).not.toBeInTheDocument();

    rerenderDetent("full");
    expect(screen.getByTestId("transport")).toBeInTheDocument();

    rerenderDetent("half");
    expect(screen.queryByTestId("transport")).not.toBeInTheDocument();
  });

  // ── 5. Escape ───────────────────────────────────────────────────────────────

  it("Escape closes at half and at full", () => {
    const { onClose, rerenderDetent } = renderSheet({ detent: "half" });
    fireEvent.keyDown(document, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);

    rerenderDetent("full");
    fireEvent.keyDown(document, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(2);
  });
});

// ── 6 + 7. Keyboard-aware detent ──────────────────────────────────────────────

describe("Sheet — keyboard promote", () => {
  it("promotes to full while the keyboard is open, restores half after", () => {
    const vv = installVisualViewportMock();
    try {
      const { onDetentChange } = renderSheet({ detent: "half" });
      expect(onDetentChange).not.toHaveBeenCalled();

      act(() => {
        vv.setHeight(window.innerHeight - 320);
      });
      expect(onDetentChange).toHaveBeenCalledWith("full");

      act(() => {
        vv.setHeight(window.innerHeight);
      });
      expect(onDetentChange).toHaveBeenLastCalledWith("half");
    } finally {
      vv.restore();
    }
  });

  it("without visualViewport the promote path no-ops and the sheet renders", () => {
    // The mock from the previous test is restored — jsdom has no
    // visualViewport of its own, so the hook's guard row is what runs here.
    expect(window.visualViewport ?? null).toBeNull();

    const { onDetentChange } = renderSheet({ detent: "half" });
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(onDetentChange).not.toHaveBeenCalled();
  });
});

// ── 8. Grabber button ─────────────────────────────────────────────────────────

describe("Sheet — grabber", () => {
  it("sr-labelled grabber button toggles detent", () => {
    const { onDetentChange, rerenderDetent } = renderSheet({ detent: "half" });

    fireEvent.click(screen.getByRole("button", { name: "Expand sheet" }));
    expect(onDetentChange).toHaveBeenCalledWith("full");

    rerenderDetent("full");
    fireEvent.click(screen.getByRole("button", { name: "Collapse sheet" }));
    expect(onDetentChange).toHaveBeenLastCalledWith("half");
  });
});

// ── 9. Body scroll lock ───────────────────────────────────────────────────────

describe("Sheet — scroll lock", () => {
  it("locks body scroll while open and restores the prior value on unmount", () => {
    document.body.style.overflow = "scroll";
    const { unmount } = renderSheet();
    expect(document.body.style.overflow).toBe("hidden");

    unmount();
    expect(document.body.style.overflow).toBe("scroll");
    document.body.style.overflow = "";
  });
});

// ── 10. Pointer wiring ────────────────────────────────────────────────────────

describe("Sheet — pointer wiring", () => {
  it("drag down on the grabber past the threshold closes", () => {
    const { onClose } = renderSheet({ detent: "half" });
    const sheet = screen.getByTestId("pocket-sheet");
    const grabber = screen.getByRole("button", { name: "Expand sheet" });

    fireEvent.pointerDown(grabber, { clientX: 100, clientY: 200, pointerId: 1 });
    fireEvent.pointerUp(sheet, { clientX: 100, clientY: 290, pointerId: 1 });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("horizontal drags never change detent or close", () => {
    const { onClose, onDetentChange } = renderSheet({ detent: "half" });
    const sheet = screen.getByTestId("pocket-sheet");

    fireEvent.pointerDown(sheet, { clientX: 40, clientY: 300, pointerId: 1 });
    fireEvent.pointerUp(sheet, { clientX: 200, clientY: 380, pointerId: 1 });
    expect(onClose).not.toHaveBeenCalled();
    expect(onDetentChange).not.toHaveBeenCalled();
  });
});
