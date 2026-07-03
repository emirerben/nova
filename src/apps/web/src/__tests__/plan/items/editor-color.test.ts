import { describe, expect, it } from "@jest/globals";
import { normalizeEditableHex } from "@/app/plan/items/[id]/_editor/editor-color";

describe("normalizeEditableHex", () => {
  it("normalizes full hex values", () => {
    expect(normalizeEditableHex("#1a2B3c")).toBe("#1A2B3C");
    expect(normalizeEditableHex("1a2B3c")).toBe("#1A2B3C");
  });

  it("expands shorthand hex values", () => {
    expect(normalizeEditableHex("#abc")).toBe("#AABBCC");
    expect(normalizeEditableHex("0f9")).toBe("#00FF99");
  });

  it("keeps invalid drafts invalid", () => {
    expect(normalizeEditableHex("#12")).toBeNull();
    expect(normalizeEditableHex("#1234")).toBeNull();
    expect(normalizeEditableHex("#ggg")).toBeNull();
  });
});
