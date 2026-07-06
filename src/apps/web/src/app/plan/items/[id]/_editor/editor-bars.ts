/**
 * Editor working-state converters: API variant data ↔ TextElementBar[].
 *
 * Same seeding precedence as the item page for untouched variants, but
 * user-edited text_elements are authoritative over caption/scene projections.
 * This keeps reload from resurrecting sequence-projected bars after Save.
 *
 * The API text-element path maps WITHOUT dropping position / x_frac / y_frac /
 * highlight_color / stroke_width (bug #6 fix — the editor canvas renders
 * overlay text from these LOCAL working bars, so every renderer-honored
 * placement field must survive the round-trip).
 *
 * Fields the bar type doesn't model (reveal_s, fade_out_ms, z, word_timings)
 * are preserved by merging bars back over the ORIGINAL API element on Save
 * (`barsToTextElements`) — the editor never destroys state it doesn't edit.
 */

import type { CaptionCue, PlanItemVariant, TextElement } from "@/lib/plan-api";
import type { TextElementBar } from "@/lib/timeline/text-timeline-reducer";

export const TEXT_LANE_BASE_HEIGHT_PX = 48;
export const TEXT_LANE_ROW_GAP_PX = 2;
export const TEXT_LANE_EXPANDED_ROW_HEIGHT_PX = 26;

export interface LaneRow<T> {
  item: T;
  rowIndex: number;
  topPx: number;
  heightPx: number;
}

export interface LaneRows<T> {
  rows: LaneRow<T>[];
  rowCount: number;
  rowHeightPx: number;
  totalHeightPx: number;
}

export type TextLaneRow = LaneRow<TextElementBar> & { bar: TextElementBar };
export interface TextLaneRows extends Omit<LaneRows<TextElementBar>, "rows"> {
  rows: TextLaneRow[];
}

/** UI-only row assignment: current ordered bars map to compacted rows. */
export function deriveLaneRows<T>(
  orderedItems: T[],
  opts: { baseHeightPx: number },
): LaneRows<T> {
  const rowCount = Math.max(1, orderedItems.length);
  const rowHeightPx =
    rowCount <= 2
      ? (opts.baseHeightPx - TEXT_LANE_ROW_GAP_PX * (rowCount - 1)) /
        rowCount
      : TEXT_LANE_EXPANDED_ROW_HEIGHT_PX;
  const totalHeightPx =
    rowCount <= 2
      ? opts.baseHeightPx
      : rowCount * rowHeightPx + (rowCount - 1) * TEXT_LANE_ROW_GAP_PX;

  return {
    rows: orderedItems.map((item, rowIndex) => ({
      item,
      rowIndex,
      topPx: rowIndex * (rowHeightPx + TEXT_LANE_ROW_GAP_PX),
      heightPx: rowHeightPx,
    })),
    rowCount,
    rowHeightPx,
    totalHeightPx,
  };
}

/** UI-only row assignment: current ordered text bars map to compacted rows. */
export function deriveTextLaneRows(bars: TextElementBar[]): TextLaneRows {
  const lane = deriveLaneRows(bars, { baseHeightPx: TEXT_LANE_BASE_HEIGHT_PX });
  return {
    ...lane,
    rows: lane.rows.map((row) => ({ ...row, bar: row.item })),
  };
}

/** Convert API TextElement[] → working bars, keeping all placement + style
 * fields the canvas/inspector edit. */
export function convertApiTextElements(
  apiElements: TextElement[] | null | undefined,
): TextElementBar[] {
  return (apiElements ?? []).map((el) => ({
    id: el.id,
    text: el.text,
    start_s: el.start_s,
    end_s: el.end_s,
    role: el.role,
    font_family: el.font_family ?? undefined,
    size_px: el.size_px ?? undefined,
    size_class: el.size_class ?? undefined,
    color: el.color ?? undefined,
    highlight_color: el.highlight_color ?? undefined,
    stroke_width: el.stroke_width ?? undefined,
    effect: el.effect ?? undefined,
    alignment: el.alignment ?? undefined,
    text_case: el.text_case ?? undefined,
    letter_spacing: el.letter_spacing ?? undefined,
    line_spacing: el.line_spacing ?? undefined,
    max_width_frac: el.max_width_frac ?? undefined,
    position: el.position ?? undefined,
    x_frac: el.x_frac ?? undefined,
    y_frac: el.y_frac ?? undefined,
    source_params: el.source_params ?? undefined,
  })).filter((bar, i) => !apiElements?.[i]?.removed);
}

/** Narrated CaptionCue[] → bars (stable index ids, same as the item page). */
export function convertCaptionCues(
  cues: CaptionCue[] | null | undefined,
): TextElementBar[] {
  return (cues ?? []).map((c, i) => ({
    id: `caption-${i}`,
    text: c.text,
    start_s: c.start_s,
    end_s: c.end_s,
    role: "narrated_caption" as const,
  }));
}

/** scene_timings[] → bars (stable index ids; untimed scenes skipped). */
export function convertSceneTimings(
  scenes:
    | Array<{ text: string; start_s: number | null; end_s: number | null }>
    | null
    | undefined,
): TextElementBar[] {
  return (scenes ?? [])
    .filter((s) => s.start_s != null && s.end_s != null)
    .map((s, i) => ({
      id: `scene-${i}`,
      text: s.text,
      start_s: s.start_s as number,
      end_s: s.end_s as number,
      role: "generative_sequence" as const,
    }));
}

/** Seed the editor's working bars from a variant.
 *
 * Once the user has committed text_elements, that persisted list owns reload
 * state.  Projection sources (caption_cues / scene_timings) are only seeds for
 * variants that have never been user-edited.
 */
export function seedBarsFromVariant(
  variant: PlanItemVariant,
): TextElementBar[] {
  if (variant.text_elements_user_edited) {
    return convertApiTextElements(variant.text_elements);
  }
  if (variant.caption_cues?.length)
    return convertCaptionCues(variant.caption_cues);
  if (variant.scene_timings?.length)
    return convertSceneTimings(variant.scene_timings);
  return convertApiTextElements(variant.text_elements);
}

/**
 * Working bars → API TextElement[] for preview layout + Save.
 *
 * Each bar merges OVER its original API element (when one exists) so fields
 * the editor doesn't model (reveal_s, fade_out_ms, z, word_timings) pass
 * through untouched. narrated_caption bars are excluded — captions persist
 * via their own endpoint, not text_elements (same rule as the item page).
 */
export function barsToTextElements(
  bars: TextElementBar[],
  originalById: ReadonlyMap<string, TextElement>,
): TextElement[] {
  return bars
    .filter((bar) => bar.role !== "narrated_caption")
    .map((bar) => {
      const original = originalById.get(bar.id);
      return {
        ...(original ?? {}),
        id: bar.id,
        text: bar.text,
        start_s: bar.start_s,
        end_s: bar.end_s,
        role: bar.role as TextElement["role"],
        font_family: bar.font_family ?? null,
        size_px: bar.size_px ?? null,
        size_class: (bar.size_class as TextElement["size_class"]) ?? null,
        color: bar.color ?? null,
        highlight_color: bar.highlight_color ?? null,
        stroke_width: bar.stroke_width ?? null,
        effect: (bar.effect as TextElement["effect"]) ?? null,
        alignment: (bar.alignment as TextElement["alignment"]) ?? null,
        text_case: (bar.text_case as TextElement["text_case"]) ?? null,
        letter_spacing: bar.letter_spacing ?? null,
        line_spacing: bar.line_spacing ?? null,
        max_width_frac: bar.max_width_frac ?? null,
        position:
          (bar.position as TextElement["position"]) ?? original?.position,
        x_frac: bar.x_frac ?? null,
        y_frac: bar.y_frac ?? null,
        source_params: bar.source_params ?? null,
      };
    });
}

/** Working narrated-caption bars -> API CaptionCue[] for the full-editor Save. */
export function barsToCaptionCues(bars: TextElementBar[]): CaptionCue[] {
  return bars
    .filter((bar) => bar.role === "narrated_caption")
    .map((bar) => ({
      text: bar.text,
      start_s: bar.start_s,
      end_s: bar.end_s,
    }));
}
