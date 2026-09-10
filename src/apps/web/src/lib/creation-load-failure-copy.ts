import { CreationThreadError } from "@/lib/creation-thread-api";

/**
 * Failure taxonomy for loading a creation/project thread (DESIGN.md §7-D10
 * naming convention: plain language for WHY, plus the action that actually
 * helps). Distinguishes network, authorization, missing-record and
 * data-integrity failures so a transient or service error never implies a
 * user's project is gone -- only a confirmed deletion does. See KRI-26.
 */
export interface CreationLoadFailure {
  title: string;
  detail: string;
  /**
   * `retryable`: show the Retry affordance, never imply loss.
   * `auth`: session expired -- point at sign-in, not Retry.
   * `terminal`: this project genuinely does not exist / isn't yours.
   */
  tone: "retryable" | "terminal" | "auth";
  actionLabel: string;
}

const OFFLINE: CreationLoadFailure = {
  title: "You’re offline",
  detail: "Your projects are safe. Reconnect, then try again.",
  tone: "retryable",
  actionLabel: "Retry",
};

const AUTH_EXPIRED: CreationLoadFailure = {
  title: "Your session expired",
  detail: "Sign in again to keep working on your projects.",
  tone: "auth",
  actionLabel: "Sign in",
};

const SERVICE_UNAVAILABLE: CreationLoadFailure = {
  title: "Creation chat couldn’t load",
  detail: "Your projects are safe. Check your connection, then try again.",
  tone: "retryable",
  actionLabel: "Retry",
};

const PROJECT_DELETED: CreationLoadFailure = {
  title: "This project was deleted",
  detail: "It’s no longer available. Go back to see your other projects.",
  tone: "terminal",
  actionLabel: "Back to projects",
};

const PROJECT_UNAVAILABLE: CreationLoadFailure = {
  title: "Project unavailable",
  detail: "This project may have been deleted or you may no longer have access to it.",
  tone: "terminal",
  actionLabel: "Back to projects",
};

// 404 codes that mean "this specific thread genuinely isn't reachable by
// you" -- everything else on a 404 (a runtime flag flip, a cursor-contract
// mismatch) is a service-shaped failure, not evidence of loss.
const TERMINAL_404_CODES = new Set(["thread_not_found", "thread_id_invalid"]);

/**
 * Classify a creation-thread load failure into user-safe copy + a tone that
 * decides whether Retry stays available. Unknown/absent codes fall back to
 * the original generic retryable copy, so an older API server (no typed
 * `code`) degrades safely rather than mis-classifying as terminal.
 */
export function creationLoadFailureCopy(
  cause: unknown,
  opts: { online: boolean } = { online: true },
): CreationLoadFailure {
  if (!opts.online) return OFFLINE;

  if (!(cause instanceof CreationThreadError)) return SERVICE_UNAVAILABLE;

  if (cause.status === 401) return AUTH_EXPIRED;

  if (cause.status === 404) {
    if (cause.code === "thread_deleted") return PROJECT_DELETED;
    if (cause.code && TERMINAL_404_CODES.has(cause.code)) return PROJECT_UNAVAILABLE;
    // No code at all (an older server mid-deploy, or a 404 from a route that
    // was never on the typed-problem contract, e.g. capabilities()) or a
    // service-shaped code (e.g. kria_runtime_unavailable during a flag
    // rollback): retryable, never a verdict that a specific project is gone.
    return SERVICE_UNAVAILABLE;
  }

  // 5xx, and the proxy's own boundary codes (upstream_unavailable,
  // upstream_error, server_misconfigured) -- all "our side", all retryable.
  return SERVICE_UNAVAILABLE;
}
