import type { EditableVariant } from "@/lib/variant-editor/types";

/**
 * Whether a variant can drive the 0-latency instant editor (live DOM text
 * overlay over the text-free base) rather than the legacy server-reburn
 * controls. Shared by the generative page (VariantCard / VariantTile) and the
 * plan content-plan flow so both branch identically.
 *
 * Instant edit needs the text-free base video AND an editable text mode —
 * lyrics variants have neither (no cached base; lyric typography is set-driven).
 * Cluster intros (intro_layout === "cluster") ARE eligible: the editorial
 * word-cluster geometry is now ported to TS (overlay-cluster-layout.ts, parity-
 * guarded by overlay-cluster-layout.test.ts) and previews live via
 * ClusterTextPreview. An edited hook re-derives its word roles heuristically —
 * exactly what the server does on a text edit — so the live preview matches.
 * Sequence intros (intro_mode === "sequence") STAY excluded: the text is synced
 * to the edit's audio (a voiceover transcript or an authored rhythm quote — the
 * server 422s text edits) and the phrase sequence has no local preview. This is
 * the load-bearing distinction: a "cluster" layout can be sequence-synced, so
 * the intro_mode === "sequence" guard must run AFTER (and independently of) the
 * cluster check.
 *
 * Caption archetypes (resolved_archetype === "narrated" | "subtitled") are ALSO
 * excluded even though they render with text_mode "none" and carry a base video:
 * their text is captions edited through the dedicated on-video CaptionEditor, and
 * their hero must play the BURNED, captioned output — NOT the caption-free base
 * that LiveEditPreview would show. Without this guard the caption hero plays the
 * base (no captions) and a right-click "Save video as" hands the user the
 * caption-free `*_base.mp4`. `narrated` = voiceover captions; `subtitled` =
 * single-clip auto-captions from the clip's own audio — both use CaptionEditor.
 */
const CAPTION_ARCHETYPES = new Set(["narrated", "subtitled"]);

/**
 * Whether a variant has editable caption cues over a reusable caption-free base.
 * This is the single client-side predicate for "is this a caption edit" — every
 * surface that routes caption editing or enables cue-aware editor behavior MUST
 * consume it so they can't drift.
 *
 * Mirrors the backend contract `_is_editable_caption_variant`
 * (generative_jobs.py): archetype ∈ {narrated, subtitled} AND a base video is
 * present. The base-video clause is load-bearing — a base-less caption variant
 * must NOT be routed to a CaptionEditor that has nothing to render (it would
 * re-open the empty-panel dead end this predicate exists to close). Deliberately
 * does NOT read `editor_capabilities.text_elements`: that flag gates the
 * optional styled-text lane, not caption cue editability.
 */
export function isCaptionArchetype(variant: EditableVariant): boolean {
  return (
    CAPTION_ARCHETYPES.has(variant.resolved_archetype ?? "") &&
    !!variant.base_video_url
  );
}

export function isInstantEditEligible(variant: EditableVariant): boolean {
  return (
    !!variant.base_video_url &&
    (variant.text_mode === "agent_text" || variant.text_mode === "none") &&
    variant.intro_mode !== "sequence" &&
    !CAPTION_ARCHETYPES.has(variant.resolved_archetype ?? "")
  );
}

export function isTextLaneEligible(variant: EditableVariant): boolean {
  if (variant.text_mode === "lyrics") return false;
  if (variant.resolved_archetype === "subtitled") {
    return (
      process.env.NEXT_PUBLIC_SUBTITLED_TEXT_LANE_ENABLED === "true" &&
      !!variant.base_video_url
    );
  }
  return variant.text_mode === "agent_text" || variant.text_mode === "none";
}
