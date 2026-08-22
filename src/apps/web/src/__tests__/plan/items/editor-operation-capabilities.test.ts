import { describe, expect, it } from "@jest/globals";
import type { EditorCapabilities } from "@/lib/plan-api";
import {
  canEditClip,
  canEditLane,
  canEditMusic,
  hasGuidedOperationCapabilities,
  operationDisabledReason,
} from "@/app/plan/items/[id]/_editor/editor-operation-capabilities";

describe("editor operation capabilities", () => {
  it("uses V2 operation gates over legacy booleans", () => {
    const capabilities: EditorCapabilities = {
      timeline: false,
      split_clips: false,
      clips: {
        trim: { editable: true, reason: null },
        split: true,
        looks: { editable: false, reason: "Looks are disabled for this story." },
      },
    };

    expect(canEditClip(capabilities, "trim", false)).toBe(true);
    expect(canEditClip(capabilities, "split", false)).toBe(true);
    expect(canEditClip(capabilities, "looks", true)).toBe(false);
    expect(operationDisabledReason(capabilities.clips?.looks)).toBe("Looks are disabled for this story.");
    expect(operationDisabledReason(capabilities.clips?.trim)).toBeNull();
  });

  it("falls back to legacy gates when V2 data is absent", () => {
    const capabilities: EditorCapabilities = { swap_song: false, mix: true };
    expect(canEditMusic(capabilities, "swap", capabilities.swap_song !== false)).toBe(false);
    expect(canEditMusic(capabilities, "level", true)).toBe(true);
  });

  it("recognizes a lane-only V2 capability response as guided and honors its operation reason", () => {
    const capabilities: EditorCapabilities = {
      lanes: {
        text: { editable: false, reason: "Text is locked to the approved story." },
        sfx: { editable: true },
      },
    };

    expect(hasGuidedOperationCapabilities(capabilities)).toBe(true);
    expect(canEditLane(capabilities, "text", true)).toBe(false);
    expect(canEditLane(capabilities, "sfx", false)).toBe(true);
    expect(operationDisabledReason(capabilities.lanes?.text)).toBe(
      "Text is locked to the approved story.",
    );
  });

  it("recognizes Nova-only trim and remove operations as a guided capability surface", () => {
    const capabilities = {
      nova: {
        trim_clip_start: { editable: true, reason: null },
        trim_output_start: { editable: true, reason: null },
        remove_music: { editable: true, reason: null },
      },
    } as EditorCapabilities;

    expect(hasGuidedOperationCapabilities(capabilities)).toBe(true);
  });

  it("preserves pool uploads while a V2 overlay lane disables overlay edits", () => {
    const capabilities: EditorCapabilities = {
      overlay_upload_mode: "pool",
      overlays: true,
      lanes: {
        overlays: { editable: false, reason: "Overlays are locked for this story." },
      },
    };

    expect(capabilities.overlay_upload_mode).toBe("pool");
    expect(canEditLane(capabilities, "overlays", capabilities.overlays !== false)).toBe(false);
    expect(operationDisabledReason(capabilities.lanes?.overlays)).toBe(
      "Overlays are locked for this story.",
    );
  });
});
