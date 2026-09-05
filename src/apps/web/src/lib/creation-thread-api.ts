import { uploadContentTypeForFile, uploadToGcs } from "@/lib/plan-api";

const BASE = "/api/plan/creation-threads";

export type CreationFormat = "montage" | "narrated_planned" | "subtitled";
export type CreationAction =
  | "select_format"
  | "select_edit_format"
  | "send_message"
  | "generate"
  | "revise"
  | "retry"
  | "remove_media"
  | "select_variant"
  | "archive";

export interface CreationThreadEvent {
  id: string;
  sequence: number;
  revision: number;
  role: "user" | "assistant" | "system";
  event_type: string;
  content: string | null;
  payload: Record<string, unknown> | null;
  created_at: string;
}

export interface CreationVariant {
  variant_id?: string;
  render_status?: string;
  output_url?: string | null;
  poster_url?: string | null;
  failure_reason?: string | null;
  [key: string]: unknown;
}

export interface CreationJob {
  id: string;
  status: string;
  current_phase?: string | null;
  failure_reason?: string | null;
  variants: CreationVariant[];
}

export interface CreationThread {
  id: string;
  /** User-authored project label. Older rows may omit it while they hydrate. */
  title?: string | null;
  status: "active" | "archived" | "failed";
  revision: number;
  state: Record<string, unknown>;
  content_plan_id: string | null;
  active_plan_item_id: string | null;
  active_creator_agent_session_id: string | null;
  active_job_id: string | null;
  creator_agent?: {
    status?: string | null;
    revision?: number;
    summary?: string | null;
    plan_hash?: string | null;
    version?: string | null;
  } | null;
  media_capabilities?: CreationMediaCapabilities | null;
  events: CreationThreadEvent[];
  job: CreationJob | null;
  created_at: string;
  updated_at: string;
}

export interface CreationUploadTarget {
  media_id: string;
  upload_url: string;
  /** Legacy upload targets exposed a path; new targets intentionally keep it opaque. */
  gcs_path?: string;
  content_type: string;
  upload_headers?: Record<string, string>;
}

export interface CreationCapability {
  id: string;
  edit_format: CreationFormat;
  /** The PlanItem clip contract. Older APIs omit this and the canonical
   *  50-clip PlanItem ceiling is used until capabilities are refreshed. */
  max_clips?: number;
  clip_limit?: number;
  limits?: {
    max_clips?: number;
    clips?: number;
    max_visuals?: number;
    visuals?: number;
  };
}

export interface CreationMediaCapabilities {
  clips?: {
    current?: number;
    max?: number;
    server_max?: number;
    max_file_bytes?: number;
    content_types?: string[];
    format?: CreationFormat;
  };
  visuals?: {
    current?: number;
    max?: number;
    max_file_bytes?: { image?: number; video?: number };
    content_types?: string[];
  };
  voiceover?: {
    current?: number;
    max?: number;
    max_file_bytes?: number;
    content_types?: string[];
  };
}

export interface CreationCapabilitiesResponse {
  formats: CreationCapability[];
  media?: CreationMediaCapabilities;
}

export class CreationThreadError extends Error {
  readonly status: number;
  constructor(message: string, status: number) {
    super(message);
    this.name = "CreationThreadError";
    this.status = status;
  }
}

function id(prefix: string): string {
  return globalThis.crypto?.randomUUID?.() ?? `${prefix}-${Date.now()}-${Math.random()}`;
}

interface UploadReservation {
  target: CreationUploadTarget;
  uploaded: boolean;
  clientEventId: string;
}

const uploadReservations = new WeakMap<File, UploadReservation>();

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${BASE}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
  });
  if (!response.ok) {
    let message = `Request failed (${response.status})`;
    try {
      const body = (await response.json()) as { detail?: string };
      if (body.detail) message = body.detail;
    } catch {
      // Keep the HTTP status when a proxy returns a non-JSON response.
    }
    throw new CreationThreadError(message, response.status);
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

export function creationFormat(value: unknown): CreationFormat | null {
  return value === "montage" || value === "narrated_planned" || value === "subtitled"
    ? value
    : null;
}

export function creationFormatLabel(value: CreationFormat | null): string {
  if (value === "narrated_planned") return "Narrated";
  if (value === "subtitled") return "Talking to camera";
  return "Montage";
}

/** PlanItem's shared upload ceiling. Kept here as a compatibility default for
 * deploy skew; the server capability response wins whenever it provides one. */
export const PLAN_ITEM_CLIP_LIMIT = 50;

export function creationClipLimit(
  capabilities: CreationCapability[],
  format: CreationFormat | null,
): number {
  if (!format) return PLAN_ITEM_CLIP_LIMIT;
  const capability = capabilities.find((item) => item.edit_format === format);
  const candidate = capability?.max_clips
    ?? capability?.clip_limit
    ?? capability?.limits?.max_clips
    ?? capability?.limits?.clips;
  return typeof candidate === "number" && Number.isFinite(candidate) && candidate > 0
    ? Math.floor(candidate)
    : PLAN_ITEM_CLIP_LIMIT;
}

export function creationThreadMediaCount(thread: CreationThread | null): number {
  const persistedCount = thread?.state.media_count;
  if (typeof persistedCount === "number" && Number.isFinite(persistedCount)) return Math.max(0, persistedCount);
  const media = thread?.state.media;
  return Array.isArray(media) ? media.length : 0;
}

export function creationJobReady(thread: CreationThread): boolean {
  return Boolean(thread.job && thread.job.variants.some((v) => v.render_status === "ready" && v.output_url));
}

export function creationJobFailed(thread: CreationThread): boolean {
  return Boolean(thread.job && [
    "failed", "processing_failed", "variants_failed", "matching_failed",
    "no_labeled_tracks", "posting_failed", "cancelled", "error",
  ].includes(thread.job.status));
}

export function creationJobPartial(thread: CreationThread): boolean {
  const variants = thread.job?.variants ?? [];
  return variants.some((variant) => variant.render_status === "ready" && variant.output_url) &&
    variants.some((variant) => ["failed", "error", "render_failed"].includes(String(variant.render_status)));
}

export function creationJobSettled(thread: CreationThread): boolean {
  if (!thread.job) return false;
  if (thread.job.variants.some((variant) => variant.render_status === "rendering")) return false;
  if (creationJobFailed(thread)) return true;
  if (["done", "variants_ready", "variants_ready_partial"].includes(thread.job.status)) return true;
  const variants = thread.job.variants;
  return variants.length > 0 && variants.every((variant) =>
    ["ready", "failed", "error", "render_failed"].includes(String(variant.render_status)),
  );
}

export async function listCreationThreads(): Promise<CreationThread[]> {
  const result = await request<CreationThread[] | { threads: CreationThread[] }>("");
  return Array.isArray(result) ? result : result.threads;
}

export async function getCreationCapabilities(): Promise<CreationCapabilitiesResponse> {
  const result = await request<{
    formats: CreationCapability[];
    media?: CreationMediaCapabilities;
  }>("/capabilities");
  const serverClipLimit = result.media?.clips?.max;
  return {
    media: result.media,
    formats: result.formats
      .filter((item) => creationFormat(item.edit_format))
      .map((item) => ({
        ...item,
        // Current API exposes the shared PlanItem contract under media.clips;
        // preserve per-format fields too for future capability responses.
        max_clips: item.max_clips ?? item.clip_limit ?? serverClipLimit,
      })),
  };
}

export function createCreationThread(message?: string): Promise<CreationThread> {
  return request<CreationThread>("", {
    method: "POST",
    body: JSON.stringify({
      client_event_id: id("thread"),
      ...(message ? { message } : {}),
    }),
  });
}

export function refreshCreationThread(threadId: string): Promise<CreationThread> {
  return request<CreationThread>(`/${threadId}`);
}

export function sendCreationMessage(thread: CreationThread, message: string): Promise<CreationThread> {
  return request<CreationThread>(`/${thread.id}/messages`, {
    method: "POST",
    body: JSON.stringify({
      message,
      client_event_id: id("message"),
      expected_revision: thread.revision,
    }),
  });
}

export function applyCreationAction(
  thread: CreationThread,
  action: CreationAction,
  payload: Record<string, unknown> = {},
  clientActionId?: string,
): Promise<CreationThread> {
  return request<CreationThread>(`/${thread.id}/actions`, {
    method: "POST",
    body: JSON.stringify({
      action,
      payload,
      client_action_id: clientActionId ?? id("action"),
      expected_revision: thread.revision,
    }),
  });
}

export async function uploadCreationMedia(
  thread: CreationThread,
  files: File[],
  onAttached?: (thread: CreationThread, file: File) => void,
): Promise<CreationThread> {
  let current = thread;
  for (const file of files) {
    let reservation = uploadReservations.get(file);
    if (!reservation) {
      const reserved = await request<CreationUploadTarget[] | { targets: CreationUploadTarget[] }>(
        `/${thread.id}/upload-urls`,
        {
          method: "POST",
          body: JSON.stringify({
            files: [{
              filename: file.name,
              content_type: uploadContentTypeForFile(file),
              file_size_bytes: file.size,
              client_upload_id: id("upload"),
            }],
          }),
        },
      );
      const targets = Array.isArray(reserved) ? reserved : reserved.targets;
      const target = targets[0];
      if (!target) throw new Error("Upload reservation was not returned.");
      reservation = { target, uploaded: false, clientEventId: id("media") };
      uploadReservations.set(file, reservation);
    }
    if (!reservation.uploaded) {
      try {
        await uploadToGcs(
          reservation.target.upload_url,
          file,
          reservation.target.upload_headers ?? {},
        );
        reservation.uploaded = true;
      } catch (cause) {
        // A failed PUT wrote no authoritative media. Mint a fresh signed target
        // on retry in case this one expired.
        uploadReservations.delete(file);
        throw cause;
      }
    }
    current = await request<CreationThread>(`/${thread.id}/media`, {
      method: "POST",
      body: JSON.stringify({
        media: [{
          media_id: reservation.target.media_id,
          ...(reservation.target.gcs_path ? { gcs_path: reservation.target.gcs_path } : {}),
          filename: file.name,
          content_type: reservation.target.content_type,
          kind: file.type.startsWith("image/")
            ? "image"
            : file.type.startsWith("audio/") ? "audio" : "video",
        }],
        client_event_id: reservation.clientEventId,
        expected_revision: current.revision,
      }),
    });
    uploadReservations.delete(file);
    onAttached?.(current, file);
  }
  return current;
}

export async function archiveCreationThread(thread: CreationThread): Promise<CreationThread> {
  return request<CreationThread>(`/${thread.id}/archive`, {
    method: "POST",
    body: JSON.stringify({
      expected_revision: thread.revision,
      client_event_id: id("archive"),
    }),
  });
}

/** Persist the short label shown in the project rail and workspace header. */
export function renameCreationThread(
  thread: CreationThread,
  name: string,
): Promise<CreationThread> {
  return request<CreationThread>(`/${thread.id}`, {
    method: "PATCH",
    body: JSON.stringify({
      title: name.trim(),
      expected_revision: thread.revision,
      client_event_id: id("rename"),
    }),
  });
}

/** Remove a project from the creator's project list. */
export function deleteCreationThread(thread: CreationThread): Promise<void> {
  const query = new URLSearchParams({ expected_revision: String(thread.revision) });
  return request<void>(`/${thread.id}?${query.toString()}`, {
    method: "DELETE",
  });
}

/** Render append-only server events as conversation rows. */
export function threadMessages(thread: CreationThread): Array<{
  id: string;
  role: "user" | "assistant";
  content: string;
  eventType: string;
  artifact?: "format" | "upload" | "voiceover" | "confirmation" | "revision" | "progress" | "result" | "failure";
}> {
  // The first strategy is the initial creation direction. A strategy becomes
  // a revision proposal only after durable evidence that the Creator Agent
  // confirmed/started a render; otherwise a ready first cut would replay that
  // initial direction as a bogus "Revision ready" card after refresh. The
  // action events cover newer projections, while the agent events preserve
  // recovered threads whose action projection was not appended.
  const events = [...thread.events].sort((left, right) => left.sequence - right.sequence);
  const latestGenerationSequence = events.reduce(
    (latest, event) => [
      "action_generate",
      "action_confirm_generation",
      "agent_user_confirmation",
      "agent_assistant_execution",
    ].includes(event.event_type)
      ? Math.max(latest, event.sequence)
      : latest,
    -1,
  );
  const latestStrategySequence = events.reduce(
    (latest, event) => event.event_type === "agent_assistant_strategy"
      ? Math.max(latest, event.sequence)
      : latest,
    -1,
  );
  return events.flatMap((event) => {
    const payload = event.payload ?? {};
    const kind = String(payload.kind ?? event.event_type);
    const content = event.content?.trim();
    if (!content && event.role === "user" && event.event_type !== "media_added") return [];
    let artifact: "format" | "upload" | "voiceover" | "confirmation" | "revision" | "progress" | "result" | "failure" | undefined;
    if (["select_format", "select_edit_format", "format_options"].includes(kind)) artifact = "format";
    else if (["collect_media", "upload_prompt"].includes(kind) || event.event_type === "media_added") artifact = "upload";
    else if (["collect_voiceover", "voiceover_prompt"].includes(kind)) artifact = "voiceover";
    else if (["confirm_generation", "confirmation"].includes(kind)) artifact = "confirmation";
    else if (["confirm_revision", "revision"].includes(kind)) artifact = "revision";
    else if (event.event_type === "agent_assistant_strategy") {
      // Strategies are durable agent history, not a queue of confirmation
      // buttons. Keep only the newest strategy actionable; older attempts
      // remain transcript content after a failed/retried render.
      if (event.sequence === latestStrategySequence) {
        artifact = latestGenerationSequence >= 0 && event.sequence > latestGenerationSequence
          ? "revision"
          : "confirmation";
      }
    }
    else if (["generation_started", "rendering"].includes(event.event_type)
      || (event.event_type === "agent_assistant_execution" && ["started", "rendering", "queued"].includes(String(payload.status)))) artifact = "progress";
    else if (["generation_failed", "render_failed"].includes(event.event_type)
      || (event.event_type === "agent_assistant_execution" && ["failed", "error"].includes(String(payload.status)))) artifact = "failure";
    else if (event.event_type === "generation_ready"
      || (event.event_type === "agent_assistant_execution" && ["ready", "completed"].includes(String(payload.status)))) artifact = "result";
    if (!content && !artifact) return [];
    return [{
      id: event.id,
      role: event.role === "user" ? "user" : "assistant",
      content: content ?? "",
      eventType: event.event_type,
      ...(artifact ? { artifact } : {}),
    }];
  });
}
