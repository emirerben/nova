/**
 * Unit tests for the editor selection store's pure interaction logic
 * (plan §5 interaction contract): set/clear via the hook, the Escape
 * precedence ladder, overlap click-cycling, and the delete-key focus guard.
 */

import { act, renderHook } from "@testing-library/react";
import { describe, expect, it } from "@jest/globals";
import {
  cycleHit,
  deleteKeyAllowed,
  escapeAction,
  nudgeBarStart,
  sameSelection,
  useEditorSelection,
} from "../../../app/plan/items/[id]/_editor/useEditorSelection";

describe("cycleHit — overlap click-cycling", () => {
  it("returns null for no hits (empty canvas / video surface = deselect)", () => {
    expect(cycleHit([], null)).toBeNull();
    expect(cycleHit([], "a")).toBeNull();
  });

  it("selects the topmost hit when nothing is selected", () => {
    expect(cycleHit(["top", "mid", "bottom"], null)).toBe("top");
  });

  it("selects the topmost hit when the current selection is not under the point", () => {
    expect(cycleHit(["top", "mid"], "elsewhere")).toBe("top");
  });

  it("cycles to the element underneath on repeated clicks", () => {
    const hits = ["top", "mid", "bottom"];
    expect(cycleHit(hits, "top")).toBe("mid");
    expect(cycleHit(hits, "mid")).toBe("bottom");
  });

  it("wraps back to the topmost after the bottom of the stack", () => {
    expect(cycleHit(["top", "mid", "bottom"], "bottom")).toBe("top");
  });

  it("re-selects a single hit (single element under the point never deselects)", () => {
    expect(cycleHit(["only"], "only")).toBe("only");
  });
});

describe("escapeAction — the §9 ladder", () => {
  it("closes the drawer first when it is open", () => {
    expect(escapeAction({ drawerOpen: true, hasSelection: true })).toBe("close-drawer");
    expect(escapeAction({ drawerOpen: true, hasSelection: false })).toBe("close-drawer");
  });

  it("clears the selection when the drawer is closed", () => {
    expect(escapeAction({ drawerOpen: false, hasSelection: true })).toBe("clear-selection");
  });

  it("does nothing when there is nothing to do", () => {
    expect(escapeAction({ drawerOpen: false, hasSelection: false })).toBe("none");
  });
});

describe("deleteKeyAllowed — focus guard", () => {
  it("allows delete when nothing has focus", () => {
    expect(deleteKeyAllowed(null)).toBe(true);
  });

  it("allows delete when focus is on a non-entry element", () => {
    expect(deleteKeyAllowed({ tagName: "BUTTON" })).toBe(true);
    expect(deleteKeyAllowed({ tagName: "DIV" })).toBe(true);
  });

  it("blocks delete while typing in text-entry surfaces", () => {
    expect(deleteKeyAllowed({ tagName: "INPUT" })).toBe(false);
    expect(deleteKeyAllowed({ tagName: "TEXTAREA" })).toBe(false);
    expect(deleteKeyAllowed({ tagName: "SELECT" })).toBe(false);
    expect(deleteKeyAllowed({ tagName: "input" })).toBe(false); // case-insensitive
    expect(deleteKeyAllowed({ tagName: "DIV", isContentEditable: true })).toBe(false);
  });
});

describe("nudgeBarStart — arrow-key timeline nudging", () => {
  it("moves by 0.1s and rounds to the timeline grid", () => {
    expect(nudgeBarStart({ start_s: 1.04, end_s: 2.04 }, 0.1, 10)).toBe(1.1);
    expect(nudgeBarStart({ start_s: 1.04, end_s: 2.04 }, -0.1, 10)).toBe(0.9);
  });

  it("moves by 1s for shifted nudges", () => {
    expect(nudgeBarStart({ start_s: 2.2, end_s: 3.7 }, 1, 10)).toBe(3.2);
    expect(nudgeBarStart({ start_s: 2.2, end_s: 3.7 }, -1, 10)).toBe(1.2);
  });

  it("clamps at zero and preserves duration at the end of the video", () => {
    expect(nudgeBarStart({ start_s: 0.05, end_s: 1.05 }, -1, 10)).toBe(0);
    expect(nudgeBarStart({ start_s: 8.8, end_s: 10 }, 1, 10)).toBe(8.8);
  });

  it("only low-clamps when duration is unknown", () => {
    expect(nudgeBarStart({ start_s: 8.8, end_s: 10 }, 1, 0)).toBe(9.8);
  });
});

describe("sameSelection", () => {
  it("treats nulls and matching kind+id as equal", () => {
    expect(sameSelection(null, null)).toBe(true);
    expect(sameSelection({ kind: "text", id: "a" }, { kind: "text", id: "a" })).toBe(true);
  });

  it("distinguishes kind, id, and null", () => {
    expect(sameSelection({ kind: "text", id: "a" }, null)).toBe(false);
    expect(sameSelection({ kind: "text", id: "a" }, { kind: "text", id: "b" })).toBe(false);
    expect(sameSelection({ kind: "text", id: "a" }, { kind: "sfx", id: "a" })).toBe(false);
  });
});

describe("useEditorSelection — set/clear store", () => {
  it("starts with no selection (first-paint spec)", () => {
    const { result } = renderHook(() => useEditorSelection());
    expect(result.current.selection).toBeNull();
  });

  it("selects one element at a time", () => {
    const { result } = renderHook(() => useEditorSelection());
    act(() => result.current.select("text", "bar-1"));
    expect(result.current.selection).toEqual({ kind: "text", id: "bar-1" });
    act(() => result.current.select("sfx", "fx-9"));
    expect(result.current.selection).toEqual({ kind: "sfx", id: "fx-9" });
  });

  it("clear() empties the selection", () => {
    const { result } = renderHook(() => useEditorSelection());
    act(() => result.current.select("text", "bar-1"));
    act(() => result.current.clear());
    expect(result.current.selection).toBeNull();
  });

  it("re-selecting the same element keeps the same state object (no churn)", () => {
    const { result } = renderHook(() => useEditorSelection());
    act(() => result.current.select("text", "bar-1"));
    const first = result.current.selection;
    act(() => result.current.select("text", "bar-1"));
    expect(result.current.selection).toBe(first);
  });
});
