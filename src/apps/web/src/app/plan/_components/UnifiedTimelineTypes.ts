/**
 * Shared types for the UnifiedTimeline lane components.
 *
 * SfxDragState, OverlayDragState, and UploadFile are used across
 * multiple lane components; centralised here so each lane can import
 * only what it needs.
 */

// ── File upload ────────────────────────────────────────────────────────────────

export interface UploadFile {
  file: File;
  filename: string;
  content_type: string;
  file_size_bytes: number;
}

// ── SFX drag ──────────────────────────────────────────────────────────────────

export interface SfxDragState {
  id: string;
  handle: "body" | "left" | "right";
  startClientX: number;
  startAtS: number;
  startEndS: number;
  startTrimStartS: number;
  startTrimEndS: number;
  durationS: number | null;
  previewAtS: number;
  previewEndS: number;
}

// ── Overlay drag ──────────────────────────────────────────────────────────────

export interface OverlayDragState {
  cardId: string;
  handle: "move" | "left" | "right" | "trim-left" | "trim-right";
  startX: number;
  origStart: number;
  origEnd: number;
  origTrimStart: number;
  origTrimEnd: number;
  containerWidth: number;
  scaleDuration: number;
  clipDurationS: number | null;
}
