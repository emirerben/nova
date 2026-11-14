/**
 * Typed client for GET /admin/kria/creation-health (KRI-1382).
 *
 * Goes through the Next.js admin proxy (`/api/admin/[...]`) so the admin token never reaches
 * the browser bundle. Backend source: src/apps/api/app/services/creation_health.py. Keep these
 * types in sync with the dict that `creation_health()` returns.
 */

const ADMIN_PROXY = "/api/admin";

/** Counts for one stage (chat turns or renders) over a window or a single day. */
export interface StageHealth {
  attempts: number;
  successes: number;
  /** successes / attempts; null when there were no attempts. */
  success_rate: number | null;
  in_progress: number;
  /** Turns: refused, blocked, recovery, failed. Renders: failed. Present only when non-zero. */
  failed?: number;
  refused?: number;
  blocked?: number;
  recovery?: number;
}

export interface DailyCreationHealth {
  date: string;
  turns: StageHealth;
  renders: StageHealth;
}

export interface FailureReason {
  source: "turn" | "render";
  kind: string;
  code: string;
  /** The sentence the client shows for a render reason; null for turn codes. */
  label: string | null;
  count: number;
  share: number;
}

export interface CreationHealth {
  window: { days: number; since: string; until: string; timezone: string };
  definitions: Record<string, string>;
  turns: StageHealth;
  renders: StageHealth;
  daily: DailyCreationHealth[];
  top_failure_reasons: FailureReason[];
}

export async function adminCreationHealth(days: number): Promise<CreationHealth> {
  const res = await fetch(`${ADMIN_PROXY}/kria/creation-health?days=${days}`);
  if (!res.ok) {
    let detail = `Request failed: ${res.status}`;
    try {
      const body = await res.json();
      if (typeof body.detail === "string") detail = body.detail;
    } catch {
      // the status line is enough
    }
    throw new Error(detail);
  }
  return (await res.json()) as CreationHealth;
}
