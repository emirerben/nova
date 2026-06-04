/**
 * Strip a duplicated "Why this works:" prefix from a plan item's rationale body.
 *
 * The UI already renders a "Why this works" label, and the model's rationale text
 * frequently begins with the same phrase — producing a doubled "Why this works:
 * Why this works: …". Remove the leading (case-insensitive) prefix from the body
 * so the label isn't echoed. Backend prompt is untouched; this is a UI strip only.
 */
export function stripRationalePrefix(s: string): string {
  return s.replace(/^\s*why this works:?\s*/i, "").trim();
}
