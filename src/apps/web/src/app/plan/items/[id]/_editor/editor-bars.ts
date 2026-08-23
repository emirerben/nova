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
import type { CaptionMetaPatch } from "@/lib/edit-copilot/ops";
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

export function isCaptionBar(bar: TextElementBar | null | undefined): boolean {
  return bar?.role === "narrated_caption";
}

export interface CaptionTextReplacement {
  patches: Array<{ id: string; patch: { text: string } }>;
  foundMatchCount: number;
  matchCount: number;
  lineCount: number;
}

/** Case-insensitive, literal replacement over every narrated caption bar.
 * The callback replacement is intentional: `$&`, `$1`, and friends in user
 * text stay literal instead of being interpreted by String.replace. */
export function buildCaptionTextReplacement(
  bars: readonly TextElementBar[],
  find: string,
  replace: string,
): CaptionTextReplacement {
  const needle = find.trim();
  if (!needle) return { patches: [], foundMatchCount: 0, matchCount: 0, lineCount: 0 };
  const pattern = new RegExp(
    needle.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"),
    "gi",
  );
  let foundMatchCount = 0;
  let matchCount = 0;
  const patches = bars
    .filter(isCaptionBar)
    .reduce<Array<{ id: string; patch: { text: string } }>>((acc, bar) => {
      const next = bar.text.replace(pattern, (match) => {
        foundMatchCount += 1;
        if (match !== replace) matchCount += 1;
        return replace;
      });
      if (next !== bar.text) acc.push({ id: bar.id, patch: { text: next } });
      return acc;
    }, []);
  return { patches, foundMatchCount, matchCount, lineCount: patches.length };
}

/**
 * True only for the AI-authored editorial sequence text (the transcript-synced
 * typographic sequence, or its rhythm-mode quote fallback — EDITORIAL_SEQUENCE_ENABLED;
 * see `text_element.py`'s `source="sequence_scene"` provenance marker persisted
 * on `source_params`).
 *
 * `role === "generative_sequence"` alone is NOT sufficient: the editor's own
 * "split and place" composition tool (EditorShell.splitAndPlaceText) reuses the
 * SAME role for ordinary user-typed multi-block text, with no `source_params`.
 * Without this narrower check, badging every `generative_sequence` bar would
 * mislabel user-authored text as AI-generated.
 */
export function isAiSequenceBar(bar: TextElementBar | null | undefined): boolean {
  return bar?.role === "generative_sequence" && bar?.source_params?.source === "sequence_scene";
}

/** Inspector/timeline badge copy for an AI-authored sequence bar. */
export const AI_SEQUENCE_BADGE_LABEL = "AI sequence";
export const AI_SEQUENCE_BADGE_TOOLTIP =
  "Written by AI from your video's speech — edit or delete it freely";

export function captionMetaPatchFromCaptionBarPatch(
  patch: Partial<Omit<TextElementBar, "id" | "role">>,
): CaptionMetaPatch {
  const metaPatch: CaptionMetaPatch = {};
  if (Object.prototype.hasOwnProperty.call(patch, "font_family")) {
    metaPatch.font = patch.font_family ?? null;
  }
  if (typeof patch.size_px === "number") {
    metaPatch.size_px = patch.size_px;
  }
  if (typeof patch.color === "string") {
    metaPatch.color = patch.color;
  }
  if (typeof patch.highlight_color === "string") {
    metaPatch.highlight_color = patch.highlight_color;
  }
  if (typeof patch.stroke_width === "number") {
    metaPatch.stroke_width = patch.stroke_width;
  }
  if (typeof patch.shadow_enabled === "boolean") {
    metaPatch.shadow_enabled = patch.shadow_enabled;
  }
  if (typeof patch.y_frac === "number") {
    metaPatch.y_frac = patch.y_frac;
  }
  return metaPatch;
}

/**
 * Inverse of `captionMetaPatchFromCaptionBarPatch`: variant-level caption meta
 * → the LOCAL bar fields that preview it.
 *
 * Load-bearing for the Captions drawer's "All captions" section. The meta patch
 * alone only changes what SAVE sends; the canvas caption preview and the
 * timeline bars read their styling off the bars themselves (see EditorCanvas's
 * `captionPreviewStyle`, which resolves `bar?.font_family ?? variant.…`). So a
 * global font change must be fanned across EVERY caption bar — patching one bar
 * previews the new font only while that single cue is on screen, which is the
 * behaviour the drawer replaced.
 *
 * `enabled` and `style` are deliberately absent: they have no per-bar
 * equivalent (the canvas reads caption on/off and sentence-vs-word from the
 * meta snapshot directly), so mapping them here would invent bar fields.
 */
export function captionBarPatchFromMetaPatch(
  patch: CaptionMetaPatch,
): Partial<Omit<TextElementBar, "id" | "role">> {
  const barPatch: Partial<Omit<TextElementBar, "id" | "role">> = {};
  // hasOwnProperty, not truthiness: `font: null` is "reset to the default
  // face", a real edit, and must not be dropped as falsy.
  if (Object.prototype.hasOwnProperty.call(patch, "font")) {
    barPatch.font_family = patch.font ?? undefined;
  }
  if (typeof patch.size_px === "number") barPatch.size_px = patch.size_px;
  if (typeof patch.color === "string") barPatch.color = patch.color;
  if (typeof patch.highlight_color === "string") {
    barPatch.highlight_color = patch.highlight_color;
  }
  if (typeof patch.stroke_width === "number") barPatch.stroke_width = patch.stroke_width;
  if (typeof patch.shadow_enabled === "boolean") {
    barPatch.shadow_enabled = patch.shadow_enabled;
  }
  if (typeof patch.y_frac === "number") barPatch.y_frac = patch.y_frac;
  return barPatch;
}

export function localCaptionBarPatchFromPatch(
  patch: Partial<Omit<TextElementBar, "id" | "role">>,
): Partial<Omit<TextElementBar, "id" | "role">> {
  const localPatch: Partial<Omit<TextElementBar, "id" | "role">> = {};
  for (const key of [
    "text",
    "start_s",
    "end_s",
    "font_family",
    "size_px",
    "size_class",
    "color",
    "highlight_color",
    "stroke_width",
    "shadow_enabled",
    "y_frac",
    // 4b emphasize toggle: hasOwnProperty (not truthiness) below lets an
    // explicit null/false clear-emphasis patch through, not just a truthy set.
    "smart_style",
    "smart_emphasis",
    // Lane PR-A per-cue overrides ("This caption" section) — hasOwnProperty lets
    // an explicit null clear-override patch through, same convention as above.
    "cue_font_family",
    "cue_text_color",
    "cue_size_px",
  ] as const) {
    if (Object.prototype.hasOwnProperty.call(patch, key)) {
      (localPatch as Record<string, unknown>)[key] = patch[key];
    }
  }
  return localPatch;
}

function captionBarId(index: number): string {
  return `caption-${index}`;
}

function captionIndexFromBarId(id: string): number | null {
  const match = id.match(/^caption-(\d+)$/);
  if (!match) return null;
  const index = Number(match[1]);
  return Number.isFinite(index) ? index : null;
}

function isSyntheticSubtitledCaptionBar(bar: TextElementBar): boolean {
  return /^subtitled-caption-\d+$/.test(bar.id);
}

function isCaptionCueProjection(bar: TextElementBar): boolean {
  return bar.source_params?.source === "caption_cue";
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

/** Stable stacking order for media rows. Timeline row order is visual chrome,
 * not compositing, but matching it to z makes overlapping cards predictable and
 * keeps equal-z cards deterministic across API reloads. */
export function sortMediaTimelineBars<
  T extends { id: string; start_s: number; z?: number | null },
>(items: readonly T[]): T[] {
  return items
    .map((item, index) => ({ item, index }))
    .sort((a, b) => {
      const zA = Number.isFinite(a.item.z) ? (a.item.z as number) : 0;
      const zB = Number.isFinite(b.item.z) ? (b.item.z as number) : 0;
      return zA - zB || a.item.start_s - b.item.start_s || a.item.id.localeCompare(b.item.id) || a.index - b.index;
    })
    .map(({ item }) => item);
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
    shadow_style: el.shadow_style ?? undefined,
    glow_color: el.glow_color ?? undefined,
    glow_strength: el.glow_strength ?? undefined,
    effect: el.effect ?? undefined,
    motion: el.motion ?? undefined,
    theme_transition: el.theme_transition ?? undefined,
    fade_out_ms: el.fade_out_ms ?? undefined,
    reveal_s: el.reveal_s ?? undefined,
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
  variant?: PlanItemVariant,
): TextElementBar[] {
  return (cues ?? []).map((c, i) => ({
    id: captionBarId(i),
    text: c.text,
    start_s: c.start_s,
    end_s: c.end_s,
    role: "narrated_caption" as const,
    font_family: variant?.voiceover_caption_font ?? undefined,
    size_px: variant?.caption_size_px ?? undefined,
    color: variant?.caption_text_color ?? undefined,
    highlight_color: variant?.caption_highlight_color ?? undefined,
    stroke_width: variant?.caption_stroke_width ?? undefined,
    shadow_enabled: variant?.caption_shadow_enabled ?? undefined,
    // 4b: server-authored role/style/emphasis, surfaced for the inspector's
    // badge + emphasize toggle. undefined (not null) so an untouched bar's
    // Save round-trips through the ORIGINAL cue (see barsToCaptionCues).
    smart_role: c.smart_role ?? undefined,
    smart_style: c.smart_style ?? undefined,
    smart_emphasis: c.smart_emphasis ?? undefined,
    // Lane PR-A per-cue overrides ("This caption" section) — DISTINCT from the
    // font_family/size_px/color above, which hold the variant-level "All
    // captions" GLOBAL preview. undefined (not null) so an untouched bar's
    // Save round-trips through the ORIGINAL cue's override (see barsToCaptionCues).
    cue_font_family: c.font_family ?? undefined,
    cue_text_color: c.text_color ?? undefined,
    cue_size_px: c.size_px ?? undefined,
  }));
}

/**
 * smart_role (chunker's SemanticRole vocabulary, read-only) → smart_style
 * (the closed ASS-style token set `persist_variant_captions` accepts, mirrors
 * `routes/generative_jobs.py` CaptionCue.smart_style Literal). Every name
 * matches its style token except "context_shift" → "context". Falls back to
 * "hook" for a role-less cue (e.g. legacy/plain cues) so the Emphasize toggle
 * always has a valid closed-set value to send.
 */
const ROLE_TO_SMART_STYLE: Record<
  NonNullable<TextElementBar["smart_role"]>,
  NonNullable<TextElementBar["smart_style"]>
> = {
  hook: "hook",
  context_shift: "context",
  list_item: "list_item",
  example: "example",
  payoff: "payoff",
  cta: "cta",
};

export function smartStyleForRole(
  role: TextElementBar["smart_role"] | null | undefined,
): NonNullable<TextElementBar["smart_style"]> {
  return (role && ROLE_TO_SMART_STYLE[role]) || "hook";
}

/** Badge copy for the caption role chip (InspectorPanel). */
export const SMART_ROLE_BADGE_LABELS: Record<NonNullable<TextElementBar["smart_role"]>, string> = {
  hook: "Hook",
  context_shift: "Context",
  list_item: "List",
  example: "Example",
  payoff: "Payoff",
  cta: "CTA",
};

/**
 * Editor-side font-size PREVIEW scale for a caption cue, keyed by smart_style.
 * Mirrors the relative role→size hierarchy in `_SMART_CAPTION_TAGS`
 * (app/pipeline/captions.py — kept after 4a removed only that dict's color
 * override) against the narrated default of 72px (`_ass_header_for`). This is
 * an ordering approximation, not a byte-exact burn size: the real burn size
 * is a text-fit measurement (`measure_caption`) the editor can't replicate
 * without running the same layout engine. It exists so "editor == output"
 * holds for RELATIVE size (emphasized cues visibly look bigger here too).
 */
const SMART_CAPTION_ROLE_SIZE_SCALE: Partial<Record<NonNullable<TextElementBar["smart_style"]>, number>> = {
  hook: 1.14,
  list_item: 1.14,
  payoff: 1.14,
  cta: 1.08,
  context: 1.06,
  example: 0.97,
};

export function smartCaptionPreviewSizePx(
  baseSizePx: number,
  smartStyle: TextElementBar["smart_style"] | null | undefined,
): number {
  const scale = smartStyle ? SMART_CAPTION_ROLE_SIZE_SCALE[smartStyle] : undefined;
  return Math.round(baseSizePx * (scale ?? 1));
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
  const textBars = filterLyrics(convertApiTextElements(variant.text_elements)).filter(
    (bar) => !isCaptionCueProjection(bar),
  );
  const captionBars = convertCaptionCues(variant.caption_cues, variant);
  if (captionBars.length) return [...captionBars, ...textBars];
  if (variant.text_elements_user_edited) {
    return textBars;
  }
  if (textBars.length) return textBars;
  if (variant.scene_timings?.length)
    return convertSceneTimings(variant.scene_timings);
  return textBars;
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
        shadow_style: bar.shadow_style ?? null,
        glow_color: bar.glow_color ?? original?.glow_color ?? null,
        glow_strength: bar.glow_strength ?? original?.glow_strength ?? null,
        effect: (bar.effect as TextElement["effect"]) ?? null,
        motion: Object.prototype.hasOwnProperty.call(bar, "motion")
          ? (bar.motion ?? null)
          : (original?.motion ?? null),
        theme_transition: bar.theme_transition ?? null,
        fade_out_ms: bar.fade_out_ms ?? original?.fade_out_ms ?? null,
        reveal_s: bar.reveal_s ?? original?.reveal_s ?? null,
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

/** Working caption bars -> API CaptionCue[] for the full-editor Save. */
export function barsToCaptionCues(
  bars: TextElementBar[],
  originalById: ReadonlyMap<string, CaptionCue> = new Map(),
): CaptionCue[] {
  return bars
    .filter((bar) => isCaptionBar(bar) && !isSyntheticSubtitledCaptionBar(bar))
    .map((bar) => {
      const cue: CaptionCue = {
        ...(originalById.get(bar.id) ??
          (captionIndexFromBarId(bar.id) != null
            ? originalById.get(captionBarId(captionIndexFromBarId(bar.id) as number))
            : undefined) ??
          {}),
        text: bar.text,
        start_s: bar.start_s,
        end_s: bar.end_s,
      };
      // 4b Emphasize toggle: undefined means untouched (the original spread
      // above already carries whatever the server authored); a defined value
      // (including explicit null/false from clearing) is the user's edit.
      if (bar.smart_style !== undefined) cue.smart_style = bar.smart_style;
      if (bar.smart_emphasis !== undefined) cue.smart_emphasis = bar.smart_emphasis;
      // Lane PR-A per-cue overrides: same undefined-vs-explicit convention.
      // undefined ⇒ untouched (the original spread above already carries
      // whatever this cue had); null ⇒ an explicit "match all captions" clear;
      // a value ⇒ an explicit per-cue override.
      if (bar.cue_font_family !== undefined) cue.font_family = bar.cue_font_family;
      if (bar.cue_text_color !== undefined) cue.text_color = bar.cue_text_color;
      if (bar.cue_size_px !== undefined) cue.size_px = bar.cue_size_px;
      return cue;
    });
}
