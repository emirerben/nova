"use client";

/**
 * Shared music-job status primitives consumed by both `TestTab` and `LyricsTab`.
 *
 * History: TestTab and LyricsTab both poll music-shaped jobs (the full template
 * test job and the lyric-only preview), so they shared three identical pieces
 * of logic — terminal statuses, the status pill, and the legacy-URL filter for
 * <video src>. Keeping them inline in each tab let them drift: LyricsTab lost
 * the `assembly_plan.output_url` fallback that TestTab carries for pre-URL-fix
 * rows, which would have silently broken playback for older lyric-preview jobs.
 * Co-locating them here in `components/` keeps the two tabs honest.
 */

export const TERMINAL_STATUSES = new Set<string>(["music_ready", "processing_failed"]);

const STATUS_COLOR: Record<string, string> = {
  queued: "bg-zinc-700 text-zinc-200",
  processing: "bg-blue-900 text-blue-300",
  music_ready: "bg-green-900 text-green-300",
  processing_failed: "bg-red-900 text-red-300",
  failed: "bg-red-900 text-red-300",
};

export function StatusPill({ status }: { status: string }) {
  const cls = STATUS_COLOR[status] ?? "bg-zinc-700 text-zinc-200";
  return (
    <span className={`text-[10px] font-semibold uppercase px-2 py-0.5 rounded-full ${cls}`}>
      {status}
    </span>
  );
}

/**
 * Resolve a music-job-shaped status object into a safe `<video src>` URL.
 *
 * Defends against two legacy shapes seen in production data:
 *   1. Rows where `output_url` lives on `assembly_plan` instead of top-level
 *      (pre-URL-fix orchestrator path that hadn't yet surfaced the signed URL).
 *   2. Rows where `output_url` is a relative GCS path rather than a signed
 *      https URL (pre-orchestrator-fix). Filtering to http(s) keeps the
 *      <video src> from falling back to a same-origin path lookup.
 *
 * Candidate preference: collects all string output_url candidates (top-level
 * first, then `assembly_plan.output_url`), returns the FIRST one that is a
 * valid http(s) URL. If both exist but only one is valid, the valid one wins
 * even if it isn't first in the list. The earlier "top-level wins regardless
 * of validity" path would let a legacy relative-path top-level mask a valid
 * https assembly_plan URL.
 *
 * @returns
 *   `outputUrl` — set when at least one candidate is a safe http(s) URL.
 *   `outputLegacy` — `true` only when at least one candidate exists but none
 *   are http(s). When no candidates exist at all (e.g. job is still rendering),
 *   both fields are falsy.
 */
export function resolveMusicJobOutputUrl(
  job:
    | {
        status?: string;
        output_url?: string | null;
        assembly_plan?: Record<string, unknown> | null;
      }
    | null
    | undefined,
): { outputUrl: string | undefined; outputLegacy: boolean } {
  if (!job || job.status !== "music_ready") {
    return { outputUrl: undefined, outputLegacy: false };
  }

  // Build the candidate list in preference order: top-level first, then
  // assembly_plan. Empty strings are dropped — they're never valid URLs and
  // shouldn't trigger the legacy banner.
  const candidates: string[] = [];
  if (typeof job.output_url === "string" && job.output_url !== "") {
    candidates.push(job.output_url);
  }
  const planUrl =
    job.assembly_plan &&
    typeof (job.assembly_plan as Record<string, unknown>).output_url === "string"
      ? ((job.assembly_plan as Record<string, unknown>).output_url as string)
      : null;
  if (planUrl && planUrl !== "") {
    candidates.push(planUrl);
  }

  // First valid http(s) wins, regardless of which source it came from.
  const validUrl = candidates.find((c) => /^https?:\/\//.test(c));
  if (validUrl) {
    return { outputUrl: validUrl, outputLegacy: false };
  }

  // At least one candidate but none are http(s) → legacy row, show the banner.
  // No candidates at all → job hasn't surfaced an output yet, both flags false.
  return { outputUrl: undefined, outputLegacy: candidates.length > 0 };
}
