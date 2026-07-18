/**
 * editor-capabilities — pure server-capability → UI copy mapping for the
 * editor shell (extracted from EditorShell so the gating logic is
 * unit-testable without mounting the full shell, same pattern as
 * editor-reseed.ts).
 */

import type { EditorCapabilities, PlanItemVariant } from "@/lib/plan-api";
import type { EditorTool } from "./ToolRail";

export const CAPTIONS_TAB_REASON = "Captions for this edit are managed in the Captions tab";

/**
 * Text-lane fallback (review fix round on plan 010): when `text_elements` is
 * false but the server gave no (or an unmapped) reason, the copy must stay
 * text-specific — never the whole-shell "This version can't be edited."
 * (the shell IS editable; only on-video text is locked).
 */
export const TEXT_ELEMENTS_LOCKED_FALLBACK = "text editing isn't available for this edit";

const LYRICS_EDITOR_UI = process.env.NEXT_PUBLIC_LYRICS_EDITOR_ENABLED === "true";

function lyricsFeatureAvailable(capabilities: EditorCapabilities | null | undefined): boolean {
  const lyrics = capabilities?.lyrics;
  return LYRICS_EDITOR_UI && !!lyrics && (lyrics.editable || lyrics.enabled || lyrics.can_toggle_on);
}

/** Server reason code → human tooltip copy. Unknown codes pass through raw. */
export function editorReasonCopy(reason: string | null | undefined): string {
  if (!reason) return "This version can't be edited.";
  if (reason === "voiceover_bed_fit" || reason === "locked_to_voiceover") {
    return "locked to your voiceover";
  }
  if (reason === "lyrics_sync") return "lyrics are synced to the song";
  if (reason === "no_slot_timeline") return "this edit has no clip timeline";
  if (reason === "masonry_preset") return "collage presets do not use a clip timeline";
  if (reason === "sources_expired") return "the source clips are no longer available";
  if (reason === "caption_archetype") return CAPTIONS_TAB_REASON;
  if (reason === "sound_effects_disabled") return "sound effects are turned off right now";
  if (reason === "media_overlays_disabled") return "overlays are turned off right now";
  if (reason === "visual_blocks_disabled") return "visual blocks are turned off right now";
  if (reason === "lyrics_variant") return "lyrics-synced edits do not support visual blocks";
  if (reason === "no_clean_base") return "this edit has no reusable clean video base";
  if (reason === "duration_unknown") return "re-render this legacy edit before adding visual blocks";
  if (reason === "no_video") return "waiting for this edit to finish rendering";
  return reason;
}

/**
 * Honest copy for the text-elements lock, shared by the tool-rail disable map
 * and EditorShell's add-text toast so they can never disagree. Mapped reason
 * codes get their human copy; null AND unmapped codes fall back to the
 * text-specific line (an unmapped snake_case code is not honest tool copy).
 */
export function textElementsLockedCopy(
  capabilities: EditorCapabilities | null | undefined,
): string {
  const reason = capabilities?.reason;
  if (!reason) return TEXT_ELEMENTS_LOCKED_FALLBACK;
  const copy = editorReasonCopy(reason);
  if (copy !== reason) return copy;
  // Passthrough: `reason` for caption archetypes is the server-authored human
  // sentence (CAPTION_TAB_COPY byte-stable contract), not a code — keep it.
  // Only bare snake_case codes are dishonest tool copy worth replacing.
  return /^[a-z0-9_]+$/.test(reason) ? TEXT_ELEMENTS_LOCKED_FALLBACK : reason;
}

/**
 * Tool-rail disable map (plan 010 OV-1). Text/Styles disable when the shell
 * is read-only OR when `text_elements` alone is false — subtitled variants
 * keep on-video text in the Captions tab even once sfx/overlays flip true,
 * so the tools stay disabled with the honest Captions-tab tooltip instead of
 * silently no-op saving. Sounds/Overlays follow their own capability +
 * server-provided reason.
 *
 * `isLyrics` is the one exception: a lyrics-synced variant's per-element text
 * genuinely can't be edited (re-cutting/retiming would break vocal-onset
 * sync), but a *whole-style-set* restyle is safe and already supported
 * server-side (dispatch_change_style re-derives lyric timing deterministically
 * from the track — only the visual style changes). So for lyrics, Styles
 * stays enabled while Text stays locked; EditorShell branches the Styles
 * commit path accordingly instead of going through the blocked per-element
 * text_elements field.
 */
export function computeToolDisabledReasons({
  capabilities,
  readOnly,
  readOnlyReason,
  isLyrics = false,
}: {
  capabilities: EditorCapabilities | null | undefined;
  readOnly: boolean;
  readOnlyReason: string;
  isLyrics?: boolean;
}): Partial<Record<EditorTool, string>> {
  const out: Partial<Record<EditorTool, string>> = {};
  if (readOnly) {
    out.nova = readOnlyReason;
    out.text = readOnlyReason;
    out.styles = readOnlyReason;
  } else if (
    capabilities?.text_elements === false &&
    !lyricsFeatureAvailable(capabilities)
  ) {
    const reason = textElementsLockedCopy(capabilities);
    out.text = reason;
    if (!isLyrics) out.styles = reason;
  }
  if (capabilities?.sfx === false) {
    out.sounds = editorReasonCopy(
      capabilities.sfx_reason ?? "sound effects aren't available for this edit",
    );
  }
  if (capabilities?.overlays === false) {
    out.overlays = editorReasonCopy(
      capabilities.overlays_reason ?? "media overlays aren't available for this edit",
    );
  }
  if (capabilities?.visual_blocks === false) {
    out.visuals = editorReasonCopy(
      capabilities.visual_blocks_reason ?? "visual blocks aren't available for this edit",
    );
  }
  return out;
}

/**
 * Edit-entry gate for the plan-item detail page (page.tsx): mirrors
 * EditorShell's own `readOnly` definition above (all six capabilities false
 * ⇒ nothing editable). Lives here rather than in page.tsx itself — Next.js's
 * App Router only allows a fixed whitelist of named exports from a `page.tsx`
 * file (default, generateMetadata, ...); anything else fails `next build`
 * with "is not a valid Page export field". Same rationale as the rest of this
 * module: pure gating logic, unit-testable without mounting a component.
 */
export function planItemEditorDisabledReason(variant: PlanItemVariant | null): string | null {
  const capabilities = variant?.editor_capabilities;
  if (
    capabilities &&
    !capabilities.text_elements &&
    !lyricsFeatureAvailable(capabilities) &&
    !capabilities.timeline &&
    !capabilities.split_clips &&
    !capabilities.mix &&
    !capabilities.sfx &&
    !capabilities.overlays
    && !capabilities.visual_blocks
  ) {
    return editorReasonCopy(capabilities.reason);
  }
  return null;
}
