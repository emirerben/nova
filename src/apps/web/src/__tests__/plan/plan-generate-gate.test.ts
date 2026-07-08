/**
 * Tests for lib/plan-generate-gate.ts — the ONE decision for the plan-item
 * Generate button (disabled state + hint copy from the same inputs, so the
 * two can never disagree — the "Record your voiceover first" bug class).
 */

import {
  FINISHING_UPLOAD_HINT,
  GENERIC_FALLBACK_BANNER,
  generateGate,
  narrationFallbackBanner,
  NO_SPEECH_BANNER,
  SELF_NARRATION_HINT,
  SPINE_FAILED_BANNER,
  VOICEOVER_REQUIRED_HINT,
  type GenerateGateInput,
} from "@/lib/plan-generate-gate";

function input(overrides: Partial<GenerateGateInput> = {}): GenerateGateInput {
  return {
    generating: false,
    isGenerating: false,
    uploaderBusy: false,
    clipCount: 1,
    isNarrated: false,
    hasVoiceover: false,
    selfNarrationEnabled: false,
    isInstructed: false,
    shotsLeft: 0,
    ...overrides,
  };
}

describe("generateGate — narrated voiceover gate", () => {
  it("flag OFF: narrated without voiceover stays blocked with the record hint", () => {
    const gate = generateGate(input({ isNarrated: true }));
    expect(gate.disabled).toBe(true);
    expect(gate.hint).toBe(VOICEOVER_REQUIRED_HINT);
  });

  it("flag ON: narrated without voiceover ENABLES once clips exist (the fix)", () => {
    const gate = generateGate(input({ isNarrated: true, selfNarrationEnabled: true }));
    expect(gate.disabled).toBe(false);
    expect(gate.hint).toBe(SELF_NARRATION_HINT);
  });

  it("flag ON: zero clips still blocks — self-narration needs footage", () => {
    const gate = generateGate(
      input({ isNarrated: true, selfNarrationEnabled: true, clipCount: 0 }),
    );
    expect(gate.disabled).toBe(true);
    expect(gate.hint).toBe("Add clips to generate");
  });

  it("a recorded voiceover enables generation regardless of the flag", () => {
    for (const selfNarrationEnabled of [false, true]) {
      const gate = generateGate(
        input({ isNarrated: true, hasVoiceover: true, selfNarrationEnabled }),
      );
      expect(gate.disabled).toBe(false);
      expect(gate.hint).toBeNull();
    }
  });

  it("shot-slot progress outranks the self-narration explainer", () => {
    const filling = generateGate(
      input({
        isNarrated: true,
        selfNarrationEnabled: true,
        isInstructed: true,
        shotsLeft: 2,
      }),
    );
    expect(filling.disabled).toBe(false);
    expect(filling.hint).toBe("2 shots left");
    // Slots full → the self-narration explainer takes over.
    const full = generateGate(
      input({
        isNarrated: true,
        selfNarrationEnabled: true,
        isInstructed: true,
        shotsLeft: 0,
      }),
    );
    expect(full.hint).toBe(SELF_NARRATION_HINT);
  });
});

describe("generateGate — busy states and non-narrated items", () => {
  it("uploaderBusy always wins the hint and disables", () => {
    const gate = generateGate(
      input({ isNarrated: true, selfNarrationEnabled: true, uploaderBusy: true }),
    );
    expect(gate.disabled).toBe(true);
    expect(gate.hint).toBe(FINISHING_UPLOAD_HINT);
  });

  it("generating / isGenerating disable without stealing the hint", () => {
    expect(generateGate(input({ generating: true })).disabled).toBe(true);
    expect(generateGate(input({ isGenerating: true })).disabled).toBe(true);
  });

  it("non-narrated: clipCount drives it, voiceover and flag are irrelevant", () => {
    expect(generateGate(input({ clipCount: 0 }))).toEqual({
      disabled: true,
      hint: "Add clips to generate",
    });
    expect(generateGate(input({ clipCount: 2 }))).toEqual({ disabled: false, hint: null });
  });

  it("instructed items show the shots-left nudge with correct pluralization", () => {
    expect(generateGate(input({ isInstructed: true, shotsLeft: 2 })).hint).toBe("2 shots left");
    expect(generateGate(input({ isInstructed: true, shotsLeft: 1 })).hint).toBe("1 shot left");
    expect(generateGate(input({ isInstructed: true, shotsLeft: 0 })).hint).toBeNull();
  });
});

describe("narrationFallbackBanner — style-downgrade explanation", () => {
  it("no_speech on a narrated item → the montage explanation", () => {
    expect(narrationFallbackBanner(true, { declared: "narrated_ready", reason: "no_speech" }))
      .toBe(NO_SPEECH_BANNER);
  });

  it("spine_extraction_failed → the unreadable-clip explanation", () => {
    expect(
      narrationFallbackBanner(true, { declared: "narrated_planned", reason: "spine_extraction_failed" }),
    ).toBe(SPINE_FAILED_BANNER);
  });

  it("silent only for non-narrated items and missing fallback", () => {
    expect(narrationFallbackBanner(false, { reason: "no_speech" })).toBeNull();
    expect(narrationFallbackBanner(true, null)).toBeNull();
    expect(narrationFallbackBanner(true, undefined)).toBeNull();
    expect(narrationFallbackBanner(true, { reason: null })).toBeNull();
  });

  it("UNKNOWN reasons still banner — a silent style swap is the original bug", () => {
    // e.g. archetype_not_implemented during an api-flipped/worker-stale flag
    // window, or a future backend reason rename: never silent on narrated items.
    expect(narrationFallbackBanner(true, { reason: "archetype_not_implemented" })).toBe(
      GENERIC_FALLBACK_BANNER,
    );
    expect(narrationFallbackBanner(true, { reason: "flag_disabled" })).toBe(
      GENERIC_FALLBACK_BANNER,
    );
  });
});
