import { describe, expect, it } from "@jest/globals";
import {
  allowedOpFamiliesFromCapabilities,
  buildCopilotSnapshot,
  COPILOT_SNAPSHOT_MAX_BYTES,
} from "@/lib/edit-copilot/snapshot";
import type { DraftSlot } from "@/app/generative/timeline-math";
import type { TextElementBar } from "@/lib/timeline/text-timeline-reducer";
import type { MediaOverlay, OverlaySuggestion, PoolAsset, SoundEffectPlacement } from "@/lib/plan-api";

function bar(over: Partial<TextElementBar> = {}): TextElementBar {
  return {
    id: "bar-1",
    text: "morning market",
    start_s: 0,
    end_s: 3,
    role: "generative_intro",
    ...over,
  };
}

function slot(over: Partial<DraftSlot> = {}): DraftSlot {
  return {
    key: "slot-1",
    slotId: "slot-1",
    clipIndex: 0,
    inS: 1,
    durationS: 3,
    durationBeats: null,
    removed: false,
    momentDescription: "coffee pour",
    ...over,
  };
}

describe("buildCopilotSnapshot", () => {
  it("excludes narrated captions but preserves the flag", () => {
    const snapshot = buildCopilotSnapshot(
      [
        bar({ id: "text", text: "visible" }),
        bar({ id: "caption", role: "narrated_caption", text: "caption" }),
      ],
      [slot()],
      [{ source_duration_s: 8 }],
      { text_elements: true, timeline: true },
    );

    expect(snapshot.text_bars).toHaveLength(1);
    expect(snapshot.text_bars[0].id).toBe("text");
    expect(snapshot.has_narrated_captions).toBe(true);
  });

  it("renders effective style values and output windows", () => {
    const snapshot = buildCopilotSnapshot(
      [
        bar({
          size_class: "small",
          color: undefined,
          letter_spacing: 10,
          line_spacing: undefined,
          max_width_frac: undefined,
        }),
      ],
      [
        slot({ key: "a", slotId: "a", durationS: 2 }),
        slot({ key: "b", slotId: "b", clipIndex: 1, inS: 0.5, durationS: 4 }),
      ],
      [{ source_duration_s: 8 }, { source_duration_s: 10 }],
      { text_elements: true, timeline: true },
    );

    expect(snapshot.text_bars[0]).toMatchObject({
      size_px: 36,
      color: "#FFFFFF",
      font_family: "PlayfairDisplay-Bold",
      alignment: "center",
      max_width_frac: 0.9,
    });
    expect(snapshot.slots.map((s) => [s.output_start_s, s.output_end_s])).toEqual([
      [0, 2],
      [2, 6],
    ]);
    expect(snapshot.total_duration_s).toBe(6);
    expect(snapshot.remaining_duration_s).toBe(54);
    expect(snapshot.allowed_op_families).toEqual(["text", "clip", "title"]);
  });

  it("removes disabled operation families from the snapshot", () => {
    const snapshot = buildCopilotSnapshot(
      [bar()],
      [slot()],
      [{ source_duration_s: 8 }],
      { text_elements: false, timeline: true },
    );

    expect(snapshot.allowed_op_families).toEqual(["clip", "title"]);
  });

  it("emits new optional sections only when families are allowed and data is provided", () => {
    const snapshot = buildCopilotSnapshot(
      [bar({ role: "narrated_caption", id: "caption-1", text: "caption cue" })],
      [slot()],
      [{ source_duration_s: 8 }],
      { text_elements: true, timeline: true, sfx: true, overlays: true },
      [],
      {
        sfxEnabled: true,
        sfxPlacements: [sfx({ at_s: 1.23456 })],
        sfxCatalog: [effect({ name: "Very long sound effect name that truncates" })],
        overlaysEnabled: true,
        overlayCards: [overlay({ scale: 0.33333 })],
        poolAssets: [asset({ status: "ready", subject: "A".repeat(70) }), asset({ id: "draft", status: "failed" })],
        pendingSuggestions: [suggestion()],
        captionsPresent: true,
        captionMeta: { enabled: true, style: "sentence", font: null, y_frac: 0.81234 },
        musicState: {
          swappable: true,
          currentTrackId: "track-1",
          currentTrackTitle: "Current Track",
          candidates: [{ id: "track-2", title: "Candidate Track" }],
        },
        mixLevel: 0.45678,
        title: "My edit",
        openTools: ["text", "sounds", "overlays", "styles"],
      },
    );

    expect(snapshot.allowed_op_families).toEqual([
      "text",
      "clip",
      "sfx",
      "overlay",
      "caption",
      "music",
      "title",
      "tool",
    ]);
    expect(snapshot.sfx?.placements[0].at_s).toBe(1.235);
    expect(snapshot.sfx?.catalog[0].name).toHaveLength(32);
    expect(snapshot.overlays?.cards[0].scale).toBe(0.333);
    expect(snapshot.overlays?.asset_pool).toHaveLength(1);
    expect(snapshot.overlays?.asset_pool[0].subject).toHaveLength(60);
    expect(snapshot.captions?.meta.y_frac).toBe(0.812);
    expect(snapshot.music?.candidates).toEqual([{ id: "track-2", title: "Candidate Track" }]);
    expect(snapshot.mix).toEqual({ music_level: 0.457 });
    expect(snapshot.title).toBe("My edit");
    expect(snapshot.open_tools).toEqual(["text", "sounds", "overlays", "styles"]);
  });

  it("caps caption cues at 40 and marks truncation from total input count", () => {
    const captions = Array.from({ length: 60 }, (_, i) => bar({
      id: `caption-${i}`,
      role: "narrated_caption",
      text: `caption ${i}`,
      start_s: i,
      end_s: i + 0.5,
    }));
    const snapshot = buildCopilotSnapshot(captions, [slot()], [{ source_duration_s: 8 }], {}, [], {
      captionsPresent: true,
      captionMeta: { enabled: true, style: "word", font: "Inter", y_frac: 0.7 },
    });

    expect(snapshot.captions?.total_cues).toBe(60);
    expect(snapshot.captions?.truncated).toBe(true);
    expect(snapshot.captions?.cues).toHaveLength(40);
  });

  it("trims oversized snapshots under the byte budget in the fixed order", () => {
    const capped = "x".repeat(80);
    const longMoment = "x".repeat(2000);
    const bars = Array.from({ length: 6 }, (_, i) =>
      bar({ id: `bar-${i}`, text: capped, start_s: i, end_s: i + 0.5 }),
    ).concat(
      Array.from({ length: 40 }, (_, i) =>
        bar({ id: `caption-${i}`, role: "narrated_caption", text: capped, start_s: i, end_s: i + 0.5 }),
      ),
    );
    const slots = Array.from({ length: 12 }, (_, i) =>
      slot({ key: `slot-${i}`, slotId: `slot-${i}`, clipIndex: 0, momentDescription: longMoment }),
    );
    const snapshot = buildCopilotSnapshot(bars, slots, [{ source_duration_s: 8 }], { sfx: true, overlays: true }, [], {
      sfxEnabled: true,
      sfxPlacements: Array.from({ length: 15 }, (_, i) => sfx({ id: `sfx-${i}` })),
      sfxCatalog: Array.from({ length: 20 }, (_, i) => effect({ id: `effect-${i}` })),
      overlaysEnabled: true,
      overlayCards: Array.from({ length: 12 }, (_, i) => overlay({ id: `overlay-${i}` })),
      poolAssets: Array.from({ length: 12 }, (_, i) => asset({ id: `asset-${i}`, subject: capped })),
      pendingSuggestions: Array.from({ length: 6 }, (_, i) => suggestion({ id: `suggestion-${i}`, reason: capped })),
      captionsPresent: true,
      captionMeta: { enabled: true, style: "sentence", font: null, y_frac: 0.7 },
      musicState: {
        swappable: true,
        currentTrackId: "track-1",
        currentTrackTitle: capped,
        candidates: Array.from({ length: 20 }, (_, i) => ({ id: `track-${i}`, title: capped })),
      },
      mixLevel: 0.5,
      title: capped,
      openTools: ["text", "sounds", "overlays", "styles"],
    });

    expect(byteLength(snapshot)).toBeLessThanOrEqual(COPILOT_SNAPSHOT_MAX_BYTES);
    expect(snapshot.captions?.cues.length).toBeLessThanOrEqual(24);
    expect(snapshot.overlays?.asset_pool.length).toBeLessThanOrEqual(6);
    expect(snapshot.sfx?.catalog.length).toBeLessThanOrEqual(12);
    expect(snapshot.music?.candidates.length).toBeLessThanOrEqual(10);
    expect(snapshot.overlays?.pending_suggestions.length).toBeLessThanOrEqual(3);
    expect(snapshot.slots.every((snapSlot) => (snapSlot.moment?.length ?? 0) <= 40)).toBe(true);
  });

  it("includes beat marks for grid variants and omits them otherwise", () => {
    const grid = [0, 0.5, 1.0, 1.5, 2.0];
    const gridSnapshot = buildCopilotSnapshot(
      [bar()],
      [slot({ durationBeats: 4, durationS: null })],
      [{ source_duration_s: 8 }],
      { text_elements: true, timeline: true },
      grid,
    );
    expect(gridSnapshot.beat_marks).toEqual([0, 0.5, 1.0, 1.5, 2.0]);

    const noGrid = buildCopilotSnapshot(
      [bar()],
      [slot()],
      [{ source_duration_s: 8 }],
      { text_elements: true, timeline: true },
    );
    expect(noGrid.beat_marks).toBeUndefined();
  });

  it("caps beat marks by even sampling, always retaining both endpoints", () => {
    const grid = Array.from({ length: 200 }, (_, i) => i * 0.25);
    const snapshot = buildCopilotSnapshot(
      [bar()],
      [slot({ durationBeats: 150, durationS: null })],
      [{ source_duration_s: 60 }],
      { text_elements: true, timeline: true },
      grid,
    );
    const marks = snapshot.beat_marks ?? [];
    expect(marks.length).toBeLessThanOrEqual(60);
    expect(marks[0]).toBe(0);
    // 150 beats over a 0.25s grid → the true final mark is 37.5s and must
    // survive sampling (late-video beats stay addressable).
    expect(marks[marks.length - 1]).toBe(37.5);
  });

  it("re-samples beat marks under byte-budget pressure", () => {
    const capped = "x".repeat(80);
    const longMoment = "x".repeat(2000);
    const grid = Array.from({ length: 200 }, (_, i) => i * 0.25);
    const bars = Array.from({ length: 6 }, (_, i) =>
      bar({ id: `bar-${i}`, text: capped, start_s: i, end_s: i + 0.5 }),
    ).concat(
      Array.from({ length: 40 }, (_, i) =>
        bar({ id: `caption-${i}`, role: "narrated_caption", text: capped, start_s: i, end_s: i + 0.5 }),
      ),
    );
    const slots = [slot({ key: "beats", slotId: "beats", durationBeats: 150, durationS: null })].concat(
      Array.from({ length: 11 }, (_, i) =>
        slot({ key: `slot-${i}`, slotId: `slot-${i}`, clipIndex: 0, momentDescription: longMoment }),
      ),
    );
    const snapshot = buildCopilotSnapshot(bars, slots, [{ source_duration_s: 60 }], { sfx: true, overlays: true }, grid, {
      sfxEnabled: true,
      sfxPlacements: Array.from({ length: 15 }, (_, i) => sfx({ id: `sfx-${i}` })),
      sfxCatalog: Array.from({ length: 20 }, (_, i) => effect({ id: `effect-${i}` })),
      overlaysEnabled: true,
      overlayCards: Array.from({ length: 12 }, (_, i) => overlay({ id: `overlay-${i}` })),
      poolAssets: Array.from({ length: 12 }, (_, i) => asset({ id: `asset-${i}`, subject: capped })),
      pendingSuggestions: Array.from({ length: 6 }, (_, i) => suggestion({ id: `suggestion-${i}`, reason: capped })),
      captionsPresent: true,
      captionMeta: { enabled: true, style: "sentence", font: null, y_frac: 0.7 },
      musicState: {
        swappable: true,
        currentTrackId: "track-1",
        currentTrackTitle: capped,
        candidates: Array.from({ length: 20 }, (_, i) => ({ id: `track-${i}`, title: capped })),
      },
      mixLevel: 0.5,
      title: capped,
      openTools: ["text", "sounds", "overlays", "styles"],
    });

    expect(byteLength(snapshot)).toBeLessThanOrEqual(COPILOT_SNAPSHOT_MAX_BYTES);
    expect(snapshot.beat_marks?.length ?? 0).toBeLessThanOrEqual(30);
    expect(snapshot.beat_marks?.[0]).toBe(0);
  });

  it("emits a meta-only captions section for subtitled variants", () => {
    const snapshot = buildCopilotSnapshot(
      [bar()],
      [slot()],
      [{ source_duration_s: 30 }],
      { text_elements: true, timeline: true },
      [],
      {
        captionsPresent: true,
        captionMeta: { enabled: true, style: "sentence", font: "TikTok Sans Bold", y_frac: 0.8 },
        captionCuesEditable: false,
        captionTotalCues: 14,
      },
    );
    expect(snapshot.captions).toMatchObject({
      total_cues: 14,
      cues_editable: false,
      cues: [],
      meta: { style: "sentence" },
    });
    expect(snapshot.allowed_op_families).toContain("caption");
  });

  it("marks caption cues editable on the narrated path", () => {
    const snapshot = buildCopilotSnapshot(
      [bar(), bar({ id: "caption-1", role: "narrated_caption", text: "line one" })],
      [slot()],
      [{ source_duration_s: 30 }],
      { text_elements: true, timeline: true },
      [],
      {
        captionsPresent: true,
        captionMeta: { enabled: true, style: "sentence", font: null, y_frac: 0.8 },
      },
    );
    expect(snapshot.captions?.cues_editable).toBe(true);
    expect(snapshot.captions?.cues.length).toBe(1);
  });

  it("emits the intro section and render family only when the variant can switch layouts", () => {
    const snapshot = buildCopilotSnapshot(
      [bar()],
      [slot()],
      [{ source_duration_s: 8 }],
      { text_elements: true, timeline: true },
      [],
      {
        intro: {
          layout: "linear",
          mode: "linear",
          text: "what a view today",
          word_count: 4,
          sequence_capable: false,
          cluster_eligible: true,
          switch_blocked_reason: null,
        },
        renderLayoutSwitchable: true,
      },
    );

    expect(snapshot.intro).toEqual({
      layout: "linear",
      mode: "linear",
      text: "what a view today",
      word_count: 4,
      sequence_capable: false,
      cluster_eligible: true,
      switch_blocked_reason: null,
    });
    expect(snapshot.allowed_op_families).toContain("render");
  });

  it("withholds the render family but keeps the intro section when switching is blocked", () => {
    const snapshot = buildCopilotSnapshot(
      [bar()],
      [slot()],
      [{ source_duration_s: 8 }],
      { text_elements: true, timeline: true },
      [],
      {
        intro: {
          layout: "cluster",
          mode: "sequence",
          text: "one two three four five six seven",
          word_count: 7,
          sequence_capable: true,
          cluster_eligible: true,
          switch_blocked_reason: "unsaved_edits",
        },
        renderLayoutSwitchable: false,
      },
    );

    expect(snapshot.intro?.switch_blocked_reason).toBe("unsaved_edits");
    expect(snapshot.intro?.layout).toBe("cluster");
    expect(snapshot.allowed_op_families).not.toContain("render");
  });

  it("derives allowed families from server capabilities and client flags", () => {
    expect(
      allowedOpFamiliesFromCapabilities(
        { text_elements: true, timeline: true, sfx: true, overlays: true },
        {
          sfxEnabled: false,
          overlaysEnabled: true,
          captionsPresent: true,
          musicSwappable: false,
          mixAllowed: true,
          openTools: ["sounds"],
        },
      ),
    ).toEqual(["text", "clip", "overlay", "caption", "music", "title", "tool"]);
    expect(
      allowedOpFamiliesFromCapabilities(
        { text_elements: false, timeline: false, split_clips: false, mix: false, sfx: false, overlays: false },
        { sfxEnabled: true, overlaysEnabled: true, captionsPresent: true, openTools: ["text"] },
      ),
    ).toEqual([]);
    expect(allowedOpFamiliesFromCapabilities({}, { readOnly: true, openTools: ["text"] })).toEqual([]);
  });
});

function byteLength(value: unknown): number {
  const json = JSON.stringify(value);
  if (typeof TextEncoder !== "undefined") return new TextEncoder().encode(json).length;
  return encodeURIComponent(json).replace(/%[0-9A-F]{2}/g, "x").length;
}

function sfx(over: Partial<SoundEffectPlacement> = {}): SoundEffectPlacement {
  return {
    id: "sfx-1",
    sound_effect_id: "effect-1",
    src_gcs_path: "",
    at_s: 1,
    gain: 1,
    duration_s: 0.5,
    label: "Whoosh",
    ...over,
  };
}

function effect(over: Record<string, unknown> = {}) {
  return {
    id: "effect-1",
    name: "Whoosh",
    duration_s: 0.5,
    published_at: null,
    archived_at: null,
    status: "ready",
    source_filename: null,
    ...over,
  };
}

function overlay(over: Partial<MediaOverlay> = {}): MediaOverlay {
  return {
    id: "overlay-1",
    kind: "image",
    src_gcs_path: "pool/card.png",
    preview_url: "https://example.com/card.png",
    position: "center",
    x_frac: 0.5,
    y_frac: 0.5,
    scale: 0.35,
    display_mode: "pip",
    start_s: 1,
    end_s: 3,
    z: 0,
    ...over,
  };
}

function asset(over: Partial<PoolAsset> = {}): PoolAsset {
  return {
    id: "asset-1",
    kind: "image",
    status: "ready",
    source_filename: "asset.png",
    duration_s: null,
    aspect: null,
    subject: "coffee pour",
    display_url: "https://example.com/asset.png",
    deduped: false,
    gcs_path: "pool/asset.png",
    ...over,
  };
}

function suggestion(over: Partial<OverlaySuggestion> = {}): OverlaySuggestion {
  return {
    id: "suggestion-1",
    asset_id: "asset-1",
    confidence_tier: "confident",
    reason: "matches the hook",
    transcript_anchor: "hook",
    overlay: overlay({ id: "suggested-overlay", start_s: 2, end_s: 4 }),
    sfx: null,
    ...over,
  };
}

describe("buildCopilotSnapshot speech + SFX suggestions", () => {
  const speechMap = {
    source: "caption_words",
    words: [
      { w: "hello", s: 0.62, e: 1.0 },
      { w: "world", s: 1.5, e: 2.0 },
    ],
    pauses: [
      { s: 0.0, e: 0.62, after: null },
      { s: 1.0, e: 1.5, after: "hello" },
    ],
  };

  it("builds the speech section from a server speech map", () => {
    const snapshot = buildCopilotSnapshot(
      [bar()],
      [slot()],
      [{ source_duration_s: 8 }],
      { text_elements: true, timeline: true },
      [],
      { speechMap },
    );
    expect(snapshot.speech).toEqual({
      source: "caption_words",
      words: [
        { text: "hello", start_s: 0.62, end_s: 1 },
        { text: "world", start_s: 1.5, end_s: 2 },
      ],
      pauses: [
        { start_s: 0, end_s: 0.62, after: null },
        { start_s: 1, end_s: 1.5, after: "hello" },
      ],
    });
  });

  it("omits the speech section when the map is null or wordless", () => {
    const withNull = buildCopilotSnapshot(
      [bar()], [slot()], [{ source_duration_s: 8 }],
      { text_elements: true, timeline: true }, [], { speechMap: null },
    );
    expect(withNull.speech).toBeUndefined();
    const wordless = buildCopilotSnapshot(
      [bar()], [slot()], [{ source_duration_s: 8 }],
      { text_elements: true, timeline: true }, [],
      { speechMap: { source: "x", words: [], pauses: [{ s: 0, e: 1, after: null }] } },
    );
    expect(wordless.speech).toBeUndefined();
  });

  it("passes catalog role_tags through and attaches sfx suggestions", () => {
    const snapshot = buildCopilotSnapshot(
      [bar()],
      [slot()],
      [{ source_duration_s: 8 }],
      { text_elements: true, timeline: true, sfx: true },
      [],
      {
        sfxEnabled: true,
        sfxCatalog: [
          {
            id: "fx_tick",
            name: "Smart keyboard tick",
            duration_s: 0.2,
            published_at: null,
            archived_at: null,
            status: "ready",
            source_filename: null,
            role_tags: ["keyword_typewriter_tick"],
          },
        ],
        sfxSuggestions: [
          { effect_id: "fx_tick", at_s: 3.1, gain: 0.7, reason: "tick under typing" },
        ],
      },
    );
    expect(snapshot.sfx?.catalog[0].role_tags).toEqual(["keyword_typewriter_tick"]);
    expect(snapshot.sfx?.suggestions).toEqual([
      { effect_id: "fx_tick", at_s: 3.1, gain: 0.7, reason: "tick under typing" },
    ]);
  });

  it("head-caps words under moderate budget pressure, keeping pauses", () => {
    // Words are the overflow source: after the head-cap stage the snapshot
    // fits, so pauses and the leading 60 words survive.
    const words = Array.from({ length: 150 }, (_, i) => ({
      w: `word${i}-${"x".repeat(24)}`,
      s: i * 0.4,
      e: i * 0.4 + 0.3,
    }));
    const bigMap = { source: "caption_words", words, pauses: [{ s: 1.0, e: 1.5, after: "word2" }] };
    const bars = Array.from({ length: 28 }, (_, i) =>
      bar({ id: `bar-${i}`, text: `label ${i} ${"y".repeat(110)}` }),
    );
    const snapshot = buildCopilotSnapshot(
      bars,
      [slot()],
      [{ source_duration_s: 8 }],
      { text_elements: true, timeline: true },
      [],
      { speechMap: bigMap },
    );
    const bytes = Buffer.byteLength(JSON.stringify(snapshot), "utf8");
    expect(bytes).toBeLessThanOrEqual(COPILOT_SNAPSHOT_MAX_BYTES);
    expect(snapshot.speech).toBeDefined();
    expect(snapshot.speech?.words.length).toBeLessThanOrEqual(60);
    expect(snapshot.speech?.words[0].text.startsWith("word0")).toBe(true);
    expect(snapshot.speech?.pauses.length).toBe(1);
  });

  it("keeps pauses when words are dropped at the middle trim stage", () => {
    // Sized so the head-capped words still blow the budget but the pause-only
    // form fits: the section must survive with words=[] and pauses intact
    // (pause-only placement stays possible; server renders the trimmed note).
    const words = Array.from({ length: 150 }, (_, i) => ({
      w: `word${i}-${"x".repeat(24)}`,
      s: i * 0.4,
      e: i * 0.4 + 0.3,
    }));
    const bigMap = { source: "caption_words", words, pauses: [{ s: 1.0, e: 1.5, after: "word2" }] };
    const bars = Array.from({ length: 36 }, (_, i) =>
      bar({ id: `bar-${i}`, text: `label ${i} ${"y".repeat(110)}` }),
    );
    const snapshot = buildCopilotSnapshot(
      bars,
      [slot()],
      [{ source_duration_s: 8 }],
      { text_elements: true, timeline: true },
      [],
      { speechMap: bigMap },
    );
    expect(snapshot.speech).toBeDefined();
    expect(snapshot.speech?.words).toEqual([]);
    expect(snapshot.speech?.pauses.length).toBe(1);
  });

  it("drops the speech section entirely as the last speech trim stage", () => {
    // The rest of the snapshot alone exceeds the budget — every speech stage
    // fires and the section is removed rather than shipping a blown budget
    // with it still attached.
    const words = Array.from({ length: 150 }, (_, i) => ({
      w: `word${i}-${"x".repeat(24)}`,
      s: i * 0.4,
      e: i * 0.4 + 0.3,
    }));
    const bigMap = { source: "caption_words", words, pauses: [{ s: 1.0, e: 1.5, after: "word2" }] };
    const bars = Array.from({ length: 70 }, (_, i) =>
      bar({ id: `bar-${i}`, text: `label ${i} ${"y".repeat(110)}` }),
    );
    const snapshot = buildCopilotSnapshot(
      bars,
      [slot()],
      [{ source_duration_s: 8 }],
      { text_elements: true, timeline: true },
      [],
      { speechMap: bigMap },
    );
    expect(snapshot.speech).toBeUndefined();
  });
});

describe("slot-less duration fallback", () => {
  it("uses videoDurationS when the variant has no clip slots", () => {
    const snapshot = buildCopilotSnapshot(
      [bar()],
      [],
      [],
      { text_elements: true, timeline: true },
      [],
      { videoDurationS: 82.4 },
    );
    expect(snapshot.total_duration_s).toBe(82.4);
    // Longer-than-cap subtitled videos have zero remaining budget, never negative.
    expect(snapshot.remaining_duration_s).toBe(0);
  });

  it("prefers the slot-layout total when slots exist", () => {
    const snapshot = buildCopilotSnapshot(
      [bar()],
      [slot()],
      [{ source_duration_s: 8 }],
      { text_elements: true, timeline: true },
      [],
      { videoDurationS: 82.4 },
    );
    expect(snapshot.total_duration_s).toBe(3);
  });

  it("stays 0 when neither slots nor a video duration exist", () => {
    const snapshot = buildCopilotSnapshot([bar()], [], [], { text_elements: true, timeline: true }, []);
    expect(snapshot.total_duration_s).toBe(0);
  });
});
