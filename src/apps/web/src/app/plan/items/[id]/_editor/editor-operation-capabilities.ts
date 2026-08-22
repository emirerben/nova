import type { EditorCapabilities } from "@/lib/plan-api";

export type ClipOperation = keyof NonNullable<EditorCapabilities["clips"]>;
export type MusicOperation = keyof NonNullable<EditorCapabilities["music_operations"]>;
export type LaneOperation = keyof NonNullable<EditorCapabilities["lanes"]>;

/**
 * V2 capability objects are authoritative when present. Legacy rows retain
 * their existing booleans, so an old montage keeps the exact old behavior.
 */
export function canEditClip(
  capabilities: EditorCapabilities | null | undefined,
  operation: ClipOperation,
  legacy: boolean,
): boolean {
  const value = capabilities?.clips?.[operation];
  if (typeof value === "boolean") return value;
  if (value && typeof value === "object") return value.editable;
  return legacy;
}

export function canEditMusic(
  capabilities: EditorCapabilities | null | undefined,
  operation: MusicOperation,
  legacy: boolean,
): boolean {
  const value = capabilities?.music_operations?.[operation];
  if (typeof value === "boolean") return value;
  if (value && typeof value === "object") return value.editable;
  return legacy;
}

export function canEditLane(
  capabilities: EditorCapabilities | null | undefined,
  operation: LaneOperation,
  legacy: boolean,
): boolean {
  const value = capabilities?.lanes?.[operation];
  if (typeof value === "boolean") return value;
  if (value && typeof value === "object") return value.editable;
  return legacy;
}

export function operationDisabledReason(
  value:
    | NonNullable<EditorCapabilities["clips"]>[ClipOperation]
    | NonNullable<EditorCapabilities["music_operations"]>[MusicOperation]
    | NonNullable<EditorCapabilities["lanes"]>[LaneOperation]
    | undefined,
): string | null {
  if (!value || typeof value === "boolean" || value.editable) return null;
  return value.reason ?? "This operation isn't available for this story.";
}

export function hasGuidedOperationCapabilities(
  capabilities: EditorCapabilities | null | undefined,
): boolean {
  return (
    !!capabilities?.clips ||
    !!capabilities?.music_operations ||
    !!capabilities?.lanes ||
    !!capabilities?.nova
  );
}
