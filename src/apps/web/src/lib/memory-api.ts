/** Typed client for the owner-scoped creator Personalization surface. */

const MEMORY_BASE = "/api/me/memory";
export const CREATOR_MEMORY_ENABLED = process.env.NEXT_PUBLIC_CREATOR_MEMORY_ENABLED === "true";

export type MemoryState = "active" | "suggested" | "dismissed" | "superseded" | "forgotten";
export type MemoryEnforcement = "constraint" | "default" | "advisory";
export type MemoryStatus = "enforced" | "advisory" | "unsupported" | "conflicted";

export interface CreatorMemoryItem {
  id: string;
  section: "content" | "video_style" | "stories_pacing" | "avoid" | "other";
  normalized_key?: string | null;
  instruction: string;
  display_text?: string | null;
  enforcement: MemoryEnforcement;
  enforcement_status?: MemoryStatus;
  scope_label?: string | null;
  source_label?: string | null;
  source_available?: boolean;
  source_thread_id?: string | null;
  source_deleted?: boolean;
  state: MemoryState;
  user_locked: boolean;
  updated_at?: string | null;
  conflict?: { message: string; choices?: string[] } | null;
}

export interface MemorySection {
  key: CreatorMemoryItem["section"];
  title: string;
  items: CreatorMemoryItem[];
}

export interface CreatorMemoryResponse {
  enabled: boolean;
  revision: number;
  sections?: MemorySection[];
  items?: CreatorMemoryItem[];
  suggestions: CreatorMemoryItem[];
  recent_undo?: {
    operation_id: string;
    item_id?: string | null;
    revision: number;
    undo_expires_at?: string | null;
  } | null;
  compatibility?: {
    summary?: string | null;
    about_your_videos?: string | null;
    tone?: string | null;
    audience?: string | null;
    content_pillars?: string[];
    posting_cadence?: string | null;
    goal?: string | null;
  };
}

export interface MemoryMutationResponse {
  memory?: CreatorMemoryResponse;
  item?: CreatorMemoryItem;
  undo_token?: string | null;
  revision?: number;
  message?: string;
  operation_id?: string;
  undo_expires_at?: string | null;
}

export class MemoryApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly retryable: boolean;

  constructor(message: string, status: number, code = "request_failed", retryable = status >= 500) {
    super(message);
    this.name = "MemoryApiError";
    this.status = status;
    this.code = code;
    this.retryable = retryable;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${MEMORY_BASE}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
  });
  if (response.ok) {
    if (response.status === 204) return undefined as T;
    return (await response.json()) as T;
  }

  let message = "Kria couldn't update Personalization. Try again.";
  let code = "request_failed";
  try {
    const body = (await response.json()) as { detail?: string | { message?: string; code?: string }; code?: string };
    const detail = typeof body.detail === "string" ? body.detail : body.detail?.message;
    message = detail || message;
    code = typeof body.detail === "object" ? body.detail.code ?? body.code ?? code : body.code ?? code;
  } catch {
    // Keep the safe fallback for non-JSON proxy responses.
  }
  throw new MemoryApiError(message, response.status, code);
}

export function getCreatorMemory(): Promise<CreatorMemoryResponse> {
  return request<{
    enabled: boolean;
    revision: number;
    active?: Array<Record<string, unknown>>;
    suggestions: Array<Record<string, unknown>>;
    recent_undo?: CreatorMemoryResponse["recent_undo"];
    compatibility?: CreatorMemoryResponse["compatibility"];
  }>("").then((raw) => {
    const normalize = (item: Record<string, unknown>): CreatorMemoryItem => ({
      id: String(item.id),
      section: ["content", "video_style", "stories_pacing", "avoid", "other"].includes(String(item.category))
        ? (String(item.category) as CreatorMemoryItem["section"])
        : "other",
      normalized_key: typeof item.normalized_key === "string" ? item.normalized_key : null,
      instruction: String(item.instruction ?? ""),
      display_text: typeof item.display_text === "string" ? item.display_text : null,
      enforcement: (item.enforcement as MemoryEnforcement) ?? "advisory",
      enforcement_status: item.enforcement_status as MemoryStatus | undefined,
      scope_label: typeof item.scope_label === "string" ? item.scope_label : "Applies to all future videos",
      source_label: typeof item.source_label === "string" ? item.source_label : null,
      source_available: Boolean(item.source_thread_id),
      source_thread_id: typeof item.source_thread_id === "string" ? item.source_thread_id : null,
      source_deleted: Boolean(item.source_deleted),
      state: (item.state as MemoryState) ?? "active",
      user_locked: Boolean(item.user_locked),
      updated_at: typeof item.updated_at === "string" ? item.updated_at : null,
      conflict: item.conflict as CreatorMemoryItem["conflict"],
    });
    return {
      enabled: raw.enabled,
      revision: raw.revision,
      items: (raw.active ?? []).map(normalize),
      suggestions: raw.suggestions.map(normalize),
      recent_undo: raw.recent_undo,
      compatibility: raw.compatibility,
    };
  });
}

export function toggleCreatorMemory(enabled: boolean, expected_revision: number): Promise<MemoryMutationResponse> {
  return request<MemoryMutationResponse>("", {
    method: "PATCH",
    body: JSON.stringify({ enabled, expected_revision, idempotency_key: idempotencyKey() }),
  });
}

export function createCreatorMemoryItem(body: {
  instruction: string;
  expected_revision: number;
  category?: string;
  enforcement?: MemoryEnforcement;
  compatibility_key?: "summary" | "goal" | "audience" | "pillars" | "cadence" | "tone";
}): Promise<MemoryMutationResponse> {
  return request<MemoryMutationResponse>("/items", { method: "POST", body: JSON.stringify({
    ...body,
    category: body.category ?? "other",
    enforcement: body.enforcement ?? "default",
    idempotency_key: idempotencyKey(),
  }) });
}

export function updateCreatorMemoryItem(
  id: string,
  body: { instruction?: string; expected_revision: number; category?: string; enforcement?: MemoryEnforcement; normalized_key?: string | null },
): Promise<MemoryMutationResponse> {
  return request<MemoryMutationResponse>(`/items/${encodeURIComponent(id)}`, {
    method: "PATCH",
    body: JSON.stringify({
      ...body,
      category: body.category ?? "other",
      enforcement: body.enforcement ?? "advisory",
      idempotency_key: idempotencyKey(),
    }),
  });
}

export function forgetCreatorMemoryItem(id: string, expected_revision: number): Promise<MemoryMutationResponse> {
  return request<MemoryMutationResponse>(`/items/${encodeURIComponent(id)}`, {
    method: "DELETE",
    body: JSON.stringify({ expected_revision, idempotency_key: idempotencyKey() }),
  });
}

export function clearCreatorMemory(expected_revision: number): Promise<MemoryMutationResponse> {
  return request<MemoryMutationResponse>("/clear", {
    method: "POST",
    body: JSON.stringify({ expected_revision, idempotency_key: idempotencyKey() }),
  });
}

export function restoreCreatorMemoryItem(id: string, expected_revision: number): Promise<MemoryMutationResponse> {
  return request<MemoryMutationResponse>(`/items/${encodeURIComponent(id)}/restore`, {
    method: "POST",
    body: JSON.stringify({ expected_revision, idempotency_key: idempotencyKey() }),
  });
}

export function acceptCreatorMemorySuggestion(id: string, expected_revision: number): Promise<MemoryMutationResponse> {
  return request<MemoryMutationResponse>(`/items/${encodeURIComponent(id)}/accept`, {
    method: "POST",
    body: JSON.stringify({ expected_revision, idempotency_key: idempotencyKey() }),
  });
}

export function dismissCreatorMemorySuggestion(id: string, expected_revision: number): Promise<MemoryMutationResponse> {
  return request<MemoryMutationResponse>(`/items/${encodeURIComponent(id)}/suggestion`, {
    method: "DELETE",
    body: JSON.stringify({ expected_revision, idempotency_key: idempotencyKey() }),
  });
}

export function undoCreatorMemoryOperation(operationId: string, expected_revision: number): Promise<MemoryMutationResponse> {
  return request<MemoryMutationResponse>(`/operations/${encodeURIComponent(operationId)}/undo`, {
    method: "POST",
    body: JSON.stringify({ expected_revision, idempotency_key: idempotencyKey() }),
  });
}

function idempotencyKey(): string {
  return globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random()}`;
}
