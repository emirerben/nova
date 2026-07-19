/**
 * Text-element timeline reducer: bounded undo/redo over TextElementBar[].
 *
 * Pure (no DOM, no React) — drives `useReducer` in TextLane and is
 * unit-testable in __tests__/text-timeline-reducer.test.ts.
 *
 * Modelled on sfx-timeline-reducer.ts (HISTORY_LIMIT, withHistory pattern,
 * past/future arrays). Simpler because text bars have no beat-grid math.
 */

export const TEXT_HISTORY_LIMIT = 50;

// ── Domain type ───────────────────────────────────────────────────────────────

/**
 * One positioned text block on the video timeline.
 *
 * `start_s` / `end_s` are assembled-time seconds (same coordinate space as
 * SoundEffectPlacement.at_s and MediaOverlay.start_s / end_s).
 *
 * Styling fields (font_family, size_px, …) were Phase-3 pass-through only.
 * T7 adds UI controls for all Tier 1+2 renderer-honored fields.
 */
export interface TextElementBar {
  id: string;
  text: string;
  start_s: number;
  end_s: number;
  role: "generative_intro" | "generative_sequence" | "narrated_caption" | "lyric_line";
  visual_block_id?: string | null;
  font_family?: string;
  size_px?: number;
  size_class?: string;
  color?: string;
  /** Optional word highlight color (karaoke / emphasis). Renderer-honored. */
  highlight_color?: string;
  /** Stroke width in canvas-px units (0 = no stroke). Renderer-honored. */
  stroke_width?: number;
  /** Explicit soft-shadow toggle. undefined preserves legacy/generated defaults. */
  shadow_enabled?: boolean | null;
  /** Optional renderer-authored editorial glow. */
  glow_color?: string | null;
  glow_strength?: number | null;
  effect?: string;
  /** Optional renderer fade tail in milliseconds. Editorial sequence blocks
   * stay fully opaque until this final window, then use the Skia quadratic
   * fade curve. */
  fade_out_ms?: number | null;
  alignment?: string;
  /** Display-case transform ("none" | "upper" | "lower" | "title") — resolved
   * at compile/layout time on both renderers (T11, parity-gated). */
  text_case?: string;
  /** Tracking in em (× font size). Renderer-honored (T11, parity-gated). */
  letter_spacing?: number;
  /** Line-height multiplier (1.15 = renderer default). Renderer-honored
   * (T11, parity-gated). */
  line_spacing?: number;
  /** Maximum wrap-box width as a fraction of frame width. Renderer-honored
   * and parity-gated; undefined = renderer default 0.9. */
  max_width_frac?: number;
  /** Named vertical position preset ("top" | "middle" | "bottom" | "custom").
   * Editor canvas drags set "custom" + explicit fracs. Renderer-honored. */
  position?: string;
  /** Fractional X center [0,1] — explicit placement wins over `position`.
   * Set by canvas drag-move in the editor shell. Renderer-honored. */
  x_frac?: number | null;
  /** Fractional Y center [0,1] — explicit placement wins over `position`. */
  y_frac?: number | null;
  /** Clockwise rotation in degrees. Used by masonry smart placement pockets. */
  rotation_deg?: number | null;
  source_params?: Record<string, unknown>;
  /** Occlude this text behind the moving subject (text-behind-subject feature).
   * Render-only compositing flag — the canvas preview cannot segment the
   * subject, so this has no visual effect here beyond the inspector toggle. */
  behind_subject?: boolean;
}

const LYRIC_ALLOWED_PATCH_FIELDS = new Set([
  "text",
  "color",
  "highlight_color",
  "font_family",
  "size_px",
  "size_class",
]);

function isLyricLine(bar: TextElementBar | undefined): boolean {
  return bar?.role === "lyric_line";
}

function targetIsLyric(state: TextEditorState, id: string): boolean {
  return isLyricLine(state.bars.find((bar) => bar.id === id));
}

function lyricPatch(
  patch: Partial<Omit<TextElementBar, "id" | "role">>,
): Partial<Omit<TextElementBar, "id" | "role">> {
  const next: Partial<Omit<TextElementBar, "id" | "role">> = {};
  for (const [key, value] of Object.entries(patch)) {
    if (LYRIC_ALLOWED_PATCH_FIELDS.has(key)) {
      (next as Record<string, unknown>)[key] = value;
    }
  }
  return next;
}

// ── Reducer state ─────────────────────────────────────────────────────────────

export interface TextEditorState {
  bars: TextElementBar[];
  /** Snapshots of bars before each mutating action (most recent last). */
  past: TextElementBar[][];
  /** Snapshots for redo (most recent first). */
  future: TextElementBar[][];
}

// ── Action union ──────────────────────────────────────────────────────────────

export type TextEditorAction =
  /** Add a new text bar (e.g. user clicks [＋]). */
  | { type: "ADD_TEXT"; bar: TextElementBar }
  /** Update the text content of a bar. */
  | { type: "EDIT_TEXT"; id: string; text: string }
  /** Move a bar left/right (drag-body): changes start_s, keeps duration. */
  | { type: "MOVE_BAR"; id: string; start_s: number }
  /** Drag the left edge (trim start): changes start_s only. */
  | { type: "TRIM_START"; id: string; start_s: number }
  /** Drag the right edge (trim end): changes end_s only. */
  | { type: "TRIM_END"; id: string; end_s: number }
  /** Remove a bar. */
  | { type: "DELETE_BAR"; id: string }
  /**
   * Split a bar at the playhead (plan §6). The original bar becomes
   * [start_s, at_s]; a new bar `newId` covers [at_s, end_s] sharing every
   * style field. No-op (no history push) when `at_s` is not strictly inside
   * the bar with room for both halves.
   */
  | { type: "SPLIT_BAR"; id: string; at_s: number; newId: string }
  /** Reorder a bar up or down in z-index stacking order. */
  | { type: "REORDER"; id: string; direction: "up" | "down" }
  /** Step back one mutation. */
  | { type: "UNDO" }
  /** Step forward one mutation. */
  | { type: "REDO" }
  /** Replace the entire state (e.g. when the parent refreshes from the API). */
  | { type: "RESET"; bars: TextElementBar[] }
  /**
   * Patch arbitrary styling fields on a single bar (T7 property panel).
   * id and role are excluded because they are immutable identifiers.
   */
  | {
      type: "PATCH_BAR";
      id: string;
      patch: Partial<Omit<TextElementBar, "id" | "role">>;
    }
  /**
   * Insert seeded lyric_line bars in one undoable step (lyrics-optional
   * elements model: Lyrics toggle ON). Individual lyric bars can't be added
   * via ADD_TEXT-style one-at-a-time flows — the toggle inserts the whole set.
   */
  | { type: "ADD_LYRIC_BARS"; bars: TextElementBar[] }
  /**
   * Remove every lyric_line bar in one undoable step (Lyrics toggle OFF).
   * DELETE_BAR no-ops on lyric_line bars (see isLyricLine guard below), so
   * this is the only removal path for the elements lyrics model.
   */
  | { type: "REMOVE_LYRIC_BARS" };

// ── Init ──────────────────────────────────────────────────────────────────────

export function initTextEditorState(bars: TextElementBar[]): TextEditorState {
  return { bars, past: [], future: [] };
}

// ── History helper ────────────────────────────────────────────────────────────

/** Push current bars to past, set next, clear future. Caps at TEXT_HISTORY_LIMIT. */
function withHistory(
  state: TextEditorState,
  next: TextElementBar[],
): TextEditorState {
  const past = [...state.past, state.bars];
  if (past.length > TEXT_HISTORY_LIMIT) past.shift();
  return { bars: next, past, future: [] };
}

// ── Reducer ───────────────────────────────────────────────────────────────────

export function textReducer(
  state: TextEditorState,
  action: TextEditorAction,
): TextEditorState {
  switch (action.type) {
    case "ADD_TEXT":
      return withHistory(state, [...state.bars, action.bar]);

    case "EDIT_TEXT": {
      const next = state.bars.map((b) =>
        b.id === action.id ? { ...b, text: action.text } : b,
      );
      return withHistory(state, next);
    }

    case "MOVE_BAR": {
      if (targetIsLyric(state, action.id)) return state;
      const next = state.bars.map((b) => {
        if (b.id !== action.id) return b;
        const dur = b.end_s - b.start_s;
        const newStart = Math.max(0, action.start_s);
        return { ...b, start_s: Math.round(newStart * 10) / 10, end_s: Math.round((newStart + dur) * 10) / 10 };
      });
      return withHistory(state, next);
    }

    case "TRIM_START": {
      if (targetIsLyric(state, action.id)) return state;
      const next = state.bars.map((b) => {
        if (b.id !== action.id) return b;
        const newStart = Math.max(0, Math.min(action.start_s, b.end_s - 0.1));
        return { ...b, start_s: Math.round(newStart * 10) / 10 };
      });
      return withHistory(state, next);
    }

    case "TRIM_END": {
      if (targetIsLyric(state, action.id)) return state;
      const next = state.bars.map((b) => {
        if (b.id !== action.id) return b;
        const newEnd = Math.max(b.start_s + 0.1, action.end_s);
        return { ...b, end_s: Math.round(newEnd * 10) / 10 };
      });
      return withHistory(state, next);
    }

    case "DELETE_BAR":
      if (targetIsLyric(state, action.id)) return state;
      return withHistory(
        state,
        state.bars.filter((b) => b.id !== action.id),
      );

    case "SPLIT_BAR": {
      const bar = state.bars.find((b) => b.id === action.id);
      if (!bar) return state;
      if (isLyricLine(bar)) return state;
      const at = Math.round(action.at_s * 10) / 10;
      // Need at least MIN duration on BOTH halves, else the split is a no-op.
      const MIN = 0.2;
      if (at <= bar.start_s + MIN - 1e-9 || at >= bar.end_s - MIN + 1e-9) {
        return state;
      }
      const left: TextElementBar = { ...bar, end_s: at };
      const right: TextElementBar = { ...bar, id: action.newId, start_s: at };
      const next = state.bars.flatMap((b) =>
        b.id === action.id ? [left, right] : [b],
      );
      return withHistory(state, next);
    }

    case "REORDER": {
      const idx = state.bars.findIndex((b) => b.id === action.id);
      if (idx === -1) return state;
      if (isLyricLine(state.bars[idx])) return state;
      const next = [...state.bars];
      if (action.direction === "up" && idx > 0) {
        [next[idx - 1], next[idx]] = [next[idx], next[idx - 1]];
      } else if (action.direction === "down" && idx < next.length - 1) {
        [next[idx], next[idx + 1]] = [next[idx + 1], next[idx]];
      } else {
        return state; // already at edge — no change, no history push
      }
      return withHistory(state, next);
    }

    case "UNDO": {
      if (state.past.length === 0) return state;
      const prev = state.past[state.past.length - 1];
      return {
        bars: prev,
        past: state.past.slice(0, -1),
        future: [state.bars, ...state.future],
      };
    }

    case "REDO": {
      if (state.future.length === 0) return state;
      const next = state.future[0];
      return {
        bars: next,
        past: [...state.past, state.bars],
        future: state.future.slice(1),
      };
    }

    case "RESET":
      return initTextEditorState(action.bars);

    case "ADD_LYRIC_BARS":
      if (action.bars.length === 0) return state;
      return withHistory(state, [...state.bars, ...action.bars]);

    case "REMOVE_LYRIC_BARS": {
      if (!state.bars.some(isLyricLine)) return state;
      return withHistory(
        state,
        state.bars.filter((b) => !isLyricLine(b)),
      );
    }

    case "PATCH_BAR": {
      const target = state.bars.find((b) => b.id === action.id);
      const patch = isLyricLine(target) ? lyricPatch(action.patch) : action.patch;
      if (Object.keys(patch).length === 0) return state;
      const next = state.bars.map((b) =>
        b.id === action.id ? { ...b, ...patch } : b,
      );
      return withHistory(state, next);
    }

    default:
      return state;
  }
}
