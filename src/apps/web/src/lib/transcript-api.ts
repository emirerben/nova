/**
 * API client for the "Get a transcript" voiceover-helper endpoints.
 *
 * Same conventions as plan-api.ts: every call goes through the same-origin
 * Next.js proxy at /api/plan/<path>, which injects the NextAuth session's
 * X-User-Id + the server-only INTERNAL_API_KEY before forwarding to FastAPI.
 * Relative URLs keep the request same-origin so the session cookie rides along.
 *
 * All routes are gated behind the TRANSCRIPT_HELPER feature flag on the backend
 * (404 when off) and ownership-checked (also 404 — not 403 — if the caller isn't
 * the item owner or the item is unknown, matching _load_owned_item). Mid-flow the
 * item is already loaded + owned, so a 404 here effectively means the flag flipped.
 *
 * These TS interfaces are HAND-MIRRORED against the backend Pydantic schemas —
 * keep field names, literal unions, and nullability in exact lockstep. A drift
 * here silently mis-parses the response.
 */

const PLAN_BASE = "/api/plan";

/** Thrown on a 401 from the proxy — caller should send the user to sign-in. */
export class NotAuthenticatedError extends Error {
  constructor() {
    super("Not authenticated");
    this.name = "NotAuthenticatedError";
  }
}

/**
 * Thrown when the feature flag is off (404) so callers can degrade quietly —
 * the entry link is already flag-gated, but a race (flag flipped off mid-session)
 * should not surface a scary error.
 */
export class TranscriptDisabledError extends Error {
  constructor() {
    super("Transcript helper is not available");
    this.name = "TranscriptDisabledError";
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${PLAN_BASE}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
  });
  if (res.status === 401) throw new NotAuthenticatedError();
  if (res.status === 404) throw new TranscriptDisabledError();
  if (!res.ok) {
    let detail = `Request failed (${res.status})`;
    try {
      const body = (await res.json()) as { detail?: string };
      if (body?.detail) detail = body.detail;
    } catch {
      // non-JSON error body; keep the generic message
    }
    throw new Error(detail);
  }
  return (await res.json()) as T;
}

// ── Footage analysis ────────────────────────────────────────────────────────

/** POST /plan-items/{id}/transcript/analyze → an async analysis job id. */
export interface AnalyzeStartResponse {
  analyze_id: string;
}

export type AnalyzeStatus = "pending" | "ready" | "failed";

/** GET /plan-items/{id}/transcript/analyze/{analyze_id} */
export interface AnalyzeResult {
  status: AnalyzeStatus;
  /** Total duration of the attached footage in seconds — drives read-time target. */
  duration_s?: number;
  /** A short prose summary of what the footage shows. null when unavailable. */
  footage_summary?: string | null;
}

/** Kick off footage analysis for an item's attached clips. */
export function startTranscriptAnalyze(itemId: string): Promise<AnalyzeStartResponse> {
  return request<AnalyzeStartResponse>(`/plan-items/${itemId}/transcript/analyze`, {
    method: "POST",
  });
}

/** Poll the analysis job. Terminal when status is "ready" or "failed". */
export function getTranscriptAnalyze(
  itemId: string,
  analyzeId: string,
): Promise<AnalyzeResult> {
  return request<AnalyzeResult>(`/plan-items/${itemId}/transcript/analyze/${analyzeId}`);
}

// ── Interview ───────────────────────────────────────────────────────────────

/** One turn of the transcript interview transcript. */
export interface TranscriptTurn {
  role: "agent" | "user";
  content: string;
}

/** POST /plan-items/{id}/transcript/interview body. */
export interface InterviewRequest {
  brief: string;
  footage_summary?: string | null;
  turns: TranscriptTurn[];
}

/** POST /plan-items/{id}/transcript/interview response. */
export interface InterviewResponse {
  question: string;
  suggestions: string[];
  /** True when the agent has nothing more to ask — advance to Script. */
  is_final: boolean;
}

/** Ask the interviewer the next question given the running turns. */
export function transcriptInterview(
  itemId: string,
  body: InterviewRequest,
): Promise<InterviewResponse> {
  return request<InterviewResponse>(`/plan-items/${itemId}/transcript/interview`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

// ── Script ──────────────────────────────────────────────────────────────────

/** POST /plan-items/{id}/transcript/script body. */
export interface ScriptRequest {
  brief: string;
  footage_summary?: string | null;
  /** The user answers collected during the interview (user turns only). */
  answers?: string[];
  /** Footage duration in seconds — the script is paced to fit this. */
  duration_s: number;
}

/** POST /plan-items/{id}/transcript/script response. */
export interface ScriptResponse {
  /** Monotonic version — bumps on every Rewrite. */
  version: number;
  /** The full script as one string (paragraph breaks preserved). */
  text: string;
  /** Estimated read-time in seconds at a natural pace. */
  read_time_s: number;
  /** The script split into teleprompter lines. */
  lines: string[];
  source: "generated" | "edited";
}

/** Generate (or re-generate) the voiceover script. */
export function generateTranscriptScript(
  itemId: string,
  body: ScriptRequest,
): Promise<ScriptResponse> {
  return request<ScriptResponse>(`/plan-items/${itemId}/transcript/script`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

/**
 * Persist an inline-edited script in place (same version, source="edited"). Returns
 * the server-split lines + refreshed read-time so the teleprompter and the DB agree.
 */
export function saveTranscriptScript(
  itemId: string,
  text: string,
): Promise<ScriptResponse> {
  return request<ScriptResponse>(`/plan-items/${itemId}/transcript/script`, {
    method: "PATCH",
    body: JSON.stringify({ text }),
  });
}

// ── Recorded ────────────────────────────────────────────────────────────────

/** POST /plan-items/{id}/transcript/recorded body + response. */
export interface RecordedRequest {
  /** The script version the recorded take was read against. */
  version: number;
}

export interface RecordedResponse {
  ok: true;
}

/**
 * Mark that a take was recorded against a given script version. The audio itself
 * is uploaded + attached via uploadVoiceover + setItemVoiceover (the existing
 * clients) — this call just records the provenance link.
 */
export function markTranscriptRecorded(
  itemId: string,
  version: number,
): Promise<RecordedResponse> {
  return request<RecordedResponse>(`/plan-items/${itemId}/transcript/recorded`, {
    method: "POST",
    body: JSON.stringify({ version } satisfies RecordedRequest),
  });
}
