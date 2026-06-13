/**
 * TS port of the server's EDITORIAL word-cluster intro geometry — the math
 * behind the live "Editorial" instant-edit preview (DOM word-blocks over the
 * fast-reburn base video).
 *
 * KEEP IN SYNC with src/apps/api/app/pipeline/intro_cluster.py — specifically
 * its STYLED path (`compute_cluster_blocks(style=EDITORIAL_STYLE)` →
 * `_compute_styled_blocks`), which is what generative/plan `intro_layout ===
 * "cluster"` variants actually render (see
 * app/tasks/generative_build.py: `cluster_style=EDITORIAL_STYLE` and
 * app/pipeline/generative_overlays.py: `_build_cluster_intro_overlays`).
 * Parity is guarded by overlay-cluster-layout.test.ts, whose fixtures are dumped
 * straight from the Python via the venv (see that file's header). Mirrored
 * constant-for-constant and branch-for-branch:
 *   - EDITORIAL_STYLE knobs (ratios, cascade geometry, script_min_px, edges)
 *   - `_derive_word_roles_with_guarantees` (heuristic roles — the frontend never
 *     receives the agent's `word_roles`, and an edited hook drops them anyway,
 *     so the render re-derives heuristically too: see generative_build.py
 *     `_resolve_text_for_reburn` "stale roles must never be applied")
 *   - `_group_styled_blocks` (one emphasis group, adjacent-role merge, fold)
 *   - `normalize_typography` (quotes / apostrophe / ellipsis)
 *   - face assignment (hero=Great Vibes; non-hero alternates body/accent by
 *     `(k + accentParity) % 2`)
 *   - `_styled_role_px` (per-role ratio × base × scale, hero floored at
 *     script_min_px, all floored at the renderer min)
 *   - the cluster-atomic shrink (×0.92, width AND cascade-height constraints,
 *     `_MIN_CLUSTER_SCALE` decline → null) and the diagonal cascade
 *     (x stagger + jitter, y from adjacent heights, recenter, clamp).
 *
 * PURITY / TESTABILITY: measurement is injected (`MeasureCluster`) exactly like
 * overlay-layout.ts injects `MeasureAtSize`, so this module is DOM-free. The
 * browser binds a canvas `measureText` + `fontBoundingBox` measure over the SAME
 * registry TTFs the server burns (byte-identical mirrors). Canvas vs Skia
 * metrics can drift ~1%, so a block MAY land a pixel or two off the committed
 * render — the preview is advisory; the burned video is authoritative. The
 * parity test injects the Python's OWN measured widths/heights so the geometry
 * ALGORITHM is verified exactly (zero tolerance); only the font-metric layer
 * carries the documented ~1% drift.
 *
 * GLYPH GATE (decision D18 in intro_cluster.py) is NOT ported: it needs Skia's
 * `assert_glyphs_present` to detect tofu and decline/fall-back a face. For the
 * preview we assume the bundled faces cover the (Latin/Turkish) hook — if the
 * server's gate declines server-side, the committed render falls back to the
 * styled/linear path and the watcher re-syncs the preview to the real output.
 *
 * PAIRING / LANGUAGE (intro_cluster.py `_cluster_pairing`, added in #511) is NOT
 * fully ported: the server now picks connector/closer faces from a data-driven
 * `cluster_pairing` registry keyed by hero font, and swaps to `_TURKISH_SAFE_PAIRING`
 * when `language === "tr"` (or a glyph gap is detected). The default English
 * EDITORIAL_STYLE pairing (Great Vibes hero + Playfair body/italic accent) — what
 * the overwhelming majority of variants render — is reproduced EXACTLY (the dumped
 * fixtures stay byte-identical across that change, so the parity test still passes).
 * Turkish hooks or a non-default hero pairing fall under the same advisory-preview
 * contract above: geometry stays right, the accent face may differ from the burn,
 * and the burned video is authoritative. Full language/pairing parity is a follow-up.
 */

// Canvas (mirror of _CANVAS_W / _CANVAS_H in intro_cluster.py).
export const CLUSTER_CANVAS_W = 1080;
export const CLUSTER_CANVAS_H = 1920;

// Role constants (intro_cluster.py ROLE_*).
export const ROLE_HERO = "hero";
export const ROLE_CONNECTOR = "connector";
export const ROLE_CLOSER = "closer";
export type ClusterRole = "hero" | "connector" | "closer";

// Word-count gate (MIN_WORDS / MAX_WORDS). The styled path lowers the floor to
// EDITORIAL_STYLE.min_words = 1; MAX stays 6.
export const MAX_WORDS = 6;

// Frame-edge safety + vertical band (intro_cluster.py).
const EDGE_MARGIN_FRAC = 0.06;
const Y_MIN = 0.15;
const Y_MAX = 0.85;
const CLUSTER_CENTER_Y = 0.44;

// Cluster-atomic shrink floor + renderer min px.
const MIN_CLUSTER_SCALE = 0.55;
const RENDERER_MIN_FONT_PX = 24;

// Reveal stagger (intro_cluster.py _STAGGER_FRACS / BLOCK_REVEAL_S / _MIN_REVEAL_S).
const STAGGER_FRACS = [0.0, 0.13, 0.17, 0.33, 0.4];
const BLOCK_REVEAL_S = 0.7;
const MIN_REVEAL_S = 0.3;

/**
 * EDITORIAL_STYLE profile — mirror of the dict in intro_cluster.py. Only the
 * knobs the geometry/face math reads are ported (shadow is a renderer concern).
 */
export const EDITORIAL_STYLE = {
  heroFont: "Great Vibes",
  bodyFont: "Playfair Display Regular",
  accentFont: "Playfair Display Italic",
  heroRatio: 1.7,
  connectorRatio: 1.0,
  closerRatio: 1.25,
  scriptMinPx: 64,
  cascadeXStart: 0.4,
  cascadeXStep: 0.07,
  cascadeXJitter: 0.02,
  cascadeYStepRatio: 0.85,
  sceneCenterYs: [0.42, 0.46, 0.44] as const,
  sceneShiftMargin: 0.02,
  minWords: 1,
  maxBlocks: 3,
} as const;

// Minimal stopword sets (en + tr) — exact copy of intro_cluster._STOPWORDS.
const STOPWORDS = new Set<string>([
  // en
  "a", "an", "the", "is", "are", "was", "were", "be", "to", "of", "in", "on",
  "at", "for", "and", "or", "but", "it", "its", "it's", "my", "your", "our",
  "their", "his", "her", "this", "that", "these", "those", "what", "what's",
  "when", "where", "who", "how", "why", "do", "does", "did", "you", "we", "i",
  "me", "so", "if", "with", "as", "than", "then",
  // tr
  "bir", "bu", "şu", "o", "ve", "ya", "de", "da", "ki", "ne", "mi", "mı", "mu",
  "mü", "ile", "için", "gibi", "ama", "çok", "en", "her",
]);

/** Resolved block ready to position: text, role, face, and the size in px. */
export interface ClusterBlockMeasured {
  text: string;
  role: ClusterRole;
  family: string;
  px: number;
  /** width in canvas px (measured by the injected measure). */
  wPx: number;
  /** ascent+descent in canvas px (measured by the injected measure). */
  hPx: number;
}

/** A laid-out cluster block at 1080×1920 canvas scale (center-anchored). */
export interface ClusterBlock {
  text: string;
  role: ClusterRole;
  family: string;
  textSizePx: number;
  /** block CENTER (text_anchor="center"). */
  positionXFrac: number;
  positionYFrac: number;
  startOffsetS: number;
  revealS: number;
}

/**
 * Injected measurement: returns the rendered width AND line height (ascent +
 * descent) of `text` drawn in `family` at `px`, in canvas pixels. The browser
 * backs this with canvas `measureText().width` + `fontBoundingBox*`; the parity
 * test backs it with the Python's own Skia measurements.
 */
export type MeasureCluster = (
  family: string,
  text: string,
  px: number,
) => { wPx: number; hPx: number };

/** Optional per-role font overrides; unset roles fall back to EDITORIAL_STYLE. */
export interface ClusterFontOverrides {
  heroFont?: string;
  bodyFont?: string;
  accentFont?: string;
}

/**
 * Optional per-role size overrides (absolute px). When set, replaces the
 * ratio-derived size for that role. Scale (for frame fit) still applies on top.
 * hero = ROLE_HERO, body = ROLE_CONNECTOR, accent = ROLE_CLOSER.
 */
export interface ClusterSizeOverrides {
  heroSizePx?: number;
  bodySizePx?: number;
  accentSizePx?: number;
}

const bare = (word: string): string =>
  word.toLowerCase().replace(/^[.,!?;:"']+|[.,!?;:"']+$/g, "");

/**
 * Heuristic word→role assignment — exact port of
 * `_derive_word_roles_with_guarantees`. The frontend never receives the agent's
 * `word_roles`, and the render drops stale roles on a user edit, so the heuristic
 * is the parity-correct source for the live preview.
 */
export function deriveWordRoles(words: string[], highlightWord?: string | null): ClusterRole[] {
  const highlight = (highlightWord ?? "").toLowerCase().replace(/^[.,!?;:"']+|[.,!?;:"']+$/g, "");
  const roles: ClusterRole[] = [];
  for (let i = 0; i < words.length; i++) {
    const word = words[i];
    const b = bare(word);
    const trimmed = word.replace(/\s+$/, "");
    const lastChar = trimmed.length ? trimmed[trimmed.length - 1] : "";
    if (i === words.length - 1 && (lastChar === "?" || lastChar === "!" || lastChar === ".")) {
      roles.push(ROLE_CLOSER);
    } else if (b && b === highlight) {
      roles.push(ROLE_HERO);
    } else if (STOPWORDS.has(b)) {
      roles.push(ROLE_CONNECTOR);
    } else {
      roles.push(ROLE_HERO);
    }
  }

  if (!roles.includes(ROLE_HERO)) {
    const candidates = roles
      .map((r, i) => (r !== ROLE_CLOSER ? i : -1))
      .filter((i) => i >= 0);
    if (candidates.length === 0) return roles;
    // Python max(..., key=len) returns the FIRST max on ties.
    let longest = candidates[0];
    for (const i of candidates) {
      if (words[i].length > words[longest].length) longest = i;
    }
    roles[longest] = ROLE_HERO;
  }

  let heroCount = roles.filter((r) => r === ROLE_HERO).length;
  if (
    !roles.includes(ROLE_CLOSER) &&
    words.length >= 4 &&
    roles[roles.length - 1] === ROLE_HERO &&
    bare(words[words.length - 1]) !== highlight &&
    heroCount >= 2
  ) {
    roles[roles.length - 1] = ROLE_CLOSER;
    heroCount -= 1;
  }
  if (
    !roles.includes(ROLE_CONNECTOR) &&
    words.length >= 5 &&
    roles[0] === ROLE_HERO &&
    bare(words[0]) !== highlight &&
    heroCount >= 2
  ) {
    roles[0] = ROLE_CONNECTOR;
  }
  return roles;
}

interface RawBlock {
  role: ClusterRole;
  text: string;
}

/**
 * Group words into reading-order blocks for the editorial cascade — exact port
 * of `_group_styled_blocks`: ONE emphasis group (heroes after the first
 * contiguous hero run demote to closer), all adjacent same-role words merge,
 * promote the longest block if no hero survives, fold tail non-hero blocks to
 * cap at maxBlocks.
 */
export function groupStyledBlocks(words: string[], roles: ClusterRole[]): RawBlock[] {
  const effective = [...roles];
  let seenHeroRun = false;
  let inHeroRun = false;
  for (let i = 0; i < effective.length; i++) {
    if (effective[i] === ROLE_HERO) {
      if (seenHeroRun && !inHeroRun) {
        effective[i] = ROLE_CLOSER;
      } else {
        seenHeroRun = true;
        inHeroRun = true;
      }
    } else {
      inHeroRun = false;
    }
  }

  const blocks: RawBlock[] = [];
  for (let i = 0; i < words.length; i++) {
    const role = effective[i];
    const word = words[i];
    if (blocks.length && blocks[blocks.length - 1].role === role) {
      blocks[blocks.length - 1].text += ` ${word}`;
    } else {
      blocks.push({ role, text: word });
    }
  }

  if (!blocks.some((b) => b.role === ROLE_HERO)) {
    // Promote the longest block (Python max(..., key=len) → first on ties).
    let longest = blocks[0];
    for (const b of blocks) {
      if (b.text.length > longest.text.length) longest = b;
    }
    longest.role = ROLE_HERO;
  }

  const maxBlocks = EDITORIAL_STYLE.maxBlocks;
  while (blocks.length > maxBlocks) {
    let merged = false;
    for (let i = blocks.length - 1; i > 0; i--) {
      if (blocks[i].role !== ROLE_HERO && blocks[i - 1].role !== ROLE_HERO) {
        blocks[i - 1].text += ` ${blocks[i].text}`;
        blocks.splice(i, 1);
        merged = true;
        break;
      }
    }
    if (!merged) {
      blocks[blocks.length - 2].text += ` ${blocks[blocks.length - 1].text}`;
      blocks.splice(blocks.length - 1, 1);
    }
  }
  return blocks;
}

/**
 * Typewriter → typographic punctuation — exact port of `normalize_typography`.
 * `...` → `…`, `'` → `’`, double quotes resolve by context (a quote following a
 * word/closing char closes; otherwise opens). Deterministic + idempotent.
 */
export function normalizeTypography(text: string): string {
  const replaced = text.replace(/\.\.\./g, "…").replace(/'/g, "’");
  let out = "";
  for (let i = 0; i < replaced.length; i++) {
    const ch = replaced[i];
    if (ch === '"') {
      const prev = i > 0 ? replaced[i - 1] : "";
      const closes = prev.length > 0 && (isAlnum(prev) || ".,!?…’”)]".includes(prev));
      out += closes ? "”" : "“";
    } else {
      out += ch;
    }
  }
  return out;
}

/** Unicode-aware analogue of Python's `str.isalnum()` for a single char, without
 * the regex `u` flag (tsconfig has no es6 `target`). A char is a letter if it
 * changes under case folding (covers en + Turkish accented letters); digits are
 * matched directly. Sufficient for the double-quote close/open decision. */
function isAlnum(ch: string): boolean {
  if (ch >= "0" && ch <= "9") return true;
  return ch.toLowerCase() !== ch.toUpperCase();
}

/** Per-role px — exact port of `_styled_role_px` (with optional direct overrides). */
function styledRolePx(
  role: ClusterRole,
  baseSizePx: number,
  scale: number,
  sizeOverrides?: ClusterSizeOverrides,
): number {
  const override =
    role === ROLE_HERO
      ? sizeOverrides?.heroSizePx
      : role === ROLE_CLOSER
        ? sizeOverrides?.accentSizePx
        : sizeOverrides?.bodySizePx;
  const px =
    override != null
      ? Math.round(override * scale)
      : Math.round(
          baseSizePx *
            (role === ROLE_HERO
              ? EDITORIAL_STYLE.heroRatio
              : role === ROLE_CLOSER
                ? EDITORIAL_STYLE.closerRatio
                : EDITORIAL_STYLE.connectorRatio) *
            scale,
        );
  if (role === ROLE_HERO) {
    return Math.max(RENDERER_MIN_FONT_PX, EDITORIAL_STYLE.scriptMinPx, px);
  }
  return Math.max(RENDERER_MIN_FONT_PX, px);
}

/**
 * Face assignment — port of the `_compute_styled_blocks` face loop WITHOUT the
 * Skia glyph gate (see module header): hero → Great Vibes; the k-th non-hero
 * block takes the italic accent when `(k + accentParity) % 2 === 1`, else the
 * body serif. Hero never takes the accent.
 */
export function assignFaces(
  blocks: RawBlock[],
  accentParity = 0,
  overrides?: ClusterFontOverrides,
): string[] {
  const heroFont = overrides?.heroFont ?? EDITORIAL_STYLE.heroFont;
  const bodyFont = overrides?.bodyFont ?? EDITORIAL_STYLE.bodyFont;
  const accentFont = overrides?.accentFont ?? EDITORIAL_STYLE.accentFont;
  const faces: string[] = [];
  let nonHeroK = 0;
  for (const block of blocks) {
    if (block.role === ROLE_HERO) {
      faces.push(heroFont);
    } else {
      const wantsAccent = (nonHeroK + accentParity) % 2 === 1;
      faces.push(wantsAccent ? accentFont : bodyFont);
      nonHeroK += 1;
    }
  }
  return faces;
}

/**
 * Compute the editorial word-cluster layout — port of `_compute_styled_blocks`
 * (the styled path of `compute_cluster_blocks`). Returns the laid-out blocks, or
 * `null` when the text doesn't suit a cluster (word count outside
 * [minWords, MAX_WORDS], or the cluster can't fit the frame at a readable size).
 *
 * `wordRoles`, when present + valid, is used verbatim (matching the server's
 * agent-roles branch); otherwise roles are derived heuristically. The frontend
 * normally passes none.
 */
export function computeClusterBlocks(
  text: string,
  measure: MeasureCluster,
  opts: {
    baseSizePx: number;
    revealWindowS?: number;
    wordRoles?: ClusterRole[] | null;
    accentParity?: number;
    fontOverrides?: ClusterFontOverrides;
    sizeOverrides?: ClusterSizeOverrides;
  },
): ClusterBlock[] | null {
  const baseSizePx = opts.baseSizePx;
  const revealWindowS = opts.revealWindowS ?? 0;
  const accentParity = opts.accentParity ?? 0;

  const words = (text ?? "").split(/\s+/).filter((w) => w.length > 0);
  if (!(words.length >= EDITORIAL_STYLE.minWords && words.length <= MAX_WORDS)) {
    return null;
  }

  const validRoles: ClusterRole[] = [ROLE_HERO, ROLE_CONNECTOR, ROLE_CLOSER];
  let roles: ClusterRole[];
  if (
    opts.wordRoles &&
    opts.wordRoles.length === words.length &&
    opts.wordRoles.every((r) => (validRoles as string[]).includes(r))
  ) {
    roles = opts.wordRoles;
  } else {
    roles = deriveWordRoles(words);
  }

  const rawBlocks = groupStyledBlocks(words, roles);
  if (!rawBlocks.length) return null;

  for (const b of rawBlocks) b.text = normalizeTypography(b.text);

  const faces = assignFaces(rawBlocks, accentParity, opts.fontOverrides);

  const usableW = 1.0 - 2 * EDGE_MARGIN_FRAC;
  const marginY = EDITORIAL_STYLE.sceneShiftMargin;
  const usableH = Y_MAX - marginY - (Y_MIN + marginY);
  const stepRatio = EDITORIAL_STYLE.cascadeYStepRatio;

  const measureBlocks = (scale: number): ClusterBlockMeasured[] =>
    rawBlocks.map((b, i) => {
      const px = styledRolePx(b.role, baseSizePx, scale, opts.sizeOverrides);
      const m = measure(faces[i], b.text, px);
      return {
        text: b.text,
        role: b.role,
        family: faces[i],
        px,
        wPx: m.wPx,
        hPx: m.hPx,
      };
    });

  const cascadeHeight = (measures: ClusterBlockMeasured[]): number => {
    let total = measures[0].hPx / CLUSTER_CANVAS_H / 2 + measures[measures.length - 1].hPx / CLUSTER_CANVAS_H / 2;
    for (let i = 1; i < measures.length; i++) {
      total += ((measures[i - 1].hPx / CLUSTER_CANVAS_H + measures[i].hPx / CLUSTER_CANVAS_H) / 2) * stepRatio;
    }
    return total;
  };

  // Cluster-atomic shrink: scale ALL roles together. wFrac/hFrac are derived
  // from the measured px (matching the Python `w_frac = measureText/CANVAS_W`).
  let scale = 1.0;
  let measures = measureBlocks(scale);
  // Guard the loop the same way Python's `while True` does.
  for (;;) {
    const widestWFrac = Math.max(...measures.map((m) => m.wPx / CLUSTER_CANVAS_W));
    if (widestWFrac <= usableW && cascadeHeight(measures) <= usableH) break;
    scale *= 0.92;
    if (scale < MIN_CLUSTER_SCALE) {
      return null;
    }
    measures = measureBlocks(scale);
  }

  const hFrac = measures.map((m) => m.hPx / CLUSTER_CANVAS_H);
  const wFrac = measures.map((m) => m.wPx / CLUSTER_CANVAS_W);

  // Diagonal cascade, strictly in reading order.
  const ys: number[] = new Array(rawBlocks.length).fill(0);
  for (let i = 1; i < rawBlocks.length; i++) {
    ys[i] = ys[i - 1] + ((hFrac[i - 1] + hFrac[i]) / 2) * stepRatio;
  }
  const xJitter = EDITORIAL_STYLE.cascadeXJitter;
  const xs: number[] = rawBlocks.map(
    (_, i) =>
      EDITORIAL_STYLE.cascadeXStart +
      EDITORIAL_STYLE.cascadeXStep * i +
      (i === 0 ? 0 : i % 2 === 1 ? xJitter : -xJitter),
  );

  // Re-center, then clamp by shifting the WHOLE cascade vertically; clamp x per
  // block to keep its measured box inside the safe area.
  const top = ys[0] - hFrac[0] / 2;
  const bottom = ys[ys.length - 1] + hFrac[hFrac.length - 1] / 2;
  let shift = CLUSTER_CENTER_Y - (top + bottom) / 2;
  shift = Math.min(shift, Y_MAX - marginY - bottom);
  shift = Math.max(shift, Y_MIN + marginY - top);
  for (let i = 0; i < rawBlocks.length; i++) {
    ys[i] += shift;
    const halfW = wFrac[i] / 2;
    xs[i] = Math.min(Math.max(xs[i], EDGE_MARGIN_FRAC + halfW), 1.0 - EDGE_MARGIN_FRAC - halfW);
  }

  const window = Math.max(0, revealWindowS);
  const out: ClusterBlock[] = [];
  for (let i = 0; i < rawBlocks.length; i++) {
    let start = STAGGER_FRACS[Math.min(i, STAGGER_FRACS.length - 1)] * window;
    start = window > 0.5 ? Math.max(0, Math.min(start, window - 0.5)) : 0;
    const revealEnd = Math.min(start + BLOCK_REVEAL_S, window);
    out.push({
      text: measures[i].text,
      role: measures[i].role,
      family: measures[i].family,
      textSizePx: measures[i].px,
      positionXFrac: round6(xs[i]),
      positionYFrac: round6(ys[i]),
      startOffsetS: round3(start),
      revealS: round3(Math.max(MIN_REVEAL_S, revealEnd - start)),
    });
  }
  return out;
}

const round6 = (v: number): number => Math.round(v * 1e6) / 1e6;
const round3 = (v: number): number => Math.round(v * 1e3) / 1e3;
