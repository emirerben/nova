import type { PoolAsset } from "@/lib/plan-api";

/**
 * Merge a fresh pool snapshot without reloading media whose signed URL only
 * changed its query string. A different origin or pathname means the server
 * selected a different object, such as replacing a raw HEIC upload with its
 * browser-safe JPEG preview, so the fresh URL must win.
 */
export function mergePoolAssetsPreservingDisplayUrls(
  previous: PoolAsset[],
  fresh: PoolAsset[],
): PoolAsset[] {
  const previousById = new Map(previous.map((asset) => [asset.id, asset]));
  return fresh.map((asset) => {
    const existing = previousById.get(asset.id);
    if (!existing?.display_url) return asset;
    if (!asset.display_url) return { ...asset, display_url: existing.display_url };
    try {
      const previousUrl = new URL(existing.display_url);
      const freshUrl = new URL(asset.display_url);
      const sameObject =
        previousUrl.origin === freshUrl.origin &&
        previousUrl.pathname === freshUrl.pathname;
      return sameObject
        ? { ...asset, display_url: existing.display_url }
        : asset;
    } catch {
      // API display URLs are absolute. If a future provider violates that
      // contract, accepting the fresh URL is safer than pinning stale media.
      return asset;
    }
  });
}
