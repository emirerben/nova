import {
  findHookOverlays,
  applyTuning,
  readTuning,
  type EditableSlot,
} from "@/app/admin/templates/[id]/components/text-tuning-matcher";

// Sized to mirror what the live recipe loader produces; using `unknown` shapes
// here on purpose so the matcher is exercised against the same loose dicts
// the API actually returns.
function dimplesPassportSlots(): EditableSlot[] {
  return [
    { text_overlays: [] }, // slots 1-3 have no overlays in the seed
    { text_overlays: [] },
    { text_overlays: [] },
    {
      text_overlays: [
        {
          role: "hook",
          text: "Welcome to",
          effect: "fade-in",
          font_style: "serif",
          text_size: "medium",
          text_size_px: 48,
          text_color: "#FFFFFF",
          position_y_frac: 0.4779,
        },
      ],
    },
    {
      text_overlays: [
        {
          role: "hook",
          text: "PERU",
          effect: "font-cycle",
          font_style: "sans",
          text_size: "medium",
          text_size_px: 265,
          text_color: "#F4D03F",
          position_y_frac: 0.45,
        },
      ],
    },
    {
      text_overlays: [
        {
          role: "hook",
          text: "Welcome to PERU",
          effect: "none",
          font_style: "sans",
          text_size: "medium",
          text_size_px: 265,
          text_color: "#F4D03F",
          position_y_frac: 0.45,
        },
      ],
    },
  ];
}

// Legacy single-slot layout: subject + prefix in the same slot. Earlier
// templates used this shape; the matcher must keep working for them.
function legacySingleSlotLayout(): EditableSlot[] {
  return [
    {
      text_overlays: [
        {
          role: "hook",
          text: "Welcome to",
          sample_text: "Welcome to",
          effect: "fade-in",
          text_size: "small",
          text_size_px: 48,
          position_y_frac: 0.48,
        },
        {
          role: "hook",
          text: "PARIS",
          sample_text: "PARIS",
          effect: "font-cycle",
          text_size: "jumbo",
          text_size_px: 200,
          position_y_frac: 0.45,
        },
      ],
    },
  ];
}

const DEFAULT_SIZE_MAP = {
  small: 36,
  medium: 72,
  large: 120,
  xlarge: 150,
  xxlarge: 250,
  jumbo: 199,
};

// ── findHookOverlays ────────────────────────────────────────────────────────

describe("findHookOverlays", () => {
  describe("Dimples Passport (cross-slot layout)", () => {
    it("finds the font-cycle subject on slot 5", () => {
      const match = findHookOverlays(dimplesPassportSlots());
      expect(match).not.toBeNull();
      expect(match!.subject.text).toBe("PERU");
      expect(match!.subject.effect).toBe("font-cycle");
    });

    it("finds the 'Welcome to' prefix on slot 4", () => {
      const match = findHookOverlays(dimplesPassportSlots());
      expect(match!.prefix).not.toBeNull();
      expect(match!.prefix!.text).toBe("Welcome to");
    });

    it("identifies slot 6 'Welcome to PERU' as a joint caption", () => {
      const match = findHookOverlays(dimplesPassportSlots());
      expect(match!.jointCaptions).toHaveLength(1);
      expect(match!.jointCaptions[0].text).toBe("Welcome to PERU");
    });
  });

  describe("Legacy single-slot layout", () => {
    it("still matches subject + prefix in the same slot", () => {
      const match = findHookOverlays(legacySingleSlotLayout());
      expect(match).not.toBeNull();
      expect(match!.subject.text).toBe("PARIS");
      expect(match!.prefix!.text).toBe("Welcome to");
    });

    it("has no joint captions when slot 6-style mirror is absent", () => {
      const match = findHookOverlays(legacySingleSlotLayout());
      expect(match!.jointCaptions).toEqual([]);
    });
  });

  describe("Subject discovery fallbacks", () => {
    it("falls back to jumbo text_size when no font-cycle is present", () => {
      const slots: EditableSlot[] = [
        {
          text_overlays: [
            { text: "WHAT", effect: "pop-in", text_size: "jumbo" },
            { text: "happened next", effect: "fade-in", text_size: "small" },
          ],
        },
      ];
      const match = findHookOverlays(slots);
      expect(match!.subject.text).toBe("WHAT");
    });

    it("returns null when there's no jumbo overlay and no font-cycle", () => {
      const slots: EditableSlot[] = [
        {
          text_overlays: [
            { text: "Welcome to", effect: "fade-in", text_size: "small" },
            { text: "the show", effect: "fade-in", text_size: "medium" },
          ],
        },
      ];
      expect(findHookOverlays(slots)).toBeNull();
    });
  });

  describe("Edge cases", () => {
    it("returns null for empty slots", () => {
      expect(findHookOverlays([])).toBeNull();
      expect(findHookOverlays(undefined)).toBeNull();
      expect(findHookOverlays(null)).toBeNull();
    });

    it("returns null when slots have no overlays", () => {
      const slots: EditableSlot[] = [{ text_overlays: [] }, {}];
      expect(findHookOverlays(slots)).toBeNull();
    });

    it("accepts the alternate `overlays` key (some API responses)", () => {
      const slots: EditableSlot[] = [
        {
          overlays: [
            { text: "TOKYO", effect: "font-cycle", text_size: "medium" },
            { text: "Welcome to", effect: "fade-in", text_size: "small" },
          ],
        },
      ];
      const match = findHookOverlays(slots);
      expect(match!.subject.text).toBe("TOKYO");
      expect(match!.prefix!.text).toBe("Welcome to");
    });

    it("returns subject without prefix when no lowercase overlay exists", () => {
      const slots: EditableSlot[] = [
        {
          text_overlays: [
            { text: "PERU", effect: "font-cycle", text_size: "medium" },
            { text: "BRAZIL", effect: "fade-in", text_size: "medium" },
          ],
        },
      ];
      const match = findHookOverlays(slots);
      expect(match!.subject.text).toBe("PERU");
      expect(match!.prefix).toBeNull();
    });
  });
});

// ── applyTuning ─────────────────────────────────────────────────────────────

describe("applyTuning", () => {
  it("propagates subject text_size_px to joint captions", () => {
    const slots = dimplesPassportSlots();
    const match = findHookOverlays(slots)!;
    applyTuning(match, {
      subjectSize: 200,
      subjectY: 0.5,
      prefixSize: 56,
      prefixY: 0.6,
    });
    expect(match.subject.text_size_px).toBe(200);
    expect(match.subject.position_y_frac).toBe(0.5);
    expect(match.prefix!.text_size_px).toBe(56);
    expect(match.prefix!.position_y_frac).toBe(0.6);
    // Joint caption inherits subject size so the merged "Welcome to PERU"
    // doesn't visually clash with the standalone PERU at the new size.
    expect(match.jointCaptions[0].text_size_px).toBe(200);
  });

  it("does NOT touch joint caption Y (different positioning is intentional)", () => {
    const slots = dimplesPassportSlots();
    const match = findHookOverlays(slots)!;
    const originalCaptionY = match.jointCaptions[0].position_y_frac;
    applyTuning(match, {
      subjectSize: 100,
      subjectY: 0.9,
      prefixSize: 30,
      prefixY: 0.1,
    });
    expect(match.jointCaptions[0].position_y_frac).toBe(originalCaptionY);
  });

  it("mutates the live recipe object so PUT carries changes", () => {
    // Verifies the underlying slot's overlay was mutated, not a copy.
    const slots = dimplesPassportSlots();
    const match = findHookOverlays(slots)!;
    applyTuning(match, {
      subjectSize: 175,
      subjectY: 0.55,
      prefixSize: 42,
      prefixY: 0.5,
    });
    const slot5Overlay = slots[4].text_overlays![0];
    expect(slot5Overlay.text_size_px).toBe(175);
    expect(slot5Overlay.position_y_frac).toBe(0.55);
  });

  it("works without a prefix (subject-only templates)", () => {
    const slots: EditableSlot[] = [
      { text_overlays: [{ text: "PERU", effect: "font-cycle", text_size: "medium" }] },
    ];
    const match = findHookOverlays(slots)!;
    expect(() =>
      applyTuning(match, { subjectSize: 200, subjectY: 0.5, prefixSize: 0, prefixY: 0 }),
    ).not.toThrow();
    expect(match.subject.text_size_px).toBe(200);
  });
});

// ── readTuning ──────────────────────────────────────────────────────────────

describe("readTuning", () => {
  it("reads pixel sizes from a fully-tuned recipe (Dimples)", () => {
    const match = findHookOverlays(dimplesPassportSlots())!;
    const tuning = readTuning(match, DEFAULT_SIZE_MAP);
    expect(tuning).toEqual({
      subjectSize: 265,
      subjectY: 0.45,
      prefixSize: 48,
      prefixY: 0.4779,
    });
  });

  it("falls back to sizeMap when text_size_px is unset", () => {
    const slots: EditableSlot[] = [
      {
        text_overlays: [
          { text: "PERU", effect: "font-cycle", text_size: "jumbo" },
          { text: "Welcome to", effect: "fade-in", text_size: "small" },
        ],
      },
    ];
    const match = findHookOverlays(slots)!;
    const tuning = readTuning(match, DEFAULT_SIZE_MAP);
    expect(tuning.subjectSize).toBe(199); // jumbo
    expect(tuning.prefixSize).toBe(36); // small
  });

  it("ignores a zero text_size_px (treats it as unset)", () => {
    const slots: EditableSlot[] = [
      {
        text_overlays: [
          {
            text: "PERU",
            effect: "font-cycle",
            text_size: "jumbo",
            text_size_px: 0,
          },
        ],
      },
    ];
    const match = findHookOverlays(slots)!;
    const tuning = readTuning(match, DEFAULT_SIZE_MAP);
    expect(tuning.subjectSize).toBe(199); // falls through to jumbo from sizeMap
  });
});
