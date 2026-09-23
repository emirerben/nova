// sfx-browse.ts — pure search + category grouping for the creator SFX pickers
// (ToolDrawer's Sounds drawer, SfxLane's popover). The KRI-173 library is
// ~120 effects; each carries a `category` (null on legacy uploads) and
// lowercase `search_terms` from GET /sound-effects.
//
// Matching is whole-word and case-insensitive over name + search_terms, with
// the plural fold and filler list of the API's chat resolver (KRI-173's
// app/services/sfx_catalog.py `_stem` / `_FILLER`), so "buzzer sounds" finds
// "Wrong buzzer" but "tap" never finds "Tape rewind". A half-typed last word
// matches word starts only when nothing matches whole (see groupSfxEffects).

import type { SoundEffectSummary } from "@/lib/sfx-api";

/** Display order: editing staples first, reactions next, niche last. Every
 *  API category must appear here; anything else falls into "Other". */
export const SFX_CATEGORY_ORDER = [
  "transition",
  "impact",
  "comedy",
  "approval",
  "rejection",
  "suspense",
  "money",
  "sports",
  "ui",
] as const;

export type SfxCategory = (typeof SFX_CATEGORY_ORDER)[number];
export type SfxGroupKey = SfxCategory | "other";

export const SFX_CATEGORY_LABELS: Record<SfxGroupKey, string> = {
  transition: "Transitions",
  impact: "Impacts",
  comedy: "Comedy",
  approval: "Approval",
  rejection: "Rejection",
  suspense: "Suspense",
  money: "Money",
  sports: "Sports",
  ui: "Text & UI",
  other: "Other",
};

export interface SfxGroup {
  key: SfxGroupKey;
  label: string;
  effects: SoundEffectSummary[];
}

// Letters and digits in any script. Text is decomposed and stripped of
// accents first, so "Olé" reads "ole", "şok" reads "sok" (one word — never
// "ok"), and a Turkish "İmpact" or "ımpact" still finds "Impact".
// Lowercasing is locale-independent on purpose: toLocaleLowerCase() on a
// Turkish browser turns "UI" into "uı", which would never match "ui".
const WORD_RE = /[\p{L}\p{N}]+/gu;
const MARKS_RE = /\p{M}+/gu;

// Words that describe the request, not the sound ("a buzzer sound effect").
// Same list as the API resolver's `_FILLER`.
const QUERY_FILLER = new Set([
  "a", "an", "the", "some", "any", "sound", "sounds", "effect", "effects", "sfx",
  "noise", "my", "this", "that", "it", "of", "to", "for", "with", "and", "please",
  "like", "kind", "type", "little", "quick", "one",
]);

function stem(word: string): string {
  return word.length > 3 && word.endsWith("s") && !word.endsWith("ss")
    ? word.slice(0, -1)
    : word;
}

// Every form a word can match as: itself, the API's `_stem`, and "-es" off
// sh/ch/x/z/ss plurals ("punches" → "punch"), which `_stem` misses. Both the
// query and the effect words expand this way, so either side may be plural.
function wordForms(word: string): string[] {
  const forms = [word, stem(word)];
  if (word.length > 4 && /(?:sh|ch|x|z|ss)es$/.test(word)) forms.push(word.slice(0, -2));
  return forms;
}

function rawWords(text: unknown): string[] {
  return (
    String(text ?? "")
      .normalize("NFKD")
      .replace(MARKS_RE, "")
      .toLowerCase()
      .replace(/ı/g, "i")
      .match(WORD_RE) ?? []
  );
}

/** Whole words of `text`, lowercased, accent-folded and plural-folded. */
export function sfxWords(text: string | null | undefined): string[] {
  return rawWords(text).map(stem);
}

// Effect objects are stable across renders, so index each one's words once
// instead of re-splitting the whole library on every keystroke. The entry is
// reused only while the name and terms are the same values.
const effectWordCache = new WeakMap<
  SoundEffectSummary,
  { name: string; terms: SoundEffectSummary["search_terms"]; words: Set<string> }
>();

function effectWords(effect: SoundEffectSummary): Set<string> {
  const cached = effectWordCache.get(effect);
  if (cached && cached.name === effect.name && cached.terms === effect.search_terms) {
    return cached.words;
  }
  // Defensive: a malformed payload must not take the picker down.
  const terms: unknown[] = Array.isArray(effect.search_terms) ? effect.search_terms : [];
  const words = new Set<string>();
  for (const word of [...rawWords(effect.name), ...terms.flatMap((term) => rawWords(term))]) {
    for (const form of wordForms(word)) words.add(form);
  }
  effectWordCache.set(effect, { name: effect.name, terms: effect.search_terms, words });
  return words;
}

function groupKey(category: string | null | undefined): SfxGroupKey {
  return (SFX_CATEGORY_ORDER as readonly string[]).includes(category ?? "")
    ? (category as SfxCategory)
    : "other";
}

/** True when any effect has a known category — i.e. the library is not
 *  legacy-only, so the pickers should show group headings. */
export function hasSfxCategories(effects: SoundEffectSummary[]): boolean {
  return effects.some((effect) => groupKey(effect.category) !== "other");
}

/** Builds a matcher for `query`: every meaningful query word must be a whole
 *  word of the effect's name or search terms. Filler words are ignored unless
 *  the query is nothing but filler ("noise" still finds "White noise"). A
 *  query with no words at all matches everything. With `prefixLast`, the last
 *  word only has to start a word — groupSfxEffects' fallback for a word the
 *  creator is still typing. */
export function sfxQueryMatcher(
  query: string,
  { prefixLast = false }: { prefixLast?: boolean } = {},
): (effect: SoundEffectSummary) => boolean {
  // Filler is checked before the plural fold, like the API ("this" ≠ "thi").
  const words = rawWords(query);
  const meaningful = words.filter((word) => !QUERY_FILLER.has(word));
  const wanted = meaningful.length > 0 ? meaningful : words;
  if (wanted.length === 0) return () => true;
  const whole = prefixLast ? wanted.slice(0, -1) : wanted;
  // The half-typed word is matched exactly as typed ("whis" never folds to
  // "whi" and pulls in "Whip").
  const partial = prefixLast ? wanted[wanted.length - 1] : null;
  return (effect) => {
    const have = effectWords(effect);
    if (!whole.every((word) => wordForms(word).some((form) => have.has(form)))) return false;
    if (partial === null) return true;
    for (const word of have) if (word.startsWith(partial)) return true;
    return false;
  };
}

/** "0.8s", or null when the API has no duration for the effect. */
export function sfxDurationLabel(effect: SoundEffectSummary): string | null {
  return effect.duration_s != null ? `${effect.duration_s.toFixed(1)}s` : null;
}

// One shared collator: localeCompare with options builds a new one per call.
const NAME_COLLATOR = new Intl.Collator(undefined, { numeric: true, sensitivity: "base" });

const byName = (a: SoundEffectSummary, b: SoundEffectSummary) =>
  NAME_COLLATOR.compare(a.name, b.name);

/** Filters `effects` by `query`, then groups them in SFX_CATEGORY_ORDER with
 *  "Other" (legacy/unknown category) last. Groups are A→Z and never empty.
 *  Whole-word matches win; only when there are none does the last word match
 *  as a word start, so "whoo" finds "Whoosh" while "tap" still skips
 *  "Tape rewind". A half-typed filler word ("whoosh sou…") that empties the
 *  results is dropped, so the list doesn't blink out while typing it. */
export function groupSfxEffects(effects: SoundEffectSummary[], query = ""): SfxGroup[] {
  let matched = effects.filter(sfxQueryMatcher(query));
  if (matched.length === 0) matched = effects.filter(sfxQueryMatcher(query, { prefixLast: true }));
  if (matched.length === 0) {
    const words = rawWords(query);
    const last = words[words.length - 1];
    if (words.length > 1 && Array.from(QUERY_FILLER).some((filler) => filler.startsWith(last))) {
      return groupSfxEffects(effects, words.slice(0, -1).join(" "));
    }
  }
  const buckets = new Map<SfxGroupKey, SoundEffectSummary[]>();
  for (const effect of matched) {
    const key = groupKey(effect.category);
    const bucket = buckets.get(key);
    if (bucket) bucket.push(effect);
    else buckets.set(key, [effect]);
  }
  return [...SFX_CATEGORY_ORDER, "other" as const]
    .filter((key) => buckets.has(key))
    .map((key) => ({
      key,
      label: SFX_CATEGORY_LABELS[key],
      effects: buckets.get(key)!.sort(byName),
    }));
}
