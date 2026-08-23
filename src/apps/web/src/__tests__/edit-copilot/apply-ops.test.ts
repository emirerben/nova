import { describe, expect, it } from "@jest/globals";
import {
  applyCopilotOps,
  applyCopilotOpsAtomic,
  snapToBeatMark,
} from "@/lib/edit-copilot/apply-ops";
import { buildCopilotSnapshot } from "@/lib/edit-copilot/snapshot";
import type { DraftSlot } from "@/app/generative/timeline-math";
import type { TextElementBar } from "@/lib/timeline/text-timeline-reducer";
import type { MediaOverlay, OverlaySuggestion, PoolAsset, SoundEffectPlacement, VisualBlock } from "@/lib/plan-api";
import { barsToCaptionCues } from "@/app/plan/items/[id]/_editor/editor-bars";
import {
  creatorBlockDurationFramesV2,
  type MotionPresetInstance,
} from "@nova/motion-runtime";

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
  videoDurationS?: number;
  textMotionV2Enabled?: boolean;
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
    videoDurationS: over.videoDurationS,
    textMotionV2Enabled: over.textMotionV2Enabled,
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
  const capabilities = over.capabilities ?? {
    text_elements: true,
    timeline: true,
    split_clips: true,
    sfx: true,
    overlays: true,
  };
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
      removable: true,
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
    snapshot: buildCopilotSnapshot(bars, slots, clips, capabilities, [], extras),
    capabilities,
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

  it("migrates explicit Copilot effects and retimes v2 trims", () => {
    const legacy = ctx({ videoDurationS: 10, textMotionV2Enabled: true });
    const selected = applyCopilotOps(
      [{ op: "patch_text_style", bar_index: 0, patch: { effect: "smooth-type" } }],
      legacy,
    );
    expect(selected.rejected).toEqual([]);
    expect(selected.textActions).toEqual([
      {
        type: "PATCH_BAR",
        id: "bar-1",
        patch: expect.objectContaining({
          effect: "smooth-type",
          motion: expect.objectContaining({ version: 2 }),
        }),
      },
    ]);

    const motion = {
      version: 2 as const,
      speed: 1,
      intensity: 1,
      easing: "ease-out-cubic" as const,
      stagger_ms: 45,
      order: "forward" as const,
      direction: "up" as const,
      travel_px: 10,
      overshoot: 0,
      blur_px: 3,
      cursor: "none" as const,
      cursor_blink_hz: 2,
      hold_s: 2,
      exit_s: 0,
      reveal_ramp_ms: 120,
    };
    const animatedBars = [bar({ effect: "smooth-type", motion, start_s: 0, end_s: 3 })];
    const trimmed = applyCopilotOps(
      [{ op: "set_text_timing", bar_index: 0, end_s: 1 }],
      ctx({ bars: animatedBars, videoDurationS: 10, textMotionV2Enabled: true }),
    );
    expect(trimmed.textActions).toEqual([
      {
        type: "PATCH_BAR",
        id: "bar-1",
        patch: expect.objectContaining({
          end_s: 1,
          motion: expect.objectContaining({ hold_s: expect.any(Number) }),
        }),
      },
    ]);

    const retexted = applyCopilotOps(
      [{ op: "edit_text", bar_index: 0, text: "A much longer animated line" }],
      ctx({ bars: animatedBars, videoDurationS: 10, textMotionV2Enabled: true }),
    );
    expect(retexted.textActions).toEqual([
      {
        type: "PATCH_BAR",
        id: "bar-1",
        patch: expect.objectContaining({
          text: "A much longer animated line",
          end_s: expect.any(Number),
        }),
      },
    ]);

    const disabled = applyCopilotOps(
      [{ op: "patch_text_style", bar_index: 0, patch: { effect: "smooth-type" } }],
      ctx({ textMotionV2Enabled: false }),
    );
    expect(disabled.textActions).toEqual([]);
    expect(disabled.rejected).toEqual([
      expect.objectContaining({ reason: "capability_disabled" }),
    ]);
  });

  it("maps clip timing, reorder, remove, and split ops to slot transforms", () => {
    const duration = applyCopilotOps([{ op: "set_clip_duration", slot_index: 1, duration_s: 3 }], ctx());
    expect(duration.nextSlots?.find((s) => s.key === "b")).toMatchObject({
      inS: 1,
      durationS: 3,
      durationBeats: null,
    });

    const shortDuration = applyCopilotOps(
      [{ op: "set_clip_duration", slot_index: 1, duration_s: 0.2 }],
      ctx(),
    );
    expect(shortDuration.nextSlots?.find((s) => s.key === "b")).toMatchObject({
      durationS: 0.2,
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

  it("distinguishes a source slip from trimming a clip start", () => {
    const slipped = applyCopilotOps(
      [{ op: "set_clip_in", slot_index: 1, in_s: 2 }],
      ctx(),
    );
    expect(slipped.nextSlots?.[1]).toMatchObject({ inS: 2, durationS: 4 });

    const trimmed = applyCopilotOps(
      [{ op: "trim_clip_start", slot_index: 1, start_s: 1 }],
      ctx(),
    );
    expect(trimmed.rejected).toEqual([]);
    expect(trimmed.nextSlots?.[1]).toMatchObject({ inS: 2, durationS: 3 });
  });

  it("trims the assembled output start with right-biased segment selection", () => {
    const atBoundary = applyCopilotOps(
      [{ op: "trim_output_start", start_s: 3 }],
      ctx(),
    );
    expect(atBoundary.rejected).toEqual([]);
    expect(atBoundary.nextSlots?.[0].removed).toBe(true);
    expect(atBoundary.nextSlots?.[1]).toMatchObject({ inS: 1, durationS: 4 });

    const crossing = applyCopilotOps(
      [{ op: "trim_output_start", start_s: 4 }],
      ctx(),
    );
    expect(crossing.nextSlots?.[0].removed).toBe(true);
    expect(crossing.nextSlots?.[1]).toMatchObject({ inS: 2, durationS: 3 });
  });

  it("reports a zero output trim as no_effect without staging timeline state", () => {
    const result = applyCopilotOps(
      [{ op: "trim_output_start", start_s: 0 }],
      ctx(),
    );

    expect(result.nextSlots).toBeNull();
    expect(result.applied).toEqual([]);
    expect(result.rejected).toMatchObject([{ reason: "no_effect" }]);
  });

  it("applies and removes Stadium Diffusion through the slot draft", () => {
    const base = ctx();
    const applied = applyCopilotOps(
      [{ op: "set_look_preset", slot_index: 1, look_preset: "stadium_diffusion" }],
      base,
    );

    expect(applied.rejected).toEqual([]);
    expect(applied.nextSlots?.[1]).toMatchObject({
      lookPreset: "stadium_diffusion",
      lookAdjustments: null,
    });
    expect(applied.applied).toEqual([
      { label: "Clip 2 look", from: "Original", to: "Stadium Diffusion" },
    ]);

    const stadiumSlots = applied.nextSlots ?? base.slots;
    const resetCtx = ctx({ slots: stadiumSlots });
    const reset = applyCopilotOps(
      [{ op: "set_look_preset", slot_index: 1, look_preset: "none" }],
      resetCtx,
    );
    expect(reset.rejected).toEqual([]);
    expect(reset.nextSlots?.[1]).toMatchObject({ lookPreset: "none", lookAdjustments: null });
  });

  it("rejects a stale look suggestion after the user changes that slot", () => {
    const base = ctx();
    const liveSlots = base.slots.map((candidate, index) =>
      index === 1 ? { ...candidate, lookPreset: "olive_film" as const } : candidate,
    );

    const result = applyCopilotOps(
      [{ op: "set_look_preset", slot_index: 1, look_preset: "stadium_diffusion" }],
      { ...base, slots: liveSlots },
    );

    expect(result.nextSlots).toBeNull();
    expect(result.rejected).toMatchObject([
      { op: "set_look_preset", reason: "user_changed" },
    ]);
  });

  it("names a replaced human-only look accurately in the edit receipt", () => {
    const base = ctx({
      slots: ctx().slots.map((candidate, index) =>
        index === 1 ? { ...candidate, lookPreset: "olive_film" as const } : candidate,
      ),
    });

    const result = applyCopilotOps(
      [{ op: "set_look_preset", slot_index: 1, look_preset: "stadium_diffusion" }],
      base,
    );

    expect(result.applied).toEqual([
      { label: "Clip 2 look", from: "Olive Film", to: "Stadium Diffusion" },
    ]);
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

  it("uses operation-level clip and music capabilities as authoritative", () => {
    const transitionContext = ctx();
    transitionContext.snapshot.allowed_op_families.push("transition");
    transitionContext.capabilities = {
      ...transitionContext.capabilities,
      clips: { transitions: { editable: false, reason: "transitions_disabled" } },
    };
    const transition = applyCopilotOps(
      [{ op: "set_transition", boundary_index: 1, transition: "flash", duration_s: 0.3 }],
      transitionContext,
    );
    expect(transition.nextSlots).toBeNull();
    expect(transition.rejected).toMatchObject([
      { reason: "capability_disabled", detail: "transitions_disabled" },
    ]);

    const musicContext = extendedCtx({
      capabilities: {
        text_elements: true,
        timeline: true,
        sfx: true,
        overlays: true,
        music_operations: { level: { editable: false, reason: "level_disabled" } },
      },
    });
    const level = applyCopilotOps([{ op: "set_mix", music_level: 0.2 }], musicContext);
    expect(level.nextMixLevel).toBeUndefined();
    expect(level.rejected).toMatchObject([
      { reason: "capability_disabled", detail: "level_disabled" },
    ]);
  });

  it("keeps Nova clip trim available when all legacy booleans are false", () => {
    const capabilities = {
      text_elements: false,
      timeline: false,
      split_clips: false,
      mix: false,
      sfx: false,
      overlays: false,
      visual_blocks: false,
      motion_scenes: false,
      camera_effects: false,
      clips: { trim: { editable: true, reason: null } },
      nova: { trim_clip_start: { editable: true, reason: null } },
    };
    const result = applyCopilotOps(
      [{ op: "trim_clip_start", slot_index: 1, start_s: 1 }],
      ctx({ capabilities }),
    );

    expect(result.rejected).toEqual([]);
    expect(result.nextSlots?.[1]).toMatchObject({ inS: 2, durationS: 3 });
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

  it("does not stage normalized same-value SFX and overlay patches", () => {
    const sound = applyCopilotOps(
      [{ op: "patch_sfx", sfx_index: 0, at_s: 1.2344, gain: 1 }],
      extendedCtx(),
    );
    expect(sound.nextSfx).toBeUndefined();
    expect(sound.applied).toEqual([]);
    expect(sound.rejected).toMatchObject([{ reason: "no_effect" }]);

    const card = applyCopilotOps(
      [{ op: "patch_overlay", overlay_index: 0, patch: { x_frac: 0.25, y_frac: 0.5 } }],
      extendedCtx(),
    );
    expect(card.nextOverlays).toBeUndefined();
    expect(card.applied).toEqual([]);
    expect(card.rejected).toMatchObject([{ reason: "no_effect" }]);
  });

  it("cascades explicitly grouped generated effects when Copilot removes an overlay", () => {
    const group = "smart-event-1";
    const groupedOverlays = [
      overlay({ source: "smart_captions", effect_group_id: group }),
      overlay({ id: "manual-overlay", source: "manual" }),
    ];
    const groupedSfx = [
      sfx({ id: "linked-sfx", source: "smart_captions", effect_group_id: group }),
      sfx({ id: "manual-sfx", source: "manual", effect_group_id: group }),
    ];
    const groupedCamera = [
      {
        id: "linked-camera",
        start_s: 1,
        end_s: 2,
        intensity: 0.04,
        easing: "sine_pulse" as const,
        source: "smart_captions",
        effect_group_id: group,
      },
    ];
    const context = extendedCtx({
      overlays: groupedOverlays,
      sfx: groupedSfx,
      cameraEffects: groupedCamera,
    });
    context.snapshot = buildCopilotSnapshot(
      context.bars,
      context.slots,
      clips,
      { text_elements: true, timeline: true, sfx: true, overlays: true, camera_effects: true },
      [],
      {
        sfxEnabled: true,
        sfxPlacements: groupedSfx,
        overlaysEnabled: true,
        overlayCards: groupedOverlays,
        cameraEffectsEnabled: true,
        cameraEffects: groupedCamera,
      },
    );
    const result = applyCopilotOps(
      [{ op: "remove_overlay", overlay_index: 0 }],
      context,
    );

    expect(result.nextOverlays?.map((item) => item.id)).toEqual(["manual-overlay"]);
    expect(result.nextSfx?.map((item) => item.id)).toEqual(["manual-sfx"]);
    expect(result.nextCameraEffects).toEqual([]);
  });

  it("groups a Director/Copilot overlay plus newly-created accents", () => {
    const context = extendedCtx({
      capabilities: {
        text_elements: true,
        timeline: true,
        split_clips: true,
        sfx: true,
        overlays: true,
        camera_effects: true,
      },
      makeCameraEffectId: () => "new-camera",
    });
    context.snapshot = buildCopilotSnapshot(
      context.bars,
      context.slots,
      clips,
      context.capabilities ?? {},
      [],
      {
        sfxEnabled: true,
        sfxPlacements: context.sfx,
        sfxCatalog: context.sfxCatalog,
        overlaysEnabled: true,
        overlayCards: context.overlays,
        poolAssets: context.poolAssets,
        cameraEffectsEnabled: true,
        cameraEffects: [],
      },
    );
    const result = applyCopilotOps(
      [
        {
          op: "add_overlay",
          asset_id: "asset-1",
          start_s: 2,
          end_s: 5,
          effect_bundle_id: "reveal-1",
        },
        {
          op: "add_sfx",
          effect_id: "effect-1",
          at_s: 2,
          effect_bundle_id: "reveal-1",
        },
        {
          op: "add_camera_effect",
          start_s: 2,
          end_s: 3,
          effect_bundle_id: "reveal-1",
        },
      ],
      context,
    );

    const group = result.nextOverlays?.at(-1)?.effect_group_id;
    expect(group).toBeTruthy();
    expect(result.nextOverlays?.at(-1)?.source).toBe("edit_ai");
    expect(result.nextSfx?.at(-1)).toMatchObject({ source: "edit_ai", effect_group_id: group });
    expect(result.nextCameraEffects?.at(-1)).toMatchObject({
      id: "new-camera",
      source: "edit_ai",
      effect_group_id: group,
    });

    const unrelated = applyCopilotOps(
      [
        { op: "add_overlay", asset_id: "asset-1", start_s: 2, end_s: 5 },
        { op: "add_sfx", effect_id: "effect-1", at_s: 18 },
        { op: "add_camera_effect", start_s: 18, end_s: 19 },
      ],
      context,
    );
    expect(unrelated.nextOverlays?.at(-1)?.effect_group_id).toBeUndefined();
    expect(unrelated.nextSfx?.at(-1)?.effect_group_id).toBeUndefined();
    expect(unrelated.nextCameraEffects?.at(-1)?.effect_group_id).toBeUndefined();

    const twoBundles = applyCopilotOps(
      [
        {
          op: "add_overlay",
          asset_id: "asset-1",
          start_s: 2,
          end_s: 5,
          effect_bundle_id: "bundle-a",
        },
        {
          op: "add_sfx",
          effect_id: "effect-1",
          at_s: 2,
          effect_bundle_id: "bundle-a",
        },
        {
          op: "add_overlay",
          asset_id: "asset-1",
          start_s: 10,
          end_s: 13,
          effect_bundle_id: "bundle-b",
        },
        {
          op: "add_sfx",
          effect_id: "effect-1",
          at_s: 10,
          effect_bundle_id: "bundle-b",
        },
      ],
      context,
    );
    const addedOverlays = twoBundles.nextOverlays?.slice(-2) ?? [];
    expect(addedOverlays[0].effect_group_id).toBeTruthy();
    expect(addedOverlays[1].effect_group_id).toBeTruthy();
    expect(addedOverlays[0].effect_group_id).not.toBe(addedOverlays[1].effect_group_id);

    const ambiguous = applyCopilotOps(
      [
        {
          op: "add_overlay",
          asset_id: "asset-1",
          start_s: 2,
          end_s: 5,
          effect_bundle_id: "ambiguous",
        },
        {
          op: "add_overlay",
          asset_id: "asset-1",
          start_s: 6,
          end_s: 9,
          effect_bundle_id: "ambiguous",
        },
        {
          op: "add_sfx",
          effect_id: "effect-1",
          at_s: 2,
          effect_bundle_id: "ambiguous",
        },
      ],
      context,
    );
    expect(ambiguous.nextOverlays?.slice(-2).every((item) => !item.effect_group_id)).toBe(true);
    expect(ambiguous.nextSfx?.at(-1)?.effect_group_id).toBeUndefined();

    const rejectedSibling = applyCopilotOps(
      [
        {
          op: "add_overlay",
          asset_id: "asset-1",
          start_s: 2,
          end_s: 5,
          effect_bundle_id: "partial",
        },
        { op: "add_sfx", effect_id: "missing", at_s: 2, effect_bundle_id: "partial" },
      ],
      context,
    );
    expect(rejectedSibling.nextOverlays?.at(-1)?.effect_group_id).toBeUndefined();
  });

  it("applies caption cue and caption meta ops", () => {
    const edit = applyCopilotOps([{ op: "edit_caption", cue_index: 0, text: "new caption" }], extendedCtx());
    expect(edit.textActions).toEqual([{ type: "EDIT_TEXT", id: "caption-1", text: "new caption" }]);

    const timing = applyCopilotOps([{ op: "set_caption_timing", cue_index: 0, start_s: 1.5 }], extendedCtx());
    expect(timing.textActions).toEqual([{ type: "PATCH_BAR", id: "caption-1", patch: { start_s: 1.5, end_s: 2.5 } }]);

    // set_caption_emphasis: turning it on derives smart_style from the cue's
    // own role (mirrors the editor's Emphasize toggle, editor-bars.ts
    // smartStyleForRole); turning it off clears both.
    const hookBars = [
      bar({ id: "caption-1", role: "narrated_caption", text: "old caption", start_s: 1.0, end_s: 2.0, smart_role: "hook" }),
    ];
    const hookSlots = [slot({ key: "a", slotId: "a", durationS: 3 })];
    const hookExtras: Parameters<typeof buildCopilotSnapshot>[5] = {
      captionsPresent: true,
      captionMeta: { enabled: true, style: "sentence", font: null, y_frac: 0.7 },
    };
    const hookCtx = {
      bars: hookBars,
      slots: hookSlots,
      snapshot: buildCopilotSnapshot(hookBars, hookSlots, clips, { text_elements: true, timeline: true }, [], hookExtras),
    };
    const emphasize = applyCopilotOps(
      [{ op: "set_caption_emphasis", cue_index: 0, emphasis: true }],
      hookCtx,
    );
    expect(emphasize.textActions).toEqual([
      { type: "PATCH_BAR", id: "caption-1", patch: { smart_emphasis: true, smart_style: "hook" } },
    ]);
    expect(emphasize.rejected).toEqual([]);

    const emphasizedBars = [
      bar({
        id: "caption-1",
        role: "narrated_caption",
        text: "old caption",
        start_s: 1.0,
        end_s: 2.0,
        smart_role: "hook",
        smart_emphasis: true,
      }),
    ];
    const emphasizedCtx = {
      bars: emphasizedBars,
      slots: hookSlots,
      snapshot: buildCopilotSnapshot(emphasizedBars, hookSlots, clips, { text_elements: true, timeline: true }, [], hookExtras),
    };
    const clear = applyCopilotOps(
      [{ op: "set_caption_emphasis", cue_index: 0, emphasis: false }],
      emphasizedCtx,
    );
    expect(clear.textActions).toEqual([
      { type: "PATCH_BAR", id: "caption-1", patch: { smart_emphasis: false, smart_style: null } },
    ]);

    // Drift: emphasis was toggled locally after the snapshot was read.
    const driftedBars = [
      bar({
        id: "caption-1",
        role: "narrated_caption",
        text: "old caption",
        start_s: 1.0,
        end_s: 2.0,
        smart_role: "hook",
        smart_emphasis: true,
      }),
    ];
    const drifted = applyCopilotOps(
      [{ op: "set_caption_emphasis", cue_index: 0, emphasis: true }],
      { bars: driftedBars, slots: hookSlots, snapshot: hookCtx.snapshot },
    );
    expect(drifted.textActions).toEqual([]);
    expect(drifted.rejected).toMatchObject([{ reason: "user_changed" }]);

    const meta = applyCopilotOps([{ op: "set_caption_meta", patch: { style: "word", y_frac: 0.8 } }], extendedCtx());
    expect(meta.captionMetaPatch).toEqual({ style: "word", y_frac: 0.8 });

    const stale = applyCopilotOps([{ op: "set_caption_meta", patch: { style: "word" } }], extendedCtx({
      captionMeta: { enabled: true, style: "word", font: null, y_frac: 0.7 },
    }));
    expect(stale.rejected).toMatchObject([{ reason: "user_changed" }]);
  });

  it("atomically replaces every literal caption match beyond snapshot caps and persists it", () => {
    const captions = Array.from({ length: 55 }, (_, index) => bar({
      id: `caption-${index}`,
      role: "narrated_caption",
      text:
        index === 2
          ? "Kriya KRIYA kriya"
          : index >= 40 && index < 50
            ? "Say Kriya here"
            : index === 54
              ? `${"x".repeat(90)} Kriya`
              : `caption ${index}`,
      start_s: index,
      end_s: index + 0.5,
    }));
    const slots = [slot({ key: "caption-slot", slotId: "caption-slot", durationS: 60 })];
    const extras: Parameters<typeof buildCopilotSnapshot>[5] = {
      captionsPresent: true,
      captionMeta: { enabled: true, style: "sentence", font: null, y_frac: 0.7 },
    };
    const snapshot = buildCopilotSnapshot(captions, slots, clips, {}, [], extras);
    expect(snapshot.captions?.cues).toHaveLength(40);
    expect(snapshot.captions?.cues.some((cue) => cue.id === "caption-54")).toBe(false);

    const result = applyCopilotOps(
      [{ op: "replace_caption_text", find: "Kriya", replace: "Kria" }],
      { bars: captions, slots, snapshot },
    );

    expect(result.rejected).toEqual([]);
    expect(result.textActions).toHaveLength(1);
    expect(result.textActions[0]).toMatchObject({ type: "PATCH_BARS" });
    const action = result.textActions[0];
    if (action.type !== "PATCH_BARS") throw new Error("expected PATCH_BARS");
    expect(action.patches).toHaveLength(12);
    expect(result.applied).toEqual([{
      label: "Caption text replaced",
      from: "Kriya",
      to: "Kria · 14 matches in 12 lines",
    }]);

    const patchById = new Map(action.patches.map((patch) => [patch.id, patch.patch]));
    const updated = captions.map((caption) => ({
      ...caption,
      ...(patchById.get(caption.id) ?? {}),
    }));
    expect(updated[2].text).toBe("Kria Kria Kria");
    expect(updated[54].text.endsWith(" Kria")).toBe(true);
    expect(barsToCaptionCues(updated)[54].text.endsWith(" Kria")).toBe(true);

    const secondSnapshot = buildCopilotSnapshot(updated, slots, clips, {}, [], extras);
    const second = applyCopilotOps(
      [{ op: "replace_caption_text", find: "Kriya", replace: "Kria" }],
      { bars: updated, slots, snapshot: secondSnapshot },
    );
    expect(second.textActions).toEqual([]);
    expect(second.rejected).toMatchObject([{ reason: "no_effect" }]);
  });

  it("treats replacement dollars literally and rejects a stale full-caption snapshot", () => {
    const captions = [
      bar({ id: "caption-1", role: "narrated_caption", text: "Kriya", start_s: 0, end_s: 1 }),
      bar({ id: "caption-2", role: "narrated_caption", text: "untouched", start_s: 1, end_s: 2 }),
    ];
    const slots = [slot()];
    const extras: Parameters<typeof buildCopilotSnapshot>[5] = {
      captionsPresent: true,
      captionMeta: { enabled: true, style: "sentence", font: null, y_frac: 0.7 },
    };
    const snapshot = buildCopilotSnapshot(captions, slots, clips, {}, [], extras);
    const literal = applyCopilotOps(
      [{ op: "replace_caption_text", find: "Kriya", replace: "$& $1" }],
      { bars: captions, slots, snapshot },
    );
    expect(literal.textActions).toEqual([{
      type: "PATCH_BARS",
      patches: [{ id: "caption-1", patch: { text: "$& $1" } }],
    }]);

    const caseOnlyBars = captions.map((caption) =>
      caption.id === "caption-2" ? { ...caption, text: "KRIYA" } : caption,
    );
    const caseOnlySnapshot = buildCopilotSnapshot(caseOnlyBars, slots, clips, {}, [], extras);
    const caseOnly = applyCopilotOps(
      [{ op: "replace_caption_text", find: "Kriya", replace: "Kriya" }],
      { bars: caseOnlyBars, slots, snapshot: caseOnlySnapshot },
    );
    expect(caseOnly.textActions).toEqual([{
      type: "PATCH_BARS",
      patches: [{ id: "caption-2", patch: { text: "Kriya" } }],
    }]);
    expect(caseOnly.applied[0].to).toBe("Kriya · 1 match in 1 line");

    const drifted = captions.map((caption) =>
      caption.id === "caption-2" ? { ...caption, text: "changed after request" } : caption,
    );
    const stale = applyCopilotOps(
      [{ op: "replace_caption_text", find: "Kriya", replace: "Kria" }],
      { bars: drifted, slots, snapshot },
    );
    expect(stale.textActions).toEqual([]);
    expect(stale.rejected).toMatchObject([{ reason: "user_changed" }]);
  });

  it("applies music, mix, title, and open_tool ops with their sub-gates", () => {
    const music = applyCopilotOps([{ op: "swap_music", track_id: "track-2" }], extendedCtx());
    expect(music.nextMusicTrackId).toBe("track-2");

    const same = applyCopilotOps([{ op: "swap_music", track_id: "track-1" }], extendedCtx());
    expect(same.rejected).toMatchObject([{ reason: "no_effect" }]);

    const removed = applyCopilotOps([{ op: "remove_music" }], extendedCtx());
    expect(removed.musicRemoved).toBe(true);
    expect(removed.applied).toEqual([
      { label: "Music", from: "Current", to: "removed" },
    ]);

    const muted = applyCopilotOps([{ op: "set_mix", music_level: 0 }], extendedCtx());
    expect(muted.nextMixLevel).toBe(0);
    expect(muted.musicRemoved).toBeUndefined();

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

  it("undo_last_edit signals historyAction 'undo' without touching the draft when canUndoLastTurn is true", () => {
    const base = extendedCtx();
    const res = applyCopilotOps([{ op: "undo_last_edit" }], { ...base, canUndoLastTurn: true });

    expect(res.historyAction).toBe("undo");
    expect(res.textActions).toEqual([]);
    expect(res.nextSlots).toBeNull();
    expect(res.applied).toEqual([]);
    expect(res.rejected).toEqual([]);
    expect(res.renderRequest).toBeUndefined();
  });

  it("undo_last_edit rejects with 'nothing safe to undo' when the staleness guard fails", () => {
    const base = extendedCtx();
    // canUndoLastTurn omitted (undefined) — same as a stale undoVersion, a
    // manual panel edit having moved the stack, or a fresh session with no
    // prior turn at all.
    const res = applyCopilotOps([{ op: "undo_last_edit" }], { ...base });

    expect(res.historyAction).toBeUndefined();
    expect(res.applied).toEqual([]);
    expect(res.rejected).toMatchObject([
      { op: "undo_last_edit", reason: "no_effect", detail: "nothing safe to undo" },
    ]);

    const explicitlyFalse = applyCopilotOps([{ op: "undo_last_edit" }], {
      ...base,
      canUndoLastTurn: false,
    });
    expect(explicitlyFalse.rejected).toMatchObject([
      { op: "undo_last_edit", reason: "no_effect", detail: "nothing safe to undo" },
    ]);
  });

  it("undo_last_edit is single-op-only, like set_intro_layout", () => {
    const base = extendedCtx();
    const res = applyCopilotOps(
      [
        { op: "undo_last_edit" },
        { op: "edit_text", bar_index: 0, text: "new hook" },
      ],
      { ...base, canUndoLastTurn: true },
    );
    expect(res.historyAction).toBeUndefined();
    expect(res.rejected).toMatchObject([
      { op: "undo_last_edit", reason: "capability_disabled" },
    ]);
  });

  it("undo_last_edit drops when the history family is not in allowed_op_families", () => {
    const base = extendedCtx();
    const res = applyCopilotOps([{ op: "undo_last_edit" }], {
      ...base,
      canUndoLastTurn: true,
      snapshot: {
        ...base.snapshot,
        allowed_op_families: base.snapshot.allowed_op_families.filter((f) => f !== "history"),
      },
    });
    expect(res.rejected).toMatchObject([{ op: "undo_last_edit", reason: "capability_disabled" }]);
  });

  it("repeat_last_edit rejects with 'nothing to repeat yet' when lastAppliedOps is empty", () => {
    const base = extendedCtx();
    const res = applyCopilotOps([{ op: "repeat_last_edit" }], { ...base });
    expect(res.historyAction).toBeUndefined();
    expect(res.rejected).toMatchObject([
      { op: "repeat_last_edit", reason: "no_effect", detail: "nothing to repeat yet" },
    ]);
  });

  it("repeat_last_edit is single-op-only and available even when undo is stale", () => {
    const base = extendedCtx();
    const lastAppliedOps = [{ op: "edit_text" as const, bar_index: 0, text: "second take" }];

    const mixed = applyCopilotOps(
      [
        { op: "repeat_last_edit" },
        { op: "set_title", title: "new title" },
      ],
      { ...base, lastAppliedOps, canUndoLastTurn: true },
    );
    expect(mixed.historyAction).toBeUndefined();
    expect(mixed.rejected).toMatchObject([{ op: "repeat_last_edit", reason: "capability_disabled" }]);

    // repeat's own staleness gating is per-op fingerprint validation on the
    // CURRENT snapshot, not the undo stack — canUndoLastTurn:false must not
    // block it.
    const staleUndo = applyCopilotOps([{ op: "repeat_last_edit" }], {
      ...base,
      lastAppliedOps,
      canUndoLastTurn: false,
    });
    expect(staleUndo.historyAction).toEqual({ kind: "repeat", ops: lastAppliedOps });
    expect(staleUndo.textActions).toEqual([{ type: "EDIT_TEXT", id: "bar-1", text: "second take" }]);
    expect(staleUndo.applied).toEqual([{ label: "Text 1", from: "old hook", to: "second take" }]);
  });

  it("repeat_last_edit re-runs the prior ops against the CURRENT snapshot and merges the mutation", () => {
    const base = extendedCtx();
    const lastAppliedOps = [{ op: "set_title" as const, title: "repeated title" }];
    const res = applyCopilotOps([{ op: "repeat_last_edit" }], {
      ...base,
      lastAppliedOps,
      canUndoLastTurn: true,
    });
    expect(res.nextTitle).toBe("repeated title");
    expect(res.applied).toEqual([{ label: "Title set", from: "Old title", to: "repeated title" }]);
    expect(res.historyAction).toEqual({ kind: "repeat", ops: lastAppliedOps });
    // Provenance stays flat — appliedOps carries the ORIGINAL set_title op,
    // never the repeat_last_edit wrapper (a later repeat must not recurse on
    // itself).
    expect(res.appliedOps).toEqual(lastAppliedOps);
  });

  it("repeat_last_edit rejects the WHOLE turn when any re-run op fails fingerprint validation, surfacing the rejection normally", () => {
    const base = extendedCtx();
    // The prior turn's edit_text op targets bar 0. Simulate the CURRENT
    // snapshot no longer matching the live bar text (e.g. a manual panel
    // edit landed between when this turn's snapshot was captured and when
    // it's applied) — edit_text's mutation-fingerprint check must fail
    // exactly like it would for a fresh (non-repeat) op.
    const lastAppliedOps = [{ op: "edit_text" as const, bar_index: 0, text: "stale rewrite" }];
    const staleSnapshot = {
      ...base.snapshot,
      text_bars: base.snapshot.text_bars.map((bar, i) =>
        i === 0 ? { ...bar, text: "a different value than the live bar" } : bar,
      ),
    };
    const res = applyCopilotOps([{ op: "repeat_last_edit" }], {
      ...base,
      snapshot: staleSnapshot,
      lastAppliedOps,
      canUndoLastTurn: true,
    });
    expect(res.historyAction).toBeUndefined();
    expect(res.textActions).toEqual([]);
    expect(res.nextTitle).toBeUndefined();
    expect(res.rejected).toMatchObject([
      { op: "edit_text", reason: "user_changed", detail: "text was changed after Kria read it" },
    ]);
  });

  it("repeat_last_edit drops when the history family is not in allowed_op_families", () => {
    const base = extendedCtx();
    const lastAppliedOps = [{ op: "set_title" as const, title: "repeated title" }];
    const res = applyCopilotOps([{ op: "repeat_last_edit" }], {
      ...base,
      lastAppliedOps,
      canUndoLastTurn: true,
      snapshot: {
        ...base.snapshot,
        allowed_op_families: base.snapshot.allowed_op_families.filter((f) => f !== "history"),
      },
    });
    expect(res.rejected).toMatchObject([{ op: "repeat_last_edit", reason: "capability_disabled" }]);
  });

  it("maps apply_custom_effect to a renderRequest without touching the draft", () => {
    const base = extendedCtx();
    const snapshot = {
      ...base.snapshot,
      allowed_op_families: [...base.snapshot.allowed_op_families, "custom_effect" as const],
    };
    const effect = {
      id: "vintage_1",
      label: "Vintage film",
      filters: [{ name: "curves", params: { preset: "vintage" } }],
      start_s: 0,
      end_s: 5,
      target: "full_frame",
    };
    const res = applyCopilotOps([{ op: "apply_custom_effect", effect }], {
      ...base,
      snapshot,
    });

    expect(res.renderRequest).toEqual({ kind: "apply_custom_effect", effect });
    expect(res.textActions).toEqual([]);
    expect(res.nextSlots).toBeNull();
    expect(res.applied).toEqual([
      { label: "Custom effect", from: "current look", to: "new look (re-rendering)" },
    ]);
  });

  it("rejects apply_custom_effect when the family isn't allowed or it's batched with another op", () => {
    const base = extendedCtx();
    const withFamily = {
      ...base.snapshot,
      allowed_op_families: [...base.snapshot.allowed_op_families, "custom_effect" as const],
    };
    const effect = {
      id: "vintage_1",
      label: "Vintage film",
      filters: [{ name: "curves", params: { preset: "vintage" } }],
      start_s: 0,
      end_s: 5,
      target: "full_frame",
    };

    const notAllowed = applyCopilotOps([{ op: "apply_custom_effect", effect }], base);
    expect(notAllowed.renderRequest).toBeUndefined();
    expect(notAllowed.rejected).not.toEqual([]);

    const mixed = applyCopilotOps(
      [
        { op: "apply_custom_effect", effect },
        { op: "edit_text", bar_index: 0, text: "new hook" },
      ],
      { ...base, snapshot: withFamily },
    );
    expect(mixed.renderRequest).toBeUndefined();
    expect(mixed.rejected).toMatchObject([
      {
        op: "apply_custom_effect",
        detail: "a custom effect re-renders the video — ask for it on its own",
      },
    ]);

    // Never shares set_intro_layout's "render" family — a variant eligible
    // for intro-layout switching alone must not unlock custom effects too.
    const renderOnly = applyCopilotOps([{ op: "apply_custom_effect", effect }], {
      ...base,
      snapshot: {
        ...base.snapshot,
        allowed_op_families: [...base.snapshot.allowed_op_families, "render" as const],
      },
    });
    expect(renderOnly.renderRequest).toBeUndefined();
  });

  it("applies a validated set_carousel_moment add as a staged draft mutation — Lane D", () => {
    // Lane D (carousel-blocks train): set_carousel_moment is now a first-class
    // draft mutation like patch_overlay/set_visual_fade — no renderRequest, no
    // single-op restriction, applied straight into nextCarouselMoment.
    const base = extendedCtx();
    const snapshot = {
      ...base.snapshot,
      allowed_op_families: [...base.snapshot.allowed_op_families, "carousel" as const],
      carousel: { eligible: true, reason: null, current: null, n_clips: 4 },
    };
    const res = applyCopilotOps(
      [{ op: "set_carousel_moment", config: { position: "intro" } }],
      { ...base, snapshot },
    );

    expect(res.renderRequest).toBeUndefined();
    expect(res.rejected).toEqual([]);
    expect(res.nextCarouselMoment).toEqual({ position: "intro" });
    expect(res.applied).toMatchObject([
      { label: "Carousel", from: "none", to: "intro" },
    ]);
  });

  it("merges a partial config over the shell's live staged value, not just the persisted snapshot", () => {
    // ctx.carouselMoment (Lane C's staged-or-persisted state) is the seed the
    // merge and no-op check compare against — an unsaved panel edit this
    // session must be visible to the copilot even though the snapshot's own
    // `carousel.current` still reflects what's on disk.
    const base = extendedCtx();
    const snapshot = {
      ...base.snapshot,
      allowed_op_families: [...base.snapshot.allowed_op_families, "carousel" as const],
      carousel: {
        eligible: true,
        reason: null,
        current: { position: "outro" as const, mode: null, effect: null, focus_clip_index: null, duration_s: null, transition: null },
        n_clips: 4,
      },
    };
    const staged = {
      position: "intro" as const,
      mode: "focus" as const,
      effect: "cover_flow" as const,
      focus_clip_index: 1,
      duration_s: 5,
      transition: "crossfade" as const,
    };
    const res = applyCopilotOps(
      [{ op: "set_carousel_moment", config: { effect: "flipbook" } }],
      { ...base, snapshot, carouselMoment: staged },
    );

    expect(res.nextCarouselMoment).toEqual({ ...staged, effect: "flipbook" });

    // The identical field on the STAGED value is a no-op even though the
    // (stale) persisted snapshot.carousel.current disagrees.
    const noop = applyCopilotOps(
      [{ op: "set_carousel_moment", config: { position: "intro" } }],
      { ...base, snapshot, carouselMoment: staged },
    );
    expect(noop.rejected).toMatchObject([
      { reason: "no_effect", detail: "carousel already matches this configuration" },
    ]);
  });

  it("applies a validated set_carousel_moment removal the same way", () => {
    const base = extendedCtx();
    const snapshot = {
      ...base.snapshot,
      allowed_op_families: [...base.snapshot.allowed_op_families, "carousel" as const],
      carousel: {
        eligible: true,
        reason: null,
        current: {
          position: "intro" as const,
          mode: "focus" as const,
          effect: "cover_flow" as const,
          focus_clip_index: 0,
          duration_s: 4,
          transition: "crossfade" as const,
        },
        n_clips: 4,
      },
    };
    const res = applyCopilotOps([{ op: "set_carousel_moment", config: null }], {
      ...base,
      snapshot,
    });

    expect(res.renderRequest).toBeUndefined();
    expect(res.rejected).toEqual([]);
    expect(res.nextCarouselMoment).toBeNull();
    expect(res.applied).toMatchObject([
      { label: "Carousel", to: "removed" },
    ]);
  });

  it("rejects set_carousel_moment for no-op config, ineligible add, missing section, and empty removal — but allows combining with other ops", () => {
    const base = extendedCtx();
    const current = {
      position: "intro" as const,
      mode: "focus" as const,
      effect: "cover_flow" as const,
      focus_clip_index: 0,
      duration_s: 4,
      transition: "crossfade" as const,
    };
    const withCarousel = {
      ...base.snapshot,
      allowed_op_families: [...base.snapshot.allowed_op_families, "carousel" as const],
      carousel: { eligible: true, reason: null, current, n_clips: 4 },
    };

    const noop = applyCopilotOps(
      [{ op: "set_carousel_moment", config: { position: "intro", mode: "focus" } }],
      { ...base, snapshot: withCarousel },
    );
    expect(noop.rejected).toMatchObject([
      { reason: "no_effect", detail: "carousel already matches this configuration" },
    ]);

    const ineligible = applyCopilotOps(
      [{ op: "set_carousel_moment", config: { position: "outro" } }],
      {
        ...base,
        snapshot: {
          ...withCarousel,
          carousel: { eligible: false, reason: "Needs at least 2 clips", current: null, n_clips: 1 },
        },
      },
    );
    expect(ineligible.rejected).toMatchObject([
      { reason: "capability_disabled", detail: "Needs at least 2 clips" },
    ]);

    const missing = applyCopilotOps(
      [{ op: "set_carousel_moment", config: { position: "intro" } }],
      {
        ...base,
        snapshot: { ...base.snapshot, allowed_op_families: [...base.snapshot.allowed_op_families, "carousel" as const] },
      },
    );
    expect(missing.rejected).toMatchObject([{ reason: "target_missing" }]);

    const emptyRemoval = applyCopilotOps(
      [{ op: "set_carousel_moment", config: null }],
      {
        ...base,
        snapshot: { ...withCarousel, carousel: { eligible: true, reason: null, current: null, n_clips: 4 } },
      },
    );
    expect(emptyRemoval.rejected).toMatchObject([
      { reason: "no_effect", detail: "no carousel to remove" },
    ]);

    // Unlike set_intro_layout, a carousel edit composes freely with other ops
    // in the same turn — no single-op rejection.
    const mixed = applyCopilotOps(
      [
        { op: "set_carousel_moment", config: { position: "outro" } },
        { op: "edit_text", bar_index: 0, text: "new hook" },
      ],
      { ...base, snapshot: withCarousel },
    );
    expect(mixed.renderRequest).toBeUndefined();
    expect(mixed.rejected).toEqual([]);
    expect(mixed.nextCarouselMoment).toEqual({ ...current, position: "outro" });
    expect(mixed.textActions).toEqual([{ type: "EDIT_TEXT", id: "bar-1", text: "new hook" }]);
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

describe("meta-only captions (subtitled)", () => {
  function subtitledCtx() {
    const bars = [bar()];
    const slots = [slot({ key: "a", slotId: "a", durationS: 21 })];
    const captionMeta = { enabled: true, style: "sentence" as const, font: null, y_frac: 0.8 };
    return {
      bars,
      slots,
      snapshot: buildCopilotSnapshot(bars, slots, clips, { text_elements: true, timeline: true }, [], {
        captionsPresent: true,
        captionMeta,
        captionCuesEditable: false,
        captionTotalCues: 14,
      }),
      capabilities: { text_elements: true, timeline: true, split_clips: true },
      captionMeta,
      makeTextBarId: () => "new-text",
      makeSlotKey: (s: DraftSlot) => `${s.key}-split`,
    };
  }

  it("applies set_caption_meta style flips", () => {
    const result = applyCopilotOps(
      [{ op: "set_caption_meta", patch: { style: "word" } }],
      subtitledCtx(),
    );
    expect(result.rejected).toEqual([]);
    expect(result.captionMetaPatch).toMatchObject({ style: "word" });
  });

  it("rejects cue text/timing edits when no editable cue list is present", () => {
    const edit = applyCopilotOps(
      [{ op: "edit_caption", cue_index: 0, text: "fixed" }],
      subtitledCtx(),
    );
    expect(edit.textActions).toEqual([]);
    expect(edit.rejected).toEqual([
      expect.objectContaining({ detail: "This draft has caption settings but no editable cue list." }),
    ]);

    const timing = applyCopilotOps(
      [{ op: "set_caption_timing", cue_index: 0, start_s: 1.0 }],
      subtitledCtx(),
    );
    expect(timing.textActions).toEqual([]);
    expect(timing.rejected).toEqual([
      expect.objectContaining({ detail: "This draft has caption settings but no editable cue list." }),
    ]);
  });
});

describe("beat mark snapping", () => {
  const GRID = [0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0];

  function beatCtx() {
    const bars = [bar(), bar({ id: "bar-2", text: "second", start_s: 3, end_s: 5 })];
    const slots = [slot({ key: "a", slotId: "a", durationBeats: 6, durationS: null })];
    const sfxPlacements = [sfx({ at_s: 1.7, gain: 1 })];
    const sfxCatalog = [effect()];
    const extras: Parameters<typeof buildCopilotSnapshot>[5] = {
      sfxEnabled: true,
      sfxPlacements,
      sfxCatalog,
    };
    return {
      bars,
      slots,
      grid: GRID,
      snapshot: buildCopilotSnapshot(
        bars,
        slots,
        clips,
        { text_elements: true, timeline: true, sfx: true },
        GRID,
        extras,
      ),
      capabilities: { text_elements: true, timeline: true, split_clips: true, sfx: true },
      sfx: sfxPlacements,
      sfxCatalog,
      makeTextBarId: () => "new-text",
      makeSlotKey: (s: DraftSlot) => `${s.key}-split`,
      makeSfxPlacementId: () => "new-sfx",
    };
  }

  it("exposes beat_marks on the snapshot for grid slots", () => {
    expect(beatCtx().snapshot.beat_marks).toEqual([0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0]);
  });

  it("snaps near-miss text timings onto the closest beat mark", () => {
    const result = applyCopilotOps(
      [{ op: "set_text_timing", bar_index: 0, start_s: 0.55, end_s: 2.93 }],
      beatCtx(),
    );
    expect(result.rejected).toEqual([]);
    expect(result.textActions).toEqual([
      { type: "PATCH_BAR", id: "bar-1", patch: { start_s: 0.5, end_s: 3.0 } },
    ]);
  });

  it("snaps add_sfx onto a beat but leaves deliberate off-beat times alone", () => {
    const snapped = applyCopilotOps(
      [{ op: "add_sfx", effect_id: "effect-1", at_s: 2.45, gain: 1 }],
      beatCtx(),
    );
    expect(snapped.rejected).toEqual([]);
    expect(snapped.nextSfx?.[1]?.at_s).toBe(2.5);

    const offBeat = applyCopilotOps(
      [{ op: "add_sfx", effect_id: "effect-1", at_s: 1.73, gain: 1 }],
      beatCtx(),
    );
    expect(offBeat.rejected).toEqual([]);
    expect(offBeat.nextSfx?.[1]?.at_s).toBe(1.73);
  });

  it("snaps patch_sfx near-miss moves onto the beat", () => {
    const result = applyCopilotOps(
      [{ op: "patch_sfx", sfx_index: 0, at_s: 1.05 }],
      beatCtx(),
    );
    expect(result.rejected).toEqual([]);
    expect(result.nextSfx?.[0]?.at_s).toBe(1.0);
  });

  it("pins the snap epsilon boundary, ties, and empty-marks behavior", () => {
    const marks = [1.0, 2.0];
    expect(snapToBeatMark(1.119, marks)).toBe(1.0); // inside epsilon: snaps
    expect(snapToBeatMark(1.121, marks)).toBe(1.121); // just past: untouched
    expect(snapToBeatMark(1.5, marks)).toBe(1.5); // equidistant but outside epsilon
    expect(snapToBeatMark(0.95, [])).toBe(0.95);
    expect(snapToBeatMark(0.95, undefined)).toBe(0.95);
  });

  it("stops snapping after a clip-timeline mutation in the same bundle", () => {
    const result = applyCopilotOps(
      [
        { op: "set_clip_duration", slot_index: 0, duration_s: 2.0 },
        { op: "add_sfx", effect_id: "effect-1", at_s: 2.45, gain: 1 },
      ],
      beatCtx(),
    );
    expect(result.rejected).toEqual([]);
    // The clip trim shifted the timeline; the stale 2.5 mark must NOT attract
    // the SFX — the raw time passes through instead.
    expect(result.nextSfx?.[1]?.at_s).toBe(2.45);
  });

  it("re-clamps an SFX snapped onto the terminal beat mark", () => {
    // Validator clamps 2.95 → 2.9, snap would pull it to the 3.0 end mark —
    // the post-snap clamp keeps it inside the audible window.
    const result = applyCopilotOps(
      [{ op: "add_sfx", effect_id: "effect-1", at_s: 2.95, gain: 1 }],
      beatCtx(),
    );
    expect(result.rejected).toEqual([]);
    expect(result.nextSfx?.[1]?.at_s).toBe(2.9);
  });

  it("keeps a deliberately short span from collapsing onto one mark", () => {
    const result = applyCopilotOps(
      [{ op: "set_text_timing", bar_index: 0, start_s: 0.45, end_s: 0.55 }],
      beatCtx(),
    );
    expect(result.rejected).toEqual([]);
    const patch = (result.textActions[0] as { patch: { start_s: number; end_s: number } }).patch;
    expect(patch.start_s).toBe(0.5);
    expect(patch.end_s).toBeGreaterThan(patch.start_s);
  });

  it("snaps overlay spans onto beat marks", () => {
    const base = beatCtx();
    const withOverlays = {
      ...base,
      snapshot: {
        ...base.snapshot,
        allowed_op_families: [...base.snapshot.allowed_op_families, "overlay" as const],
        overlays: { cards: [], asset_pool: [{ id: "asset-1", kind: "image" as const, subject: null, duration_s: null }], pending_suggestions: [] },
      },
      capabilities: { ...base.capabilities, overlays: true },
      overlays: [],
      poolAssets: [asset()],
      makeOverlayId: () => "new-overlay",
    };
    const result = applyCopilotOps(
      [{ op: "add_overlay", asset_id: "asset-1", start_s: 0.55, end_s: 2.45 }],
      withOverlays,
    );
    expect(result.rejected).toEqual([]);
    expect(result.nextOverlays?.[0]).toMatchObject({ start_s: 0.5, end_s: 2.5 });
  });

  it("does not snap when the snapshot has no beat marks", () => {
    const noGrid = ctx();
    expect(noGrid.snapshot.beat_marks).toBeUndefined();
    const result = applyCopilotOps(
      [{ op: "set_text_timing", bar_index: 0, start_s: 0.55, end_s: 2.93 }],
      noGrid,
    );
    expect(result.textActions).toEqual([
      { type: "PATCH_BAR", id: "bar-1", patch: { start_s: 0.55, end_s: 2.93 } },
    ]);
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
    width: 1080,
    height: 1920,
    subject: "coffee pour",
    user_context: "",
    nova_description: null,
    nova_on_screen_text: null,
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

describe("Director editor operations", () => {
  const cameraEffect = {
    id: "camera-1",
    token: "semantic_crop_pulse",
    start_s: 0.5,
    end_s: 1.5,
    intensity: 0.04,
    easing: "sine_pulse" as const,
    source: "user",
  };
  const visualBlock: VisualBlock = {
    version: 1,
    id: "visual-1",
    kind: "montage",
    start_s: 2,
    end_s: 5,
    timing_mode: "manual",
    origin: "user",
    transition_in: "cut",
    transition_out: "cut",
    audio_policy: { base: "continue", sfx: "continue" },
    shots: [],
  };

  function directorCtx() {
    const slots = [
      slot({ key: "a", slotId: "a", durationS: 3 }),
      slot({
        key: "b",
        slotId: "b",
        clipIndex: 1,
        durationS: 3,
        lookPreset: "olive_film",
        lookAdjustments: {
          intensity: 0.7,
          warmth: 0.2,
          contrast: -0.1,
          grain: 0.3,
          vignette: 0.4,
        },
      }),
      slot({ key: "c", slotId: "c", clipIndex: 2, durationS: 3 }),
    ];
    const bars = [bar()];
    return {
      bars,
      slots,
      snapshot: buildCopilotSnapshot(
        bars,
        slots,
        clips,
        { text_elements: true, timeline: true, camera_effects: true, visual_blocks: true },
        [],
        {
          cameraEffectsEnabled: true,
          transitionsEnabled: true,
          visualBlocksEnabled: true,
          cameraEffects: [cameraEffect],
          visualBlocks: [visualBlock],
        },
      ),
      capabilities: { text_elements: true, timeline: true, camera_effects: true, visual_blocks: true },
      cameraEffects: [cameraEffect],
      visualBlocks: [visualBlock],
      makeTextBarId: () => "new-text",
      makeSlotKey: (s: DraftSlot) => `${s.key}-split`,
      makeCameraEffectId: () => "camera-2",
    };
  }

  it("adds, patches, and removes persisted camera effects", () => {
    const added = applyCopilotOps(
      [{ op: "add_camera_effect", start_s: 2, end_s: 3.5, intensity: 0.05 }],
      directorCtx(),
    );
    expect(added.rejected).toEqual([]);
    expect(added.nextCameraEffects).toHaveLength(2);
    expect(added.nextCameraEffects?.[1]).toMatchObject({
      id: "camera-2",
      start_s: 2,
      end_s: 3.5,
      intensity: 0.05,
    });

    const patched = applyCopilotOps(
      [{ op: "patch_camera_effect", camera_effect_index: 0, intensity: 0.07 }],
      directorCtx(),
    );
    expect(patched.nextCameraEffects?.[0]).toMatchObject({ id: "camera-1", intensity: 0.07 });

    const removed = applyCopilotOps(
      [{ op: "remove_camera_effect", camera_effect_index: 0 }],
      directorCtx(),
    );
    expect(removed.nextCameraEffects).toEqual([]);
  });

  it("stores the transition on the left side of the selected boundary", () => {
    const result = applyCopilotOps(
      [{ op: "set_transition", boundary_index: 1, transition: "flash", duration_s: 0.3 }],
      directorCtx(),
    );

    expect(result.rejected).toEqual([]);
    expect(result.nextSlots?.[0].transitionAfter).toBeUndefined();
    expect(result.nextSlots?.[1]).toMatchObject({
      transitionAfter: "flash",
      transitionDurationS: 0.3,
    });
  });

  it("applies visual-block entrance and exit fades as draft state", () => {
    const result = applyCopilotOpsAtomic(
      [{
        op: "set_visual_fade",
        visual_block_index: 0,
        transition_in: "fade",
        transition_out: "fade",
      }],
      directorCtx(),
    );

    expect(result.rejected).toEqual([]);
    expect(result.nextVisualBlocks?.[0]).toMatchObject({
      id: "visual-1",
      transition_in: "fade",
      transition_out: "fade",
    });
  });

  it("does not stage normalized same-value director operations", () => {
    const camera = applyCopilotOps(
      [{ op: "patch_camera_effect", camera_effect_index: 0, intensity: 0.04 }],
      directorCtx(),
    );
    expect(camera.nextCameraEffects).toBeUndefined();
    expect(camera.rejected).toMatchObject([{ reason: "no_effect" }]);

    const transitionContext = directorCtx();
    transitionContext.slots[1] = {
      ...transitionContext.slots[1],
      transitionAfter: "flash",
      transitionDurationS: 0.3,
    };
    transitionContext.snapshot = buildCopilotSnapshot(
      transitionContext.bars,
      transitionContext.slots,
      clips,
      transitionContext.capabilities,
      [],
      {
        cameraEffectsEnabled: true,
        transitionsEnabled: true,
        visualBlocksEnabled: true,
        cameraEffects: transitionContext.cameraEffects,
        visualBlocks: transitionContext.visualBlocks,
      },
    );
    const transition = applyCopilotOps(
      [{ op: "set_transition", boundary_index: 1, transition: "flash", duration_s: 0.3 }],
      transitionContext,
    );
    expect(transition.nextSlots).toBeNull();
    expect(transition.rejected).toMatchObject([{ reason: "no_effect" }]);

    const visual = applyCopilotOps(
      [{ op: "set_visual_fade", visual_block_index: 0, transition_in: "cut", transition_out: "cut" }],
      directorCtx(),
    );
    expect(visual.nextVisualBlocks).toBeUndefined();
    expect(visual.rejected).toMatchObject([{ reason: "no_effect" }]);
  });

  it("indexes transitions across active clips when a removed slot sits between them", () => {
    const ctx = directorCtx();
    ctx.slots = ctx.slots.map((item, index) =>
      index === 1 ? { ...item, removed: true } : item
    );
    ctx.snapshot = buildCopilotSnapshot(
      ctx.bars,
      ctx.slots,
      clips,
      { text_elements: true, timeline: true, camera_effects: true },
      [],
      {
        cameraEffectsEnabled: true,
        transitionsEnabled: true,
        cameraEffects: [cameraEffect],
      },
    );
    const result = applyCopilotOps(
      [{ op: "set_transition", boundary_index: 0, transition: "crossfade" }],
      ctx,
    );

    expect(result.rejected).toEqual([]);
    expect(result.nextSlots?.[0].transitionAfter).toBe("crossfade");
    expect(result.nextSlots?.[1].removed).toBe(true);
  });

  it("rejects the whole creative bundle when any operation is invalid or stale", () => {
    const result = applyCopilotOpsAtomic(
      [
        { op: "edit_text", bar_index: 0, text: "A stronger hook" },
        { op: "set_transition", boundary_index: 99, transition: "flash" },
      ],
      directorCtx(),
    );

    expect(result.textActions).toEqual([]);
    expect(result.nextSlots).toBeNull();
    expect(result.applied).toEqual([]);
    expect(result.rejected).toHaveLength(1);
  });

  it("applies every non-overlapping Director card against one source snapshot", () => {
    const source = extendedCtx();

    const text = applyCopilotOpsAtomic(
      [{ op: "edit_text", bar_index: 0, text: "A sharper hook" }],
      source,
    );
    expect(text.rejected).toEqual([]);
    const bars = source.bars.map((item) =>
      item.id === "bar-1" ? { ...item, text: "A sharper hook" } : item
    );

    const title = applyCopilotOpsAtomic(
      [{ op: "set_title", title: "Building Nova" }],
      { ...source, bars },
    );
    expect(title.rejected).toEqual([]);

    const sound = applyCopilotOpsAtomic(
      [{ op: "add_sfx", effect_id: "effect-1", at_s: 0.5, gain: 0.6 }],
      { ...source, bars, title: title.nextTitle ?? source.title },
    );
    expect(sound.rejected).toEqual([]);

    const timeline = applyCopilotOpsAtomic(
      [{ op: "set_clip_duration", slot_index: 0, duration_s: 1.5 }],
      {
        ...source,
        bars,
        title: title.nextTitle ?? source.title,
        sfx: sound.nextSfx ?? source.sfx,
      },
    );
    expect(timeline.rejected).toEqual([]);

    expect({
      text: bars[0].text,
      title: title.nextTitle,
      sfx: sound.nextSfx?.map((item) => item.sound_effect_id),
      firstClipDuration: timeline.nextSlots?.[0].durationS,
    }).toEqual({
      text: "A sharper hook",
      title: "Building Nova",
      sfx: ["effect-1", "effect-1"],
      firstClipDuration: 1.5,
    });
  });

  it("rejects destructive Director cards when any snapshotted target field changed", () => {
    const extended = extendedCtx();
    const changedText = applyCopilotOpsAtomic(
      [{ op: "remove_text", bar_index: 0 }],
      {
        ...extended,
        bars: extended.bars.map((item) =>
          item.id === "bar-1" ? { ...item, shadow_enabled: true } : item
        ),
      },
    );
    expect(changedText.rejected).toEqual([
      expect.objectContaining({ reason: "user_changed" }),
    ]);

    const changedSound = applyCopilotOpsAtomic(
      [{ op: "remove_sfx", sfx_index: 0 }],
      {
        ...extended,
        sfx: extended.sfx.map((item) => ({ ...item, trim_start_s: 0.1 })),
      },
    );
    expect(changedSound.rejected).toEqual([
      expect.objectContaining({ reason: "user_changed" }),
    ]);

    const changedOverlay = applyCopilotOpsAtomic(
      [{ op: "remove_overlay", overlay_index: 0 }],
      {
        ...extended,
        overlays: extended.overlays.map((item) => ({ ...item, exit_token: "dissolve-out" as const })),
      },
    );
    expect(changedOverlay.rejected).toEqual([
      expect.objectContaining({ reason: "user_changed" }),
    ]);

    const director = directorCtx();
    const changedClip = applyCopilotOpsAtomic(
      [{ op: "remove_clip", slot_index: 1 }],
      {
        ...director,
        slots: director.slots.map((item) =>
          item.key === "b" ? { ...item, lookPreset: "smoky_split_tone" as const } : item
        ),
      },
    );
    expect(changedClip.rejected).toEqual([
      expect.objectContaining({ reason: "user_changed" }),
    ]);

    const changedCamera = applyCopilotOpsAtomic(
      [{ op: "remove_camera_effect", camera_effect_index: 0 }],
      {
        ...director,
        cameraEffects: director.cameraEffects.map((item) => ({
          ...item,
          token: "alternate_camera_pulse",
        })),
      },
    );
    expect(changedCamera.rejected).toEqual([
      expect.objectContaining({ reason: "user_changed" }),
    ]);
  });

  it("rejects a remove-clip card after a sibling recommendation reordered its target", () => {
    const source = directorCtx();
    const reordered = applyCopilotOpsAtomic(
      [{ op: "reorder_clip", from_index: 1, to_index: 0 }],
      source,
    );
    expect(reordered.rejected).toEqual([]);

    const staleRemoval = applyCopilotOpsAtomic(
      [{ op: "remove_clip", slot_index: 1 }],
      { ...source, slots: reordered.nextSlots ?? source.slots },
    );

    expect(staleRemoval.rejected).toEqual([
      expect.objectContaining({ reason: "user_changed" }),
    ]);
    expect(staleRemoval.nextSlots).toBeNull();
  });

  it("inserts a completed generated asset at the nearest clip boundary", () => {
    const result = applyCopilotOpsAtomic(
      [{
        op: "insert_generated_asset",
        asset_id: "asset-omni-1",
        clip_index: 3,
        insert_at_s: 3.1,
        duration_s: 5,
      }],
      directorCtx(),
    );

    expect(result.rejected).toEqual([]);
    expect(result.nextSlots).toHaveLength(4);
    expect(result.nextSlots?.[1]).toMatchObject({
      key: "generated-asset-omni-1",
      clipIndex: 3,
      durationS: 5,
      momentDescription: "Generated by Kria",
    });
  });

  it("replaces the selected source segment for an Omni restyle", () => {
    const result = applyCopilotOpsAtomic(
      [{
        op: "replace_generated_segment",
        asset_id: "asset-omni-restyle",
        clip_index: 3,
        source_clip_index: 1,
        source_start_s: 0,
        source_end_s: 3,
        duration_s: 4,
      }],
      directorCtx(),
    );

    expect(result.rejected).toEqual([]);
    expect(result.nextSlots).toHaveLength(3);
    expect(result.nextSlots?.[1]).toMatchObject({
      key: "generated-asset-omni-restyle",
      clipIndex: 3,
      durationS: 4,
      momentDescription: "Restyled by Kria",
      lookPreset: "olive_film",
      lookAdjustments: {
        intensity: 0.7,
        warmth: 0.2,
        contrast: -0.1,
        grain: 0.3,
        vignette: 0.4,
      },
    });
    expect(result.nextSlots?.some((item) => item.clipIndex === 1)).toBe(false);
  });
});

describe("slot-less variants (zero layout duration)", () => {
  // Regression: subtitled talk-to-camera variants have no clip slots, so the
  // snapshot's layout total is 0. Every at_s used to be clamped to
  // min(at_s, max(0, 0 - 0.1)) = 0 — SFX placed at second 0 regardless of the
  // model's (correct) requested times.
  it("does not collapse add_sfx at_s to 0 when the snapshot total is 0", () => {
    const sfxCatalog = [effect()];
    const snapshot = buildCopilotSnapshot(
      [bar()],
      [],
      clips,
      { text_elements: true, timeline: true, sfx: true },
      [],
      { sfxEnabled: true, sfxPlacements: [], sfxCatalog },
    );
    expect(snapshot.total_duration_s).toBe(0);
    const res = applyCopilotOps(
      [{ op: "add_sfx", effect_id: "effect-1", at_s: 46.22, gain: 0.7 }],
      {
        bars: [bar()],
        slots: [],
        snapshot,
        capabilities: { text_elements: true, timeline: true, sfx: true },
        videoDurationS: 80,
        sfx: [],
        sfxCatalog,
        makeTextBarId: () => "new-text",
        makeSlotKey: (s: DraftSlot) => `${s.key}-split`,
        makeSfxPlacementId: () => "new-sfx",
      },
    );
    expect(res.rejected).toEqual([]);
    expect(res.nextSfx?.at(-1)).toMatchObject({ at_s: 46.22 });
  });

  it("clamps against the real video duration when the layout total is 0", () => {
    const sfxCatalog = [effect()];
    const snapshot = buildCopilotSnapshot(
      [bar()],
      [],
      clips,
      { text_elements: true, timeline: true, sfx: true },
      [],
      { sfxEnabled: true, sfxPlacements: [], sfxCatalog },
    );
    const res = applyCopilotOps(
      [{ op: "add_sfx", effect_id: "effect-1", at_s: 500, gain: 0.7 }],
      {
        bars: [bar()],
        slots: [],
        snapshot,
        capabilities: { text_elements: true, timeline: true, sfx: true },
        videoDurationS: 80,
        sfx: [],
        sfxCatalog,
        makeTextBarId: () => "new-text",
        makeSlotKey: (s: DraftSlot) => `${s.key}-split`,
        makeSfxPlacementId: () => "new-sfx",
      },
    );
    expect(res.nextSfx?.at(-1)).toMatchObject({ at_s: 79.9 });
  });
});

describe("Creator Block operations", () => {
  const motionScene = {
    id: "motion-1",
    preset_id: "kinetic_word" as const,
    preset_version: 1 as const,
    start_frame: 0,
    end_frame_exclusive: 75,
    palette: { primary: "#0C0C0E", accent: "#C7FF3D" },
    intensity: 0.72,
    params: { text: "OLD" },
  };
  const imageAssets = [
    asset({ id: "image-1", gcs_path: "users/user/plan/item/pool/image-1.png" }),
    asset({ id: "image-2", gcs_path: "users/user/plan/item/pool/image-2.png" }),
  ];

  function motionCtx(evolvingTypeEnabled = false) {
    const bars = [bar()];
    const slots = [slot({ durationS: 9 })];
    const motionScenes: MotionPresetInstance[] = [motionScene];
    const capabilities = { text_elements: true, timeline: true, motion_scenes: true };
    const snapshot = buildCopilotSnapshot(bars, slots, clips, capabilities, [], {
      motionScenesEnabled: true,
      evolvingTypeEnabled,
      motionScenes,
      poolAssets: imageAssets,
    });
    return {
      bars,
      slots,
      snapshot,
      capabilities,
      motionScenes,
      poolAssets: imageAssets,
      videoDurationS: 9,
      makeMotionId: () => "motion-2",
    };
  }

  it("adds media blocks with validated asset references and readable chips", () => {
    const result = applyCopilotOps([{
      op: "add_motion_block",
      preset_id: "card_stack",
      start_s: 2.5,
      end_s: 6.5,
      params: { asset_ids: ["image-1", "image-2"] },
    }], motionCtx());

    expect(result.rejected).toEqual([]);
    expect(result.nextMotionScenes?.[1]).toMatchObject({
      id: "motion-2",
      preset_id: "card_stack",
      params: {
        assets: [
          { asset_id: "image-1", gcs_path: "users/user/plan/item/pool/image-1.png" },
          { asset_id: "image-2", gcs_path: "users/user/plan/item/pool/image-2.png" },
        ],
      },
    });
    expect(result.applied[0].label).toBe("Card Stack");
  });

  it("patches and removes by immutable ID while rejecting stale targets", () => {
    const context = motionCtx();
    const patched = applyCopilotOps([{
      op: "patch_motion_block",
      motion_id: "motion-1",
      patch: { params: { text: "NEW" }, intensity: 0.5 },
    }], context);
    expect(patched.nextMotionScenes?.[0]).toMatchObject({ params: { text: "NEW" }, intensity: 0.5 });

    const staleContext = motionCtx();
    staleContext.motionScenes = [{ ...motionScene, intensity: 0.2 }];
    expect(applyCopilotOps([{
      op: "remove_motion_block",
      motion_id: "motion-1",
    }], staleContext).rejected).toMatchObject([{ reason: "user_changed" }]);

    expect(applyCopilotOps([{
      op: "remove_motion_block",
      motion_id: "motion-1",
    }], motionCtx()).nextMotionScenes).toEqual([]);
  });

  it("does not stage a normalized same-value Creator Block patch", () => {
    const result = applyCopilotOps([{
      op: "patch_motion_block",
      motion_id: "motion-1",
      patch: { params: { text: "OLD" }, intensity: 0.72 },
    }], motionCtx());

    expect(result.nextMotionScenes).toBeUndefined();
    expect(result.applied).toEqual([]);
    expect(result.rejected).toMatchObject([{ reason: "no_effect" }]);
  });

  it("preserves preset v1 for content-only patches and upgrades only motion controls", () => {
    const content = applyCopilotOps([{
      op: "patch_motion_block",
      motion_id: "motion-1",
      patch: { params: { text: "NEW" } },
    }], motionCtx());
    expect(content.nextMotionScenes?.[0]).toMatchObject({
      preset_version: 1,
      params: { text: "NEW" },
      start_frame: 0,
      end_frame_exclusive: 75,
    });
    expect(content.nextMotionScenes?.[0]).not.toHaveProperty("motion");

    const selectedOnlyContext = motionCtx();
    const untouchedScene = {
      ...motionScene,
      id: "motion-untouched",
      start_frame: 150,
      end_frame_exclusive: 225,
      params: { text: "UNTOUCHED" },
    };
    selectedOnlyContext.motionScenes = [motionScene, untouchedScene];
    selectedOnlyContext.snapshot = buildCopilotSnapshot(
      selectedOnlyContext.bars,
      selectedOnlyContext.slots,
      clips,
      selectedOnlyContext.capabilities,
      [],
      {
        motionScenesEnabled: true,
        motionScenes: selectedOnlyContext.motionScenes,
        poolAssets: imageAssets,
      },
    );
    const speed = applyCopilotOps([{
      op: "patch_motion_block",
      motion_id: "motion-1",
      patch: { speed: 2 },
    }], selectedOnlyContext);
    expect(speed.rejected).toEqual([]);
    expect(speed.nextMotionScenes?.[0]).toMatchObject({
      preset_version: 2,
      start_frame: 0,
      end_frame_exclusive: 62,
      motion: { version: 2, speed: 2 },
    });
    expect(speed.nextMotionScenes?.[1]).toEqual(untouchedScene);
  });

  it("upgrades easing without retiming and retimes only speed or hold", () => {
    const easing = applyCopilotOps([{
      op: "patch_motion_block",
      motion_id: "motion-1",
      patch: { easing: "ease-out-cubic" },
    }], motionCtx());
    expect(easing.rejected).toEqual([]);
    expect(easing.nextMotionScenes?.[0]).toMatchObject({
      preset_version: 2,
      start_frame: 0,
      end_frame_exclusive: 75,
      motion: { version: 2, easing: "ease-out-cubic" },
    });

    const hold = applyCopilotOps([{
      op: "patch_motion_block",
      motion_id: "motion-1",
      patch: { hold_frames: 60 },
    }], motionCtx());
    expect(hold.rejected).toEqual([]);
    expect(hold.nextMotionScenes?.[0]).toMatchObject({
      preset_version: 2,
      start_frame: 0,
      end_frame_exclusive: 105,
      motion: { version: 2, hold_frames: 60 },
    });
  });

  it("canonicalizes custom speed and hold timing on add", () => {
    const result = applyCopilotOps([{
      op: "add_motion_block",
      preset_id: "kinetic_word",
      start_s: 2,
      end_s: 6,
      params: { text: "FAST" },
      speed: 2,
      hold_frames: 0,
    }], motionCtx());

    expect(result.rejected).toEqual([]);
    expect(result.nextMotionScenes?.[1]).toMatchObject({
      preset_version: 2,
      start_frame: 60,
      end_frame_exclusive: 92,
      motion: { version: 2, speed: 2, hold_frames: 0 },
    });
  });

  it("reconciles the requested span when v2 defaults are used on add", () => {
    const result = applyCopilotOps([{
      op: "add_motion_block",
      preset_id: "kinetic_word",
      start_s: 2,
      end_s: 3,
      params: { text: "SHORT" },
    }], motionCtx());

    expect(result.rejected).toEqual([]);
    const added = result.nextMotionScenes?.[1];
    expect(added?.preset_version).toBe(2);
    if (!added || added.preset_version !== 2) throw new Error("Expected Creator Block v2");
    expect(added.start_frame).toBe(60);
    expect(added.end_frame_exclusive - added.start_frame).toBe(
      creatorBlockDurationFramesV2(added),
    );
    expect(added.end_frame_exclusive).toBe(90);
    expect(added.motion.speed).toBeGreaterThan(1);
  });

  it("consumes hold before changing speed on a manual v2 trim", () => {
    const context = motionCtx();
    const v2Scene = {
      ...motionScene,
      preset_version: 2 as const,
      motion: {
        version: 2 as const,
        speed: 1,
        easing: "ease-in-out-cubic" as const,
        hold_frames: 30,
      },
    };
    context.motionScenes = [v2Scene];
    context.snapshot = buildCopilotSnapshot(
      context.bars,
      context.slots,
      clips,
      context.capabilities,
      [],
      { motionScenesEnabled: true, motionScenes: [v2Scene], poolAssets: imageAssets },
    );

    const result = applyCopilotOps([{
      op: "patch_motion_block",
      motion_id: "motion-1",
      patch: { end_s: 2 },
    }], context);

    expect(result.rejected).toEqual([]);
    expect(result.nextMotionScenes?.[0]).toMatchObject({
      preset_version: 2,
      start_frame: 0,
      end_frame_exclusive: 60,
      motion: { version: 2, speed: 1, hold_frames: 15 },
    });
  });

  it("fails closed for Evolving Type when its exposure flag is off", () => {
    const result = applyCopilotOps([{
      op: "add_motion_block",
      preset_id: "evolving_type",
      start_s: 0,
      end_s: 5.3,
      params: {
        headline: "EVOLVE THE IDEA",
        subtitle: "Shape, split, and settle into focus",
      },
    }], { ...motionCtx(true), evolvingTypeEnabled: false });

    expect(result.nextMotionScenes).toBeUndefined();
    expect(result.rejected).toMatchObject([{ reason: "capability_disabled" }]);
  });
});
