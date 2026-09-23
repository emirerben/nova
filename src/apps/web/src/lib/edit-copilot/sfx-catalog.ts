// sfx-catalog.ts — orders the copilot snapshot's SFX catalog for one request.
//
// GET /sound-effects serves the whole creator library (~120 effects), newest
// first; the legacy snapshot budget keeps 20 rows (12 under byte pressure) and
// add_sfx only accepts an effect_id the snapshot listed. Taking the first 20
// made "add a buzzer" unreachable unless the buzzer was a recent upload.
//
// Scoring mirrors src/apps/api/app/services/sfx_catalog.py (match_score,
// rank_for_request, _round_robin) so the editor copilot, the chat planner and
// the placement agent pick the same effect for the same words. Keep them in
// step when either side changes.

import type { SoundEffectSummary } from "@/lib/sfx-api";

/** services.sfx_catalog.SFX_CATEGORIES, in round-robin order. */
export const SFX_CATEGORIES = [
  "rejection",
  "approval",
  "suspense",
  "transition",
  "impact",
  "comedy",
  "ui",
  "sports",
  "money",
] as const;

// Words that describe the request, not the sound ("add a sound effect").
const FILLER = new Set([
  "a", "an", "the", "some", "any", "sound", "sounds", "effect", "effects", "sfx",
  "noise", "my", "this", "that", "it", "of", "to", "for", "with", "and", "please",
  "like", "kind", "type", "little", "quick", "one",
]);

type Prominence = [tier: number, uploadOrder: number, nameLength: number, name: string, id: string];

interface CatalogEntry {
  effect: SoundEffectSummary;
  name: string;
  category: string | null;
  searchTerms: string[];
  nameWords: Set<string>;
  termWords: Set<string>;
  prominence: Prominence;
}

function words(text: unknown): string[] {
  return String(text ?? "").toLowerCase().match(/[a-z0-9]+/g) ?? [];
}

function stem(word: string): string {
  return word.length > 3 && word.endsWith("s") && !word.endsWith("ss") ? word.slice(0, -1) : word;
}

function contentWords(text: unknown): string[] {
  return words(text).filter((word) => !FILLER.has(word)).map(stem);
}

/** The public list omits quality_tier, so infer _TIER_RANK from the metadata
 * each tier carries: level-matched library effects (0) have a category; the
 * quiet smart-* sound-design layer ("core", 1) is role-tagged; legacy uploads
 * have neither (2). */
function tierRank(effect: SoundEffectSummary): number {
  if (effect.category) return 0;
  if (effect.role_tags?.length) return 1;
  return 2;
}

function toEntry(effect: SoundEffectSummary, uploadOrder: number): CatalogEntry {
  const name = String(effect.name ?? "");
  const searchTerms = (effect.search_terms ?? []).filter((term): term is string => Boolean(term)).map(String);
  return {
    effect,
    name,
    category: effect.category ?? null,
    searchTerms,
    nameWords: new Set(words(name).map(stem)),
    termWords: new Set(searchTerms.flatMap((term) => words(term).map(stem))),
    prominence: [tierRank(effect), uploadOrder, name.length, name.toLowerCase(), effect.id],
  };
}

function compareProminence(a: CatalogEntry, b: CatalogEntry): number {
  for (let i = 0; i < a.prominence.length; i += 1) {
    const left = a.prominence[i];
    const right = b.prominence[i];
    if (left < right) return -1;
    if (left > right) return 1;
  }
  return 0;
}

function matchScore(wanted: Set<string>, joined: string, entry: CatalogEntry): number {
  let score = 0;
  wanted.forEach((word) => {
    if (entry.nameWords.has(word)) score += 3;
    else if (entry.termWords.has(word)) score += 1.5;
  });
  for (const term of entry.searchTerms) {
    const phrase = words(term).map(stem).join(" ");
    if (phrase.includes(" ") && ` ${joined} `.includes(` ${phrase} `)) score += 2;
  }
  if (entry.category && wanted.has(entry.category)) score += 1;
  // The first search term is the effect's primary keyword ("pop" for Soft
  // pop): a bare "a pop" should land on the effect that IS that sound.
  if (entry.searchTerms.length > 0 && contentWords(entry.searchTerms[0]).join(" ") === joined) score += 1;
  return score;
}

/** How well free text describes an effect (0 = unrelated). */
export function sfxMatchScore(query: string, effect: SoundEffectSummary): number {
  const queryWords = contentWords(query);
  if (queryWords.length === 0) return 0;
  return matchScore(new Set(queryWords), queryWords.join(" "), toEntry(effect, 0));
}

function rankForRequest(entries: CatalogEntry[], query: string): CatalogEntry[] {
  const queryWords = contentWords(query);
  if (queryWords.length === 0) return [];
  const wanted = new Set(queryWords);
  const joined = queryWords.join(" ");
  return entries
    .map((entry) => ({ entry, score: matchScore(wanted, joined, entry) }))
    .filter(({ score }) => score > 0)
    .sort((a, b) => b.score - a.score || compareProminence(a.entry, b.entry))
    .map(({ entry }) => entry);
}

/** "Wrong buzzer long" is a variant of "Wrong buzzer". */
function isVariant(entry: CatalogEntry, siblings: CatalogEntry[]): boolean {
  const name = entry.name.toLowerCase();
  return siblings.some((sibling) => sibling !== entry && name.startsWith(`${sibling.name.toLowerCase()} `));
}

/** One effect per category per round; base effects before their variants. */
function roundRobin(entries: CatalogEntry[]): CatalogEntry[] {
  const groups = new Map<string, CatalogEntry[]>();
  for (const entry of [...entries].sort(compareProminence)) {
    const category = (SFX_CATEGORIES as readonly string[]).includes(entry.category ?? "")
      ? entry.category!
      : "other";
    const members = groups.get(category) ?? [];
    members.push(entry);
    groups.set(category, members);
  }
  groups.forEach((members, category) => {
    const bases = members.filter((entry) => !isVariant(entry, members));
    groups.set(category, [...bases, ...members.filter((entry) => !bases.includes(entry))]);
  });
  const order = [...SFX_CATEGORIES, "other"].filter((category) => groups.has(category));
  const out: CatalogEntry[] = [];
  while (order.some((category) => groups.get(category)!.length > 0)) {
    for (const category of order) {
      const next = groups.get(category)!.shift();
      if (next) out.push(next);
    }
  }
  return out;
}

export interface OrderSfxCatalogOptions {
  /** The copilot message this turn answers first, then earlier user messages
   * (newest first) — so a clarification reply keeps the sound it is about. */
  requestTexts?: readonly string[];
  /** Effects the draft already references (placed pins, pending suggestions).
   * They lead, in this order, so their ids stay addressable. */
  keepEffectIds?: readonly (string | null | undefined)[];
}

/**
 * The whole catalog, most useful first: referenced effects, then effects the
 * request texts describe (whole-word match on name + search terms), then one
 * headline effect per category, round-robin. `catalog` must be in
 * GET /sound-effects order (created_at desc) — its reverse is upload order,
 * the curated tie-break.
 */
export function orderSfxCatalogForRequest(
  catalog: readonly SoundEffectSummary[],
  options: OrderSfxCatalogOptions = {},
): SoundEffectSummary[] {
  const byId = new Map<string, CatalogEntry>();
  catalog.forEach((effect, index) => {
    if (!byId.has(effect.id)) byId.set(effect.id, toEntry(effect, catalog.length - 1 - index));
  });
  const out: CatalogEntry[] = [];
  const claim = (entry: CatalogEntry | undefined) => {
    if (!entry || !byId.has(entry.effect.id)) return;
    byId.delete(entry.effect.id);
    out.push(entry);
  };
  for (const id of options.keepEffectIds ?? []) {
    if (id) claim(byId.get(id));
  }
  for (const text of options.requestTexts ?? []) {
    rankForRequest([...byId.values()], text).forEach(claim);
  }
  roundRobin([...byId.values()]).forEach(claim);
  return out.map((entry) => entry.effect);
}
