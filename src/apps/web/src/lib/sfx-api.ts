"use client";

// sfx-api.ts — typed client for sound-effects endpoints.
// Public:   GET /sound-effects
// Admin:    POST /upload-init-file, POST /{id}/upload-confirm, GET "", PATCH /{id},
//           DELETE /{id}, GET /{id}/audio-url

const API_BASE = process.env.NEXT_PUBLIC_API_URL ?? "";
const ADMIN_PROXY = "/api/admin/sound-effects";

export interface SoundEffectSummary {
  id: string;
  name: string;
  duration_s: number | null;
  published_at: string | null;
  archived_at: string | null;
  status: string;           // "pending" | "ready" | "failed"
  source_filename: string | null;
  // Short-lived signed audio URL from GET /sound-effects. Field name MUST match
  // the API (app/routes/sound_effects.py SoundEffectSummary.preview_audio_url) —
  // a mismatch silently drops live SFX preview audio.
  preview_audio_url?: string | null;
  // Closed-vocabulary role tags (smart sound design) — surfaced to the copilot
  // catalog so sounds can be picked by fit. Empty/absent on legacy effects.
  role_tags?: string[] | null;
}

export interface SoundEffectListResponse {
  effects: SoundEffectSummary[];
}

/** Public: list published, non-archived, ready effects. Used by the SoundEffectEditor picker. */
export async function getSoundEffects(): Promise<SoundEffectSummary[]> {
  const res = await fetch(`${API_BASE}/sound-effects`, { next: { revalidate: 60 } });
  if (!res.ok) throw new Error(`getSoundEffects failed: ${res.status}`);
  const data: SoundEffectListResponse = await res.json();
  return data.effects;
}

// ── Admin helpers ─────────────────────────────────────────────────────────────

async function adminRequest<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${ADMIN_PROXY}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
  });
  if (!res.ok) {
    const text = await res.text().catch(() => res.statusText);
    throw new Error(`Admin SFX request failed (${res.status}): ${text}`);
  }
  if (res.status === 204) return undefined as T;
  return res.json() as Promise<T>;
}

export interface InitUploadResponse {
  effect_id: string;
  upload_url: string;
  gcs_path: string;
}

/** Phase 1: mint an effect row + get a signed PUT URL. */
export async function initSfxUpload(
  filename: string,
  name?: string,
): Promise<InitUploadResponse> {
  return adminRequest<InitUploadResponse>(`/upload-init-file`, {
    method: "POST",
    body: JSON.stringify({ filename, name: name ?? filename }),
  });
}

/** Phase 2: PUT the file to GCS using the signed URL. Reports progress via onProgress. */
export async function putFileToGcs(
  uploadUrl: string,
  file: File,
  onProgress?: (pct: number) => void,
): Promise<void> {
  await new Promise<void>((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("PUT", uploadUrl);
    xhr.setRequestHeader("Content-Type", file.type);
    if (onProgress) {
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable) onProgress(Math.round((e.loaded / e.total) * 100));
      };
    }
    xhr.onload = () => (xhr.status >= 200 && xhr.status < 300 ? resolve() : reject(new Error(`GCS PUT ${xhr.status}`)));
    xhr.onerror = () => reject(new Error("GCS PUT network error"));
    xhr.send(file);
  });
}

export interface ConfirmUploadResponse {
  effect: SoundEffectSummary;
}

/** Phase 3: tell the backend to HEAD the blob, ffprobe, set status=ready. */
export async function confirmSfxUpload(effectId: string): Promise<SoundEffectSummary> {
  const res = await adminRequest<ConfirmUploadResponse>(`/${effectId}/upload-confirm`, {
    method: "POST",
    body: JSON.stringify({}),
  });
  return res.effect;
}

/** List all sound effects (admin view, includes unpublished). */
export async function listAdminSoundEffects(
  limit = 100,
  offset = 0,
): Promise<SoundEffectListResponse> {
  return adminRequest<SoundEffectListResponse>(`?limit=${limit}&offset=${offset}`);
}

export interface PatchSoundEffectPayload {
  name?: string;
  published?: boolean;   // true → set published_at=now; false → clear
  archived?: boolean;
}

/** Rename / publish / unpublish a sound effect. */
export async function patchSoundEffect(
  effectId: string,
  payload: PatchSoundEffectPayload,
): Promise<SoundEffectSummary> {
  return adminRequest<SoundEffectSummary>(`/${effectId}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}

/** Soft-archive a sound effect (hide from admin list). */
export async function archiveSoundEffect(effectId: string): Promise<void> {
  await adminRequest<void>(`/${effectId}`, { method: "DELETE" });
}

/** Get a 1-hour signed GET URL for the effect audio (for preview). */
export async function getSfxAudioUrl(effectId: string): Promise<string> {
  const res = await adminRequest<{ url: string }>(`/${effectId}/audio-url`);
  return res.url;
}

/** Full 3-phase upload: init → GCS PUT → confirm. Returns the ready effect. */
export async function adminUploadSfx(
  file: File,
  name?: string,
  onProgress?: (stage: "uploading" | "confirming", pct?: number) => void,
): Promise<SoundEffectSummary> {
  const { effect_id, upload_url } = await initSfxUpload(file.name, name);
  onProgress?.("uploading", 0);
  await putFileToGcs(upload_url, file, (pct) => onProgress?.("uploading", pct));
  onProgress?.("confirming");
  return confirmSfxUpload(effect_id);
}
