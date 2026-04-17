/**
 * API client for music-track endpoints.
 * Mirrors the pattern used in src/lib/api.ts for template calls.
 */

const API_BASE = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
// Admin calls go through the Next.js API proxy (/api/admin/...) so the
// admin token is read server-side only — never embedded in the browser bundle.
const ADMIN_PROXY = "/api/admin";

// ── Public types ──────────────────────────────────────────────────────────────

export interface MusicTrackSummary {
  id: string;
  title: string;
  artist: string;
  thumbnail_url: string | null;
  section_duration_s: number;
  required_clips_min: number;
  required_clips_max: number;
}

export interface MusicTrackListResponse {
  tracks: MusicTrackSummary[];
}

// ── Admin types ───────────────────────────────────────────────────────────────

export interface MusicTrackDetail {
  id: string;
  title: string;
  artist: string;
  source_url: string;
  audio_gcs_path: string | null;
  duration_s: number | null;
  beat_count: number;
  analysis_status: "queued" | "analyzing" | "ready" | "failed";
  error_detail: string | null;
  thumbnail_url: string | null;
  published_at: string | null;
  archived_at: string | null;
  track_config: TrackConfig | null;
  created_at: string;
}

export interface TrackConfig {
  best_start_s: number;
  best_end_s: number;
  slot_every_n_beats: number;
  required_clips_min: number;
  required_clips_max: number;
}

export interface AdminMusicListResponse {
  tracks: MusicTrackDetail[];
  total: number;
}

export interface MusicJobResponse {
  job_id: string;
  status: string;
  music_track_id: string;
}

export interface MusicJobStatus {
  job_id: string;
  status: string;
  music_track_id: string | null;
  assembly_plan: Record<string, unknown> | null;
  error_detail: string | null;
  created_at: string;
  updated_at: string;
}

// ── Public API ────────────────────────────────────────────────────────────────

export async function getMusicTracks(): Promise<MusicTrackListResponse> {
  const res = await fetch(`${API_BASE}/music-tracks`);
  if (!res.ok) throw new Error(`Failed to load music tracks: ${res.status}`);
  return res.json();
}

export async function createMusicJob(
  music_track_id: string,
  clip_gcs_paths: string[],
): Promise<MusicJobResponse> {
  const res = await fetch(`${API_BASE}/music-jobs`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ music_track_id, clip_gcs_paths }),
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(detail.detail ?? "Failed to create music job");
  }
  return res.json();
}

export async function getMusicJobStatus(jobId: string): Promise<MusicJobStatus> {
  const res = await fetch(`${API_BASE}/music-jobs/${jobId}/status`);
  if (!res.ok) throw new Error(`Failed to get job status: ${res.status}`);
  return res.json();
}

// ── Admin API ─────────────────────────────────────────────────────────────────
// All admin requests go through /api/admin/... (Next.js proxy) so the
// admin token is never sent to the browser.

const JSON_HEADERS = { "Content-Type": "application/json" };

export async function adminListMusicTracks(
  limit = 50,
  offset = 0,
): Promise<AdminMusicListResponse> {
  const res = await fetch(
    `${ADMIN_PROXY}/music-tracks?limit=${limit}&offset=${offset}`,
  );
  if (!res.ok) throw new Error(`Admin list failed: ${res.status}`);
  return res.json();
}

export async function adminGetMusicTrack(id: string): Promise<MusicTrackDetail> {
  const res = await fetch(`${ADMIN_PROXY}/music-tracks/${id}`);
  if (!res.ok) throw new Error(`Admin get track failed: ${res.status}`);
  return res.json();
}

export async function adminCreateMusicTrack(
  source_url: string,
  title?: string,
  artist?: string,
): Promise<{ id: string; analysis_status: string }> {
  const res = await fetch(`${ADMIN_PROXY}/music-tracks`, {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify({ source_url, title, artist }),
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(detail.detail ?? "Failed to create music track");
  }
  return res.json();
}

export async function adminUpdateMusicTrack(
  id: string,
  body: {
    title?: string;
    artist?: string;
    thumbnail_url?: string;
    track_config?: Partial<TrackConfig>;
    publish?: boolean;
    archive?: boolean;
  },
): Promise<MusicTrackDetail> {
  const res = await fetch(`${ADMIN_PROXY}/music-tracks/${id}`, {
    method: "PATCH",
    headers: JSON_HEADERS,
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`Admin update failed: ${res.status}`);
  return res.json();
}

export async function adminReanalyzeMusicTrack(
  id: string,
): Promise<{ track_id: string; analysis_status: string }> {
  const res = await fetch(`${ADMIN_PROXY}/music-tracks/${id}/reanalyze`, {
    method: "POST",
  });
  if (!res.ok) throw new Error(`Reanalyze failed: ${res.status}`);
  return res.json();
}

export async function adminArchiveMusicTrack(id: string): Promise<void> {
  const res = await fetch(`${ADMIN_PROXY}/music-tracks/${id}`, {
    method: "DELETE",
  });
  if (!res.ok && res.status !== 204) {
    throw new Error(`Archive failed: ${res.status}`);
  }
}
