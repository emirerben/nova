/**
 * Canonical pipeline phase names (mirrored from app/services/job_phases.py)
 * and their user-facing copy. Single source of truth — the result page reads
 * from here, and any new phase added on the backend just needs a row here to
 * get a friendly label.
 */

export type JobPhase =
  | "queued"
  | "download_clips"
  | "analyze_clips"
  | "match_clips"
  | "assemble"
  | "mix_audio"
  | "generate_copy"
  | "upload"
  | "finalize";

/** Order phases are expected to fire. Drives the progress bar position so a
 *  phase that arrives late still slots in correctly. */
export const PHASE_ORDER: readonly JobPhase[] = [
  "queued",
  "download_clips",
  "analyze_clips",
  "match_clips",
  "assemble",
  "mix_audio",
  "generate_copy",
  "upload",
  "finalize",
];

/** Short copy shown in the active-phase line. Kept upbeat, no jargon. */
export const PHASE_LABEL: Record<JobPhase, string> = {
  queued: "Waiting in queue…",
  download_clips: "Pulling in your clips…",
  analyze_clips: "Analysing your clips with AI…",
  match_clips: "Picking the best moments…",
  assemble: "Assembling the video…",
  mix_audio: "Mixing the audio…",
  generate_copy: "Writing your caption…",
  upload: "Almost there — saving the result…",
  finalize: "Wrapping up…",
};

/** Fallback when the backend reports a phase the frontend doesn't recognise
 *  yet (forward-compat — a new phase added server-side won't blank the UI). */
export function humanisePhase(name: string | null | undefined): string {
  if (!name) return "Working on it…";
  if (name in PHASE_LABEL) return PHASE_LABEL[name as JobPhase];
  // Humanise: snake_case → Sentence case.
  return (
    name.charAt(0).toUpperCase() +
    name.slice(1).replace(/_/g, " ") +
    "…"
  );
}

/** Approximate progress 0..1 based on which phase is live. Pure heuristic —
 *  the back-end doesn't ship an ETA in this PR, so the bar is just a steady
 *  left-to-right march that gives the user a sense of "how far in." */
export function phaseProgress(current: string | null | undefined): number {
  if (!current) return 0.02; // tiny non-zero so the bar is visible
  const idx = PHASE_ORDER.indexOf(current as JobPhase);
  if (idx < 0) return 0.5; // unknown phase — middle of the bar
  return Math.min(0.98, (idx + 1) / PHASE_ORDER.length);
}
