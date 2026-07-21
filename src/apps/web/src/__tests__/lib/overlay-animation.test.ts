import {
  animationStateAt,
  easeOutCubic,
  normalizeAnimatedRevealText,
  popInScaleAt,
  sequenceFadeOutAlphaAt,
  sequenceOverlayFadeOutAlphaAt,
  segmentGraphemes,
  staggeredSliceSettleS,
  staggeredSlicePreviewVisibleAt,
  staggeredSliceStateAt,
} from "@/lib/overlay-animation";

describe("easeOutCubic", () => {
  it("t=0 → 0", () => {
    expect(easeOutCubic(0)).toBeCloseTo(0);
  });

  it("t=1 → 1", () => {
    expect(easeOutCubic(1)).toBeCloseTo(1);
  });

  it("t=0.5 → 0.875", () => {
    // 1 - (1 - 0.5)^3 = 1 - 0.125 = 0.875
    expect(easeOutCubic(0.5)).toBeCloseTo(0.875);
  });

  it("t < 0 clamps to 0", () => {
    expect(easeOutCubic(-1)).toBeCloseTo(0);
  });

  it("t > 1 clamps to 1", () => {
    expect(easeOutCubic(2)).toBeCloseTo(1);
  });
});

describe("sequenceFadeOutAlphaAt", () => {
  it("stays opaque when the renderer emitted no fade tail", () => {
    expect(sequenceFadeOutAlphaAt(5.9, 6, null)).toBe(1);
  });

  it("matches the Skia quadratic fade over the final window", () => {
    expect(sequenceFadeOutAlphaAt(5.49, 6, 500)).toBe(1);
    expect(sequenceFadeOutAlphaAt(5.75, 6, 500)).toBeCloseTo(0.75);
    expect(sequenceFadeOutAlphaAt(6, 6, 500)).toBe(0);
  });

  it("clamps a fade longer than the whole block", () => {
    expect(sequenceFadeOutAlphaAt(0.5, 1, 10_000)).toBeCloseTo(0.75);
  });
});

describe("sequenceOverlayFadeOutAlphaAt", () => {
  it("matches the renderer role/effect matrix", () => {
    expect(sequenceOverlayFadeOutAlphaAt("generative_sequence", "static", 5.75, 6, 500)).toBeCloseTo(
      0.75,
    );
    expect(sequenceOverlayFadeOutAlphaAt("lyric_line", "static", 5.75, 6, 500)).toBe(1);
    expect(sequenceOverlayFadeOutAlphaAt("generative_intro", "static", 5.75, 6, 500)).toBe(1);
    expect(sequenceOverlayFadeOutAlphaAt("generative_sequence", "typewriter", 5.75, 6, 500)).toBe(
      1,
    );
  });
});

const DUR = 5.0;
const TEXT = "Hello World";

describe("animationStateAt — identity effects", () => {
  const identityEffects = ["none", "static", "unknown-effect", "karaoke-line", "lyric-line"];
  for (const effect of identityEffects) {
    it(`${effect} → identity state`, () => {
      const state = animationStateAt(effect, 1.0, DUR, TEXT);
      expect(state.scale).toBeCloseTo(1.0);
      expect(state.alpha).toBeCloseTo(1.0);
      expect(state.yTranslate).toBeCloseTo(0.0);
      expect(state.visibleText).toBe(TEXT);
    });
  }
});

describe("animationStateAt — fade-in", () => {
  // window = min(dur=5, 0.4) = 0.4
  it("t=0 → alpha≈0", () => {
    const s = animationStateAt("fade-in", 0, DUR, TEXT);
    expect(s.alpha).toBeCloseTo(easeOutCubic(0));
    expect(s.scale).toBeCloseTo(1.0);
    expect(s.yTranslate).toBeCloseTo(0.0);
    expect(s.visibleText).toBe(TEXT);
  });

  it("t=0.4 → alpha≈1 (saturated)", () => {
    const s = animationStateAt("fade-in", 0.4, DUR, TEXT);
    expect(s.alpha).toBeCloseTo(1.0);
  });

  it("t=0.2 → alpha = easeOutCubic(0.5) ≈ 0.875", () => {
    // progress = 0.2 / 0.4 = 0.5
    const s = animationStateAt("fade-in", 0.2, DUR, TEXT);
    expect(s.alpha).toBeCloseTo(0.875);
  });

  it("settled (t=1) → alpha=1, scale=1, yTranslate=0", () => {
    const s = animationStateAt("fade-in", 1.0, DUR, TEXT);
    expect(s.alpha).toBeCloseTo(1.0);
    expect(s.scale).toBeCloseTo(1.0);
    expect(s.yTranslate).toBeCloseTo(0.0);
  });
});

describe("animationStateAt — scale-up", () => {
  // window = min(dur=5, 0.6) = 0.6
  it("t=0 → scale≈0.6", () => {
    const s = animationStateAt("scale-up", 0, DUR, TEXT);
    expect(s.scale).toBeCloseTo(0.6 + 0.4 * easeOutCubic(0));
  });

  it("t=0.6 → scale≈1.0 (saturated)", () => {
    const s = animationStateAt("scale-up", 0.6, DUR, TEXT);
    expect(s.scale).toBeCloseTo(1.0);
  });

  it("t=0.3 → scale = 0.6 + 0.4 * easeOutCubic(0.5) ≈ 0.95", () => {
    // progress = 0.3 / 0.6 = 0.5; easeOutCubic(0.5)=0.875
    const s = animationStateAt("scale-up", 0.3, DUR, TEXT);
    expect(s.scale).toBeCloseTo(0.6 + 0.4 * 0.875);
  });

  it("settled visibleText equals full text", () => {
    const s = animationStateAt("scale-up", 1.0, DUR, TEXT);
    expect(s.visibleText).toBe(TEXT);
  });
});

describe("animationStateAt — typewriter", () => {
  // CHARS_PER_S = 12
  it('t=0 → "H" (floor(0*12)+1=1 char)', () => {
    const s = animationStateAt("typewriter", 0, DUR, TEXT);
    expect(s.visibleText).toBe("H");
  });

  it('t=4/12 → "Hello" (floor((4/12)*12)+1=5 chars)', () => {
    const s = animationStateAt("typewriter", 4 / 12, DUR, TEXT);
    // floor(4/12 * 12) + 1 = floor(4) + 1 = 5
    expect(s.visibleText).toBe("Hello");
  });

  it("t=10 → full text", () => {
    const s = animationStateAt("typewriter", 10, DUR, TEXT);
    expect(s.visibleText).toBe(TEXT);
  });

  it("scale and alpha are identity", () => {
    const s = animationStateAt("typewriter", 0.5, DUR, TEXT);
    expect(s.scale).toBeCloseTo(1.0);
    expect(s.alpha).toBeCloseTo(1.0);
    expect(s.yTranslate).toBeCloseTo(0.0);
  });
});

describe("animationStateAt — stream-in", () => {
  const text4 = "a b c d";
  // WORDS_PER_S = 6

  it('t=0 → "a" (n=max(1,floor(0)+1)=1 word)', () => {
    const s = animationStateAt("stream-in", 0, DUR, text4);
    expect(s.visibleText).toBe("a");
    expect(s.showCursor).toBe(true);
  });

  it("t=1/6 → n=2 words", () => {
    // floor((1/6) * 6) + 1 = floor(1) + 1 = 2
    const s = animationStateAt("stream-in", 1 / 6, DUR, text4);
    // n=2 < 4. floor((1/6)*2)=0 → even → cursor
    expect(s.visibleText).toMatch(/^a b/);
    expect(s.visibleText.startsWith("a b")).toBe(true);
    expect(s.showCursor).toBe(true);
  });

  it("normalizes whitespace like Skia while revealing complete words", () => {
    const s = animationStateAt("stream-in", 1 / 6, DUR, "a  b\nc");
    expect(s.visibleText).toBe("a b");
  });

  it("hides the cursor once all words are visible", () => {
    const s = animationStateAt("stream-in", 10, DUR, text4);
    expect(s.visibleText).toBe(text4);
    expect(s.showCursor).toBe(false);
  });

  it("scale and alpha are identity", () => {
    const s = animationStateAt("stream-in", 0.5, DUR, text4);
    expect(s.scale).toBeCloseTo(1.0);
    expect(s.alpha).toBeCloseTo(1.0);
    expect(s.yTranslate).toBeCloseTo(0.0);
  });
});

describe("normalizeAnimatedRevealText", () => {
  it("collapses intra-line whitespace while preserving explicit line breaks", () => {
    expect(normalizeAnimatedRevealText("  ALPHA   BETA\n GAMMA\tDELTA ")).toBe(
      "ALPHA BETA\nGAMMA DELTA",
    );
  });
});

describe("staggeredSliceStateAt", () => {
  const text = "GOAL OF THE\nTOURNAMENT";

  it("builds the first word, pauses, then reveals the remainder", () => {
    const paused = staggeredSliceStateAt(text, 0.7, 4);
    const glyphs = paused.lines[0].glyphs;
    expect(glyphs.slice(0, 4).every((glyph) => glyph.opacity === 1)).toBe(true);
    expect(glyphs.slice(4).every((glyph) => glyph.opacity === 0)).toBe(true);

    const revealing = staggeredSliceStateAt(text, 1.05, 4);
    expect(revealing.lines[0].glyphs.slice(4).some((glyph) => glyph.opacity > 0)).toBe(true);
  });

  it("builds subsequent lines with the same glyph motion as the first line", () => {
    const before = staggeredSliceStateAt(text, 1.49, 4).lines[1].glyphs;
    expect(before.every((glyph) => glyph.opacity === 0)).toBe(true);

    const active = staggeredSliceStateAt(text, 1.6, 4).lines[1].glyphs;
    expect(active.some((glyph) => glyph.opacity > 0)).toBe(true);
    expect(active.some((glyph) => glyph.opacity < 1)).toBe(true);
    expect(active.some((glyph) => glyph.translateYEm > 0)).toBe(true);
    expect(active.some((glyph) => glyph.rotateDeg < 0)).toBe(true);
    expect(active.some((glyph) => glyph.rotateDeg > 0)).toBe(true);

    const settled = staggeredSliceStateAt(text, 1.85, 4);
    expect(settled.settled).toBe(true);
    expect(settled.lines[1].glyphs.every((glyph) => glyph.opacity === 1)).toBe(true);
  });

  it("compresses the full choreography into short overlay windows", () => {
    expect(staggeredSliceStateAt(text, 0.99, 1).settled).toBe(false);
    expect(staggeredSliceStateAt(text, 1, 1).settled).toBe(true);
  });

  it("compresses many explicit lines into the 2.4s cap without snapping the last line", () => {
    const manyLines = Array.from({ length: 10 }, (_, index) => `LINE ${index + 1}`).join("\n");
    expect(staggeredSliceSettleS(manyLines)).toBe(2.4);
    expect(staggeredSliceStateAt(manyLines, 2.39, 4).settled).toBe(false);
    const settled = staggeredSliceStateAt(manyLines, 2.4, 4);
    expect(settled.settled).toBe(true);
    expect(settled.lines[settled.lines.length - 1].glyphs.every((glyph) => glyph.opacity === 1)).toBe(
      true,
    );
  });

  it("uses character-build only for a single logical line", () => {
    const state = staggeredSliceStateAt("ONE LINE", 2, 4);
    expect(state.lines).toHaveLength(1);
    expect(state.lines[0].kind).toBe("glyphs");
    expect(staggeredSliceSettleS("ONE LINE")).toBe(1.35);
  });

  it("segments combining marks and joined emoji as user-visible characters", () => {
    expect(segmentGraphemes("e\u0301👩‍💻")).toEqual(["e\u0301", "👩‍💻"]);
    expect(staggeredSliceStateAt("e\u0301👩‍💻", 0, 4).lines[0].glyphs).toHaveLength(2);
  });
});

describe("staggeredSlicePreviewVisibleAt", () => {
  it("pre-mounts invisibly while playing so coarse media events cannot skip the opening", () => {
    expect(staggeredSlicePreviewVisibleAt(-0.3, 3, true)).toBe(true);
    expect(staggeredSlicePreviewVisibleAt(-0.36, 3, true)).toBe(false);
    expect(staggeredSlicePreviewVisibleAt(-0.1, 3, false)).toBe(false);
  });

  it("keeps normal timing bounds once the effect begins", () => {
    expect(staggeredSlicePreviewVisibleAt(0, 3, true)).toBe(true);
    expect(staggeredSlicePreviewVisibleAt(2.99, 3, false)).toBe(true);
    expect(staggeredSlicePreviewVisibleAt(3, 3, true)).toBe(false);
  });
});

describe("animationStateAt — slide-up", () => {
  // animateFor = min(0.35, 5*0.5) = 0.35
  it("t=0 → yTranslate = -220 (start fully offset)", () => {
    const s = animationStateAt("slide-up", 0, DUR, TEXT);
    // progress=0; eased=easeOutCubic(0)=0; yTranslate = -1 * 220 * (1-0) = -220
    expect(s.yTranslate).toBeCloseTo(-220.0);
  });

  it("t=0.35 → yTranslate ≈ 0 (fully settled)", () => {
    const s = animationStateAt("slide-up", 0.35, DUR, TEXT);
    expect(s.yTranslate).toBeCloseTo(0.0, 1);
  });

  it("visibleText equals full text", () => {
    const s = animationStateAt("slide-up", 0, DUR, TEXT);
    expect(s.visibleText).toBe(TEXT);
  });
});

describe("animationStateAt — slide-down", () => {
  it("t=0 → yTranslate = +220", () => {
    const s = animationStateAt("slide-down", 0, DUR, TEXT);
    // direction = +1; yTranslate = +1 * 220 * 1 = 220
    expect(s.yTranslate).toBeCloseTo(220.0);
  });

  it("t=0.35 → yTranslate ≈ 0", () => {
    const s = animationStateAt("slide-down", 0.35, DUR, TEXT);
    expect(s.yTranslate).toBeCloseTo(0.0, 1);
  });
});

describe("animationStateAt — pop-in", () => {
  it("t=0 → scale=0.30", () => {
    const s = animationStateAt("pop-in", 0, DUR, TEXT);
    expect(s.scale).toBeCloseTo(0.30);
  });

  it("t=0.15 → scale=1.15 (overshoot keyframe)", () => {
    const s = animationStateAt("pop-in", 0.15, DUR, TEXT);
    expect(s.scale).toBeCloseTo(1.15);
  });

  it("t=0.25 → scale=1.00 (last keyframe)", () => {
    const s = animationStateAt("pop-in", 0.25, DUR, TEXT);
    expect(s.scale).toBeCloseTo(1.00);
  });

  it("t=1 → scale=1.00 (settled)", () => {
    const s = animationStateAt("pop-in", 1.0, DUR, TEXT);
    expect(s.scale).toBeCloseTo(1.00);
  });

  describe("clamped case: dur=0.1 (< last keyframe 0.25)", () => {
    const DUR_SHORT = 0.1;
    // scale_factor = 0.1 / 0.25 = 0.4
    // kfS = [0.0, 0.06, 0.10]

    it("t=0 → scale=0.30", () => {
      const s = animationStateAt("pop-in", 0, DUR_SHORT, TEXT);
      expect(s.scale).toBeCloseTo(0.30);
    });

    it("t=0.1 → scale=1.00 (last clamped keyframe reached)", () => {
      const s = animationStateAt("pop-in", 0.1, DUR_SHORT, TEXT);
      expect(s.scale).toBeCloseTo(1.00);
    });
  });
});

describe("animationStateAt — bounce", () => {
  // animateFor = min(0.5, 5*0.8) = 0.5
  it("t=0 → scale=1.0 (start of bounce: p=0 < 0.36 → 1+0.25*(0/0.36)=1.0)", () => {
    const s = animationStateAt("bounce", 0, DUR, TEXT);
    expect(s.scale).toBeCloseTo(1.0);
  });

  it("t=0.18 (p≈0.36) → scale≈1.25 (peak)", () => {
    // p = 0.18 / 0.5 = 0.36; in branch p < 0.36 → scale=1+0.25*(0.36/0.36)=1.25
    const s = animationStateAt("bounce", 0.18, DUR, TEXT);
    expect(s.scale).toBeCloseTo(1.25);
  });

  it("t=0.5 → scale=1.0 (animation window done)", () => {
    // tLocal >= animateFor → identity
    const s = animationStateAt("bounce", 0.5, DUR, TEXT);
    expect(s.scale).toBeCloseTo(1.0);
  });

  it("visibleText equals full text", () => {
    const s = animationStateAt("bounce", 0.1, DUR, TEXT);
    expect(s.visibleText).toBe(TEXT);
  });
});

describe("non-text-reveal effects preserve visibleText", () => {
  const nonReveal = ["fade-in", "scale-up", "slide-up", "slide-down", "pop-in", "bounce", "none", "static"];
  for (const effect of nonReveal) {
    it(`${effect} at t=0 has visibleText === full text`, () => {
      const s = animationStateAt(effect, 0, DUR, TEXT);
      expect(s.visibleText).toBe(TEXT);
    });
  }
});

describe("settled frames (t >= animation window)", () => {
  it("fade-in at t=1 → alpha=1, scale=1, yTranslate=0", () => {
    const s = animationStateAt("fade-in", 1.0, DUR, TEXT);
    expect(s.alpha).toBeCloseTo(1.0);
    expect(s.scale).toBeCloseTo(1.0);
    expect(s.yTranslate).toBeCloseTo(0.0);
  });

  it("scale-up at t=1 → scale=1, alpha=1, yTranslate=0", () => {
    const s = animationStateAt("scale-up", 1.0, DUR, TEXT);
    expect(s.scale).toBeCloseTo(1.0);
    expect(s.alpha).toBeCloseTo(1.0);
    expect(s.yTranslate).toBeCloseTo(0.0);
  });

  it("slide-up at t=1 → yTranslate≈0, scale=1, alpha=1", () => {
    const s = animationStateAt("slide-up", 1.0, DUR, TEXT);
    expect(s.yTranslate).toBeCloseTo(0.0, 1);
    expect(s.scale).toBeCloseTo(1.0);
    expect(s.alpha).toBeCloseTo(1.0);
  });
});

describe("popInScaleAt direct tests", () => {
  it("interpolates between KF 0 and 1 at midpoint", () => {
    // midpoint between t=0 and t=0.15 → p=0.5 → scale = 0.3 + 0.5*(1.15-0.3) = 0.725
    const s = popInScaleAt(0.075, 5.0);
    expect(s).toBeCloseTo(0.3 + 0.5 * (1.15 - 0.3));
  });
});
