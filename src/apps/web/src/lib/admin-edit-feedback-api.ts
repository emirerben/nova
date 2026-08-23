/**
 * Typed client for the edit-feedback learning-loop admin workbench.
 *
 * Requests intentionally go through the same Next.js admin proxy as the other
 * admin clients. Playback URLs are issued by the API; this module never builds
 * storage URLs in the browser.
 */

const ADMIN_PROXY = "/api/admin";

export type ReviewState = "all" | "unreviewed" | "reviewed" | "needs_correction";
export type QualitySignal = "all" | "good" | "bad" | "mixed" | "not_applicable";
export type AnnotationRating = "good" | "bad" | "mixed" | "not_applicable";

export interface EditFeedbackListParams {
  cursor?: string;
  limit?: number;
  format?: string;
  language?: string;
  media_mix?: string;
  date_from?: string;
  date_to?: string;
  prompt_version?: string;
  model_version?: string;
  review_state?: ReviewState;
  quality_signal?: QualitySignal;
  edit_signal?: QualitySignal;
  sampling?: "chronological" | "stratified";
}

export interface EditFeedbackTimelineEvent {
  id: string;
  kind: string;
  label?: string | null;
  start_s: number;
  end_s?: number | null;
  metadata?: Record<string, string | number | boolean | null>;
}

export interface EditFeedbackArtifact {
  id: string;
  artifact_id?: string;
  title?: string | null;
  creator_group_id?: string | null;
  plan_item_id?: string | null;
  job_id?: string | null;
  variant_id?: string | null;
  format?: string | null;
  language?: string | null;
  media_mix?: string | null;
  prompt_version?: string | null;
  model_version?: string | null;
  created_at: string;
  duration_s: number;
  render_generation?: string | null;
  render_receipt?: Record<string, unknown> | null;
  review_state: Exclude<ReviewState, "all">;
  quality_signal?: Exclude<QualitySignal, "all"> | null;
  edit_signal?: Exclude<QualitySignal, "all"> | null;
  reviewed_at?: string | null;
  edit_count?: number;
  poster_url?: string | null;
  playback_url: string | null;
  playback_identity?: string | null;
  playback_expires_at?: string | null;
  timeline?: EditFeedbackTimelineEvent[];
}

export type EditFeedbackListItem = EditFeedbackArtifact;

export interface EditFeedbackListResponse {
  items: EditFeedbackListItem[];
  next_cursor?: string | null;
  total?: number | null;
}

export interface EditFeedbackAnnotation {
  id: string;
  dimension: string;
  rating: AnnotationRating;
  rationale?: string | null;
  frame_start_s?: number | null;
  frame_end_s?: number | null;
  reviewer?: string | null;
  created_at: string;
  superseded_by?: string | null;
  is_current?: boolean;
  current?: boolean;
}

export interface EditFeedbackDetailResponse {
  artifact: EditFeedbackArtifact;
  annotations: EditFeedbackAnnotation[];
  timeline: EditFeedbackTimelineEvent[];
  proposal?: Record<string, unknown> | null;
  execution_receipt?: Record<string, unknown> | null;
}

export interface SaveEditFeedbackAnnotationInput {
  dimension: string;
  rating: AnnotationRating;
  rationale?: string;
  frame_start_s?: number | null;
  frame_end_s?: number | null;
  supersedes_annotation_id?: string | null;
}

export interface SaveEditFeedbackAnnotationResponse {
  annotation: EditFeedbackAnnotation;
}

export interface SaveEditFeedbackAnnotationsBulkResponse {
  annotations: EditFeedbackAnnotation[];
}

async function adminJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${ADMIN_PROXY}${path}`, init);
  if (!response.ok) {
    let detail = `Request failed: ${response.status}`;
    try {
      const body = (await response.json()) as { detail?: unknown };
      if (typeof body.detail === "string") detail = body.detail;
    } catch {
      // Keep the HTTP status when a proxy error is not JSON.
    }
    throw new Error(detail);
  }
  return (await response.json()) as T;
}

export async function adminListEditFeedback(
  params: EditFeedbackListParams = {},
): Promise<EditFeedbackListResponse> {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== "" && value !== "all") {
      // The UI keeps date inputs as YYYY-MM-DD while FastAPI's datetime query
      // fields need an explicit boundary. Keep the inclusive date semantics
      // stable for both list and URL refreshes.
      if (key === "date_from" && /^\d{4}-\d{2}-\d{2}$/.test(String(value))) {
        query.set(key, `${value}T00:00:00Z`);
      } else if (key === "date_to" && /^\d{4}-\d{2}-\d{2}$/.test(String(value))) {
        query.set(key, `${value}T23:59:59Z`);
      } else {
        query.set(key, String(value));
      }
    }
  }
  const suffix = query.toString() ? `?${query.toString()}` : "";
  return adminJson<EditFeedbackListResponse>(`/edit-feedback${suffix}`);
}

export async function adminGetEditFeedback(
  artifactId: string,
): Promise<EditFeedbackDetailResponse> {
  return adminJson<EditFeedbackDetailResponse>(
    `/edit-feedback/${encodeURIComponent(artifactId)}`,
  );
}

export async function adminSaveEditFeedbackAnnotation(
  artifactId: string,
  input: SaveEditFeedbackAnnotationInput,
): Promise<SaveEditFeedbackAnnotationResponse> {
  return adminJson<SaveEditFeedbackAnnotationResponse>(
    `/edit-feedback/${encodeURIComponent(artifactId)}/annotations`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(input),
    },
  );
}

export async function adminSaveEditFeedbackAnnotationsBulk(
  artifactId: string,
  annotations: SaveEditFeedbackAnnotationInput[],
): Promise<SaveEditFeedbackAnnotationsBulkResponse> {
  return adminJson<SaveEditFeedbackAnnotationsBulkResponse>(
    `/edit-feedback/${encodeURIComponent(artifactId)}/annotations/bulk`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ annotations }),
    },
  );
}
