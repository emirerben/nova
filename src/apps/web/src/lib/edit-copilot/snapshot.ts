import type { DraftSlot } from "@/app/generative/timeline-math";
import type { EditorCapabilities } from "@/lib/plan-api";
import type { TextElementBar } from "@/lib/timeline/text-timeline-reducer";
import { FONT_SIZE_MAP } from "@/lib/overlay-constants";
import {
  resolveLetterSpacingEm,
  resolveLineSpacing,
  resolveMaxWidthFrac,
} from "@/lib/overlay-layout";
import { sequentialSlotLayout } from "@/app/plan/items/[id]/_editor/editor-bar-drag";
import type { CopilotOpFamily } from "./ops";

export interface CopilotClipLike {
  source_duration_s?: number | null;
  duration_s?: number | null;
  durationS?: number | null;
  moment?: string | null;
  moment_description?: string | null;
}

export interface CopilotTextSnapshotBar {
  index: number;
  id: string;
  text: string;
  start_s: number;
  end_s: number;
  role: Exclude<TextElementBar["role"], "narrated_caption">;
  font_family: string;
  size_px: number;
  color: string;
  highlight_color: string | null;
  effect: string;
  alignment: string;
  text_case: string;
  letter_spacing: number;
  line_spacing: number;
  max_width_frac: number;
  stroke_width: number;
  position: string;
  x_frac: number | null;
  y_frac: number | null;
}

export interface CopilotSlotSnapshot {
  index: number;
  key: string;
  slot_id: string | null;
  clip_index: number;
  in_s: number;
  duration_s: number;
  removed: boolean;
  source_duration_s: number | null;
  moment: string | null;
  output_start_s: number | null;
  output_end_s: number | null;
}

export interface CopilotSnapshot {
  text_bars: CopilotTextSnapshotBar[];
  slots: CopilotSlotSnapshot[];
  has_narrated_captions: boolean;
  total_duration_s: number;
  max_duration_s: 60;
  remaining_duration_s: number;
  allowed_op_families: CopilotOpFamily[];
}

function round(value: number): number {
  return Math.round(value * 1000) / 1000;
}

function effectiveSizePx(bar: TextElementBar): number {
  if (typeof bar.size_px === "number" && Number.isFinite(bar.size_px)) {
    return Math.max(1, Math.trunc(bar.size_px));
  }
  return FONT_SIZE_MAP[bar.size_class ?? "medium"] ?? 72;
}

function allCoreCapabilitiesFalse(capabilities: EditorCapabilities | null | undefined): boolean {
  return !!capabilities &&
    capabilities.text_elements === false &&
    capabilities.timeline === false &&
    capabilities.split_clips === false &&
    capabilities.mix === false &&
    capabilities.sfx === false &&
    capabilities.overlays === false;
}

export function allowedOpFamiliesFromCapabilities(
  capabilities: EditorCapabilities | null | undefined,
): CopilotOpFamily[] {
  if (allCoreCapabilitiesFalse(capabilities)) return [];
  const families: CopilotOpFamily[] = [];
  if (capabilities?.text_elements !== false) families.push("text");
  if (capabilities?.timeline !== false) families.push("clip");
  return families;
}

function sourceDurationForSlot(slot: DraftSlot, clips: CopilotClipLike[]): number | null {
  const clip = clips[slot.clipIndex];
  const source = clip?.source_duration_s ?? clip?.duration_s ?? clip?.durationS ?? null;
  return typeof source === "number" && Number.isFinite(source) ? source : null;
}

export function buildCopilotSnapshot(
  bars: TextElementBar[],
  slots: DraftSlot[],
  clips: CopilotClipLike[],
  capabilities?: EditorCapabilities | null,
  grid: number[] = [],
): CopilotSnapshot {
  const visibleBars = bars.filter(
    (bar): bar is TextElementBar & { role: Exclude<TextElementBar["role"], "narrated_caption"> } =>
      bar.role !== "narrated_caption",
  );
  const textBars: CopilotTextSnapshotBar[] = visibleBars.map((bar, index) => ({
    index,
    id: bar.id,
    text: bar.text,
    start_s: round(bar.start_s),
    end_s: round(bar.end_s),
    role: bar.role,
    font_family: bar.font_family ?? "PlayfairDisplay-Bold",
    size_px: effectiveSizePx(bar),
    color: bar.color ?? "#FFFFFF",
    highlight_color: bar.highlight_color ?? null,
    effect: bar.effect ?? "static",
    alignment: bar.alignment ?? "center",
    text_case: bar.text_case ?? "none",
    letter_spacing: resolveLetterSpacingEm(bar.letter_spacing),
    line_spacing: resolveLineSpacing(bar.line_spacing),
    max_width_frac: resolveMaxWidthFrac(bar.max_width_frac),
    stroke_width: bar.stroke_width ?? 0,
    position: bar.position ?? "middle",
    x_frac: bar.x_frac ?? null,
    y_frac: bar.y_frac ?? null,
  }));

  const layout = sequentialSlotLayout(slots, grid);
  const snapSlots: CopilotSlotSnapshot[] = slots.map((slot, index) => {
    const win = layout.windows[index];
    const durationS = round(win?.durationS ?? slot.durationS ?? 0);
    const outputStartS = win?.startS == null ? null : round(win.startS);
    return {
      index,
      key: slot.key,
      slot_id: slot.slotId,
      clip_index: slot.clipIndex,
      in_s: round(slot.inS),
      duration_s: durationS,
      removed: slot.removed,
      source_duration_s: sourceDurationForSlot(slot, clips),
      moment:
        slot.momentDescription ??
        clips[slot.clipIndex]?.moment ??
        clips[slot.clipIndex]?.moment_description ??
        null,
      output_start_s: outputStartS,
      output_end_s: outputStartS == null ? null : round(outputStartS + durationS),
    };
  });

  const total = round(layout.totalDurationS);
  return {
    text_bars: textBars,
    slots: snapSlots,
    has_narrated_captions: bars.some((bar) => bar.role === "narrated_caption"),
    total_duration_s: total,
    max_duration_s: 60,
    remaining_duration_s: round(Math.max(0, 60 - total)),
    allowed_op_families: allowedOpFamiliesFromCapabilities(capabilities),
  };
}
