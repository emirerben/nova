import type { LibraryJob } from "./me-api";

type PosterIdentitySource = Pick<
  LibraryJob,
  "poster_identity" | "output_variant_id" | "created_at"
>;

/**
 * Stable identity shared by the recovery scheduler and the tile callbacks.
 * Older API rows may not have poster_identity yet, so keep the rollout-safe
 * fallback order identical everywhere that tracks a poster attempt.
 */
export function libraryPosterIdentity(job: PosterIdentitySource): string | null {
  return job.poster_identity ?? job.output_variant_id ?? job.created_at ?? null;
}

export function libraryPosterRecoveryKey(
  job: PosterIdentitySource & Pick<LibraryJob, "id">,
): string {
  return `${job.id}:${libraryPosterIdentity(job) ?? "unknown"}`;
}

export function libraryPosterRecoveryKeyForIdentity(
  jobId: string,
  posterIdentity: string | null,
): string {
  return `${jobId}:${posterIdentity ?? "unknown"}`;
}
