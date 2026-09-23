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
  // Creator-library browse metadata (KRI-173): one of the API's
  // SFX_CATEGORIES ("rejection", "approval", …) and lowercase search words.
  category?: string | null;
  search_terms?: string[] | null;
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
  // The content type the PUT URL was signed for; the PUT must send exactly it.
  content_type: string;
  expires_in_s: number;
}

/** Containers the iPhone renderer can play (mirrors the API allowlist). */
export const SFX_UPLOAD_EXTENSIONS = [".m4a", ".mp4", ".wav", ".mp3", ".aac"] as const;

export function sfxUploadExtension(filename: string): string {
  const dot = filename.lastIndexOf(".");
  const ext = dot >= 0 ? filename.slice(dot).toLowerCase() : "";
  if (!(SFX_UPLOAD_EXTENSIONS as readonly string[]).includes(ext)) {
    throw new Error(`Use ${SFX_UPLOAD_EXTENSIONS.join(", ")} — the iPhone can't play "${ext || "no extension"}".`);
  }
  return ext;
}

/** Phase 1: mint an effect row + get a signed PUT URL. */
export async function initSfxUpload(file: File, name?: string): Promise<InitUploadResponse> {
  return adminRequest<InitUploadResponse>(`/upload-init-file`, {
    method: "POST",
    body: JSON.stringify({
      filename: file.name,
      name: name ?? file.name,
      ext: sfxUploadExtension(file.name),
      byte_count: file.size,
    }),
  });
}

/** Phase 2: PUT the file to GCS using the signed URL. Reports progress via onProgress. */
export async function putFileToGcs(
  uploadUrl: string,
  file: File,
  contentType: string,
  onProgress?: (pct: number) => void,
): Promise<void> {
  await new Promise<void>((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("PUT", uploadUrl);
    // Must match the signed content type, not the browser's guess
    // (Safari reports .m4a as audio/x-m4a, which breaks the signature).
    xhr.setRequestHeader("Content-Type", contentType);
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
  effect_id: string;
  status: string;
  duration_s: number | null;
}

/** Phase 3: tell the backend to HEAD the blob, ffprobe, set status=ready. */
export async function confirmSfxUpload(effectId: string): Promise<ConfirmUploadResponse> {
  return adminRequest<ConfirmUploadResponse>(`/${effectId}/upload-confirm`, {
    method: "POST",
    body: JSON.stringify({}),
  });
}

/** List all sound effects (admin view, includes unpublished). */
export async function listAdminSoundEffects(
  limit = 100,
  offset = 0,
): Promise<SoundEffectListResponse> {
  return adminRequest<SoundEffectListResponse>(`?limit=${limit}&offset=${offset}`);
}

// Field names mirror the API's UpdateSoundEffectRequest exactly — unknown
// keys are silently ignored server-side, so a typo is a no-op, not an error.
export interface PatchSoundEffectPayload {
  name?: string;
  publish?: boolean; // true → set published_at=now; false → clear
  archive?: boolean;
  manual_audit_status?: "pending" | "approved" | "rejected";
  contains_voice?: boolean;
  category?: string;
  search_terms?: string[];
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
  const res = await adminRequest<{ audio_url: string }>(`/${effectId}/audio-url`);
  return res.audio_url;
}

/** Full 3-phase upload: init → GCS PUT → confirm. Resolves once the effect is ready. */
export async function adminUploadSfx(
  file: File,
  name?: string,
  onProgress?: (stage: "uploading" | "confirming", pct?: number) => void,
): Promise<ConfirmUploadResponse> {
  const { effect_id, upload_url, content_type } = await initSfxUpload(file, name);
  onProgress?.("uploading", 0);
  await putFileToGcs(upload_url, file, content_type, (pct) => onProgress?.("uploading", pct));
  onProgress?.("confirming");
  return confirmSfxUpload(effect_id);
}
