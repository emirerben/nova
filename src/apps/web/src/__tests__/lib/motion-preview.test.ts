import { creatorBlockPreviewFrame } from "@/lib/motion-preview";

describe("Creator Block catalog preview", () => {
  it("uses a stable representative still when reduced motion is requested", () => {
    expect(creatorBlockPreviewFrame(30, 90, true, 0)).toBe(57);
    expect(creatorBlockPreviewFrame(30, 90, true, 8_000)).toBe(57);
  });

  it("advances visible previews at the shared 15fps sampling cadence", () => {
    expect(creatorBlockPreviewFrame(30, 90, false, 0)).toBe(30);
    expect(creatorBlockPreviewFrame(30, 90, false, 100)).toBe(32);
    expect(creatorBlockPreviewFrame(30, 90, false, 2_000)).toBe(30);
  });
});
