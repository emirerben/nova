/**
 * API client for generative-edit endpoints.
 * Mirrors src/lib/music-api.ts. Clip upload reuses the music slot-upload endpoint
 * (lands under the `music-uploads/` prefix the backend allowlists).
 */

const API_BASE = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

// Intro font-size envelope + nudge step (mirrors overlay_sizing.MIN/MAX_INTRO_PX
// on the backend, which clamps server-side regardless of what the UI sends).
export const INTRO_SIZE_MIN = 40;
export const INTRO_SIZE_MAX = 80;
export const INTRO_SIZE_STEP = 6;

export type GenerativeTextMode = "lyrics" | "agent_text" | "none";

export interface GenerativeVariant {
  variant_id: string;
  rank: number;
  text_mode: GenerativeTextMode;
  music_track_id: string | null;
  track_title: string | null;
  style_set_id: string | null;
  output_url: string | null;
  video_path: string | null;
  render_status: "ready" | "rendering" | "failed" | null;
  ok: boolean;
  error: string | null;
  // Agent-decided (or user-pinned) intro size. null for non-text variants.
  intro_text_size_px: number | null;
  intro_size_source: "computed" | "user" | null;
}

export interface GenerativeStyleSet {
  id: string;
  label: string;
  tags: string[];
  // Display-only typography of the set's representative (hook) role, so the picker
  // can render a real-font preview chip BEFORE a re-render. All optional — older
  // API builds omit them and the chip falls back to the page font. `css_family`
  // matches a `@font-face` from the shared registry (see lib/font-faces.ts).
  font_family?: string | null;
  css_family?: string | null;
  font_file?: string | null;
  font_weight?: number | null;
  text_color?: string | null;
  highlight_color?: string | null;
  effect?: string | null;
}

export interface GenerativeJobResponse {
  job_id: string;
  status: string;
}

export interface GenerativeJobStatus {
  job_id: string;
  status: string;
  variants: GenerativeVariant[];
  error_detail: string | null;
  created_at: string;
  updated_at: string;
}

/** Terminal statuses the poller should stop on. */
export const GENERATIVE_TERMINAL_STATUSES = [
  "variants_ready",
  "variants_ready_partial",
  "variants_failed",
  "processing_failed",
];

export async function uploadGenerativeClip(
  file: File,
): Promise<{ gcs_path: string; kind: "video" | "image" }> {
  const fd = new FormData();
  fd.append("file", file);
  const res = await fetch(`${API_BASE}/music-jobs/upload-slot`, { method: "POST", body: fd });
  if (!res.ok) {
    const detail = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(detail.detail ?? "Upload failed");
  }
  return res.json();
}

export async function createGenerativeJob(
  clip_gcs_paths: string[],
): Promise<GenerativeJobResponse> {
  // No target length: the backend derives output length from the uploaded
  // footage (and the matched song's beat structure), so the edit can never run
  // longer than the clips the user provided.
  const res = await fetch(`${API_BASE}/generative-jobs`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ clip_gcs_paths }),
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(detail.detail ?? "Failed to create generative job");
  }
  return res.json();
}

export async function getGenerativeJobStatus(jobId: string): Promise<GenerativeJobStatus> {
  const res = await fetch(`${API_BASE}/generative-jobs/${jobId}/status`);
  if (!res.ok) throw new Error(`Failed to get job status: ${res.status}`);
  return res.json();
}

export async function swapVariantSong(
  jobId: string,
  variantId: string,
  newTrackId: string,
): Promise<GenerativeJobResponse> {
  const res = await fetch(`${API_BASE}/generative-jobs/${jobId}/variants/${variantId}/swap-song`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ new_track_id: newTrackId }),
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(detail.detail ?? "Failed to swap song");
  }
  return res.json();
}

export async function retextVariant(
  jobId: string,
  variantId: string,
  opts: { text?: string; remove?: boolean },
): Promise<GenerativeJobResponse> {
  const res = await fetch(`${API_BASE}/generative-jobs/${jobId}/variants/${variantId}/retext`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text: opts.text ?? null, remove: opts.remove ?? false }),
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(detail.detail ?? "Failed to update text");
  }
  return res.json();
}

/** The curated text style sets selectable for a generative edit (generative-eligible). */
export async function getGenerativeStyleSets(): Promise<GenerativeStyleSet[]> {
  const res = await fetch(`${API_BASE}/generative-jobs/style-sets`);
  if (!res.ok) throw new Error(`Failed to load style sets: ${res.status}`);
  const data = await res.json();
  return data.style_sets;
}

export async function changeVariantStyle(
  jobId: string,
  variantId: string,
  styleSetId: string,
): Promise<GenerativeJobResponse> {
  const res = await fetch(
    `${API_BASE}/generative-jobs/${jobId}/variants/${variantId}/change-style`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ style_set_id: styleSetId }),
    },
  );
  if (!res.ok) {
    const detail = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(detail.detail ?? "Failed to change style");
  }
  return res.json();
}

export async function setVariantIntroSize(
  jobId: string,
  variantId: string,
  textSizePx: number,
): Promise<GenerativeJobResponse> {
  const res = await fetch(
    `${API_BASE}/generative-jobs/${jobId}/variants/${variantId}/intro-size`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text_size_px: Math.round(textSizePx) }),
    },
  );
  if (!res.ok) {
    const detail = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(detail.detail ?? "Failed to resize intro text");
  }
  return res.json();
}
