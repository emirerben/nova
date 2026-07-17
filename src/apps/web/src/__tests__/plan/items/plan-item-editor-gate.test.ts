/**
 * Tests for `planItemEditorDisabledReason` — the plan-item detail page's
 * Edit-button entry gate (page.tsx). Extracted as a standalone assertion
 * here (not a full page mount — see EditorShell-lyrics-restyle.test.tsx and
 * editor-capabilities.test.tsx for why: this file mirrors the same "pure
 * gating logic is unit-testable without mounting the component" rationale
 * documented in _editor/editor-capabilities.ts).
 *
 * Regression coverage: the gate used to check only 4 of the 6 relevant
 * capabilities (text_elements/timeline/split_clips/mix — missing sfx/
 * overlays), so a variant with sfx/overlays already granted (e.g. any
 * lyrics-synced song variant) could never open the editor at all, even
 * though the backend intentionally leaves those additive tools editable.
 */

import { planItemEditorDisabledReason } from "@/app/plan/items/[id]/page";
import type { EditorCapabilities, PlanItemVariant } from "@/lib/plan-api";

function makeVariant(editor_capabilities: EditorCapabilities | undefined): PlanItemVariant {
  return {
    variant_id: "v1",
    output_url: "https://storage.example/v1.mp4",
    render_status: "ready",
    editor_capabilities,
  } as unknown as PlanItemVariant;
}

describe("planItemEditorDisabledReason", () => {
  it("returns null (editable) for a lyrics-synced variant with sfx/overlays granted", () => {
    // Real prod shape observed for job 2a00c97d-... (song_lyrics variant).
    const variant = makeVariant({
      text_elements: false,
      timeline: false,
      split_clips: false,
      mix: false,
      sfx: true,
      overlays: true,
      suggestions: false,
      reason: "lyrics_sync",
    });
    expect(planItemEditorDisabledReason(variant)).toBeNull();
  });

  it("returns a human reason (disabled) when all six capabilities are false", () => {
    const variant = makeVariant({
      text_elements: false,
      timeline: false,
      split_clips: false,
      mix: false,
      sfx: false,
      overlays: false,
      reason: "sources_expired",
    });
    expect(planItemEditorDisabledReason(variant)).toBe("the source clips are no longer available");
  });

  it("returns null when only the standard four are false but sfx is true", () => {
    const variant = makeVariant({
      text_elements: false,
      timeline: false,
      split_clips: false,
      mix: false,
      sfx: true,
      overlays: false,
    });
    expect(planItemEditorDisabledReason(variant)).toBeNull();
  });

  it("returns null when only overlays is true (regression: previously ignored)", () => {
    const variant = makeVariant({
      text_elements: false,
      timeline: false,
      split_clips: false,
      mix: false,
      sfx: false,
      overlays: true,
    });
    expect(planItemEditorDisabledReason(variant)).toBeNull();
  });

  it("returns null for a fully editable variant", () => {
    const variant = makeVariant({
      text_elements: true,
      timeline: true,
      split_clips: true,
      mix: true,
      sfx: true,
      overlays: true,
    });
    expect(planItemEditorDisabledReason(variant)).toBeNull();
  });

  it("returns null when editor_capabilities is absent (no server opinion — let the shell decide)", () => {
    expect(planItemEditorDisabledReason(makeVariant(undefined))).toBeNull();
  });

  it("returns null for a null variant", () => {
    expect(planItemEditorDisabledReason(null)).toBeNull();
  });
});
