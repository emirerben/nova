import type { PoolAsset } from "@/lib/plan-api";

/** Human-readable Nova analysis state for pool tiles.
 *
 * Descriptions are optional, especially for video. The asset status is the
 * authoritative completion signal; a missing description does not mean the
 * analysis is still running. Keyless/local fallback analyses are identified
 * separately so the UI does not overstate filename-only metadata as a full
 * visual analysis.
 */
export function poolAssetAnalysisLine(asset: PoolAsset): string {
  const generatedCopy =
    asset.nova_description?.trim() || asset.nova_on_screen_text?.trim();
  if (generatedCopy) return generatedCopy;
  if (asset.status === "failed") return "Analysis failed";
  if (asset.status !== "ready") return "Analysis pending";
  return asset.source_type === "stub"
    ? "Basic file details ready"
    : "Analysis complete";
}
