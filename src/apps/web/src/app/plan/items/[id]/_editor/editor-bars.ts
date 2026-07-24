/**
 * Editor working-state converters: API variant data ↔ TextElementBar[].
 *
 * Same seeding precedence as the item page for untouched variants. Caption
 * cues keep their special save path, then API text_elements win because they
 * carry the renderer-projected words and geometry for generated text.
 * This keeps reload from resurrecting sequence-projected bars after Save.
 *
 * The API text-element path maps WITHOUT dropping position / x_frac / y_frac /
 * highlight_color / stroke_width (bug #6 fix — the editor canvas renders
 * overlay text from these LOCAL working bars, so every renderer-honored
 * placement field must survive the round-trip).
 *
 * Fields the bar type doesn't model (reveal_s, z, word_timings)
 * are preserved by merging bars back over the ORIGINAL API element on Save
 * (`barsToTextElements`) — the editor never destroys state it doesn't edit.
 */

import type { CaptionCue, PlanItemVariant, TextElement } from "@/lib/plan-api";
import type { LyricLineOverride } from "@/lib/editor-commit";
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

const LYRIC_KEY_RE = /^lyric_(L\d+)$/;

export function isLyricBar(bar: TextElementBar | TextElement | null | undefined): boolean {
  return bar?.role === "lyric_line";
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
    visual_block_id: el.visual_block_id ?? undefined,
    font_family: el.font_family ?? undefined,
    size_px: el.size_px ?? undefined,
    size_class: el.size_class ?? undefined,
    color: el.color ?? undefined,
    highlight_color: el.highlight_color ?? undefined,
    stroke_width: el.stroke_width ?? undefined,
    shadow_enabled: el.shadow_enabled ?? undefined,
    glow_color: el.glow_color ?? undefined,
    glow_strength: el.glow_strength ?? undefined,
    effect: el.effect ?? undefined,
    theme_transition: el.theme_transition ?? undefined,
    fade_out_ms: el.fade_out_ms ?? undefined,
    alignment: el.alignment ?? undefined,
    text_case: el.text_case ?? undefined,
    letter_spacing: el.letter_spacing ?? undefined,
    line_spacing: el.line_spacing ?? undefined,
    max_width_frac: el.max_width_frac ?? undefined,
    position: el.position ?? undefined,
    x_frac: el.x_frac ?? undefined,
    y_frac: el.y_frac ?? undefined,
    rotation_deg: el.rotation_deg ?? undefined,
    source_params: el.source_params ?? undefined,
    behind_subject: el.behind_subject ?? undefined,
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
 * state. Caption cues are still seeded first so narrated caption saves continue
 * through the caption endpoint. For generated sequence/intro text, prefer the
 * API's projected text_elements over scene_timings because scene_timings is a
 * timing-only compatibility shim and may not carry text placement/style.
 */
export function seedBarsFromVariant(
  variant: PlanItemVariant,
  opts: { includeLyrics?: boolean } = {},
): TextElementBar[] {
  const includeLyrics = opts.includeLyrics ?? true;
  const filterLyrics = (bars: TextElementBar[]) =>
    includeLyrics ? bars : bars.filter((bar) => !isLyricBar(bar));
  if (variant.text_elements_user_edited) {
    return filterLyrics(convertApiTextElements(variant.text_elements));
  }
  if (variant.resolved_archetype !== "subtitled" && variant.caption_cues?.length)
    return convertCaptionCues(variant.caption_cues);
  if (variant.text_elements?.length)
    return filterLyrics(convertApiTextElements(variant.text_elements));
  if (variant.scene_timings?.length)
    return convertSceneTimings(variant.scene_timings);
  return filterLyrics(convertApiTextElements(variant.text_elements));
}

/**
 * Lyrics-optional "elements" model: convert the `GET .../lyric-seeds`
 * response (TextElement-shaped dicts, role "lyric_line") into working bars
 * for a single ADD_LYRIC_BARS dispatch. Reuses convertApiTextElements — the
 * seed shape is a plain TextElement[], so no bespoke mapping is needed.
 *
 * Normalizes a bare "karaoke" effect (the contract's word-timed shorthand) to
 * "karaoke-line" — the literal every renderer/style path in this codebase
 * (overlay-animation.ts, overlay-layout.ts, TextLane.tsx) actually matches on
 * for the per-word highlight sweep. Word-timed bars without an explicit
 * effect also default to it, since `word_timings` alone means nothing to the
 * renderer without the effect flag.
 */
export function seedBarsFromLyricSeeds(elements: TextElement[]): TextElementBar[] {
  const normalized = elements.map((el) => {
    const raw = el.effect as string | null | undefined;
    if (raw === "karaoke") return { ...el, effect: "karaoke-line" as TextElement["effect"] };
    if (!raw && el.word_timings?.length) {
      return { ...el, effect: "karaoke-line" as TextElement["effect"] };
    }
    return el;
  });
  return convertApiTextElements(normalized);
}

function lyricKeyForBar(bar: TextElementBar): string | null {
  const sourceKey = bar.source_params?.key;
  if (typeof sourceKey === "string" && /^L\d+$/.test(sourceKey)) return sourceKey;
  const match = bar.id.match(LYRIC_KEY_RE);
  return match?.[1] ?? null;
}

function sourceTextFor(original: TextElement): string {
  const sourceText = original.source_params?.source_text;
  return typeof sourceText === "string" ? sourceText : original.text;
}

function sameOptional<T>(a: T | null | undefined, b: T | null | undefined): boolean {
  return (a ?? null) === (b ?? null);
}

export function buildLyricLineOverrides(
  bars: TextElementBar[],
  originalsById: ReadonlyMap<string, TextElement>,
): Record<string, LyricLineOverride> {
  const overrides: Record<string, LyricLineOverride> = {};
  originalsById.forEach((original, id) => {
    if (!isLyricBar(original)) return;
    const bar = bars.find((candidate) => candidate.id === id);
    if (!bar) {
      throw new Error(`Missing locked lyric bar ${id}`);
    }
    const key = lyricKeyForBar(bar);
    if (!key) return;
    // Server style validation accepts concrete values only (no nulls) — a
    // cleared field simply omits its key and falls back to the burned style.
    const style: NonNullable<LyricLineOverride["style"]> = {};
    if (bar.color != null && !sameOptional(bar.color, original.color)) style.color = bar.color;
    if (
      bar.highlight_color != null &&
      !sameOptional(bar.highlight_color, original.highlight_color)
    ) {
      style.highlight_color = bar.highlight_color;
    }
    if (bar.font_family != null && !sameOptional(bar.font_family, original.font_family)) {
      style.font_family = bar.font_family;
    }
    if (bar.size_px != null && !sameOptional(bar.size_px, original.size_px)) {
      style.size_px = bar.size_px;
    }
    const textChanged = bar.text !== original.text;
    const styleChanged = Object.keys(style).length > 0;
    if (!textChanged && !styleChanged) return;
    overrides[key] = {
      ...(textChanged ? { text: bar.text } : {}),
      ...(styleChanged ? { style } : {}),
      orig_text: sourceTextFor(original),
      // Projected element timing is video time, while the server fingerprint
      // compares track time; orig_text is the authoritative drift check.
      orig_start_s: original.start_s,
    };
  });
  return overrides;
}

/**
 * Working bars → API TextElement[] for preview layout + Save.
 *
 * Each bar merges OVER its original API element (when one exists) so fields
 * the editor doesn't model (reveal_s, z, word_timings) pass
 * through untouched. narrated_caption bars are excluded — captions persist
 * via their own endpoint, not text_elements (same rule as the item page).
 *
 * `includeLyrics` defaults to false (legacy behaviour: baked-model lyric_line
 * bars persist through the separate `lyrics.line_overrides` commit section,
 * not text_elements). The lyrics-optional "elements" model passes `true` —
 * on those variants lyric_line bars are ordinary persisted text elements.
 */
export function barsToTextElements(
  bars: TextElementBar[],
  originalById: ReadonlyMap<string, TextElement>,
  opts: { includeLyrics?: boolean } = {},
): TextElement[] {
  return barsToTextElementsInternal(bars, originalById, {
    includeLyrics: opts.includeLyrics ?? false,
  });
}

export function barsToPreviewTextElements(
  bars: TextElementBar[],
  originalById: ReadonlyMap<string, TextElement>,
): TextElement[] {
  return barsToTextElementsInternal(bars, originalById, { includeLyrics: true });
}

function barsToTextElementsInternal(
  bars: TextElementBar[],
  originalById: ReadonlyMap<string, TextElement>,
  opts: { includeLyrics: boolean },
): TextElement[] {
  return bars
    .filter(
      (bar) =>
        bar.role !== "narrated_caption" &&
        (opts.includeLyrics || !isLyricBar(bar)),
    )
    .map((bar) => {
      const original = originalById.get(bar.id);
      return {
        ...(original ?? {}),
        id: bar.id,
        text: bar.text,
        start_s: bar.start_s,
        end_s: bar.end_s,
        visual_block_id: bar.visual_block_id ?? null,
        role: bar.role as TextElement["role"],
        font_family: bar.font_family ?? null,
        size_px: bar.size_px ?? null,
        size_class: (bar.size_class as TextElement["size_class"]) ?? null,
        color: bar.color ?? null,
        highlight_color: bar.highlight_color ?? null,
        stroke_width: bar.stroke_width ?? null,
        shadow_enabled: bar.shadow_enabled ?? null,
        glow_color: bar.glow_color ?? original?.glow_color ?? null,
        glow_strength: bar.glow_strength ?? original?.glow_strength ?? null,
        effect: (bar.effect as TextElement["effect"]) ?? null,
        theme_transition: bar.theme_transition ?? null,
        fade_out_ms: bar.fade_out_ms ?? original?.fade_out_ms ?? null,
        alignment: (bar.alignment as TextElement["alignment"]) ?? null,
        text_case: (bar.text_case as TextElement["text_case"]) ?? null,
        letter_spacing: bar.letter_spacing ?? null,
        line_spacing: bar.line_spacing ?? null,
        max_width_frac: bar.max_width_frac ?? null,
        position:
          (bar.position as TextElement["position"]) ?? original?.position,
        x_frac: bar.x_frac ?? null,
        y_frac: bar.y_frac ?? null,
        rotation_deg: bar.rotation_deg ?? null,
        source_params: bar.source_params ?? null,
        behind_subject: bar.behind_subject ?? false,
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
