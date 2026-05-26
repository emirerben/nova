// @ts-nocheck
import "@testing-library/jest-dom";
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, jest, test } from "@jest/globals";

import { FontAlternatives } from "@/app/admin/templates/[id]/components/FontAlternatives";
import {
  FontLibraryBrowser,
  fontsByVibe,
} from "@/app/admin/templates/[id]/components/FontLibraryBrowser";
import { PropertyPanel } from "@/app/admin/templates/[id]/components/PropertyPanel";
import {
  StaleFontAlternativesBanner,
  shouldShowStaleFontAlternativesBanner,
} from "@/app/admin/templates/[id]/components/EditorTab";
import type {
  EditorAction,
  EditorSelection,
  Recipe,
  RecipeTextOverlay,
} from "@/app/admin/templates/[id]/components/recipe-types";

function overlay(
  overrides: Partial<RecipeTextOverlay> = {},
): RecipeTextOverlay {
  return {
    role: "hook",
    text: "Hello",
    position: "center",
    effect: "pop-in",
    font_style: "sans",
    font_family: undefined,
    text_size: "large",
    text_color: "#FFFFFF",
    start_s: 0,
    end_s: 1,
    start_s_override: null,
    end_s_override: null,
    has_darkening: false,
    has_narrowing: false,
    sample_text: "Hello",
    font_cycle_accel_at_s: null,
    font_alternatives: null,
    ...overrides,
  };
}

function recipe(textOverlay: RecipeTextOverlay): Recipe {
  return {
    shot_count: 1,
    total_duration_s: 1,
    hook_duration_s: 1,
    slots: [
      {
        position: 1,
        target_duration_s: 1,
        priority: 1,
        slot_type: "hook",
        transition_in: "hard-cut",
        color_hint: "none",
        speed_factor: 1,
        energy: 5,
        media_type: "video",
        text_overlays: [textOverlay],
      },
    ],
    copy_tone: "direct",
    caption_style: "short",
    beat_timestamps_s: [],
    creative_direction: "",
    transition_style: "hard-cut",
    color_grade: "none",
    pacing_style: "fast",
    sync_style: "freeform",
    interstitials: [],
    font_default: null,
  };
}

describe("FontAlternatives", () => {
  test("renders top active matches and filters deprecated fonts", () => {
    const dispatch = jest.fn<void, [EditorAction]>();
    render(
      <FontAlternatives
        overlay={overlay({
          font_alternatives: [
            { family: "Outfit", similarity: 0.99 },
            { family: "Montserrat", similarity: 0.98 },
            { family: "Bebas Neue", similarity: 0.97 },
            { family: "Anton", similarity: 0.96 },
            { family: "Bowlby One SC", similarity: 0.95 },
            { family: "Bangers", similarity: 0.94 },
            { family: "Space Grotesk", similarity: 0.93 },
          ],
        })}
        slotIndex={0}
        overlayIndex={0}
        dispatch={dispatch}
      />,
    );

    expect(screen.getByText("Top matches (5)")).toBeInTheDocument();
    expect(screen.queryByText("Outfit")).not.toBeInTheDocument();
    expect(screen.getAllByText("Bangers").length).toBeGreaterThan(0);
    expect(screen.queryByText("Space Grotesk")).not.toBeInTheDocument();

    fireEvent.click(screen.getByTitle(/Bangers/));
    expect(dispatch).toHaveBeenCalledWith({
      type: "UPDATE_OVERLAY_FIELD",
      slotIndex: 0,
      overlayIndex: 0,
      field: "font_family",
      value: "Bangers",
    });
  });
});

describe("FontLibraryBrowser", () => {
  test("groups active fonts by vibe and dispatches picks", () => {
    const grouped = fontsByVibe();
    expect(grouped.viral_headlines).toHaveLength(6);
    expect(grouped.clean_captions).toHaveLength(9);
    expect(grouped.editorial).toHaveLength(7);
    expect(grouped.handwritten).toHaveLength(1);
    expect(grouped.script).toHaveLength(1);
    expect(Object.values(grouped).flat()).not.toContain("Outfit");

    const onPickFont = jest.fn();
    render(<FontLibraryBrowser onPickFont={onPickFont} />);

    expect(screen.getByText("Viral Headlines")).toBeInTheDocument();
    expect(screen.getByText("Clean Captions")).toBeInTheDocument();
    expect(screen.getByText("Editorial")).toBeInTheDocument();
    expect(screen.getByText("Handwritten")).toBeInTheDocument();
    expect(screen.getByText("Script")).toBeInTheDocument();

    fireEvent.click(screen.getByTitle("Use Bangers"));
    expect(onPickFont).toHaveBeenCalledWith("Bangers");
  });
});

describe("PropertyPanel", () => {
  test("shows deprecated badge and switches to the top active alternative", () => {
    const dispatch = jest.fn<void, [EditorAction]>();
    const selection: EditorSelection = {
      type: "overlay",
      slotIndex: 0,
      overlayIndex: 0,
    };
    render(
      <PropertyPanel
        recipe={recipe(
          overlay({
            font_family: "Outfit",
            font_alternatives: [
              { family: "Outfit", similarity: 0.9 },
              { family: "Bangers", similarity: 0.8 },
            ],
          }),
        )}
        selection={selection}
        dispatch={dispatch}
      />,
    );

    expect(screen.getByText("deprecated")).toBeInTheDocument();
    expect(screen.getByText("Outfit")).toBeInTheDocument();

    fireEvent.click(screen.getByText("Switch to active alternative"));
    expect(dispatch).toHaveBeenCalledWith({
      type: "UPDATE_OVERLAY_FIELD",
      slotIndex: 0,
      overlayIndex: 0,
      field: "font_family",
      value: "Bangers",
    });
  });
});

describe("stale font alternatives banner", () => {
  test("renders, dismisses, and triggers reanalysis", () => {
    const onDismiss = jest.fn();
    const onReanalyze = jest.fn();
    render(
      <StaleFontAlternativesBanner
        onDismiss={onDismiss}
        onReanalyze={onReanalyze}
      />,
    );

    expect(
      screen.getByText(
        "Font alternatives reflect an older library. Re-analyze for fresh suggestions.",
      ),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByText("Re-analyze"));
    expect(onReanalyze).toHaveBeenCalled();
    fireEvent.click(screen.getByLabelText("Dismiss stale font alternatives banner"));
    expect(onDismiss).toHaveBeenCalled();
  });

  test("is gated on stale recipe state", () => {
    expect(shouldShowStaleFontAlternativesBanner(null, false)).toBe(false);
    expect(
      shouldShowStaleFontAlternativesBanner({ analysis_pool_stale: false }, false),
    ).toBe(false);
    expect(
      shouldShowStaleFontAlternativesBanner({ analysis_pool_stale: true }, false),
    ).toBe(true);
    expect(
      shouldShowStaleFontAlternativesBanner({ analysis_pool_stale: true }, true),
    ).toBe(false);
  });
});
