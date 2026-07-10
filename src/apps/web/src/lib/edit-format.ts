export type PickerEditFormat = "montage" | "narrated_planned" | "subtitled";

/**
 * Planner-only vocab the item page must normalize AND persist before a render
 * dispatches — the backend renders these as montage fallback while the picker
 * shows a different flow. Backend-native formats (narrated family, subtitled,
 * montage) must NOT be re-persisted from the resolved value: that would
 * destroy sub-modes like narrated_ready or stomp a stored subtitled when the
 * frontend flag is off.
 */
export function needsFormatPersist(editFormat: string | null | undefined): boolean {
  return (
    editFormat === "talking_head" || editFormat === "day_vlog" || editFormat === "single_hero"
  );
}

/**
 * Planner vocab is broader than picker vocab; backend unification is a tracked
 * follow-up.
 */
export function resolvePickerFormat(
  editFormat: string | null | undefined,
  subtitledEnabled: boolean,
): PickerEditFormat {
  switch (editFormat) {
    case "subtitled":
    case "talking_head":
      return subtitledEnabled ? "subtitled" : "montage";
    case "narrated":
    case "narrated_planned":
    case "narrated_ready":
      return "narrated_planned";
    case "montage":
    case "day_vlog":
    case "single_hero":
    default:
      return "montage";
  }
}
