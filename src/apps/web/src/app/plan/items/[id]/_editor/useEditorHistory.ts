"use client";

/**
 * useEditorHistory — the UNIFIED undo/redo command stack for the TikTok-parity
 * editor shell (plan §5/§7, task T8).
 *
 * Scope: DOCUMENT state only — text bars, clip slots, per-track mutes (mix),
 * and the working title. View state (zoom / pan / tool / selection / playhead)
 * is EXCLUDED, so ⌘Z after zooming undoes the last EDIT, not the zoom.
 *
 * Reducer-history unification (the choice, documented):
 *   The shared `text-timeline-reducer` keeps its OWN internal past/future for
 *   the item-page lanes. Rather than edit that shared, item-page-coupled
 *   reducer, the shell SNAPSHOTS the whole document per command into this
 *   single stack and drives ALL undo/redo from here. On undo/redo the shell
 *   dispatches `RESET` to the reducer (restoring bars) and setState-restores
 *   slots/mutes/title. The reducer's internal history is left dormant — RESET
 *   empties it, so the two systems never fight. One command = one snapshot =
 *   one undo step (drags, restyle-all, and preset-apply each collapse to one).
 *
 * The stack machinery is exposed as PURE functions (recordSnapshot /
 * undoSnapshot / redoSnapshot) so it is unit-testable without React, alongside
 * the sessionStorage draft (de)serializers.
 */

import { useCallback, useRef, useState } from "react";
import type { TextElementBar } from "@/lib/timeline/text-timeline-reducer";
import type { DraftSlot } from "@/app/generative/timeline-math";

/** Command-stack depth (plan §7). Oldest snapshot drops past this. */
export const EDITOR_HISTORY_DEPTH = 50;

/** The full working document the stack snapshots (document state only). */
export interface EditorDocument {
  bars: TextElementBar[];
  slots: DraftSlot[] | null;
  videoMuted: boolean;
  soundMuted: boolean;
  title: string;
}

export interface EditorHistoryState {
  /** Pre-change snapshots, oldest first (most recent last). */
  past: EditorDocument[];
  /** Undone snapshots for redo (most recent first). */
  future: EditorDocument[];
  /**
   * Coalesce tag of the most recent record. A record with a non-null tag equal
   * to this one is dropped, so a typing burst on one field (title, text
   * content) collapses into a single undo step. Cleared by undo/redo.
   */
  lastTag: string | null;
}

export function initEditorHistoryState(): EditorHistoryState {
  return { past: [], future: [], lastTag: null };
}

/**
 * Record a pre-change snapshot. Clears the redo stack. Coalesces consecutive
 * records that share a non-null `tag` (returns the state unchanged).
 */
export function recordSnapshot(
  h: EditorHistoryState,
  prev: EditorDocument,
  tag: string | null = null,
): EditorHistoryState {
  if (tag !== null && tag === h.lastTag) return h;
  const past = [...h.past, prev];
  if (past.length > EDITOR_HISTORY_DEPTH) past.shift();
  return { past, future: [], lastTag: tag };
}

/** Compute the undo transition, or null when there is nothing to undo. */
export function undoSnapshot(
  h: EditorHistoryState,
  current: EditorDocument,
): { history: EditorHistoryState; doc: EditorDocument } | null {
  if (h.past.length === 0) return null;
  const doc = h.past[h.past.length - 1];
  return {
    history: {
      past: h.past.slice(0, -1),
      future: [current, ...h.future],
      lastTag: null,
    },
    doc,
  };
}

/** Compute the redo transition, or null when there is nothing to redo. */
export function redoSnapshot(
  h: EditorHistoryState,
  current: EditorDocument,
): { history: EditorHistoryState; doc: EditorDocument } | null {
  if (h.future.length === 0) return null;
  const doc = h.future[0];
  return {
    history: {
      past: [...h.past, current],
      future: h.future.slice(1),
      lastTag: null,
    },
    doc,
  };
}

// ── sessionStorage draft (plan §9 crash recovery) ─────────────────────────────

/** sessionStorage key for a variant's unsaved draft. */
export function draftKey(variantId: string): string {
  return `nova-editor-draft:${variantId}`;
}

export interface SerializedDraft {
  v: 1;
  variantId: string;
  doc: EditorDocument;
}

/** JSON-serialize a draft for sessionStorage. */
export function serializeDraft(variantId: string, doc: EditorDocument): string {
  const payload: SerializedDraft = { v: 1, variantId, doc };
  return JSON.stringify(payload);
}

/**
 * Parse a sessionStorage draft. Returns null on any malformed / foreign-shape
 * input (privacy mode, quota-truncated write, schema drift) so a bad draft can
 * never crash the shell — draft safety degrades silently, editing continues.
 */
export function deserializeDraft(raw: string | null | undefined): SerializedDraft | null {
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw) as Partial<SerializedDraft> & {
      doc?: Partial<EditorDocument>;
    };
    if (!parsed || parsed.v !== 1 || typeof parsed.variantId !== "string") return null;
    const doc = parsed.doc;
    if (!doc || !Array.isArray(doc.bars)) return null;
    return {
      v: 1,
      variantId: parsed.variantId,
      doc: {
        bars: doc.bars as TextElementBar[],
        slots: Array.isArray(doc.slots) ? (doc.slots as DraftSlot[]) : null,
        videoMuted: Boolean(doc.videoMuted),
        soundMuted: Boolean(doc.soundMuted),
        title: typeof doc.title === "string" ? doc.title : "",
      },
    };
  } catch {
    return null;
  }
}

// ── React hook ────────────────────────────────────────────────────────────────

export interface EditorHistory {
  /**
   * Record the CURRENT document as a restore point — call at the top of a
   * command handler, BEFORE the mutating setState/dispatch (which read the
   * same pre-mutation state). Pass a coalesce `tag` for typing bursts.
   */
  record: (tag?: string | null) => void;
  undo: () => void;
  redo: () => void;
  /** Drop the whole stack (Save — no undoing into a pre-persist world). */
  clear: () => void;
  canUndo: boolean;
  canRedo: boolean;
}

export function useEditorHistory(opts: {
  /** Reads the live working document (for snapshots). */
  getCurrent: () => EditorDocument;
  /** Writes a restored document back into the shell (RESET + setState). */
  apply: (doc: EditorDocument) => void;
}): EditorHistory {
  const [hist, setHist] = useState<EditorHistoryState>(initEditorHistoryState);
  // Ref mirror so undo/redo/record read the authoritative stack synchronously
  // (no updater-side-effects → StrictMode double-invoke safe).
  const histRef = useRef<EditorHistoryState>(hist);
  const getCurrentRef = useRef(opts.getCurrent);
  const applyRef = useRef(opts.apply);
  getCurrentRef.current = opts.getCurrent;
  applyRef.current = opts.apply;

  const commit = useCallback((next: EditorHistoryState) => {
    histRef.current = next;
    setHist(next);
  }, []);

  const record = useCallback(
    (tag: string | null = null) => {
      commit(recordSnapshot(histRef.current, getCurrentRef.current(), tag));
    },
    [commit],
  );

  const undo = useCallback(() => {
    const res = undoSnapshot(histRef.current, getCurrentRef.current());
    if (!res) return;
    applyRef.current(res.doc);
    commit(res.history);
  }, [commit]);

  const redo = useCallback(() => {
    const res = redoSnapshot(histRef.current, getCurrentRef.current());
    if (!res) return;
    applyRef.current(res.doc);
    commit(res.history);
  }, [commit]);

  const clear = useCallback(() => {
    commit(initEditorHistoryState());
  }, [commit]);

  return {
    record,
    undo,
    redo,
    clear,
    canUndo: hist.past.length > 0,
    canRedo: hist.future.length > 0,
  };
}
