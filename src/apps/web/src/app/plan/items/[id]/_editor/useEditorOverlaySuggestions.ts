"use client";

/**
 * useEditorOverlaySuggestions — suggestion-run state for the editor shell's
 * Overlays drawer (AI overlay auto-placement inside the TikTok-parity editor).
 *
 * Ports SuggestionRail's poll/staleness handling to a page-agnostic hook:
 *   - initial GET on mount / variant switch restores a pending run;
 *   - poll every 2.5s while "matching", "Still working…" past 60s;
 *   - R4 (C12): NO client-side give-up — "failed" only when the server says so;
 *   - rows only (re)seed when entering ready from a non-ready phase, so local
 *     accept/reject edits are never clobbered by a poll tick;
 *   - stale_cleared → transient script-changed notice.
 *
 * Unlike the item-page rail there is no Apply here: accepted envelopes become
 * working overlay cards in EditorShell and persist via editor-commit
 * (`accepted_suggestion_ids`), so this hook only removes rows locally. When a
 * reject empties the set and nothing was accepted this run, the pending set is
 * dismissed server-side (best-effort) so it doesn't reappear next session —
 * the dismiss endpoint clears the WHOLE set, so it must never fire while
 * accepted-but-unsaved envelopes still need to survive for the commit.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import {
  dismissOverlaySuggestions,
  getOverlaySuggestions,
  suggestVariantOverlays,
  type OverlaySuggestion,
  type OverlaySuggestionsResponse,
} from "@/lib/plan-api";

export const SUGGESTION_POLL_INTERVAL_MS = 2500;
/** After 60s of matching, add "Still working…" (keep polling — D19). */
const STILL_WORKING_TICKS = Math.ceil(60_000 / SUGGESTION_POLL_INTERVAL_MS);
const STALE_NOTICE_MS = 8000;

export type EditorSuggestionPhase = "idle" | "matching" | "ready" | "zero" | "failed";

/** Backend flag off → routes 404 (dual-flag trap, same detection as AssetPool). */
export function isUnavailableError(err: unknown): boolean {
  return (
    err instanceof Error && (/not available/i.test(err.message) || err.message.includes("(404)"))
  );
}

export interface EditorOverlaySuggestionsState {
  phase: EditorSuggestionPhase;
  rows: OverlaySuggestion[];
  wishlist: string[];
  staleNotice: boolean;
  stillWorking: boolean;
  unavailable: boolean;
  /** Kick off (or retry) the matcher. */
  start: () => void;
  /** Remove one row locally; `accepted` rows keep their envelope server-side
   *  until the commit drops it. */
  removeRow: (id: string, opts?: { accepted?: boolean }) => void;
}

export function useEditorOverlaySuggestions({
  itemId,
  variantId,
  enabled,
}: {
  itemId: string;
  variantId: string;
  enabled: boolean;
}): EditorOverlaySuggestionsState {
  const [phase, setPhase] = useState<EditorSuggestionPhase>("idle");
  const phaseRef = useRef<EditorSuggestionPhase>("idle");
  phaseRef.current = phase;

  const [rows, setRows] = useState<OverlaySuggestion[]>([]);
  const rowsRef = useRef<OverlaySuggestion[]>([]);
  rowsRef.current = rows;
  const [wishlist, setWishlist] = useState<string[]>([]);
  const [staleNotice, setStaleNotice] = useState(false);
  const [pollTicks, setPollTicks] = useState(0);
  const [unavailable, setUnavailable] = useState(false);
  /** True once any row was accepted for the CURRENT pending set — blocks the
   *  emptied-set auto-dismiss (those envelopes must survive for the commit). */
  const acceptedThisRunRef = useRef(false);

  const staleTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const showStaleNotice = useCallback(() => {
    setStaleNotice(true);
    if (staleTimer.current) clearTimeout(staleTimer.current);
    staleTimer.current = setTimeout(() => setStaleNotice(false), STALE_NOTICE_MS);
  }, []);
  useEffect(
    () => () => {
      if (staleTimer.current) clearTimeout(staleTimer.current);
    },
    [],
  );

  /** Fold a GET response into local state (SuggestionRail semantics). */
  const applyResponse = useCallback(
    (res: OverlaySuggestionsResponse) => {
      if (res.stale_cleared) showStaleNotice();
      const prev = phaseRef.current;
      switch (res.status) {
        case "matching":
          setPhase("matching");
          break;
        case "ready":
          if (prev !== "ready") {
            setRows(res.suggestions);
            setWishlist(res.wishlist);
            acceptedThisRunRef.current = false;
            setPhase("ready");
          }
          break;
        case "zero":
          setWishlist(res.wishlist);
          setPhase("zero");
          break;
        case "failed":
          setPhase("failed");
          break;
        default:
          // status null — nothing pending.
          setPhase("idle");
      }
    },
    [showStaleNotice],
  );

  /** Monotonic identity token: bumped on every itemId/variantId reset so a
   *  late-resolving GET from the previous variant can never seed rows into the
   *  current one (out-of-order poll responses are dropped, not applied). */
  const runIdRef = useRef(0);

  const refresh = useCallback(async () => {
    const runId = runIdRef.current;
    try {
      const res = await getOverlaySuggestions(itemId, variantId);
      if (runIdRef.current !== runId) return; // stale response — identity moved
      applyResponse(res);
    } catch (err) {
      if (runIdRef.current !== runId) return;
      if (isUnavailableError(err)) setUnavailable(true);
      // Transient poll errors: keep the current phase; the next tick retries.
    }
  }, [applyResponse, itemId, variantId]);

  // Initial load + reset on variant switch.
  useEffect(() => {
    if (!enabled) return;
    runIdRef.current += 1;
    setPhase("idle");
    setRows([]);
    setWishlist([]);
    setPollTicks(0);
    acceptedThisRunRef.current = false;
    void refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps -- reset is keyed on the identity, not refresh's ref
  }, [enabled, itemId, variantId]);

  // Poll while matching.
  useEffect(() => {
    if (!enabled || phase !== "matching") return;
    const id = setInterval(() => {
      setPollTicks((t) => t + 1);
      void refresh();
    }, SUGGESTION_POLL_INTERVAL_MS);
    return () => clearInterval(id);
  }, [enabled, phase, refresh]);

  const start = useCallback(() => {
    setStaleNotice(false);
    setPollTicks(0);
    setPhase("matching");
    void suggestVariantOverlays(itemId, variantId).catch((err) => {
      if (isUnavailableError(err)) setUnavailable(true);
      setPhase("failed");
    });
  }, [itemId, variantId]);

  const removeRow = useCallback(
    (id: string, opts?: { accepted?: boolean }) => {
      if (opts?.accepted) acceptedThisRunRef.current = true;
      const next = rowsRef.current.filter((r) => r.id !== id);
      setRows(next);
      // All rows rejected (none accepted) → clear the pending set server-side
      // so it doesn't reappear next session. Best-effort; the local list
      // already emptied. Never fires when anything was accepted this run —
      // those envelopes must survive until the commit drops them by id.
      if (next.length === 0 && !acceptedThisRunRef.current) {
        void dismissOverlaySuggestions(itemId, variantId).catch(() => {});
      }
    },
    [itemId, variantId],
  );

  return {
    phase,
    rows,
    wishlist,
    staleNotice,
    stillWorking: pollTicks >= STILL_WORKING_TICKS,
    unavailable,
    start,
    removeRow,
  };
}
