"use client";

/**
 * WYSIWYG render of the generative hero-intro overlay — the 0-latency half of
 * the instant editor. This is the dispatch wrapper:
 * - `layout === "cluster"` → the editorial word-cluster (ClusterTextPreview),
 *   which itself falls back to the linear preview when the cluster engine
 *   declines the hook (the server renders linear in exactly that case).
 * - `layout === "linear"` (default) → LinearIntroTextPreview (single block).
 *
 * Both render the SETTLED (hold) state over the text-free base video in the
 * exact registry typeface the server burns with; the committed render stays
 * authoritative (canvas-vs-Skia metric drift ~1%).
 */

import type { IntroOverlayParams } from "@/lib/overlay-layout";
import { ClusterTextPreview } from "@/components/variant-editor/ClusterTextPreview";
import { LinearIntroTextPreview } from "@/components/variant-editor/LinearIntroTextPreview";

export function IntroTextPreview({
  params,
  editable = false,
  onTextChange,
  layout = "linear",
}: {
  params: IntroOverlayParams;
  editable?: boolean;
  onTextChange?: (text: string) => void;
  /** "cluster" → editorial word-cluster preview (delegates to ClusterTextPreview);
   * "linear" (default) → the single-block hero intro. */
  layout?: "linear" | "cluster" | null;
}) {
  if (layout === "cluster") {
    return (
      <ClusterTextPreview params={params} editable={editable} onTextChange={onTextChange} />
    );
  }
  return (
    <LinearIntroTextPreview params={params} editable={editable} onTextChange={onTextChange} />
  );
}
