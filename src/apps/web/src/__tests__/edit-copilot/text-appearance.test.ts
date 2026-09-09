import { describe, expect, it } from "@jest/globals";
import { buildCopilotSnapshot } from "@/lib/edit-copilot/snapshot";
import { applyCopilotOpsAtomic } from "@/lib/edit-copilot/apply-ops";
import { barsToCaptionCues } from "@/app/plan/items/[id]/_editor/editor-bars";
import { initTextEditorState, textReducer, type TextElementBar } from "@/lib/timeline/text-timeline-reducer";
import { createCreatorBlockInstance, type MotionPresetInstance } from "@nova/motion-runtime";

const capabilities = {
  text_appearance_version: 1 as const,
  text_elements: true,
  motion_scenes: true,
  timeline: true,
};

function makeBar(id: string, role: TextElementBar["role"] = "generative_intro", start = 0): TextElementBar {
  return { id, role, text: id, start_s: start, end_s: start + 1, stroke_width: 2, shadow_enabled: true };
}

function makeContext(bars: TextElementBar[], motionScenes: MotionPresetInstance[] = []) {
  const captionBars = bars.filter((bar) => bar.role === "narrated_caption");
  const captionMeta = captionBars.length ? { enabled: true, style: "sentence" as const, font: null, y_frac: 0.2, stroke_width: 2, shadow_enabled: true } : null;
  const snapshot = buildCopilotSnapshot(bars, [], [], capabilities, [], {
    captionCues: captionBars.map((bar) => ({ id: bar.id, text: bar.text, start_s: bar.start_s, end_s: bar.end_s })),
    captionMeta: captionMeta ?? undefined,
    captionsPresent: captionBars.length > 0,
    captionCuesEditable: true,
    motionScenes,
    motionScenesEnabled: motionScenes.length > 0,
  });
  return { bars, slots: [], motionScenes, snapshot, capabilities, captionMeta };
}

function motion(id = "motion-1"): MotionPresetInstance {
  const instance = createCreatorBlockInstance({ id, presetId: "kinetic_word", startFrame: 0, endFrameExclusive: 30 });
  return { ...instance, text_appearance: { stroke_width: 2, shadow_enabled: true } } as MotionPresetInstance;
}

describe("patch_text_appearance contract", () => {
  it("covers a mixed draft beyond the ordinary twelve operation limit in one transaction", () => {
    const bars = [...Array.from({ length: 13 }, (_, i) => makeBar(`text-${i}`, "generative_intro", i)), makeBar("caption-0", "narrated_caption", 13)];
    const scene = motion();
    const context = makeContext(bars, [scene]);
    const result = applyCopilotOpsAtomic([{ op: "patch_text_appearance", text_appearance_version: 1, selector: { scope: "editable_text", quantifier: "all" }, patch: { stroke_width: 0, shadow_enabled: false } }], context);
    expect(result.rejected).toEqual([]);
    expect(result.textActions).toHaveLength(1);
    expect((result.textActions[0] as { patches: unknown[] }).patches).toHaveLength(14);
    expect(result.nextMotionScenes?.[0].text_appearance).toEqual({ stroke_width: 0, shadow_enabled: false });
  });

  it("applies explicit target IDs only to those targets", () => {
    const bars = [makeBar("caption-0", "narrated_caption"), makeBar("caption-1", "narrated_caption", 1), makeBar("text-0")];
    const context = makeContext(bars);
    const result = applyCopilotOpsAtomic([{ op: "patch_text_appearance", text_appearance_version: 1, selector: { scope: "editable_text", quantifier: "all", target_ids: ["caption-1"] }, patch: { shadow_enabled: false } }], context);
    expect(result.rejected).toEqual([]);
    expect(result.captionMetaPatch).toBeUndefined();
    expect((result.textActions[0] as { patches: Array<{ id: string; patch: Record<string, unknown> }> }).patches).toEqual([{ id: "caption-1", patch: { cue_shadow_enabled: false } }]);
  });

  it("supports category scope while leaving other lanes untouched", () => {
    const bars = [makeBar("text-0"), makeBar("caption-0", "narrated_caption", 1)];
    const result = applyCopilotOpsAtomic([{ op: "patch_text_appearance", text_appearance_version: 1, selector: { scope: "editable_text", quantifier: "all", category: "text" }, patch: { shadow_enabled: false } }], makeContext(bars));
    expect(result.rejected).toEqual([]);
    expect(result.textActions).toEqual([{ type: "PATCH_BARS", patches: [{ id: "text-0", patch: { shadow_enabled: false } }] }]);
    expect(result.captionMetaPatch).toBeUndefined();
  });

  it("rejects stale membership and identity atomically", () => {
    const original = [makeBar("text-0"), makeBar("text-1", "generative_intro", 1)];
    const context = makeContext(original);
    const changed = { ...context, bars: [original[0], makeBar("text-new", "generative_intro", 1)] };
    const result = applyCopilotOpsAtomic([{ op: "patch_text_appearance", text_appearance_version: 1, selector: { scope: "editable_text", quantifier: "all" }, patch: { shadow_enabled: false } }], changed);
    expect(result.textActions).toEqual([]);
    expect(result.applied).toEqual([]);
    expect(result.rejected[0]).toMatchObject({ reason: "user_changed" });
  });

  it("rejects the whole bundle when a sibling operation is invalid", () => {
    const bars = [makeBar("text-0"), makeBar("text-1", "generative_intro", 1)];
    const context = makeContext(bars);
    const result = applyCopilotOpsAtomic([
      { op: "patch_text_appearance", text_appearance_version: 1, selector: { scope: "editable_text", quantifier: "all", target_ids: ["text-0"] }, patch: { shadow_enabled: false } },
      { op: "patch_text_appearance", text_appearance_version: 1, selector: { scope: "editable_text", quantifier: "all", target_ids: ["missing"] }, patch: { shadow_enabled: false } },
    ], context);
    expect(result.textActions).toEqual([]);
    expect(result.applied).toEqual([]);
    expect(result.rejected.length).toBeGreaterThan(0);
  });

  it("rejects readonly targets and preserves the whole draft", () => {
    const scene = motion();
    const context = makeContext([makeBar("text-0")], [scene]);
    const inventory = context.snapshot.text_appearance!;
    const readonly = inventory.targets.find((target) => target.kind === "motion")!;
    readonly.supported_fields = [];
    const result = applyCopilotOpsAtomic([{ op: "patch_text_appearance", text_appearance_version: 1, selector: { scope: "editable_text", quantifier: "all", category: "motion" }, patch: { shadow_enabled: false } }], context);
    expect(result.textActions).toEqual([]);
    expect(result.nextMotionScenes).toBeUndefined();
    expect(result.rejected[0]).toMatchObject({ reason: "unsupported_field" });
  });

  it("reports a complete no-op and preserves explicit cue zero/false on save", () => {
    const cue = makeBar("caption-0", "narrated_caption");
    cue.cue_stroke_width = 0;
    cue.cue_shadow_enabled = false;
    const context = makeContext([cue]);
    const result = applyCopilotOpsAtomic([{ op: "patch_text_appearance", text_appearance_version: 1, selector: { scope: "editable_text", quantifier: "all", target_ids: ["caption-0"] }, patch: { stroke_width: 0, shadow_enabled: false } }], context);
    expect(result.rejected[0]).toMatchObject({ reason: "no_effect" });
    expect(barsToCaptionCues([cue])[0]).toMatchObject({ stroke_width: 0, shadow_enabled: false });
    const state = initTextEditorState([cue]);
    const patched = textReducer(state, { type: "PATCH_BARS", patches: [{ id: "caption-0", patch: { cue_stroke_width: 0, cue_shadow_enabled: false } }] });
    expect(patched.bars[0]).toMatchObject({ cue_stroke_width: 0, cue_shadow_enabled: false });
    expect(textReducer(patched, { type: "UNDO" }).bars[0]).toMatchObject({ cue_stroke_width: 0, cue_shadow_enabled: false });
  });
  it.each(["added", "removed", "style", "caption-meta", "motion"])("rejects a %s draft change before staging any lane", (kind) => {
    const context = makeContext([makeBar("title"), makeBar("caption-0", "narrated_caption", 1)], [motion()]);
    if (kind === "added") context.bars = [...context.bars, makeBar("new")];
    if (kind === "removed") context.bars = context.bars.slice(1);
    if (kind === "style") context.bars = context.bars.map(b => b.id === "title" ? { ...b, color: "#123456" } : b);
    if (kind === "caption-meta") context.captionMeta = { ...context.captionMeta!, stroke_width: 5 };
    if (kind === "motion") context.motionScenes = [{ ...context.motionScenes[0], intensity: 0.5 }];
    const result = applyCopilotOpsAtomic([{ op: "patch_text_appearance", text_appearance_version: 1, selector: { scope: "editable_text", quantifier: "all" }, patch: { stroke_width: 0, shadow_enabled: false } }], context);
    expect(result.rejected[0].reason).toBe("user_changed");
    expect(result.textActions).toEqual([]);
    expect(result.captionMetaPatch).toBeUndefined();
    expect(result.nextMotionScenes).toBeUndefined();
  });

  it("counts no-depth Creator Blocks as covered when disabling shadows", () => {
    const scene = createCreatorBlockInstance({ id: "tags", presetId: "tag_stack", startFrame: 0, endFrameExclusive: 30 });
    const context = makeContext([makeBar("title")], [scene]);
    const result = applyCopilotOpsAtomic([{ op: "patch_text_appearance", text_appearance_version: 1, selector: { scope: "editable_text", quantifier: "all" }, patch: { shadow_enabled: false } }], context);
    expect(result.rejected).toEqual([]);
    expect(result.textActions).toHaveLength(1);
    expect(result.nextMotionScenes).toBeUndefined();
    expect(scene.text_appearance).toBeUndefined();
  });

  it.each([undefined, 2])("rejects an unnegotiated operation version %s", (version) => {
    const result = applyCopilotOpsAtomic([{ op: "patch_text_appearance", text_appearance_version: version, selector: { scope: "editable_text", quantifier: "all" }, patch: { shadow_enabled: false } }], makeContext([makeBar("title")]));
    expect(result.rejected[0].reason).toBe("invalid_op");
    expect(result.textActions).toEqual([]);
  });

  it("keeps legacy snapshots free of the negotiated operation", () => {
    const context = makeContext([makeBar("title")]);
    delete context.snapshot.text_appearance_version;
    delete context.snapshot.text_appearance;
    const result = applyCopilotOpsAtomic([{ op: "patch_text_appearance", text_appearance_version: 1, selector: { scope: "editable_text", quantifier: "all" }, patch: { shadow_enabled: false } }], context);
    expect(result.textActions).toEqual([]);
    expect(result.rejected[0].reason).toBe("invalid_op");
  });

});
