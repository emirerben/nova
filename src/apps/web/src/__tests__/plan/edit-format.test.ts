import {
  needsFormatPersist,
  resolvePickerFormat,
  type PickerEditFormat,
} from "@/lib/edit-format";

type Case = {
  input: string | null | undefined;
  enabled: PickerEditFormat;
  disabled: PickerEditFormat;
};

const cases: Case[] = [
  { input: "subtitled", enabled: "subtitled", disabled: "montage" },
  { input: "talking_head", enabled: "subtitled", disabled: "montage" },
  { input: "narrated", enabled: "narrated_planned", disabled: "narrated_planned" },
  { input: "narrated_planned", enabled: "narrated_planned", disabled: "narrated_planned" },
  { input: "narrated_ready", enabled: "narrated_planned", disabled: "narrated_planned" },
  { input: "montage", enabled: "montage", disabled: "montage" },
  { input: "day_vlog", enabled: "montage", disabled: "montage" },
  { input: "single_hero", enabled: "montage", disabled: "montage" },
  { input: null, enabled: "montage", disabled: "montage" },
  { input: undefined, enabled: "montage", disabled: "montage" },
  { input: "unknown", enabled: "montage", disabled: "montage" },
];

describe("resolvePickerFormat", () => {
  it.each(cases)("%p with subtitled enabled", ({ input, enabled }) => {
    expect(resolvePickerFormat(input, true)).toBe(enabled);
  });

  it.each(cases)("%p with subtitled disabled", ({ input, disabled }) => {
    expect(resolvePickerFormat(input, false)).toBe(disabled);
  });
});

describe("needsFormatPersist", () => {
  it.each(["talking_head", "day_vlog", "single_hero"])(
    "persists planner-only vocab %p before generate",
    (format) => {
      expect(needsFormatPersist(format)).toBe(true);
    },
  );

  // Backend-native formats must never be re-persisted from the resolved value:
  // that would destroy the narrated_ready sub-mode or stomp a stored subtitled
  // item when the frontend flag is off.
  it.each(["montage", "narrated", "narrated_planned", "narrated_ready", "subtitled", null, undefined, "unknown"])(
    "does not persist backend-native/unknown %p",
    (format) => {
      expect(needsFormatPersist(format as string | null | undefined)).toBe(false);
    },
  );
});
