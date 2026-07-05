/**
 * Plan 009 T5 — lyrics disabled toggle (D5/E9) + page-wiring helpers.
 *
 * Covers:
 *  - fullscreenDisabledReason threads UnifiedTimeline → OverlayLane →
 *    OverlayCardPopover: the "Full screen" segmented option renders disabled
 *    (aria-disabled, non-interactive) with the reason as a copy line below
 *    the control; promote paths (segmented, chip F, max-scale affordance)
 *    are all inert; demote stays available for legacy fullscreen cards.
 *  - enabled behavior is untouched when the reason is not set.
 *  - computeIntroTextWindow unit table (first element ∪ elements starting
 *    before 4s; null when no elements).
 *  - resolveAssetMeta threads UnifiedTimeline → OverlayLane → popover: dims
 *    returned for the card's src_gcs_path drive the low-res warning.
 */

// @ts-nocheck

import React from "react";
import { act, fireEvent, render, screen } from "@testing-library/react";
import "@testing-library/jest-dom";

import UnifiedTimeline from "@/app/plan/_components/UnifiedTimeline";
import {
  computeIntroTextWindow,
  INTRO_CUTOFF_S,
} from "@/app/plan/_components/introTextWindow";
import type { MediaOverlay } from "@/lib/plan-api";

const LYRICS_REASON = "Full-screen cutaways aren't available on lyric edits.";

function makeCard(override: Partial<MediaOverlay> = {}): MediaOverlay {
  return {
    id: "ov-1",
    kind: "image",
    src_gcs_path: "users/u1/plan/item-1/pool/img.jpg",
    position: "center",
    x_frac: 0.5,
    y_frac: 0.5,
    scale: 0.35,
    start_s: 6.2,
    end_s: 9.0,
    z: 0,
    ...override,
  };
}

function timelineProps(override = {}) {
  return {
    totalDurationS: 30,
    currentTimeS: 5,
    sfxPlacements: [],
    sfxGlossaryEffects: [],
    sfxGlossaryLoading: false,
    sfxRendering: false,
    sfxUploading: false,
    onSfxChange: jest.fn(),
    onSfxUploadRequest: jest.fn().mockResolvedValue(undefined),
    overlayCards: [],
    overlaysEnabled: true,
    overlayUploading: false,
    localPreviewUrls: {},
    onOverlayUploadRequest: jest.fn(),
    onUpdateCard: jest.fn(),
    onRemoveCard: jest.fn(),
    onClearOverlays: jest.fn(),
    ...override,
  };
}

function chipFor(cardId: string): HTMLElement {
  const el = document.querySelector(`[data-overlay-chip="${cardId}"]`);
  expect(el).toBeTruthy();
  return el as HTMLElement;
}

/** Open a manual card's popover via its keyboard path (Enter on the chip). */
function openPopover(cardId: string) {
  fireEvent.keyDown(chipFor(cardId), { key: "Enter" });
}

// ── D5/E9 — disabled Full screen option on lyric variants ─────────────────────

describe("fullscreenDisabledReason — UnifiedTimeline → OverlayLane → popover", () => {
  it("disables the Full screen option (aria-disabled, non-interactive) with the reason copy", async () => {
    const onUpdateCard = jest.fn();
    render(
      <UnifiedTimeline
        {...timelineProps({
          overlayCards: [makeCard()],
          onUpdateCard,
          fullscreenDisabledReason: LYRICS_REASON,
        })}
      />,
    );
    openPopover("ov-1");

    const fullBtn = screen.getByRole("button", { name: "Full screen" });
    expect(fullBtn).toBeDisabled();
    expect(fullBtn).toHaveAttribute("aria-disabled", "true");
    // Reason as a small copy line below the segmented control.
    expect(screen.getByTestId("fullscreen-disabled-reason")).toHaveTextContent(LYRICS_REASON);

    await act(async () => {
      fireEvent.click(fullBtn);
    });
    expect(onUpdateCard).not.toHaveBeenCalled();
  });

  it("chip-level F never promotes while the reason is set", async () => {
    const onUpdateCard = jest.fn();
    render(
      <UnifiedTimeline
        {...timelineProps({
          overlayCards: [makeCard()],
          onUpdateCard,
          fullscreenDisabledReason: LYRICS_REASON,
        })}
      />,
    );

    await act(async () => {
      fireEvent.keyDown(chipFor("ov-1"), { key: "f" });
    });
    expect(onUpdateCard).not.toHaveBeenCalled();
  });

  it("hides the max-scale 'Make full screen →' affordance while the reason is set", () => {
    render(
      <UnifiedTimeline
        {...timelineProps({
          overlayCards: [makeCard({ scale: 1.0 })],
          fullscreenDisabledReason: LYRICS_REASON,
        })}
      />,
    );
    openPopover("ov-1");

    expect(screen.getByText("Full width")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Make full screen →" })).toBeNull();
  });

  it("demote stays available for a legacy fullscreen card on a lyric variant", async () => {
    const onUpdateCard = jest.fn();
    render(
      <UnifiedTimeline
        {...timelineProps({
          overlayCards: [makeCard({ display_mode: "fullscreen" })],
          onUpdateCard,
          fullscreenDisabledReason: LYRICS_REASON,
        })}
      />,
    );
    openPopover("ov-1");

    const pipBtn = screen.getByRole("button", { name: "PiP" });
    expect(pipBtn).not.toBeDisabled();
    await act(async () => {
      fireEvent.click(pipBtn);
    });
    expect(onUpdateCard).toHaveBeenCalledWith("ov-1", { display_mode: "pip" });
  });

  it("without the reason the option is enabled, promotes, and shows no copy line", async () => {
    const onUpdateCard = jest.fn();
    render(<UnifiedTimeline {...timelineProps({ overlayCards: [makeCard()], onUpdateCard })} />);
    openPopover("ov-1");

    expect(screen.queryByTestId("fullscreen-disabled-reason")).toBeNull();
    const fullBtn = screen.getByRole("button", { name: "Full screen" });
    expect(fullBtn).not.toBeDisabled();
    expect(fullBtn).not.toHaveAttribute("aria-disabled");
    await act(async () => {
      fireEvent.click(fullBtn);
    });
    expect(onUpdateCard).toHaveBeenCalledWith("ov-1", { display_mode: "fullscreen" });
  });
});

// ── computeIntroTextWindow (page-wiring helper, exported for unit tests) ─────

describe("computeIntroTextWindow", () => {
  it("null/empty elements → null", () => {
    expect(computeIntroTextWindow(null)).toBeNull();
    expect(computeIntroTextWindow(undefined)).toBeNull();
    expect(computeIntroTextWindow([])).toBeNull();
  });

  it("single element → its own window", () => {
    expect(computeIntroTextWindow([{ start_s: 2, end_s: 4.5 }])).toEqual({
      start_s: 2,
      end_s: 4.5,
    });
  });

  it("unions the first element with others starting before 4s (min start → max end)", () => {
    expect(
      computeIntroTextWindow([
        { start_s: 0.5, end_s: 2 },
        { start_s: 3, end_s: 6 }, // starts < 4 → widens the window
      ]),
    ).toEqual({ start_s: 0.5, end_s: 6 });
    // An earlier-starting second element pulls the start down too.
    expect(
      computeIntroTextWindow([
        { start_s: 1, end_s: 2 },
        { start_s: 0.2, end_s: 1.5 },
      ]),
    ).toEqual({ start_s: 0.2, end_s: 2 });
  });

  it("excludes elements starting at/after the 4s cutoff", () => {
    expect(INTRO_CUTOFF_S).toBe(4);
    expect(
      computeIntroTextWindow([
        { start_s: 0.5, end_s: 2 },
        { start_s: 4, end_s: 5 }, // at cutoff → excluded
        { start_s: 10, end_s: 12 }, // deep sequence scene → excluded
      ]),
    ).toEqual({ start_s: 0.5, end_s: 2 });
  });

  it("first element counts even when IT starts after the cutoff", () => {
    expect(
      computeIntroTextWindow([
        { start_s: 5, end_s: 7 },
        { start_s: 10, end_s: 12 },
      ]),
    ).toEqual({ start_s: 5, end_s: 7 });
  });
});

// ── resolveAssetMeta pass-through ─────────────────────────────────────────────

describe("resolveAssetMeta — UnifiedTimeline → OverlayLane → popover warnings", () => {
  it("dims resolved for the card's src_gcs_path raise the low-res fullscreen warning", () => {
    const resolveAssetMeta = jest.fn((srcGcsPath: string) =>
      srcGcsPath === "users/u1/plan/item-1/pool/img.jpg"
        ? { aspect: 0.56, width: 600, height: 800 }
        : undefined,
    );
    render(
      <UnifiedTimeline
        {...timelineProps({
          overlayCards: [makeCard({ display_mode: "fullscreen" })],
          resolveAssetMeta,
        })}
      />,
    );
    openPopover("ov-1");

    expect(resolveAssetMeta).toHaveBeenCalledWith("users/u1/plan/item-1/pool/img.jpg");
    expect(
      screen.getByText("Low resolution — this may look blurry full screen"),
    ).toBeInTheDocument();
  });

  it("without a resolver the metadata warnings stay suppressed (never faked)", () => {
    render(
      <UnifiedTimeline
        {...timelineProps({ overlayCards: [makeCard({ display_mode: "fullscreen" })] })}
      />,
    );
    openPopover("ov-1");

    expect(screen.queryByText(/Low resolution/)).toBeNull();
    expect(screen.queryByText("Sides will be cropped")).toBeNull();
  });
});
