import { formatTimecode, formatSeconds } from "../time-format";

// Note: formatMSS is tested in lib/format-time.ts — we only test the additions here.

describe("formatTimecode", () => {
  it("formats zero as '0:00'", () => {
    expect(formatTimecode(0)).toBe("0:00");
  });

  it("formats 63 seconds as '1:03'", () => {
    expect(formatTimecode(63)).toBe("1:03");
  });

  it("floors fractional seconds", () => {
    expect(formatTimecode(4.9)).toBe("0:04");
  });

  it("pads single-digit seconds", () => {
    expect(formatTimecode(9)).toBe("0:09");
  });

  it("clamps negative input to 0", () => {
    expect(formatTimecode(-5)).toBe("0:00");
  });
});

describe("formatSeconds", () => {
  it("formats 2.3s", () => {
    expect(formatSeconds(2.3)).toBe("2.3s");
  });

  it("rounds to one decimal", () => {
    expect(formatSeconds(2.35)).toBe("2.4s");
    expect(formatSeconds(2.34)).toBe("2.3s");
  });

  it("formats 0", () => {
    expect(formatSeconds(0)).toBe("0.0s");
  });

  it("formats a whole number with .0 suffix", () => {
    expect(formatSeconds(5)).toBe("5.0s");
  });
});
