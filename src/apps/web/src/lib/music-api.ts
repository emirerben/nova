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
  /**
   * "templated" tracks have typed slots (e.g. Love-From-Moon: slot 1 fixed
   * image, slot 2 user upload). The frontend renders a per-slot upload UI
   * instead of the generic clip-list textarea.
   */
  template_kind: "beat_sync" | "templated";
  user_slot_count: number;
  /** One entry per user_upload slot; comma-joined accepted kinds, e.g. "video,image". */
  user_slot_accepts: string[];
}

export interface SlotUploadResponse {
  gcs_path: string;
  kind: "video" | "image";
}

export interface MusicTrackListResponse {
  tracks: MusicTrackSummary[];
}

// ── Admin types ───────────────────────────────────────────────────────────────

/**
 * One ranked edit-worthy section from the `song_sections` agent.
 * Source of truth: src/apps/api/app/agents/_schemas/song_sections.py.
 * Keep the literal unions in sync manually when that schema changes.
 */
export interface SongSection {
  rank: 1 | 2 | 3;
  start_s: number;
  end_s: number;
  label:
    | "intro"
    | "verse"
    | "pre_chorus"
    | "chorus"
    | "drop"
    | "bridge"
    | "outro"
    | "hook"
    | "build";
  energy: "low" | "medium" | "high" | "peaks_high";
  suggested_use: "hook" | "build" | "climax" | "ambient" | "transition";
  rationale: string;
}

export interface MusicTrackDetail {
  id: string;
  title: string;
  artist: string;
  source_url: string;
  audio_gcs_path: string | null;
  duration_s: number | null;
  beat_count: number;
  beat_timestamps_s: number[] | null;
  analysis_status: "queued" | "analyzing" | "ready" | "failed";
  error_detail: string | null;
  thumbnail_url: string | null;
  published_at: string | null;
  archived_at: string | null;
  track_config: TrackConfig | null;
  lyrics_status: LyricsStatus;
  lyrics_source: string | null;
  lyrics_error_detail: string | null;
  lyrics_cached: LyricsCache | null;
  /**
   * Non-publishable Whisper-only draft kept for admin reference when the
   * production extraction fails (status='needs_manual_lyrics'). Surfaced in
   * the admin UI below the diagnostic block so the operator can cross-check
   * timestamps when picking an LRCLIB row. NEVER read by production
   * consumers. Added 2026-05-27 (Beauty And A Beat PR).
   */
  lyrics_whisper_draft: LyricsCache | null;
  /**
   * Structured trace of the last LRCLIB lookup attempt: cleaned title/artist
   * sent, response statuses of /api/get and /api/search, top fuzzy score,
   * matched LRCLIB id, duration delta. Rendered as the diagnostic block on
   * the Lyrics tab when status='needs_manual_lyrics'. Added 2026-05-27.
   */
  lyrics_diagnostic: LyricsDiagnostic | null;
  /**
   * Monotonic counter behind the stale-task discard gate. The FE pins its
   * paste-ID action to whatever version it last saw, so an admin who clicks
   * Force-ID while a prior extraction is still running can't race it.
   * Added 2026-05-27.
   */
  lyrics_extraction_version: number;
  lyrics_extracted_at: string | null;
  best_sections: SongSection[] | null;
  section_version: string | null;
  /**
   * Last reason `_run_song_sections` returned None for this track on the
   * broad-Exception (best-effort fail-open) branch. NULL when sections are
   * populated, when the agent has not run yet, or after a successful
   * re-analyze. Surfaced under the amber "no agent sections" tag in the
   * admin UI so the operator can see WHY before re-analyzing blindly.
   * Added 2026-05-28.
   */
  section_error_detail: string | null;
  label_version: string | null;
  has_ai_labels: boolean;
  generative_matchable: boolean;
  created_at: string;
}

export interface MusicTrackListItem {
  id: string;
  title: string;
  artist: string;
  analysis_status: "queued" | "analyzing" | "ready" | "failed";
  thumbnail_url: string | null;
  beat_count: number;
  published_at: string | null;
  archived_at: string | null;
  label_version: string | null;
  section_version: string | null;
  has_ai_labels: boolean;
  generative_matchable: boolean;
  created_at: string;
}

export type LyricsStatus =
  | "pending"
  | "extracting"
  | "ready"
  | "failed"
  | "unavailable"
  /**
   * LRCLIB lookup failed (or matched a wrong-recording row at low confidence).
   * The Whisper-only draft lives on `lyrics_whisper_draft`; the admin must
   * paste an LRCLIB row ID via `adminForceLrclibId` to recover.
   * Added 2026-05-27 (Beauty And A Beat PR).
   */
  | "needs_manual_lyrics";

/**
 * Structured trace of the last LRCLIB lookup attempt. Backend writes one
 * blob per extraction terminal state. The Lyrics tab surfaces a
 * human-readable summary block when status='needs_manual_lyrics'.
 *
 * The shape is loose by design: backend additions (new diagnostic fields,
 * deeper trace levels) deserialize without a FE change. Only the fields
 * the FE renders are typed strictly.
 */
export interface LyricsDiagnostic {
  query: {
    title: string;
    artist: string;
    duration_s: number | null;
    forced_lrclib_id: number | null;
  };
  /**
   * Status of the /api/get pass.
   *   "hit" | "not_found" | "error" | "skipped" (when forced_lrclib_id set)
   *   | "forced_id_hit" | "forced_id_not_found" | "forced_id_error"
   * Loose `string` so backend additions don't require a FE bump.
   */
  get_status: string;
  /**
   * Status of the /api/search fuzzy fallback pass.
   *   "hit" | "no_strong_match" | "not_found" | "error" | "skipped" | etc.
   */
  search_status: string;
  search_top_score: number | null;
  lrclib_id_matched: number | null;
  fallback_path: string;
  /**
   * |LRCLIB recording duration − uploaded audio duration| at the matched
   * row, in seconds. Used by the UI to render the duration-mismatch
   * warning banner ("LRCLIB recording is X:XX, your audio is Y:YY"). Null
   * when not computed (e.g. forced-ID 404, network error).
   */
  duration_delta_s: number | null;
  lrclib_error: string | null;
  attempted_at: string;
}

export type LyricsStyle = "karaoke" | "per-word-pop" | "line";

/** Per-template visual config. Stored nested under `track_config.lyrics_config`. */
export interface LyricsConfig {
  enabled: boolean;
  style: LyricsStyle;
  position?: string;
  text_color?: string;
  highlight_color?: string; // karaoke only
  font_style?: "display" | "sans" | "serif";
  text_size?: "small" | "medium" | "large" | "xlarge";
  outline_px?: number;
  pre_roll_s?: number;
  post_dwell_s?: number;
  next_line_gap_s?: number;
  fade_in_ms?: number;
  fade_out_ms?: number;
  hold_to_next_threshold_ms?: number;
  font_family?: string;
  /**
   * Admin manual override: pin a specific LRCLIB row ID for extraction.
   * Set via `adminForceLrclibId` (POST /lyrics-force-lrclib-id), never
   * directly via PATCH /lyrics-config. The agent fetches /api/get/{id}
   * instead of doing title/artist search. Added 2026-05-27.
   */
  forced_lrclib_id?: number | null;
}

export interface LyricsConfigOverride {
  pre_roll_s?: number;
  post_dwell_s?: number;
  next_line_gap_s?: number;
  fade_in_ms?: number;
  fade_out_ms?: number;
  hold_to_next_threshold_ms?: number;
  font_family?: string;
}

export interface LyricsCacheWord {
  text: string;
  start_s: number;
  end_s: number;
}

export interface LyricsCacheLine {
  text: string;
  start_s: number;
  end_s: number;
  words: LyricsCacheWord[];
}

export interface LyricsCache {
  source: string;
  language: string;
  track_title_matched: string;
  artist_matched: string;
  genius_url: string;
  confidence: number;
  lines: LyricsCacheLine[];
}

export interface TrackConfig {
  best_start_s: number;
  best_end_s: number;
  slot_every_n_beats: number;
  required_clips_min: number;
  required_clips_max: number;
  /** Lives nested in the same JSONB column to avoid an extra round trip. */
  lyrics_config?: LyricsConfig;
}

export interface AdminMusicListResponse {
  tracks: MusicTrackListItem[];
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

export interface LyricsPreviewStatus {
  job_id: string;
  status: string;
  output_url: string | null;
  error_detail: string | null;
  lyrics_config_effective: Record<string, unknown> | null;
  // Window the preview rendered, anchored at the first lyric line minus a
  // small lead-in. Null on legacy rows rendered before the auto-anchor PR.
  preview_start_s: number | null;
  preview_duration_s: number | null;
  // The lyric style this preview was rendered in. Null on rows rendered
  // before the multi-style dashboard shipped (those were always Line).
  style: LyricsStyle | null;
  created_at: string;
  updated_at: string;
}

export interface LyricsPreviewCreateResponse {
  job_id: string;
  style: LyricsStyle;
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
    throw new Error(detail.detail || "Failed to create music job");
  }
  return res.json();
}

export async function getMusicJobStatus(jobId: string): Promise<MusicJobStatus> {
  const res = await fetch(`${API_BASE}/music-jobs/${jobId}/status`);
  if (!res.ok) throw new Error(`Failed to get job status: ${res.status}`);
  return res.json();
}

export async function uploadMusicSlot(file: File): Promise<SlotUploadResponse> {
  const fd = new FormData();
  fd.append("file", file);
  const res = await fetch(`${API_BASE}/music-jobs/upload-slot`, {
    method: "POST",
    body: fd,
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(detail.detail || "Upload failed");
  }
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
    throw new Error(detail.detail || "Failed to create music track");
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

export async function adminPatchLyricsConfig(
  trackId: string,
  partial: LyricsConfigOverride,
): Promise<{ lyrics_config: Partial<LyricsConfig> }> {
  const res = await fetch(`${ADMIN_PROXY}/music-tracks/${trackId}/lyrics-config`, {
    method: "PATCH",
    headers: JSON_HEADERS,
    body: JSON.stringify(partial),
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(detail.detail || `Lyrics config save failed: ${res.status}`);
  }
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

export async function adminExtractLyrics(
  id: string,
): Promise<{ track_id: string; analysis_status: string }> {
  const res = await fetch(`${ADMIN_PROXY}/music-tracks/${id}/extract-lyrics`, {
    method: "POST",
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(detail.detail || `Extract lyrics failed: ${res.status}`);
  }
  return res.json();
}

/**
 * Pin a specific LRCLIB row for this track and re-extract.
 *
 * Recovery path for the `needs_manual_lyrics` state: admin pastes a numeric
 * LRCLIB row ID or an `lrclib.net/lyrics/<id>` URL. The backend validates the
 * input via a strict host-allowlist parser, persists `forced_lrclib_id` into
 * `track_config.lyrics_config`, bumps `lyrics_extraction_version` (stale-task
 * gate), and dispatches the Celery extraction task — which fetches the named
 * row directly via /api/get/{id} instead of doing title/artist search.
 *
 * Throws on 4xx with the server's error detail (e.g. "URL host 'evil.com' is
 * not an LRCLIB host"), so the caller can surface a clear UI message.
 * Added 2026-05-27 (Beauty And A Beat PR).
 */
export async function adminForceLrclibId(
  id: string,
  idOrUrl: string,
): Promise<{ track_id: string; analysis_status: string }> {
  const res = await fetch(
    `${ADMIN_PROXY}/music-tracks/${id}/lyrics-force-lrclib-id`,
    {
      method: "POST",
      headers: JSON_HEADERS,
      body: JSON.stringify({ id_or_url: idOrUrl }),
    },
  );
  if (!res.ok) {
    const detail = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(detail.detail || `Force LRCLIB ID failed: ${res.status}`);
  }
  return res.json();
}

/** Extensions accepted by the admin SPA's "Upload file" form. Must mirror the
 *  backend's _BROWSER_AUDIO_EXT_ALLOWLIST in app/routes/admin_music.py — the
 *  init endpoint Pydantic-rejects anything else with a 422. */
const FILE_UPLOAD_ALLOWED_EXTS = new Set([
  ".m4a",
  ".mp3",
  ".wav",
  ".ogg",
  ".aac",
  ".mp4",
  ".webm",
  ".opus",
]);

function extOf(filename: string): string {
  const dot = filename.lastIndexOf(".");
  return dot >= 0 ? filename.slice(dot).toLowerCase() : "";
}

/** Direct-file upload from the admin SPA, via the signed-URL bypass that
 *  sidesteps the Vercel admin-proxy 4.5 MB function body cap.
 *
 *  Three phases:
 *    1. POST /upload-init-file        → pending row + signed PUT URL
 *    2. PUT  <signed-url>             → bytes go straight to GCS (no proxy hop)
 *    3. POST /{id}/upload-confirm     → HEAD + ffprobe + dispatch analyze task
 *
 *  Progress is reported through the same IngestProgress shape used by the
 *  extension flow so the SPA can reuse ExtensionProgressBar verbatim. The PUT
 *  uses XHR (not fetch) because fetch still has no upload-progress event — a
 *  20 MB upload with a single spinner is exactly the tab-close-panic UX the
 *  extension flow's three-stage bar was built to fix.
 */
export async function adminUploadMusicTrack(
  file: File,
  title?: string,
  artist?: string,
  onProgress?: (p: IngestProgress) => void,
): Promise<{ id: string; analysis_status: string }> {
  const ext = extOf(file.name);
  if (!FILE_UPLOAD_ALLOWED_EXTS.has(ext)) {
    throw new Error(
      `Unsupported audio extension: ${ext || "(none)"}. ` +
        "Use m4a, mp3, wav, ogg, aac, mp4, webm, or opus.",
    );
  }

  // Phase 1: mint signed URL (Vercel proxy hop, body is JSON not blob).
  onProgress?.({ stage: "uploading", percent: 0, detail: "Preparing upload…" });
  const initRes = await fetch(`${ADMIN_PROXY}/music-tracks/upload-init-file`, {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify({
      filename: file.name,
      title: title || undefined,
      artist: artist || undefined,
      ext,
      byte_count: file.size,
    }),
  });
  if (!initRes.ok) {
    const detail = await initRes.json().catch(() => ({ detail: initRes.statusText }));
    const msg =
      typeof detail.detail === "string" && detail.detail
        ? detail.detail
        : `upload-init-file failed (${initRes.status})`;
    onProgress?.({ stage: "failed", detail: msg });
    throw new Error(msg);
  }
  const init: BrowserUploadInitResponse = await initRes.json();
  onProgress?.({
    stage: "uploading",
    percent: 0,
    detail: `0 / ${(file.size / 1024 / 1024).toFixed(1)} MB`,
    track_id: init.track_id,
  });

  // Phase 2: PUT bytes directly to GCS. CORS on the prod bucket is configured
  // to allow exactly two origins: https://nova-video.vercel.app and
  // http://localhost:3000 (Allow-Methods GET/PUT/HEAD, Allow-Headers
  // Content-Type) — empirically verified via OPTIONS preflight. Vercel preview
  // deploys (nova-video-git-*.vercel.app) are NOT in the allowlist, so this
  // upload will fail on previews with a CORS error. Test on prod or update the
  // bucket CORS config to add a preview pattern if preview testing is needed.
  // The signed URL pins Content-Type, so we MUST send exactly init.content_type
  // or GCS returns 403 SignatureDoesNotMatch.
  await new Promise<void>((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("PUT", init.upload_url, true);
    xhr.setRequestHeader("Content-Type", init.content_type);
    xhr.upload.onprogress = (e: ProgressEvent) => {
      if (!e.lengthComputable) return;
      const percent = e.total > 0 ? e.loaded / e.total : 0;
      const loadedMb = (e.loaded / 1024 / 1024).toFixed(1);
      const totalMb = (e.total / 1024 / 1024).toFixed(1);
      onProgress?.({
        stage: "uploading",
        percent,
        detail: `${loadedMb} / ${totalMb} MB`,
        track_id: init.track_id,
      });
    };
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) resolve();
      else
        reject(
          new Error(
            `GCS PUT failed (${xhr.status}): ${xhr.responseText.slice(0, 200) || xhr.statusText}`,
          ),
        );
    };
    xhr.onerror = () =>
      reject(new Error("Network error during upload (check connection)"));
    xhr.onabort = () => reject(new Error("Upload aborted"));
    xhr.send(file);
  }).catch((err: unknown) => {
    const msg = err instanceof Error ? err.message : String(err);
    onProgress?.({ stage: "failed", detail: msg, track_id: init.track_id });
    throw err instanceof Error ? err : new Error(msg);
  });

  // Phase 3: ask the server to verify + dispatch Celery analysis.
  onProgress?.({
    stage: "confirming",
    detail: "Verifying upload…",
    track_id: init.track_id,
  });
  const confirmRes = await fetch(
    `${ADMIN_PROXY}/music-tracks/${init.track_id}/upload-confirm`,
    { method: "POST", headers: JSON_HEADERS },
  );
  if (!confirmRes.ok) {
    const detail = await confirmRes
      .json()
      .catch(() => ({ detail: confirmRes.statusText }));
    const msg =
      typeof detail.detail === "string" && detail.detail
        ? detail.detail
        : `upload-confirm failed (${confirmRes.status})`;
    onProgress?.({ stage: "failed", detail: msg, track_id: init.track_id });
    throw new Error(msg);
  }
  const confirmed: BrowserUploadConfirmResponse = await confirmRes.json();
  onProgress?.({
    stage: "analyzing",
    detail: "Beat detection running…",
    track_id: init.track_id,
  });
  return { id: confirmed.track_id, analysis_status: confirmed.analysis_status };
}

export async function adminGetAudioUrl(id: string): Promise<string> {
  const res = await fetch(`${ADMIN_PROXY}/music-tracks/${id}/audio-url`);
  if (!res.ok) throw new Error(`Failed to get audio URL: ${res.status}`);
  const data = await res.json();
  return data.audio_url;
}

// ── Browser-side ingest (Chrome extension flow) ────────────────────────────
//
// Two-phase upload that bypasses both Vercel's body-size cap and Fly's IP
// being flagged by YouTube. The browser extension does the actual extraction
// from googlevideo.com (residential IP + admin's logged-in YT cookies); these
// helpers are the SPA's side of the contract. See plan:
// ~/.claude/plans/sen-k-demli-bir-yaz-l-m-rosy-acorn.md

/** Placeholder returned when the extension hasn't injected its ID yet. */
const EXTENSION_ID_NOT_SET = "nova-extension-id-not-set";

/** Resolve the Nova extension ID at call time.
 *  The extension's content script (src/apps/extension/src/content.js) sets
 *  `<html data-nova-extension-id="…">` at document_start, so the attribute
 *  is normally already on the document by the time SPA code runs. The
 *  `window.__NOVA_EXTENSION_ID__` fallback is kept for back-compat with any
 *  external setter that may already wire it that way.
 *  manifest.key is deferred (Phase 2), so the ID is per-machine random for
 *  unpacked loads — a hardcoded constant would not work. */
export function novaExtensionId(): string {
  if (typeof document !== "undefined" && document.documentElement) {
    const attr = document.documentElement.getAttribute("data-nova-extension-id");
    if (attr) return attr;
  }
  if (typeof window !== "undefined") {
    const w = window as unknown as { __NOVA_EXTENSION_ID__?: string };
    if (w.__NOVA_EXTENSION_ID__) return w.__NOVA_EXTENSION_ID__;
  }
  return EXTENSION_ID_NOT_SET;
}

interface ChromeRuntime {
  sendMessage(
    extensionId: string,
    message: unknown,
    callback: (response: unknown) => void,
  ): void;
  lastError?: { message?: string };
}
interface ChromeNS {
  runtime?: ChromeRuntime;
}

function chromeRuntime(): ChromeRuntime | null {
  const c = (globalThis as unknown as { chrome?: ChromeNS }).chrome;
  return c?.runtime ?? null;
}

/** Race-tolerantly resolve the extension ID. If content.js has already set
 *  the DOM attribute we return immediately; otherwise we wait up to
 *  `timeoutMs` for either the `nova-extension-ready` CustomEvent or the
 *  attribute to appear (polled at 50ms). Returns the placeholder on timeout
 *  so callers can short-circuit. */
async function resolveExtensionId(timeoutMs: number): Promise<string> {
  const initial = novaExtensionId();
  if (initial && initial !== EXTENSION_ID_NOT_SET) return initial;
  if (typeof document === "undefined") return EXTENSION_ID_NOT_SET;
  return new Promise<string>((resolve) => {
    let done = false;
    const finish = (v: string) => {
      if (done) return;
      done = true;
      document.removeEventListener("nova-extension-ready", onReady);
      clearInterval(poll);
      clearTimeout(timer);
      resolve(v);
    };
    const onReady = (e: Event) => {
      const ce = e as CustomEvent<{ extensionId?: string }>;
      if (ce.detail?.extensionId) finish(ce.detail.extensionId);
    };
    document.addEventListener("nova-extension-ready", onReady);
    const poll = setInterval(() => {
      const v = novaExtensionId();
      if (v && v !== EXTENSION_ID_NOT_SET) finish(v);
    }, 50);
    const timer = setTimeout(() => finish(novaExtensionId()), timeoutMs);
  });
}

/** Returns true iff the Nova extension is installed AND reachable from this page.
 *  First resolves the extension ID (DOM attribute set by the content script,
 *  with an event-driven fallback for slow profiles), then sends a one-shot
 *  `ping` and waits for a `pong`. Resolves false on overall timeout so the UI
 *  can swap to "Install Nova extension" without hanging. */
export async function detectExtension(timeoutMs = 1500): Promise<boolean> {
  const runtime = chromeRuntime();
  if (!runtime) return false;
  const startedAt = Date.now();
  // Reserve a chunk of the budget for the ID handshake; the rest goes to
  // the ping round-trip.
  const idBudget = Math.min(timeoutMs - 250, Math.round(timeoutMs * 0.75));
  const extensionId = await resolveExtensionId(Math.max(idBudget, 100));
  if (!extensionId || extensionId === EXTENSION_ID_NOT_SET) return false;
  const remaining = Math.max(timeoutMs - (Date.now() - startedAt), 200);
  return new Promise<boolean>((resolve) => {
    let done = false;
    const finish = (ok: boolean) => {
      if (done) return;
      done = true;
      resolve(ok);
    };
    const timer = setTimeout(() => finish(false), remaining);
    try {
      runtime.sendMessage(
        extensionId,
        { target: "nova_extension", type: "ping" },
        (resp: unknown) => {
          clearTimeout(timer);
          if (runtime.lastError) {
            finish(false);
            return;
          }
          finish(
            typeof resp === "object" &&
              resp !== null &&
              (resp as { ok?: boolean }).ok === true,
          );
        },
      );
    } catch {
      clearTimeout(timer);
      finish(false);
    }
  });
}

export interface BrowserUploadInitResponse {
  track_id: string;
  upload_url: string;
  gcs_path: string;
  content_type: string;
  expires_in_s: number;
}

export interface BrowserUploadConfirmResponse {
  track_id: string;
  analysis_status: string;
  duration_s: number | null;
}

export interface ExtensionInitArgs {
  source_url: string;
  title?: string;
  artist?: string;
  ext: string; // ".m4a", ".webm", ...
  byte_count: number;
}

export async function extensionUploadInit(
  args: ExtensionInitArgs,
): Promise<BrowserUploadInitResponse> {
  const res = await fetch(`${ADMIN_PROXY}/music-tracks/upload-init`, {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify(args),
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => ({ detail: res.statusText }));
    // 409 dedup carries a structured detail object with existing_track_id —
    // bubble it up untransformed so the caller can offer "View existing track".
    if (res.status === 409 && typeof detail.detail === "object") {
      throw new ExtensionDedupError(detail.detail);
    }
    throw new Error(
      typeof detail.detail === "string" && detail.detail
        ? detail.detail
        : `upload-init failed (${res.status})`,
    );
  }
  return res.json();
}

export async function extensionUploadConfirm(
  trackId: string,
): Promise<BrowserUploadConfirmResponse> {
  const res = await fetch(
    `${ADMIN_PROXY}/music-tracks/${trackId}/upload-confirm`,
    { method: "POST", headers: JSON_HEADERS },
  );
  if (!res.ok) {
    const detail = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(detail.detail || `upload-confirm failed (${res.status})`);
  }
  return res.json();
}

export class ExtensionDedupError extends Error {
  existing_track_id: string;
  existing_status: string;
  constructor(detail: { existing_track_id: string; existing_status: string }) {
    super(
      `Track already exists (status: ${detail.existing_status}). Use existing ID.`,
    );
    this.name = "ExtensionDedupError";
    this.existing_track_id = detail.existing_track_id;
    this.existing_status = detail.existing_status;
  }
}

/** UX-facing stage indicator for the 3-stage progress UI. Single spinner is
 *  forbidden — the admin needs to know whether we're (a) pulling from YouTube
 *  via their browser, (b) uploading bytes to our GCS, or (c) waiting on Celery
 *  beat-detect. Conflating these into "Processing..." causes tab-close panic. */
export type IngestStage =
  | "extension_check"
  | "extracting"
  | "uploading"
  | "confirming"
  | "analyzing"
  | "ready"
  | "failed";

export interface IngestProgress {
  stage: IngestStage;
  /** 0..1 within the current stage when reportable, else null. */
  percent?: number | null;
  /** Free-form status detail, e.g. "5.2 MB / 12 MB" or error message. */
  detail?: string;
  /** Once init has run, the track id we're ingesting against. */
  track_id?: string;
}

/** Drive the full ingest end-to-end via the extension.
 *
 *  Lifecycle:
 *    extension_check → extracting (extension fetches from googlevideo)
 *    → uploading (extension PUTs blob to GCS via signed URL we mint)
 *    → confirming (we tell server "blob landed, please verify + dispatch")
 *    → analyzing (Celery runs beat detect; SPA can poll separately)
 *
 *  The extension does all the heavy work; this function is the message-passing
 *  glue. Errors at any stage produce `IngestProgress { stage: "failed", detail }`
 *  and reject the returned promise.
 */
export async function extensionIngest(
  args: { url: string; title?: string; artist?: string },
  onProgress: (p: IngestProgress) => void,
): Promise<{ track_id: string }> {
  const runtime = chromeRuntime();
  if (!runtime) {
    onProgress({ stage: "failed", detail: "Nova extension not installed" });
    throw new Error("Nova extension not installed");
  }
  onProgress({ stage: "extension_check" });
  const ok = await detectExtension();
  if (!ok) {
    onProgress({ stage: "failed", detail: "Nova extension not reachable" });
    throw new Error("Nova extension not reachable");
  }
  // detectExtension() resolved the ID via the DOM-attribute bridge and
  // confirmed it's reachable; reuse the same accessor here. Stable for the
  // duration of this ingest because content.js writes the attribute once
  // at document_start and doesn't mutate it.
  const extensionId = novaExtensionId();

  // Per-ingest jobId so the listener can filter out events from concurrent
  // ingests in other tabs / a previous abandoned ingest in this tab.
  // Without this, two overlapping calls would both react to the same
  // `ready`/`failed` event and resolve with the wrong track_id.
  const jobId = `j_${Date.now().toString(36)}_${Math.random()
    .toString(36)
    .slice(2, 8)}`;

  // Absolute origin (not relative). Otherwise the extension's offscreen doc
  // — which has no notion of "the SPA's current origin" — defaults to its
  // hardcoded prod fallback and a preview-deploy admin would silently pollute
  // PROD's MusicTrack table from a non-prod SPA.
  const proxyBase =
    typeof window !== "undefined" && window.location?.origin
      ? `${window.location.origin}${ADMIN_PROXY}`
      : ADMIN_PROXY;

  // Delegate to the extension. The extension calls upload-init/confirm on its
  // own (via fetch to the Nova proxy, which injects the admin token); we just
  // kick it off and tail the progress events it broadcasts back.
  return new Promise((resolve, reject) => {
    const listener = (msg: unknown) => {
      if (typeof msg !== "object" || msg === null) return;
      const m = msg as {
        type?: string;
        stage?: IngestStage;
        jobId?: string;
        payload?: IngestProgress & { jobId?: string };
      };
      if (m.type !== "nova_ingest_event" || !m.stage) return;
      // Filter: event must carry our jobId, either at message top-level or
      // inside payload. Reject events from other ingests (older tab, peer tab).
      const eventJobId = m.jobId ?? m.payload?.jobId;
      if (eventJobId && eventJobId !== jobId) return;
      const event = (m.payload ?? { stage: m.stage }) as IngestProgress;
      onProgress(event);
      if (event.stage === "failed") {
        cleanup();
        reject(new Error(event.detail ?? "Extension ingest failed"));
        return;
      }
      if (event.stage === "ready" && event.track_id) {
        cleanup();
        resolve({ track_id: event.track_id });
      }
    };
    const win = globalThis as unknown as {
      addEventListener: (type: string, fn: (e: MessageEvent) => void) => void;
      removeEventListener: (type: string, fn: (e: MessageEvent) => void) => void;
    };
    const messageHandler = (e: MessageEvent) => listener(e.data);
    win.addEventListener("message", messageHandler);
    const cleanup = () => win.removeEventListener("message", messageHandler);

    try {
      runtime.sendMessage(
        extensionId,
        {
          target: "nova_extension",
          type: "ingest",
          jobId,
          payload: {
            url: args.url,
            title: args.title,
            artist: args.artist,
            proxy_base: proxyBase,
            jobId,
          },
        },
        (resp: unknown) => {
          if (runtime.lastError) {
            cleanup();
            const err = runtime.lastError.message ?? "Extension call failed";
            onProgress({ stage: "failed", detail: err });
            reject(new Error(err));
            return;
          }
          const r = resp as { ok?: boolean; error?: string };
          if (!r?.ok) {
            cleanup();
            const err = r?.error ?? "Extension rejected the ingest call";
            onProgress({ stage: "failed", detail: err });
            reject(new Error(err));
          }
          // Success ack just means the extension picked up the work. The real
          // resolution comes from the `ready` event the listener handles.
        },
      );
    } catch (e: unknown) {
      cleanup();
      const err = e instanceof Error ? e.message : String(e);
      onProgress({ stage: "failed", detail: err });
      reject(new Error(err));
    }
  });
}

export async function adminArchiveMusicTrack(id: string): Promise<void> {
  const res = await fetch(`${ADMIN_PROXY}/music-tracks/${id}`, {
    method: "DELETE",
  });
  if (!res.ok && res.status !== 204) {
    throw new Error(`Archive failed: ${res.status}`);
  }
}

// ── Admin music test jobs ─────────────────────────────────────────────────────

/** A music job rendered from the admin Test tab (any analysis_status=ready track). */
export interface AdminMusicTestJobSummary {
  job_id: string;
  status: string;
  error_detail: string | null;
  output_url: string | null;
  clip_count: number;
  created_at: string;
  updated_at: string;
}

export async function adminCreateMusicTestJob(
  trackId: string,
  clipGcsPaths: string[],
  lyricsConfigOverride?: LyricsConfigOverride,
): Promise<MusicJobResponse> {
  const res = await fetch(`${ADMIN_PROXY}/music-tracks/${trackId}/test-job`, {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify({
      clip_gcs_paths: clipGcsPaths,
      ...(lyricsConfigOverride
        ? { lyrics_config_override: lyricsConfigOverride }
        : {}),
    }),
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(detail.detail || "Failed to create test job");
  }
  return res.json();
}

export async function adminRerenderMusicJob(
  trackId: string,
  sourceJobId: string,
  lyricsConfigOverride?: LyricsConfigOverride,
): Promise<MusicJobResponse> {
  const res = await fetch(`${ADMIN_PROXY}/music-tracks/${trackId}/rerender-job`, {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify({
      source_job_id: sourceJobId,
      ...(lyricsConfigOverride
        ? { lyrics_config_override: lyricsConfigOverride }
        : {}),
    }),
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(detail.detail || "Failed to re-render job");
  }
  return res.json();
}

export async function adminListMusicTestJobs(
  trackId: string,
  limit = 10,
): Promise<AdminMusicTestJobSummary[]> {
  const res = await fetch(
    `${ADMIN_PROXY}/music-tracks/${trackId}/test-jobs?limit=${limit}`,
  );
  if (!res.ok) throw new Error(`Admin list test jobs failed: ${res.status}`);
  const data = await res.json();
  return data.jobs;
}

/** Admin-gated status poll. Use this from admin UIs instead of the public
 *  GET /music-jobs/{id}/status, which has no auth. */
export async function adminGetMusicJobStatus(
  trackId: string,
  jobId: string,
): Promise<MusicJobStatus> {
  const res = await fetch(
    `${ADMIN_PROXY}/music-tracks/${trackId}/jobs/${jobId}/status`,
  );
  if (!res.ok) throw new Error(`Admin job status failed: ${res.status}`);
  return res.json();
}

export async function adminCreateLyricsPreview(
  trackId: string,
  lyricsConfigOverride?: LyricsConfigOverride,
  style: LyricsStyle = "line",
): Promise<LyricsPreviewCreateResponse> {
  // Strip line-only override fields when previewing in a non-Line style.
  // The backend already does this defensively before validation, but
  // dropping them client-side keeps the request body honest and avoids
  // surprising the network panel reviewer with knobs that won't fire.
  const safeOverride: LyricsConfigOverride | undefined =
    style !== "line" ? undefined : lyricsConfigOverride;
  const body: Record<string, unknown> = { style };
  if (safeOverride) {
    body.lyrics_config_override = safeOverride;
  }
  const res = await fetch(`${ADMIN_PROXY}/music-tracks/${trackId}/lyrics-preview`, {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(detail.detail || "Failed to create lyrics preview");
  }
  return res.json();
}

export async function adminGetLyricsPreviewStatus(
  trackId: string,
  jobId: string,
): Promise<LyricsPreviewStatus> {
  const res = await fetch(
    `${ADMIN_PROXY}/music-tracks/${trackId}/lyrics-preview-jobs/${jobId}/status`,
  );
  if (!res.ok) throw new Error(`Lyrics preview status failed: ${res.status}`);
  return res.json();
}
