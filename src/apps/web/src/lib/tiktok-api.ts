import { NotAuthenticatedError } from "./plan-api";

const BASE = "/api/plan/tiktok";

export interface TikTokConnection {
  available: boolean;
  connected: boolean;
  status: string;
  account: { display_name?: string; avatar_url?: string } | null;
  granted_scopes: string[];
  can_publish: boolean;
  can_upload_draft: boolean;
  can_analyze: boolean;
  audited: boolean;
  beta: boolean;
  last_synced_at: string | null;
  learned_post_count: number;
}

export interface TikTokPublishOptions {
  preview_url: string;
  source_revision: string;
  variant_id: string | null;
  duration_s: number | null;
  creator_nickname: string;
  privacy_options: string[];
  comment_disabled: boolean;
  duet_disabled: boolean;
  stitch_disabled: boolean;
  max_duration_s: number;
  suggested_title: string;
  audited: boolean;
  consent_version: string;
  can_direct_post: boolean;
  can_upload_draft: boolean;
}

export interface TikTokPublication {
  id: string;
  job_id: string;
  variant_id: string | null;
  delivery_mode?: "direct_post" | "draft_upload";
  /** Optional during the Fly-before-Vercel response-shape rollout. */
  title?: string;
  /** TikTok's own publish id, for support correlation. Optional for the same rollout reason. */
  tiktok_publish_id?: string | null;
  privacy_level?: string;
  allow_comment?: boolean;
  allow_duet?: boolean;
  allow_stitch?: boolean;
  creator_nickname?: string | null;
  processing_status: string;
  visibility_status: string;
  public_at?: string | null;
  retryable: boolean;
  /** API-authoritative guard for deleting the owning library job. */
  deletion_blocked?: boolean;
  failure_code: string | null;
  failure_detail: string | null;
  latest_metrics: Record<string, number | null> | null;
  metrics_synced_at: string | null;
  evaluation_metrics?: Record<string, number | null> | null;
  evaluation_captured_at?: string | null;
  created_at: string;
  updated_at: string;
}

export function shouldPollTikTokPublication(publication: TikTokPublication): boolean {
  if (publication.id.startsWith("local-preview-")) {
    return false;
  }
  if (publication.delivery_mode === "draft_upload" && publication.processing_status === "complete") {
    return false;
  }
  if (publication.processing_status === "submission_unknown") {
    return false;
  }
  if (publication.processing_status === "failed" && !publication.retryable) {
    return false;
  }
  return !["draft", "public", "private", "removed"].includes(publication.visibility_status);
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${BASE}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
  });
  if (response.status === 401) throw new NotAuthenticatedError();
  if (!response.ok) {
    const payload = await response.json().catch(() => ({})) as { detail?: string };
    throw new Error(payload.detail || `TikTok request failed (${response.status})`);
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

export const getTikTokConnection = () => request<TikTokConnection>("/connection");

export async function startTikTokOAuth(returnTo?: string): Promise<void> {
  const result = await request<{ authorization_url: string }>("/oauth/start", {
    method: "POST",
    body: JSON.stringify({ return_to: returnTo ?? null }),
  });
  window.location.assign(result.authorization_url);
}

export const disconnectTikTok = () => request<void>("/connection", { method: "DELETE" });
export const syncTikTok = () => request<{ status: string }>("/sync", { method: "POST" });

export function getTikTokPublishOptions(jobId: string, variantId?: string | null) {
  const query = new URLSearchParams({ job_id: jobId });
  if (variantId) query.set("variant_id", variantId);
  return request<TikTokPublishOptions>(`/publish-options?${query}`);
}

export function createTikTokPublication(body: {
  job_id: string;
  variant_id?: string | null;
  source_revision: string;
  idempotency_key: string;
  delivery_mode: "direct_post" | "draft_upload";
  title: string;
  privacy_level: string;
  allow_comment: boolean;
  allow_duet: boolean;
  allow_stitch: boolean;
  brand_content_toggle: boolean;
  brand_organic_toggle: boolean;
  is_aigc: boolean;
  music_usage_confirmed: boolean;
  draft_handoff_confirmed: boolean;
  consent_version: string;
}) {
  return request<TikTokPublication>("/publications", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export const getTikTokPublication = (id: string) =>
  request<TikTokPublication>(`/publications/${id}`);

export function getTikTokPublicationReceipt(jobId: string, variantId?: string | null) {
  const query = new URLSearchParams({ job_id: jobId });
  if (variantId) query.set("variant_id", variantId);
  return request<TikTokPublication | null>(`/publications/receipt?${query}`);
}

export function listTikTokPublications(filters?: { jobId?: string; variantId?: string | null }) {
  const query = new URLSearchParams();
  if (filters?.jobId) query.set("job_id", filters.jobId);
  if (filters?.variantId) query.set("variant_id", filters.variantId);
  const encoded = query.toString();
  const suffix = encoded ? `?${encoded}` : "";
  return request<TikTokPublication[]>(`/publications${suffix}`);
}
