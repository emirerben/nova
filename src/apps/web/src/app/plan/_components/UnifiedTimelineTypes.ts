/**
 * Shared types for the UnifiedTimeline lane components.
 *
 * SfxDragState, OverlayDragState, and UploadFile are used across
 * multiple lane components; centralised here so each lane can import
 * only what it needs.
 */

import type { MediaOverlay, SoundEffectPlacement } from "@/lib/plan-api";

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
  /** Set when the dragged card is an AI suggestion (006 T3): patches route to
   *  onSuggestionEdit(suggestionId, …) instead of onUpdateCard — suggestions
   *  never mutate the manual media_overlays state. */
  suggestionId?: string | null;
}

// ── Overlay suggestions in the lanes (plans/006 T3, 005-4A) ───────────────────

/**
 * One pending AI overlay suggestion rendered in the timeline lanes with
 * provenance styling (dashed lime-600 + ✦). Derived from the rail's working
 * `OverlaySuggestion` envelopes; `id` is the ENVELOPE id (the rail row
 * identity), NOT the embedded overlay's id.
 */
export interface SuggestionLaneEntry {
  id: string;
  overlay: MediaOverlay;
  sfx: SoundEffectPlacement | null;
  /** True once the row is ✓-kept or lane-edited — drives the dashed→solid
   *  border + ✦ fade accept transition (005-6A). */
  staged: boolean;
}
