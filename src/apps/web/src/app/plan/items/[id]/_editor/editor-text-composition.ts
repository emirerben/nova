const WORDS_PER_TIMED_CHUNK = 4;
export const TEXT_ELEMENT_MAX_CHARS = 500;

/** Keep one composition bounded so a pasted document cannot create thousands
 * of reducer updates or DOM overlays on the editor's main thread. */
export const TEXT_ELEMENTS_API_MAX = 50;

function normalizeCompositionText(text: string): string {
  return text.replace(/\s+/g, " ").trim();
}

function splitByElementLimit(text: string): string[] {
  const normalized = normalizeCompositionText(text);
  if (!normalized) return [];
  const chunks: string[] = [];
  let current = "";
  const flush = () => {
    if (current) chunks.push(current);
    current = "";
  };
  for (const word of normalized.split(" ")) {
    if (word.length > TEXT_ELEMENT_MAX_CHARS) {
      flush();
      for (let offset = 0; offset < word.length; offset += TEXT_ELEMENT_MAX_CHARS) {
        chunks.push(word.slice(offset, offset + TEXT_ELEMENT_MAX_CHARS));
      }
      continue;
    }
    const candidate = current ? `${current} ${word}` : word;
    if (candidate.length > TEXT_ELEMENT_MAX_CHARS) flush();
    current = current ? `${current} ${word}` : word;
  }
  flush();
  return chunks;
}

/** Split authored composition copy for temporal playback. Explicit lines are
 * an authoring contract and are never merged. Prose falls back to sentences,
 * then linear four-word phrases. */
export function splitTextForTimedSequence(text: string): string[] {
  const manualLines = text
    .split(/\n+/)
    .map(normalizeCompositionText)
    .filter(Boolean);
  if (manualLines.length >= 2) return manualLines.flatMap(splitByElementLimit);

  const normalized = normalizeCompositionText(text);
  if (!normalized) return [];
  const sentences = (normalized.match(/[^.!?]+[.!?]?/g) ?? [normalized])
    .map(normalizeCompositionText)
    .filter(Boolean);
  if (sentences.length >= 2) return sentences.flatMap(splitByElementLimit);

  const words = normalized.split(/\s+/).filter(Boolean);
  if (words.length <= WORDS_PER_TIMED_CHUNK) return splitByElementLimit(normalized);
  const chunks: string[] = [];
  for (let index = 0; index < words.length; index += WORDS_PER_TIMED_CHUNK) {
    chunks.push(...splitByElementLimit(words.slice(index, index + WORDS_PER_TIMED_CHUNK).join(" ")));
  }
  return chunks;
}

export interface TimedTextSequenceItem {
  text: string;
  start_s: number;
  end_s: number;
}

/** Spread every chunk over the available video. `null` means the composition
 * exceeds `maxChunks` and should be rejected visibly. */
export function buildTimedTextSequence(
  text: string,
  currentTimeS: number,
  durationS: number,
  minWindowS = 0.5,
  maxChunks = TEXT_ELEMENTS_API_MAX,
): TimedTextSequenceItem[] | null {
  const chunks = splitTextForTimedSequence(text);
  if (chunks.length > maxChunks) return null;
  if (chunks.length === 0) return [];
  if (!(durationS > 0)) {
    return chunks.map((chunk, index) => ({
      text: chunk,
      start_s: Math.max(0, currentTimeS) + index * 2,
      end_s: Math.max(0, currentTimeS) + (index + 1) * 2,
    }));
  }

  const minimumSpan = chunks.length * minWindowS;
  const requestedStart = Math.max(0, Math.min(currentTimeS, durationS));
  const start = Math.min(requestedStart, Math.max(0, durationS - minimumSpan));
  const windowS = (durationS - start) / chunks.length;

  return chunks.map((chunk, index) => ({
    text: chunk,
    start_s: start + index * windowS,
    end_s: index === chunks.length - 1 ? durationS : start + (index + 1) * windowS,
  }));
}
