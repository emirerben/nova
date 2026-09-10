/**
 * overlayInspectorDocked — KRI-18 Defect B: at ~590px of embedded-pane width
 * the floating tool drawer (360px, unbounded) and the docked InspectorPanel
 * column (320px) overlap in "overlay" layout mode, clipping the inspector on
 * any tool click, Captions included. This pure function decides whether the
 * docked column should render; the surrounding component wiring is exercised
 * indirectly through the EditorShell suites.
 */
import { describe, expect, it } from "@jest/globals";

import { overlayInspectorDocked } from "@/app/plan/items/[id]/_editor/useEditorLayoutMode";
import type { EditorLayoutMode } from "@/app/plan/items/[id]/_editor/useEditorLayoutMode";
import type { EditorTool } from "@/app/plan/items/[id]/_editor/ToolRail";

describe("overlayInspectorDocked (KRI-18 Defect B)", () => {
  const LAYOUT_MODES: EditorLayoutMode[] = ["full", "overlay", "light"];
  const TOOLS: Array<EditorTool | null> = [null, "nova", "captions", "text", "visuals"];

  it("full and light are always docked, regardless of tool or width", () => {
    for (const layoutMode of ["full", "light"] as const) {
      for (const activeTool of TOOLS) {
        for (const isDesktopWidth of [true, false]) {
          expect(overlayInspectorDocked({ layoutMode, activeTool, isDesktopWidth })).toBe(true);
        }
      }
    }
  });

  it("overlay mode with no tool open (or the nova copilot) stays docked at any width", () => {
    for (const activeTool of [null, "nova"] as const) {
      for (const isDesktopWidth of [true, false]) {
        expect(
          overlayInspectorDocked({ layoutMode: "overlay", activeTool, isDesktopWidth }),
        ).toBe(true);
      }
    }
  });

  it("overlay mode with a tool drawer open docks only at desktop width — this is the fix", () => {
    for (const activeTool of ["captions", "text", "visuals", "sounds", "overlays", "styles"] as const) {
      expect(
        overlayInspectorDocked({ layoutMode: "overlay", activeTool, isDesktopWidth: true }),
      ).toBe(true);
      expect(
        overlayInspectorDocked({ layoutMode: "overlay", activeTool, isDesktopWidth: false }),
      ).toBe(false);
    }
  });

  it("non-embedded overlay mode (1024-1280px) is unaffected — isDesktopWidth is always true there", () => {
    // Documents the invariant the fix relies on: rail(92) + drawer(360) +
    // inspector(320) = 772px fits inside the narrowest non-embedded overlay
    // width (1024px), so overlay mode's own DESKTOP_QUERY threshold (1024px)
    // never produces isDesktopWidth: false outside an embedded pane.
    expect(92 + 360 + 320).toBeLessThan(1024);
    expect(
      overlayInspectorDocked({ layoutMode: "overlay", activeTool: "captions", isDesktopWidth: true }),
    ).toBe(true);
  });

  it.each(LAYOUT_MODES)("never throws for layoutMode=%s across the full tool set", (layoutMode) => {
    for (const activeTool of TOOLS) {
      for (const isDesktopWidth of [true, false]) {
        expect(() =>
          overlayInspectorDocked({ layoutMode, activeTool, isDesktopWidth }),
        ).not.toThrow();
      }
    }
  });
});
