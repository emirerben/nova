export type PickerEditFormat = "montage" | "narrated_planned" | "subtitled" | "talking_head";

/**
 * Planner-only vocab the item page must normalize AND persist before a render
 * dispatches. Backend-native formats (narrated family, subtitled, montage, and
 * talking_head) must NOT be re-persisted from the resolved picker value: that
 * would destroy sub-modes like narrated_ready or collapse a multi-clip
 * talking_head item into the single-clip subtitled path.
 */
export function needsFormatPersist(editFormat: string | null | undefined): boolean {
  return editFormat === "day_vlog" || editFormat === "single_hero";
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
      return subtitledEnabled ? "subtitled" : "montage";
    case "talking_head":
      return "talking_head";
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
