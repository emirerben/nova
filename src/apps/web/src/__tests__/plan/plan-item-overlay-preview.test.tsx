/**
 * Overlay lane in UnifiedTimeline — upload + card interaction tests.
 *
 * PR-3 migration: MediaOverlayEditor was retired; the same upload zone and
 * card controls now live in the Overlays lane of UnifiedTimeline.
 *
 * Scope:
 *  A. Overlay upload zone fires onOverlayUploadRequest with correct file metadata.
 *  B. Overlay upload zone filters unsupported MIME types.
 *  C. Upload zone is disabled (pointer-events-none) while overlayUploading=true.
 *  D. Per-card popover opens on click and exposes Remove + position + scale controls.
 */

// @ts-nocheck
// crypto.randomUUID polyfill lives in jest.setup.ts (global for all tests).

import React from "react";
import { act, fireEvent, render, screen } from "@testing-library/react";
import "@testing-library/jest-dom";

import UnifiedTimeline from "@/app/plan/_components/UnifiedTimeline";
import type { MediaOverlay } from "@/lib/plan-api";

// ─── Helpers ──────────────────────────────────────────────────────────────────

function makeCard(overrides: Partial<MediaOverlay> = {}): MediaOverlay {
  return {
    id: "card-1",
    kind: "image",
    src_gcs_path: "users/u1/plan/item-1/overlays/sticker.png",
    position: "center",
    x_frac: 0.5,
    y_frac: 0.5,
    scale: 0.35,
    start_s: 0,
    end_s: 5,
    z: 0,
    ...overrides,
  };
}

function defaultProps(overrides = {}) {
  return {
    totalDurationS: 30,
    currentTimeS: 5,
    // SFX (unused by these tests)
    sfxPlacements: [],
    sfxGlossaryEffects: [],
    sfxGlossaryLoading: false,
    sfxRendering: false,
    sfxUploading: false,
    onSfxChange: jest.fn(),
    onSfxUploadRequest: jest.fn().mockResolvedValue(undefined),
    // Overlays
    overlayCards: [] as MediaOverlay[],
    overlaysEnabled: true,
    overlayUploading: false,
    localPreviewUrls: {} as Record<string, string>,
    onOverlayUploadRequest: jest.fn(),
    onUpdateCard: jest.fn(),
    onRemoveCard: jest.fn(),
    onClearOverlays: jest.fn(),
    // Read-only lanes
    hasText: false,
    onOpenTab: jest.fn(),
    ...overrides,
  };
}

/** Simulate selecting files on the hidden overlay file input. */
function selectOverlayFiles(files: File[]) {
  // Find the file input that accepts image/video types (overlay), not audio (SFX).
  const inputs = document.querySelectorAll('input[type="file"]');
  const input = Array.from(inputs).find((el) =>
    (el as HTMLInputElement).accept?.includes("image/jpeg"),
  ) as HTMLInputElement;
  if (!input) throw new Error("Overlay file input not found");
  Object.defineProperty(input, "files", { value: files, configurable: true });
  fireEvent.change(input);
}

// ─── Tests ────────────────────────────────────────────────────────────────────

describe("UnifiedTimeline overlay lane — file upload", () => {
  it("test_upload_image_file: onOverlayUploadRequest called with PNG metadata", async () => {
    const onOverlayUploadRequest = jest.fn();
    render(<UnifiedTimeline {...defaultProps({ onOverlayUploadRequest })} />);

    const file = new File(["img-data"], "sticker.png", { type: "image/png" });
    await act(async () => { selectOverlayFiles([file]); });

    expect(onOverlayUploadRequest).toHaveBeenCalledTimes(1);
    const [callArgs] = onOverlayUploadRequest.mock.calls;
    expect(callArgs[0]).toHaveLength(1);
    const entry = callArgs[0][0];
    expect(entry.file).toBe(file);
    expect(entry.filename).toBe("sticker.png");
    expect(entry.content_type).toBe("image/png");
    expect(entry.file_size_bytes).toBe(file.size);
  });

  it("test_upload_video_file: onOverlayUploadRequest called with MP4 metadata", async () => {
    const onOverlayUploadRequest = jest.fn();
    render(<UnifiedTimeline {...defaultProps({ onOverlayUploadRequest })} />);

    const file = new File(["vid-data"], "clip.mp4", { type: "video/mp4" });
    await act(async () => { selectOverlayFiles([file]); });

    expect(onOverlayUploadRequest).toHaveBeenCalledTimes(1);
    const [callArgs] = onOverlayUploadRequest.mock.calls;
    expect(callArgs[0][0]).toMatchObject({ content_type: "video/mp4", filename: "clip.mp4" });
  });

  it("test_upload_unsupported_mime_skipped: onOverlayUploadRequest not called for PDFs", async () => {
    const onOverlayUploadRequest = jest.fn();
    render(<UnifiedTimeline {...defaultProps({ onOverlayUploadRequest })} />);

    const unsupported = new File(["x"], "doc.pdf", { type: "application/pdf" });
    await act(async () => { selectOverlayFiles([unsupported]); });

    expect(onOverlayUploadRequest).not.toHaveBeenCalled();
  });

  it("test_upload_mixed_types: valid files pass, unsupported files are silently skipped", async () => {
    const onOverlayUploadRequest = jest.fn();
    render(<UnifiedTimeline {...defaultProps({ onOverlayUploadRequest })} />);

    const valid = new File(["v"], "sticker.webp", { type: "image/webp" });
    const invalid = new File(["x"], "doc.pdf", { type: "application/pdf" });
    await act(async () => { selectOverlayFiles([valid, invalid]); });

    expect(onOverlayUploadRequest).toHaveBeenCalledTimes(1);
    const [callArgs] = onOverlayUploadRequest.mock.calls;
    expect(callArgs[0]).toHaveLength(1);
    expect(callArgs[0][0].filename).toBe("sticker.webp");
  });
});

describe("UnifiedTimeline overlay lane — upload zone disabled state", () => {
  it("test_uploading_state_disables_drop_zone: pointer-events-none when overlayUploading=true", () => {
    render(<UnifiedTimeline {...defaultProps({ overlayUploading: true })} />);
    const uploadZone = document.querySelector(".pointer-events-none.opacity-40");
    expect(uploadZone).not.toBeNull();
  });
});

describe("UnifiedTimeline overlay lane — card list", () => {
  it("test_cards_list_renders: overlay timing bars visible when cards present", () => {
    const card = makeCard({ id: "card-1" });
    render(<UnifiedTimeline {...defaultProps({ overlayCards: [card] })} />);
    expect(screen.getByText("Overlays")).toBeInTheDocument();
  });

  it("test_card_popover_opens_on_click: Remove button appears after clicking a card bar", async () => {
    const card = makeCard({ id: "c1", kind: "image" });
    render(<UnifiedTimeline {...defaultProps({ overlayCards: [card] })} />);

    const barLabel = screen.getByText(/⊞/);
    await act(async () => { fireEvent.click(barLabel); });

    expect(screen.getByRole("button", { name: /Remove card/i })).toBeInTheDocument();
  });

  it("test_card_remove_calls_onRemoveCard: Remove button fires onRemoveCard", async () => {
    const card = makeCard({ id: "card-xyz" });
    const onRemoveCard = jest.fn();
    render(<UnifiedTimeline {...defaultProps({ overlayCards: [card], onRemoveCard })} />);

    await act(async () => { fireEvent.click(screen.getByText(/⊞/)); });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /Remove card/i }));
    });

    expect(onRemoveCard).toHaveBeenCalledWith("card-xyz");
  });

  it("test_clear_all_button_visible_with_cards: 'Clear all overlays' appears when cards present", () => {
    const card = makeCard();
    render(<UnifiedTimeline {...defaultProps({ overlayCards: [card] })} />);
    expect(screen.getByText(/Clear all overlays/i)).toBeInTheDocument();
  });

  it("test_clear_all_calls_onClearOverlays", async () => {
    const onClearOverlays = jest.fn();
    const card = makeCard();
    render(<UnifiedTimeline {...defaultProps({ overlayCards: [card], onClearOverlays })} />);
    await act(async () => {
      fireEvent.click(screen.getByText(/Clear all overlays/i));
    });
    expect(onClearOverlays).toHaveBeenCalledTimes(1);
  });
});

describe("UnifiedTimeline overlay lane — position presets in popover", () => {
  it("test_position_preset_calls_onUpdateCard: clicking Top fires patch with position=top", async () => {
    const card = makeCard({ id: "c1" });
    const onUpdateCard = jest.fn();
    render(<UnifiedTimeline {...defaultProps({ overlayCards: [card], onUpdateCard })} />);

    await act(async () => { fireEvent.click(screen.getByText(/⊞/)); });
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Top" })); });

    expect(onUpdateCard).toHaveBeenCalledWith("c1", { position: "top" });
  });
});
