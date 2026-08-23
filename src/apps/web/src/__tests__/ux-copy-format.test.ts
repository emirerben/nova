import { formatElapsed } from "@/components/progress/logic";
import { formatCount, formatDurationWords } from "@/lib/ux-copy-format";

describe("UX copy interpolation", () => {
  it("formats singular and plural counts in US English", () => {
    expect(formatCount(1, "clip")).toBe("1 clip");
    expect(formatCount(0, "clip")).toBe("0 clips");
    expect(formatCount(2, "version", "versions")).toBe("2 versions");
  });

  it("formats elapsed time without fragmented JSX", () => {
    expect(formatElapsed(7_000)).toBe("0:07");
    expect(formatElapsed(65_000)).toBe("1:05");
  });

  it("formats approximate durations as translation-ready words", () => {
    expect(formatDurationWords(30)).toBe("30 seconds");
    expect(formatDurationWords(60)).toBe("1 minute");
    expect(formatDurationWords(90)).toBe("1 minute 30 seconds");
  });
});
