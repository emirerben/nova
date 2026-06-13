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
 */
export function isInstantEditEligible(variant: EditableVariant): boolean {
  return (
    !!variant.base_video_url &&
    (variant.text_mode === "agent_text" || variant.text_mode === "none") &&
    variant.intro_mode !== "sequence"
  );
}
