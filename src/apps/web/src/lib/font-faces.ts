import fontRegistryJson from "@/data/font-registry.json";

// ── Font face declarations from the shared registry ──────────────────────────
// TTFs are served from /fonts/ (public/fonts). The CSS family deliberately
// collapses e.g. Inter Regular/Medium/Bold onto a single `'Inter'` family with
// different `font-weight` values so the browser picks the right weight via
// standard CSS — matching how the registry resolves them. `font-display: swap`
// means text shows immediately in a fallback and swaps when the TTF lands, so a
// 404 or slow font never blocks render (it just stays in the fallback face).
//
// Shared by the admin layout and the plan item page's style-preview chips so the
// chip can render a style set's label in its REAL typeface (the same font the
// renderer will burn) before the user commits a re-render.
function buildFontFaces(registry: {
  fonts: Record<string, { file: string; weight: number; css_family: string }>;
}): string {
  // De-dup by (css-family, weight, style, file) so we don't emit the same
  // @font-face multiple times when two registry keys share a CSS family (e.g.
  // Inter).
  const seen = new Set<string>();
  const blocks: string[] = [];
  for (const entry of Object.values(registry.fonts)) {
    // Extract the first family from `'Family Name', fallback` so the @font-face
    // family is the bare family token without the fallback list.
    const match = entry.css_family.match(/^\s*['"]([^'"]+)['"]/);
    const family = match ? match[1] : entry.css_family;
    // Italic faces share a family + weight with their upright sibling (e.g.
    // Playfair Display Regular vs Italic — both `'Playfair Display'` @ 400), so
    // they are addressable ONLY via `font-style: italic`. Detect from the TTF
    // file name (the registry's only italic signal). The editorial cluster
    // preview's accent face depends on this — without it the browser can't
    // distinguish italic from regular and renders/measures the wrong glyphs.
    const isItalic = /italic/i.test(entry.file);
    const style = isItalic ? "italic" : "normal";
    const key = `${family}|${entry.weight}|${style}|${entry.file}`;
    if (seen.has(key)) continue;
    seen.add(key);
    blocks.push(
      `@font-face {\n` +
        `  font-family: '${family}';\n` +
        `  src: url('/fonts/${entry.file}') format('truetype');\n` +
        `  font-weight: ${entry.weight};\n` +
        `  font-style: ${style};\n` +
        `  font-display: swap;\n` +
        `}`,
    );
  }
  return blocks.join("\n");
}

/** Concatenated `@font-face` CSS for every registry font. Inject via a `<style>`. */
export const FONT_FACES = buildFontFaces(fontRegistryJson);
