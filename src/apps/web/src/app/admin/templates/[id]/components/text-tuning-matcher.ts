/**
 * Pure matcher logic for the admin "Text Tuning" panel.
 *
 * Finds the hook overlays the panel can fine-tune (subject font size, prefix
 * font size, vertical positions). Extracted from EditorTab.tsx so it can be
 * unit-tested without mounting React, and so the matcher can evolve without
 * touching the panel's UI code.
 *
 * Layout this matcher must support:
 *   1. Single-slot layout: one slot with subject + prefix overlays side-by-side
 *      (the original assumption when TextTuningPanel was written).
 *   2. Cross-slot layout: subject and prefix on different adjacent slots, plus
 *      an optional joint-caption slot (e.g., Dimples Passport: slot 4
 *      "Welcome to" → slot 5 "PERU" → slot 6 "Welcome to PERU"). The original
 *      matcher silently failed on this — every Dimples slot has only 1 overlay.
 */

// We work with loose shapes here because text_size_px and position_y_frac
// aren't on the typed RecipeTextOverlay (the backend schema accepts them as
// optional fine-tuning fields). Keeping the matcher at this level lets it
// also handle slot dicts that came from the API as `text_overlays` OR
// `overlays` (the API has accepted both spellings).
export type EditableOverlay = Record<string, unknown> & {
  text?: unknown;
  sample_text?: unknown;
  effect?: unknown;
  text_size?: unknown;
  text_size_px?: unknown;
  position_y_frac?: unknown;
};

export type EditableSlot = {
  text_overlays?: EditableOverlay[];
  overlays?: EditableOverlay[];
};

export interface HookOverlayMatch {
  /** The big animated overlay that holds the user-input subject (e.g., "PERU"). */
  subject: EditableOverlay;
  /** The small leading text (e.g., "Welcome to"), or null if not present. */
  prefix: EditableOverlay | null;
  /**
   * Overlays whose text contains the subject string verbatim, e.g., the slot 6
   * "Welcome to PERU" caption that mirrors slot 5's PERU. The Apply step keeps
   * these in sync with subject's text_size_px so the joined caption matches.
   */
  jointCaptions: EditableOverlay[];
}

const JUMBO_SIZES = /jumbo|xxlarge|xlarge/;

function overlayText(o: EditableOverlay): string {
  if (typeof o.text === "string" && o.text.length > 0) return o.text;
  if (typeof o.sample_text === "string") return o.sample_text;
  return "";
}

function isJumboSize(size: unknown): boolean {
  return typeof size === "string" && JUMBO_SIZES.test(size);
}

/**
 * Walk every overlay across every slot. Picking subject first lets the
 * matcher work whether the layout is single-slot (legacy) or cross-slot
 * (Dimples Passport) — the structural shape doesn't matter, only the
 * per-overlay attributes.
 */
export function findHookOverlays(
  slots: EditableSlot[] | undefined | null,
): HookOverlayMatch | null {
  if (!slots || slots.length === 0) return null;

  const allOverlays: EditableOverlay[] = [];
  for (const slot of slots) {
    const overlays = slot.text_overlays ?? slot.overlays;
    if (!overlays) continue;
    for (const ov of overlays) allOverlays.push(ov);
  }
  if (allOverlays.length === 0) return null;

  // Subject: prefer font-cycle (the animation that defines the hook moment),
  // fall back to any overlay flagged as jumbo/xxlarge/xlarge. If neither
  // exists, this template doesn't have a tunable hook subject.
  const subject =
    allOverlays.find((o) => o.effect === "font-cycle") ??
    allOverlays.find((o) => isJumboSize(o.text_size));
  if (!subject) return null;

  const subjectText = overlayText(subject);

  // Joint captions echo the subject text within a longer phrase. They must
  // track subject.text_size_px so the joined "Welcome to PERU" doesn't
  // visually clash with the standalone "PERU".
  const jointCaptions = allOverlays.filter((o) => {
    if (o === subject) return false;
    if (subjectText.length === 0) return false;
    const t = overlayText(o);
    return t !== subjectText && t.includes(subjectText);
  });

  // Prefix: the small leading text. Heuristic: any overlay with text that has
  // at least one lowercase letter (rules out other ALL-CAPS placeholders) and
  // is neither the subject nor a joint caption. We pick the first match in
  // slot order, which mirrors the user's reading order.
  const prefix =
    allOverlays.find((o) => {
      if (o === subject) return false;
      if (jointCaptions.includes(o)) return false;
      const t = overlayText(o);
      if (!t) return false;
      return /[a-z]/.test(t);
    }) ?? null;

  return { subject, prefix, jointCaptions };
}

/**
 * Apply tuned values back onto the matched overlays. Mutates in place because
 * the caller passes the live recipe object that gets PUT to the backend.
 *
 * Joint captions inherit subjectSize so the merged caption visually matches
 * the standalone subject overlay. We do NOT touch the joint caption's Y
 * because it usually sits at a different position than the standalone subject.
 */
export interface TuningValues {
  subjectSize: number;
  subjectY: number;
  prefixSize: number;
  prefixY: number;
}

export function applyTuning(
  match: HookOverlayMatch,
  values: TuningValues,
): void {
  match.subject.text_size_px = values.subjectSize;
  match.subject.position_y_frac = values.subjectY;
  if (match.prefix) {
    match.prefix.text_size_px = values.prefixSize;
    match.prefix.position_y_frac = values.prefixY;
  }
  for (const caption of match.jointCaptions) {
    caption.text_size_px = values.subjectSize;
  }
}

/**
 * Read currently-applied tuning values from a match so the panel can pre-load
 * with what's actually live in the recipe (not the panel's hardcoded defaults).
 *
 * Falls back to the named text_size enum via sizeMap when text_size_px isn't set.
 */
export function readTuning(
  match: HookOverlayMatch,
  sizeMap: Record<string, number>,
): Partial<TuningValues> {
  const out: Partial<TuningValues> = {};
  const sSize = match.subject.text_size_px;
  if (typeof sSize === "number" && sSize > 0) {
    out.subjectSize = sSize;
  } else if (typeof match.subject.text_size === "string" && sizeMap[match.subject.text_size]) {
    out.subjectSize = sizeMap[match.subject.text_size];
  }
  if (typeof match.subject.position_y_frac === "number") {
    out.subjectY = match.subject.position_y_frac;
  }
  if (match.prefix) {
    const pSize = match.prefix.text_size_px;
    if (typeof pSize === "number" && pSize > 0) {
      out.prefixSize = pSize;
    } else if (typeof match.prefix.text_size === "string" && sizeMap[match.prefix.text_size]) {
      out.prefixSize = sizeMap[match.prefix.text_size];
    }
    if (typeof match.prefix.position_y_frac === "number") {
      out.prefixY = match.prefix.position_y_frac;
    }
  }
  return out;
}
