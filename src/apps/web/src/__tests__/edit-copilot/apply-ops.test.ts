import { describe, expect, it } from "@jest/globals";
import { applyCopilotOps } from "@/lib/edit-copilot/apply-ops";
import { buildCopilotSnapshot } from "@/lib/edit-copilot/snapshot";
import type { DraftSlot } from "@/app/generative/timeline-math";
import type { TextElementBar } from "@/lib/timeline/text-timeline-reducer";
import type { MediaOverlay, OverlaySuggestion, PoolAsset, SoundEffectPlacement } from "@/lib/plan-api";

function bar(over: Partial<TextElementBar> = {}): TextElementBar {
  return {
    id: "bar-1",
    text: "old hook",
    start_s: 0,
    end_s: 3,
    role: "generative_intro",
    font_family: "Inter",
    size_px: 64,
    color: "#FFFFFF",
    effect: "static",
    alignment: "center",
    position: "middle",
    ...over,
  };
}

function slot(over: Partial<DraftSlot> = {}): DraftSlot {
  return {
    key: "slot-1",
    slotId: "slot-1",
    clipIndex: 0,
    inS: 0,
    durationS: 4,
    durationBeats: null,
    removed: false,
    momentDescription: null,
    ...over,
  };
}

const clips = [
  { source_duration_s: 10 },
  { source_duration_s: 8 },
  { source_duration_s: 7 },
];

function ctx(over: {
  bars?: TextElementBar[];
  slots?: DraftSlot[];
  capabilities?: Parameters<typeof buildCopilotSnapshot>[3];
  extras?: Parameters<typeof buildCopilotSnapshot>[5];
} = {}) {
  const bars = over.bars ?? [bar(), bar({ id: "bar-2", text: "second", start_s: 3, end_s: 5 })];
  const slots = over.slots ?? [
    slot({ key: "a", slotId: "a", durationS: 3 }),
    slot({ key: "b", slotId: "b", clipIndex: 1, inS: 1, durationS: 4 }),
    slot({ key: "c", slotId: "c", clipIndex: 2, durationS: 2 }),
  ];
  const capabilities = over.capabilities ?? { text_elements: true, timeline: true, split_clips: true };
  return {
    bars,
    slots,
    snapshot: buildCopilotSnapshot(bars, slots, clips, capabilities, [], over.extras),
    capabilities,
    makeTextBarId: () => "new-text",
    makeSlotKey: (s: DraftSlot) => `${s.key}-split`,
  };
}

function extendedCtx(over: Partial<Parameters<typeof applyCopilotOps>[1]> = {}) {
  const bars = [
    bar(),
    bar({ id: "bar-2", text: "second", start_s: 3, end_s: 5 }),
    bar({ id: "caption-1", role: "narrated_caption", text: "old caption", start_s: 1.2344, end_s: 2.5 }),
  ];
  const slots = [slot({ key: "a", slotId: "a", durationS: 3 })];
  const sfxPlacements = [sfx({ at_s: 1.2344, gain: 1 })];
  const overlays = [overlay({ x_frac: 0.25, y_frac: 0.5 })];
  const poolAssets = [asset()];
  const pendingSuggestions = [suggestion()];
  const sfxCatalog = [effect()];
  const extras: Parameters<typeof buildCopilotSnapshot>[5] = {
    sfxEnabled: true,
    sfxPlacements,
    sfxCatalog,
    overlaysEnabled: true,
    overlayCards: overlays,
    poolAssets,
    pendingSuggestions,
    captionsPresent: true,
    captionMeta: { enabled: true, style: "sentence", font: null, y_frac: 0.7 },
    musicState: {
      swappable: true,
      currentTrackId: "track-1",
      currentTrackTitle: "Current",
      candidates: [{ id: "track-1", title: "Current" }, { id: "track-2", title: "Next" }],
    },
    mixLevel: 0.6,
    title: "Old title",
    openTools: ["text", "sounds", "overlays", "styles"],
  };
  return {
    bars,
    slots,
    snapshot: buildCopilotSnapshot(bars, slots, clips, { text_elements: true, timeline: true, sfx: true, overlays: true }, [], extras),
    capabilities: { text_elements: true, timeline: true, split_clips: true, sfx: true, overlays: true },
    sfx: sfxPlacements,
    sfxCatalog,
    overlays,
    poolAssets,
    pendingSuggestions,
    musicTrackId: "track-1",
    mixLevel: 0.6,
    title: "Old title",
    captionMeta: { enabled: true, style: "sentence" as const, font: null, y_frac: 0.7 },
    makeTextBarId: () => "new-text",
    makeSlotKey: (s: DraftSlot) => `${s.key}-split`,
    makeSfxPlacementId: () => "new-sfx",
    makeOverlayId: () => "new-overlay",
    ...over,
  };
}

describe("applyCopilotOps", () => {
  it("maps every text op to the expected text action", () => {
    expect(applyCopilotOps([{ op: "edit_text", bar_index: 0, text: "new hook" }], ctx()).textActions)
      .toEqual([{ type: "EDIT_TEXT", id: "bar-1", text: "new hook" }]);

    expect(
      applyCopilotOps(
        [{ op: "patch_text_style", bar_index: 0, patch: { size_px: 54, font_family: "Playfair Display" } }],
        ctx(),
      ).textActions,
    ).toEqual([
      {
        type: "PATCH_BAR",
        id: "bar-1",
        patch: { size_px: 54, font_family: "Playfair Display", size_class: undefined },
      },
    ]);

    expect(
      applyCopilotOps([{ op: "set_text_timing", bar_index: 0, start_s: 0.2, end_s: 2.8 }], ctx()).textActions,
    ).toEqual([{ type: "PATCH_BAR", id: "bar-1", patch: { start_s: 0.2, end_s: 2.8 } }]);

    expect(applyCopilotOps([{ op: "add_text", text: "day 1", start_s: 5, end_s: 7 }], ctx()).textActions)
      .toEqual([
        {
          type: "ADD_TEXT",
          bar: expect.objectContaining({
            id: "new-text",
            text: "day 1",
            start_s: 5,
            end_s: 7,
          }),
        },
      ]);

    expect(applyCopilotOps([{ op: "remove_text", bar_index: 1 }], ctx()).textActions)
      .toEqual([{ type: "DELETE_BAR", id: "bar-2" }]);
  });

  it("allows lyric text edits but rejects lyric timing and removal", () => {
    const lyric = bar({ id: "lyric_L0", role: "lyric_line", text: "old lyric" });
    const base = ctx({ bars: [lyric] });

    expect(
      applyCopilotOps([{ op: "edit_text", bar_index: 0, text: "new lyric" }], base)
        .textActions,
    ).toEqual([{ type: "EDIT_TEXT", id: "lyric_L0", text: "new lyric" }]);

    const timing = applyCopilotOps(
      [{ op: "set_text_timing", bar_index: 0, start_s: 1.2 }],
      base,
    );
    expect(timing.textActions).toEqual([]);
    expect(timing.rejected).toEqual([
      expect.objectContaining({ detail: "Lyric timing is locked to the vocal." }),
    ]);

    const remove = applyCopilotOps([{ op: "remove_text", bar_index: 0 }], base);
    expect(remove.textActions).toEqual([]);
    expect(remove.rejected).toEqual([
      expect.objectContaining({ detail: "Lyric timing is locked to the vocal." }),
    ]);
  });

  it("maps clip timing, reorder, remove, and split ops to slot transforms", () => {
    const duration = applyCopilotOps([{ op: "set_clip_duration", slot_index: 1, duration_s: 3 }], ctx());
    expect(duration.nextSlots?.find((s) => s.key === "b")).toMatchObject({
      inS: 1,
      durationS: 3,
      durationBeats: null,
    });

    const clipIn = applyCopilotOps([{ op: "set_clip_in", slot_index: 1, in_s: 0.4 }], ctx());
    expect(clipIn.nextSlots?.find((s) => s.key === "b")).toMatchObject({
      inS: 0.4,
      durationS: 4,
      durationBeats: null,
    });

    const reordered = applyCopilotOps([{ op: "reorder_clip", from_index: 2, to_index: 0 }], ctx());
    expect(reordered.nextSlots?.map((s) => s.key)).toEqual(["c", "a", "b"]);

    const removed = applyCopilotOps([{ op: "remove_clip", slot_index: 2 }], ctx());
    expect(removed.nextSlots?.find((s) => s.key === "c")?.removed).toBe(true);

    const split = applyCopilotOps([{ op: "split_clip", slot_index: 1, at_s: 5 }], ctx());
    expect(split.nextSlots?.map((s) => s.key)).toEqual(["a", "b", "b-split", "c"]);
    expect(split.nextSlots?.find((s) => s.key === "b")?.durationS).toBe(2);
    expect(split.nextSlots?.find((s) => s.key === "b-split")?.inS).toBe(3);
  });

  it("resolves indices through the snapshotted slot array including removed slots", () => {
    const slots = [
      slot({ key: "a", slotId: "a", durationS: 3 }),
      slot({ key: "removed", slotId: "removed", clipIndex: 1, removed: true, durationS: 4 }),
      slot({ key: "c", slotId: "c", clipIndex: 2, durationS: 2 }),
    ];
    const res = applyCopilotOps([{ op: "set_clip_in", slot_index: 2, in_s: 1.2 }], ctx({ slots }));

    expect(res.nextSlots?.find((s) => s.key === "c")?.inS).toBe(1.2);
  });

  it("rejects unknown and out-of-bounds ops", () => {
    const res = applyCopilotOps(
      [{ op: "swap_song" }, { op: "remove_text", bar_index: 99 }],
      ctx(),
    );

    expect(res.rejected.map((r) => r.reason)).toEqual(["invalid_op", "invalid_op"]);
  });

  it("strips non-vocabulary style keys before applying a patch", () => {
    const res = applyCopilotOps(
      [
        {
          op: "patch_text_style",
          bar_index: 0,
          patch: { size_px: 50, shadow_enabled: false },
        },
      ],
      ctx(),
    );

    expect(res.textActions).toEqual([
      { type: "PATCH_BAR", id: "bar-1", patch: { size_px: 50, size_class: undefined } },
    ]);
  });

  it("soft-fails when the user changed the patched field after the snapshot", () => {
    const base = ctx();
    const res = applyCopilotOps(
      [{ op: "patch_text_style", bar_index: 0, patch: { size_px: 54 } }],
      { ...base, bars: [bar({ size_px: 70 }), base.bars[1]] },
    );

    expect(res.textActions).toEqual([]);
    expect(res.rejected).toMatchObject([{ reason: "user_changed" }]);
  });

  it("rejects an op family disabled by capabilities", () => {
    const res = applyCopilotOps(
      [{ op: "edit_text", bar_index: 0, text: "nope" }],
      ctx({ capabilities: { text_elements: false, timeline: true } }),
    );

    expect(res.textActions).toEqual([]);
    expect(res.rejected).toMatchObject([{ reason: "capability_disabled" }]);
  });

  it("applies sfx ops by stable snapshotted id and catches rounded fingerprint changes", () => {
    const add = applyCopilotOps([{ op: "add_sfx", effect_id: "effect-1", at_s: 3, gain: 0.5 }], extendedCtx());
    expect(add.nextSfx?.at(-1)).toMatchObject({
      id: "new-sfx",
      sound_effect_id: "effect-1",
      at_s: 2.9,
      gain: 0.5,
      label: "Whoosh",
    });

    const patch = applyCopilotOps([{ op: "patch_sfx", sfx_index: 0, at_s: 1.5 }], extendedCtx());
    expect(patch.nextSfx?.[0]).toMatchObject({ id: "sfx-1", at_s: 1.5 });

    const rounded = applyCopilotOps([{ op: "patch_sfx", sfx_index: 0, gain: 0.4 }], extendedCtx({
      sfx: [sfx({ id: "sfx-1", at_s: 1.2345, gain: 1 })],
    }));
    expect(rounded.rejected).toEqual([]);
    expect(rounded.nextSfx?.[0].gain).toBe(0.4);

    const stale = applyCopilotOps([{ op: "patch_sfx", sfx_index: 0, gain: 0.4 }], extendedCtx({
      sfx: [sfx({ id: "sfx-1", at_s: 1.234, gain: 1.2 })],
    }));
    expect(stale.rejected).toMatchObject([{ reason: "user_changed" }]);
  });

  it("applies overlay ops, accepts suggestions, and rejects stale ids", () => {
    const added = applyCopilotOps(
      [{ op: "add_overlay", asset_id: "asset-1", start_s: 2, end_s: 5, x_frac: 0.7 }],
      extendedCtx(),
    );
    expect(added.nextOverlays?.at(-1)).toMatchObject({
      id: "new-overlay",
      src_gcs_path: "pool/asset.png",
      preview_url: "https://example.com/asset.png",
      position: "custom",
      x_frac: 0.7,
      scale: 0.35,
      z: 1,
    });

    const patched = applyCopilotOps([{ op: "patch_overlay", overlay_index: 0, patch: { x_frac: 0.4 } }], extendedCtx());
    expect(patched.nextOverlays?.[0].x_frac).toBe(0.4);

    const accepted = applyCopilotOps([{ op: "accept_overlay_suggestion", suggestion_id: "suggestion-1" }], extendedCtx());
    expect(accepted.nextOverlays?.at(-1)?.id).toBe("suggested-overlay");
    expect(accepted.acceptedSuggestionRefs).toEqual([{ id: "suggestion-1", overlayId: "suggested-overlay" }]);

    const stale = applyCopilotOps([{ op: "remove_overlay", overlay_index: 0 }], extendedCtx({ overlays: [] }));
    expect(stale.rejected).toMatchObject([{ reason: "target_missing" }]);
  });

  it("applies caption cue and caption meta ops", () => {
    const edit = applyCopilotOps([{ op: "edit_caption", cue_index: 0, text: "new caption" }], extendedCtx());
    expect(edit.textActions).toEqual([{ type: "EDIT_TEXT", id: "caption-1", text: "new caption" }]);

    const timing = applyCopilotOps([{ op: "set_caption_timing", cue_index: 0, start_s: 1.5 }], extendedCtx());
    expect(timing.textActions).toEqual([{ type: "PATCH_BAR", id: "caption-1", patch: { start_s: 1.5, end_s: 2.5 } }]);

    const meta = applyCopilotOps([{ op: "set_caption_meta", patch: { style: "word", y_frac: 0.8 } }], extendedCtx());
    expect(meta.captionMetaPatch).toEqual({ style: "word", y_frac: 0.8 });

    const stale = applyCopilotOps([{ op: "set_caption_meta", patch: { style: "word" } }], extendedCtx({
      captionMeta: { enabled: true, style: "word", font: null, y_frac: 0.7 },
    }));
    expect(stale.rejected).toMatchObject([{ reason: "user_changed" }]);
  });

  it("applies music, mix, title, and open_tool ops with their sub-gates", () => {
    const music = applyCopilotOps([{ op: "swap_music", track_id: "track-2" }], extendedCtx());
    expect(music.nextMusicTrackId).toBe("track-2");

    const same = applyCopilotOps([{ op: "swap_music", track_id: "track-1" }], extendedCtx());
    expect(same.rejected).toMatchObject([{ reason: "no_effect" }]);

    const mix = applyCopilotOps([{ op: "set_mix", music_level: 0.4 }], extendedCtx());
    expect(mix.nextMixLevel).toBe(0.4);

    const title = applyCopilotOps([{ op: "set_title", title: "New title" }], extendedCtx());
    expect(title.nextTitle).toBe("New title");

    const tool = applyCopilotOps([{ op: "open_tool", tool: "sounds" }], extendedCtx());
    expect(tool.openTool).toBe("sounds");

    const noMix = applyCopilotOps([{ op: "set_mix", music_level: 0.4 }], extendedCtx({
      snapshot: { ...extendedCtx().snapshot, mix: undefined },
    }));
    expect(noMix.rejected).toMatchObject([{ reason: "capability_disabled" }]);
  });

  it("maps set_intro_layout to a renderRequest without touching the draft", () => {
    const base = extendedCtx();
    const snapshot = {
      ...base.snapshot,
      allowed_op_families: [...base.snapshot.allowed_op_families, "render" as const],
      intro: {
        layout: "linear" as const,
        mode: "linear",
        text: "what a view today",
        word_count: 4,
        sequence_capable: false,
        cluster_eligible: true,
        switch_blocked_reason: null,
      },
    };
    const res = applyCopilotOps([{ op: "set_intro_layout", layout: "cluster" }], {
      ...base,
      snapshot,
    });

    expect(res.renderRequest).toEqual({ kind: "set_intro_layout", layout: "cluster" });
    expect(res.textActions).toEqual([]);
    expect(res.nextSlots).toBeNull();
    expect(res.nextSfx).toBeUndefined();
    expect(res.nextOverlays).toBeUndefined();
    expect(res.applied).toEqual([
      { label: "Intro layout", from: "Classic", to: "Editorial (re-rendering)" },
    ]);
  });

  it("rejects set_intro_layout for same layout, ineligible cluster, missing intro, and mixed batches", () => {
    const base = extendedCtx();
    const withIntro = {
      ...base.snapshot,
      allowed_op_families: [...base.snapshot.allowed_op_families, "render" as const],
      intro: {
        layout: "linear" as const,
        mode: "linear",
        text: "what a view today",
        word_count: 4,
        sequence_capable: false,
        cluster_eligible: true,
        switch_blocked_reason: null,
      },
    };

    const same = applyCopilotOps([{ op: "set_intro_layout", layout: "linear" }], {
      ...base,
      snapshot: withIntro,
    });
    expect(same.rejected).toMatchObject([{ reason: "no_effect", detail: "intro already uses this layout" }]);

    const ineligible = applyCopilotOps([{ op: "set_intro_layout", layout: "cluster" }], {
      ...base,
      snapshot: {
        ...withIntro,
        intro: { ...withIntro.intro, word_count: 9, cluster_eligible: false },
      },
    });
    expect(ineligible.rejected).toMatchObject([{ reason: "invalid_op", detail: "the editorial layout needs a 3-6 word hook" }]);

    const missing = applyCopilotOps([{ op: "set_intro_layout", layout: "cluster" }], {
      ...base,
      snapshot: { ...base.snapshot, allowed_op_families: [...base.snapshot.allowed_op_families, "render" as const] },
    });
    expect(missing.rejected).toMatchObject([{ reason: "target_missing" }]);

    const mixed = applyCopilotOps(
      [
        { op: "set_intro_layout", layout: "cluster" },
        { op: "edit_text", bar_index: 0, text: "new hook" },
      ],
      { ...base, snapshot: withIntro },
    );
    expect(mixed.renderRequest).toBeUndefined();
    expect(mixed.rejected).toMatchObject([
      {
        op: "set_intro_layout",
        detail: "a layout change re-renders the video — ask for it on its own",
      },
    ]);
  });

  it("aggregates multiple output channels in one call and rejects disabled families", () => {
    const res = applyCopilotOps(
      [
        { op: "edit_caption", cue_index: 0, text: "new caption" },
        { op: "add_sfx", effect_id: "effect-1", at_s: 3 },
        { op: "set_title", title: "New title" },
        { op: "open_tool", tool: "overlays" },
      ],
      extendedCtx(),
    );

    expect(res.textActions).toHaveLength(1);
    expect(res.nextSfx?.at(-1)?.id).toBe("new-sfx");
    expect(res.nextTitle).toBe("New title");
    expect(res.openTool).toBe("overlays");

    const disabled = applyCopilotOps([{ op: "add_sfx", effect_id: "effect-1", at_s: 3 }], ctx());
    expect(disabled.rejected).toMatchObject([{ reason: "capability_disabled" }]);
  });
});

import { consolidateChips } from "@/lib/edit-copilot/apply-ops";

describe("consolidateChips", () => {
  it("drops no-op chips where from equals to", () => {
    expect(
      consolidateChips([
        { label: "Size", from: "65", to: "52" },
        { label: "size_class", from: "default", to: "default" },
      ]),
    ).toEqual([{ label: "Size", from: "65", to: "52" }]);
  });

  it("collapses identical chips into one with a count", () => {
    const out = consolidateChips([
      { label: "effect", from: "fade-in", to: "pop-in" },
      { label: "effect", from: "fade-in", to: "pop-in" },
      { label: "effect", from: "fade-in", to: "pop-in" },
    ]);
    expect(out).toEqual([{ label: "effect", from: "fade-in", to: "pop-in", count: 3 }]);
  });
});

function sfx(over: Partial<SoundEffectPlacement> = {}): SoundEffectPlacement {
  return {
    id: "sfx-1",
    sound_effect_id: "effect-1",
    src_gcs_path: "",
    at_s: 1.234,
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
    x_frac: 0.25,
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
