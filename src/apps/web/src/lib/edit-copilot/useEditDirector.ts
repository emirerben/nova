"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  applySpeechCutCandidate,
  cancelOmniAsset,
  claimOmniAsset,
  editDirectorFeedback,
  editDirectorSuggestions,
  getOmniAsset,
  restoreOriginalSpeechTiming,
  startOmniAsset,
  type EditorSuggestion,
  type OmniAssetResponse,
  type SpeechCutOperation,
} from "@/lib/plan-api";
import type { CopilotOp } from "./ops";
import type { ApplyCopilotOpsResult, ChangeChip } from "./apply-ops";
import {
  CopilotSnapshotTooLargeError,
  type CopilotSnapshot,
} from "./snapshot";
import { isFeatureUnavailable } from "./availability";

export interface UseEditDirectorOptions {
  enabled: boolean;
  omniEnabled: boolean;
  itemId: string;
  variantId: string;
  buildSnapshot: () => CopilotSnapshot;
  applyOpsAtomic: (
    ops: CopilotOp[],
    snapshot: CopilotSnapshot,
  ) => ApplyCopilotOpsResult;
  onApplied: (result: ApplyCopilotOpsResult) => DirectorApplyPresentation | void;
  onRevealApplied?: (focus: DirectorPreviewFocus) => void;
  onGeneratedAssetReady?: () => void | Promise<void>;
  speechCutRevision?: string | null;
  speechCutLastReceipt?: SpeechCutOperation | null;
  speechCutLastError?: { operation_id?: string | null; message: string } | null;
  serverRenderPending?: boolean;
  serverOperationsEnabled?: boolean;
  onServerRenderStarted?: () => void | Promise<void>;
  canRestoreOriginalTiming?: boolean;
}

export interface DirectorGenerationState {
  suggestionId: string;
  assetId: string;
  status: OmniAssetResponse["status"];
  progress: number;
}

export interface DirectorPreviewFocus {
  kind: "text" | "clip" | "sfx" | "overlay";
  id: string;
  seekS: number;
}

export interface DirectorApplyPresentation {
  undoVersion?: number;
  previewFocus?: DirectorPreviewFocus;
  /** Chat steps feed (PR4, NEXT_PUBLIC_NOVA_STEPS_FEED_ENABLED): marks the
   *  turn as a server-render dispatch (today: set_intro_layout) so
   *  CopilotDrawer shows the disclosure + live activity feed instead of
   *  receipt rows, and never offers an Undo affordance. Unused by the
   *  director itself — carried here only so handleCopilotOps' single return
   *  type stays compatible with both useEditCopilot and useEditDirector. */
  isRenderTurn?: boolean;
  /** Overrides the default outcome-derived assistant reply for this turn
   *  (chat steps feed only). Unused by the director. */
  assistantText?: string;
}

export interface DirectorAppliedReceipt {
  id: string;
  suggestionId: string;
  title: string;
  startS: number;
  endS: number;
  changes: ChangeChip[];
  undoVersion?: number;
  previewFocus?: DirectorPreviewFocus;
}

export interface UseEditDirectorResult {
  suggestions: EditorSuggestion[];
  appliedReceipts: DirectorAppliedReceipt[];
  /** True after the user has explicitly requested and received a review. */
  reviewed: boolean;
  loading: boolean;
  error: string | null;
  /** The API cannot serve director reviews at all; polling has been stopped. */
  unavailable: boolean;
  /** A non-retryable budget breaker is active until its server-provided reset. */
  reviewBlocked: boolean;
  modelUsed: string;
  omniMaxCostPerSecondUsd: number | null;
  generation: DirectorGenerationState | null;
  omniDispatchPending: boolean;
  serverRendering: boolean;
  refresh: () => void;
  accept: (
    suggestion: EditorSuggestion,
    options?: { omniCostConfirmed?: boolean },
  ) => void;
  dismiss: (suggestion: EditorSuggestion) => void;
  revealApplied: (receipt: DirectorAppliedReceipt) => void;
  cancelGeneration: () => void;
  restoreOriginalTiming: () => void;
  canRestoreOriginalTiming: boolean;
}

function dismissedKey(itemId: string, variantId: string): string {
  return `nova-edit-director-dismissed:${itemId}:${variantId}`;
}

function readDismissed(itemId: string, variantId: string): string[] {
  if (typeof window === "undefined") return [];
  try {
    const parsed = JSON.parse(window.sessionStorage.getItem(dismissedKey(itemId, variantId)) ?? "[]");
    return Array.isArray(parsed)
      ? parsed.filter((item): item is string => typeof item === "string").slice(-30)
      : [];
  } catch {
    return [];
  }
}

function writeDismissed(itemId: string, variantId: string, ids: string[]): void {
  try {
    window.sessionStorage.setItem(
      dismissedKey(itemId, variantId),
      JSON.stringify(ids.slice(-30)),
    );
  } catch {
    // Session storage is optional; server-side filtering still applies this turn.
  }
}

/** Shown once when the API has no director route, in place of a dead retry. */
export const DIRECTOR_UNAVAILABLE_MESSAGE =
  "Kria’s review isn’t available right now. Your draft is unchanged.";
export const DIRECTOR_CAPABILITY_MISMATCH_MESSAGE =
  "Kria’s review is updating. Retry the review shortly.";
export const DIRECTOR_SNAPSHOT_TOO_LARGE_MESSAGE =
  "The editor context is too large to review in one request. Your draft is unchanged.";
export const DIRECTOR_DRAFT_CHANGED_MESSAGE =
  "The draft changed while Kria was reviewing it. Review again when you’re ready.";
const DIRECTOR_REVIEW_DEBOUNCE_MS = 1200;
const MAX_APPLIED_RECEIPTS = 8;

/**
 * Editor focus is useful to local UI affordances but is volatile navigation
 * state, not Director context. Preserve the snapshot's local row identities
 * and non-enumerable mutation fingerprints while removing it from the wire
 * payload and revision input.
 */
export function stripDirectorEditorFocus(snapshot: CopilotSnapshot): CopilotSnapshot {
  const withoutFocus = Object.create(
    Object.getPrototypeOf(snapshot),
    Object.getOwnPropertyDescriptors(snapshot),
  ) as CopilotSnapshot;
  delete withoutFocus.editor_focus;
  return withoutFocus;
}

type DirectorSnapshotBuildResult =
  | { snapshot: CopilotSnapshot; error: null }
  | { snapshot: null; error: string };

function tryBuildDirectorSnapshot(
  buildSnapshot: () => CopilotSnapshot,
): DirectorSnapshotBuildResult {
  try {
    return { snapshot: stripDirectorEditorFocus(buildSnapshot()), error: null };
  } catch (caught) {
    if (caught instanceof CopilotSnapshotTooLargeError) {
      return { snapshot: null, error: DIRECTOR_SNAPSHOT_TOO_LARGE_MESSAGE };
    }
    throw caught;
  }
}

function friendlyDirectorError(caught: unknown): string {
  if (caught instanceof DOMException && caught.name === "AbortError") return "";
  if (isFeatureUnavailable(caught)) return DIRECTOR_UNAVAILABLE_MESSAGE;
  const status = directorErrorStatus(caught);
  const code = caught && typeof caught === "object" && "code" in caught
    ? String((caught as { code?: unknown }).code ?? "")
    : "";
  const stage = caught && typeof caught === "object" && "stage" in caught
    ? String((caught as { stage?: unknown }).stage ?? "")
    : "";
  const retryable = caught && typeof caught === "object" && "retryable" in caught
    ? (caught as { retryable?: unknown }).retryable
    : true;
  const resetAt = directorBudgetResetAt(caught);
  if (status === 429 && code === "ai_budget_exhausted" && retryable === false) {
    const formatted = resetAt
      ? new Intl.DateTimeFormat(undefined, {
          dateStyle: "medium",
          timeStyle: "short",
        }).format(new Date(resetAt))
      : "the next budget window";
    return `This draft’s review limit has been reached. Reviews become available again ${formatted}. Your draft is unchanged.`;
  }
  if (status === null) {
    return "Kria couldn’t connect to the review service. Check your connection and retry the review. Your draft is unchanged.";
  }
  if (
    status === 502 ||
    code.includes("director_failed") ||
    code.includes("model") ||
    stage.includes("model")
  ) {
    return "Kria couldn’t complete this review. Retry the review. Your draft is unchanged.";
  }
  if (status >= 500) {
    return "Kria couldn’t complete this review because the service is unavailable. Retry shortly. Your draft is unchanged.";
  }
  return "Kria couldn’t review this draft. Retry the review. Your draft is unchanged.";
}

function directorBudgetResetAt(caught: unknown): string | null {
  if (!caught || typeof caught !== "object") return null;
  const value = "resetAt" in caught ? (caught as { resetAt?: unknown }).resetAt : null;
  if (typeof value !== "string" || !Number.isFinite(Date.parse(value))) return null;
  return value;
}

function directorErrorStatus(caught: unknown): number | null {
  if (!caught || typeof caught !== "object" || !("status" in caught)) return null;
  const status = (caught as { status?: unknown }).status;
  return typeof status === "number" ? status : null;
}

function friendlyOmniError(status: OmniAssetResponse["status"]): string {
  if (status === "cancelled") {
    return "Generated clip cancelled. Your draft was not changed.";
  }
  return "Kria couldn’t generate that clip. Retry the request. Your draft is unchanged.";
}

export function directorSnapshotRevision(snapshot: CopilotSnapshot): string {
  const revisionSnapshot = stripDirectorEditorFocus(snapshot);
  // Background metadata refreshes do not edit the draft. Keep their status in
  // the request for honest reasoning, but only changed asset content invalidates
  // a suggestion or an in-flight generated clip.
  delete revisionSnapshot.asset_context_status;
  const value = JSON.stringify(revisionSnapshot);
  let hash = 2166136261;
  for (let i = 0; i < value.length; i += 1) {
    hash ^= value.charCodeAt(i);
    hash = Math.imul(hash, 16777619);
  }
  return `v1-${(hash >>> 0).toString(16)}-${value.length}`;
}

export function useEditDirector(
  opts: UseEditDirectorOptions,
): UseEditDirectorResult {
  const [suggestions, setSuggestions] = useState<EditorSuggestion[]>([]);
  const [appliedReceipts, setAppliedReceipts] = useState<DirectorAppliedReceipt[]>([]);
  const suggestionsRef = useRef<EditorSuggestion[]>([]);
  suggestionsRef.current = suggestions;
  const [reviewed, setReviewed] = useState(false);
  const [loading, setLoading] = useState(false);
  const [reviewQueued, setReviewQueued] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [unavailable, setUnavailable] = useState(false);
  const [reviewBlockedUntil, setReviewBlockedUntil] = useState<string | null>(null);
  const [reviewBlockTimerTick, setReviewBlockTimerTick] = useState(0);
  const [modelUsed, setModelUsed] = useState("");
  const [omniMaxCostPerSecondUsd, setOmniMaxCostPerSecondUsd] = useState<number | null>(null);
  const [generation, setGeneration] = useState<DirectorGenerationState | null>(null);
  const [omniDispatchPending, setOmniDispatchPending] = useState(false);
  const [serverDispatchPending, setServerDispatchPending] = useState(false);
  const [reviewRequestKey, setReviewRequestKey] = useState(0);
  const settledReviewRequestKeyRef = useRef(0);
  const requestIdRef = useRef(0);
  const sourceSnapshotRef = useRef<CopilotSnapshot | null>(null);
  const sourceRevisionRef = useRef("");
  const optsRef = useRef(opts);
  const generationTokenRef = useRef(0);
  const omniDispatchPendingRef = useRef(false);
  const receiptSequenceRef = useRef(0);
  const lastServerReceiptIdRef = useRef("");
  const lastServerFailureIdRef = useRef("");
  const pendingServerOperationIdRef = useRef("");
  const pendingServerSuggestionRef = useRef<EditorSuggestion | null>(null);
  // Stays armed across abort/restart cycles until a replacement review either
  // lands or fails. A one-render latch loses refreshes during async hydration.
  const forceRefreshRef = useRef(false);
  const snapshotBuildErrorRef = useRef<string | null>(null);
  optsRef.current = opts;
  // Unlike history.version, this includes async editor hydration (asset pool,
  // captions, capabilities, overlays). Hydration invalidates an in-flight paid
  // review; the user explicitly starts the replacement once the draft settles.
  const directorEnabled = opts.enabled;
  const canRestoreOriginalTiming = opts.canRestoreOriginalTiming === true;
  const buildSnapshot = opts.buildSnapshot;
  const currentSnapshotState = useMemo(() => {
    if (!directorEnabled) return { revision: "", error: null };
    const built = tryBuildDirectorSnapshot(buildSnapshot);
    if (!built.snapshot) {
      // An oversized draft is a changed revision too. Cancel stale requests
      // and let restoring even the previous draft restart review normally.
      return { revision: "context-too-large", error: built.error };
    }
    const revision = directorSnapshotRevision(built.snapshot);
    return { revision, error: null };
  }, [directorEnabled, buildSnapshot]);
  const currentSnapshotRevision = currentSnapshotState.revision;
  const snapshotBuildError = currentSnapshotState.error;
  snapshotBuildErrorRef.current = snapshotBuildError;

  useEffect(() => {
    setError((current) => snapshotBuildError ??
      (current === DIRECTOR_SNAPSHOT_TOO_LARGE_MESSAGE ? null : current));
  }, [snapshotBuildError]);

  useEffect(() => {
    suggestionsRef.current = [];
    setSuggestions([]);
    setAppliedReceipts([]);
    setReviewed(false);
    setReviewQueued(false);
    setError(snapshotBuildErrorRef.current);
    setUnavailable(false);
    setReviewBlockedUntil(null);
    setModelUsed("");
    setOmniMaxCostPerSecondUsd(null);
    sourceRevisionRef.current = "";
    settledReviewRequestKeyRef.current = 0;
    setReviewRequestKey(0);
    forceRefreshRef.current = false;
    generationTokenRef.current += 1;
    setGeneration(null);
    omniDispatchPendingRef.current = false;
    setOmniDispatchPending(false);
    setServerDispatchPending(false);
    lastServerReceiptIdRef.current = "";
    lastServerFailureIdRef.current = "";
    pendingServerOperationIdRef.current = "";
    pendingServerSuggestionRef.current = null;
  }, [opts.itemId, opts.variantId]);

  useEffect(() => {
    if (!reviewBlockedUntil) return;
    const remainingMs = Date.parse(reviewBlockedUntil) - Date.now();
    if (remainingMs <= 0) {
      setReviewBlockedUntil(null);
      return;
    }
    const maxTimerMs = 2_147_000_000;
    const timer = window.setTimeout(() => {
      if (remainingMs > maxTimerMs) {
        // Monthly resets can sit beyond the browser's maximum timer delay.
        // Recalculate the remainder instead of enabling the paid action early.
        setReviewBlockTimerTick((value) => value + 1);
      } else {
        setReviewBlockedUntil(null);
      }
    }, Math.min(remainingMs, maxTimerMs));
    return () => window.clearTimeout(timer);
  }, [reviewBlockedUntil, reviewBlockTimerTick]);

  useEffect(() => {
    if (opts.serverRenderPending) {
      setServerDispatchPending(false);
    }
  }, [opts.serverRenderPending]);

  useEffect(() => {
    const receipt = opts.speechCutLastReceipt;
    if (!receipt || receipt.status !== "applied") return;
    const receiptId = receipt.render_generation_id || receipt.revision;
    if (!receiptId || lastServerReceiptIdRef.current === receiptId) return;
    lastServerReceiptIdRef.current = receiptId;
    pendingServerOperationIdRef.current = "";
    setServerDispatchPending(false);
    const removed = receipt.removed;
    const saved = Number(receipt.time_saved_s || 0);
    const restored = Number(receipt.restored_s || 0);
    const serverReceipt: DirectorAppliedReceipt = {
      id: `speech-cut-${receiptId}`,
      suggestionId: pendingServerSuggestionRef.current?.id || receipt.operation,
      title:
        receipt.operation === "restore_original_timing"
          ? "Original timing restored"
          : "Reviewed speech cut applied",
      startS: Number(removed?.start_s || 0),
      endS: Number(removed?.end_s || removed?.start_s || 0),
      changes: [
        receipt.operation === "restore_original_timing"
          ? {
              label: "Speech timing",
              from: `${restored.toFixed(3)}s removed`,
              to: "Original timing restored",
            }
          : {
              label: "Speech timing",
              from: `${Number(removed?.start_s || 0).toFixed(3)}-${Number(removed?.end_s || 0).toFixed(3)}s`,
              to: `${saved.toFixed(3)}s removed and downstream timing rebuilt`,
            },
      ],
    };
    setAppliedReceipts((current) =>
      [...current.filter((item) => item.id !== serverReceipt.id), serverReceipt].slice(
        -MAX_APPLIED_RECEIPTS,
      ),
    );
  }, [opts.speechCutLastReceipt]);

  const serverRendering = serverDispatchPending || opts.serverRenderPending === true;

  useEffect(() => () => {
    generationTokenRef.current += 1;
  }, []);

  useEffect(() => {
    if (!opts.enabled || !opts.itemId || !opts.variantId) return;
    // Paid reviews are explicit. One button press owns one provider attempt;
    // later snapshot changes may invalidate its result but never auto-start a
    // replacement paid review.
    if (reviewRequestKey === 0) return;
    if (settledReviewRequestKeyRef.current === reviewRequestKey) return;
    if (unavailable) return;
    const forceRefresh = forceRefreshRef.current;
    // Keep a returned review stable while the user works through it. Director
    // suggestions are server-validated into sequentially compatible edit
    // domains, and applyOpsAtomic rejects a card if its own target changed.
    if (snapshotBuildErrorRef.current) {
      setReviewQueued(false);
      return;
    }
    if (suggestionsRef.current.length > 0 && !forceRefresh) return;
    const controller = new AbortController();
    let activeRequestId = 0;
    const timer = window.setTimeout(() => {
      const built = tryBuildDirectorSnapshot(optsRef.current.buildSnapshot);
      if (!built.snapshot) {
        forceRefreshRef.current = false;
        setReviewQueued(false);
        setError(built.error);
        return;
      }
      const snapshot = built.snapshot;
      if (snapshot.allowed_op_families.length === 0) {
        settledReviewRequestKeyRef.current = reviewRequestKey;
        setReviewed(true);
        forceRefreshRef.current = false;
        setReviewQueued(false);
        suggestionsRef.current = [];
        setSuggestions([]);
        return;
      }
      const revision = directorSnapshotRevision(snapshot);
      requestIdRef.current += 1;
      const requestId = requestIdRef.current;
      activeRequestId = requestId;
      setReviewQueued(false);
      setLoading(true);
      setError(null);
      void editDirectorSuggestions(optsRef.current.itemId, optsRef.current.variantId, {
        snapshot,
        snapshot_revision: revision,
        dismissed_suggestion_ids: readDismissed(
          optsRef.current.itemId,
          optsRef.current.variantId,
        ),
        omni_enabled: optsRef.current.omniEnabled,
      }, controller.signal)
        .then((response) => {
          if (requestId !== requestIdRef.current) return;
          const current = tryBuildDirectorSnapshot(optsRef.current.buildSnapshot);
          if (!current.snapshot) {
            setError(current.error);
            return;
          }
          const currentRevision = directorSnapshotRevision(current.snapshot);
          if (
            response.snapshot_revision !== revision ||
            currentRevision !== revision
          ) {
            settledReviewRequestKeyRef.current = reviewRequestKey;
            forceRefreshRef.current = false;
            suggestionsRef.current = [];
            setSuggestions([]);
            setReviewed(true);
            setError(DIRECTOR_DRAFT_CHANGED_MESSAGE);
            return;
          }
          sourceSnapshotRef.current = snapshot;
          sourceRevisionRef.current = revision;
          const nextSuggestions = optsRef.current.omniEnabled
            ? response.suggestions
            : response.suggestions.filter((item) => item.apply_mode !== "omni_async");
          suggestionsRef.current = nextSuggestions;
          setSuggestions(nextSuggestions);
          settledReviewRequestKeyRef.current = reviewRequestKey;
          setReviewed(true);
          forceRefreshRef.current = false;
          setModelUsed(response.model_used);
          setOmniMaxCostPerSecondUsd(response.omni_max_cost_per_second_usd ?? null);
          setReviewBlockedUntil(null);
          if (nextSuggestions.length === 0 && response.suggestions.length > 0) {
            setError(DIRECTOR_CAPABILITY_MISMATCH_MESSAGE);
          }
        })
        .catch((caught) => {
          if (requestId !== requestIdRef.current || controller.signal.aborted) return;
          if (directorErrorStatus(caught) === 409) {
            // The server rejected a stale revision before provider contact.
            // Keep the one-click/one-request invariant: the user can choose
            // Review again once the draft has settled.
            forceRefreshRef.current = false;
            settledReviewRequestKeyRef.current = reviewRequestKey;
            suggestionsRef.current = [];
            setSuggestions([]);
            setReviewed(true);
            setError(DIRECTOR_DRAFT_CHANGED_MESSAGE);
            return;
          }
          forceRefreshRef.current = false;
          settledReviewRequestKeyRef.current = reviewRequestKey;
          if (isFeatureUnavailable(caught)) setUnavailable(true);
          const budgetResetAt = directorBudgetResetAt(caught);
          if (directorErrorStatus(caught) === 429 && budgetResetAt) {
            setReviewBlockedUntil(budgetResetAt);
          }
          const requestRef = caught && typeof caught === "object" && "requestId" in caught
            ? String((caught as { requestId?: unknown }).requestId ?? "")
            : "";
          console.error("Nova Director review failed", {
            request_id: requestRef || `client-${requestId}`,
            snapshot_revision: revision,
            status: directorErrorStatus(caught),
          });
          setError(friendlyDirectorError(caught));
        })
        .finally(() => {
          if (requestId === requestIdRef.current) setLoading(false);
        });
    }, DIRECTOR_REVIEW_DEBOUNCE_MS);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
      if (activeRequestId === requestIdRef.current) setLoading(false);
      requestIdRef.current += 1;
    };
  }, [
    opts.enabled,
    opts.itemId,
    opts.variantId,
    reviewRequestKey,
    unavailable,
  ]);

  const feedback = useCallback(
    (suggestion: EditorSuggestion, action: "accepted" | "dismissed") => {
      void editDirectorFeedback(optsRef.current.itemId, optsRef.current.variantId, {
        suggestion_id: suggestion.id,
        action,
        category: suggestion.category,
        model_used: modelUsed,
      }).catch(() => {});
    },
    [modelUsed],
  );

  useEffect(() => {
    const receipt = opts.speechCutLastReceipt;
    const pending = pendingServerSuggestionRef.current;
    if (!pending || !receipt || receipt.status !== "applied") return;
    feedback(pending, "accepted");
    pendingServerSuggestionRef.current = null;
  }, [feedback, opts.speechCutLastReceipt]);

  const removeSuggestion = useCallback((suggestionId: string) => {
    const remaining = suggestionsRef.current.filter((item) => item.id !== suggestionId);
    suggestionsRef.current = remaining;
    setSuggestions(remaining);
  }, []);

  const refreshReview = useCallback(() => {
    if (reviewBlockedUntil && Date.parse(reviewBlockedUntil) > Date.now()) return;
    forceRefreshRef.current = true;
    setUnavailable(false);
    setReviewQueued(true);
    setReviewRequestKey((value) => value + 1);
  }, [reviewBlockedUntil]);

  useEffect(() => {
    const failure = opts.speechCutLastError;
    const operationId = String(failure?.operation_id || "");
    if (
      !operationId ||
      operationId !== pendingServerOperationIdRef.current ||
      operationId === lastServerFailureIdRef.current ||
      opts.serverRenderPending
    ) {
      return;
    }
    lastServerFailureIdRef.current = operationId;
    pendingServerOperationIdRef.current = "";
    pendingServerSuggestionRef.current = null;
    setServerDispatchPending(false);
    setError("Kria couldn’t complete that timing change. Retry the change. The current video is unchanged.");
    refreshReview();
  }, [opts.serverRenderPending, opts.speechCutLastError, refreshReview]);

  const dismiss = useCallback(
    (suggestion: EditorSuggestion) => {
      removeSuggestion(suggestion.id);
      const ids = [
        ...readDismissed(optsRef.current.itemId, optsRef.current.variantId),
        suggestion.id,
      ];
      writeDismissed(
        optsRef.current.itemId,
        optsRef.current.variantId,
        Array.from(new Set(ids)),
      );
      feedback(suggestion, "dismissed");
    },
    [feedback, removeSuggestion],
  );

  const completeAcceptance = useCallback(
    (suggestion: EditorSuggestion, result: ApplyCopilotOpsResult): boolean => {
      let presentation: DirectorApplyPresentation | void;
      try {
        presentation = optsRef.current.onApplied(result);
      } catch {
        setError("Kria couldn’t confirm that change. Check the preview or undo it before retrying.");
        return false;
      }

      receiptSequenceRef.current += 1;
      const receipt: DirectorAppliedReceipt = {
        id: `${suggestion.id}-${receiptSequenceRef.current}`,
        suggestionId: suggestion.id,
        title: suggestion.title,
        startS: suggestion.start_s,
        endS: suggestion.end_s,
        changes: result.applied,
        undoVersion: presentation?.undoVersion,
        previewFocus: presentation?.previewFocus,
      };
      setAppliedReceipts((current) =>
        [...current, receipt].slice(-MAX_APPLIED_RECEIPTS),
      );
      removeSuggestion(suggestion.id);
      setError(null);
      feedback(suggestion, "accepted");
      return true;
    },
    [feedback, removeSuggestion],
  );

  const accept = useCallback(
    (
      suggestion: EditorSuggestion,
      options?: { omniCostConfirmed?: boolean },
    ) => {
      if (suggestion.apply_mode === "server_async") {
        const op = suggestion.ops.find(
          (candidate) => candidate.op === "apply_speech_cut_candidate",
        );
        if (!op || op.op !== "apply_speech_cut_candidate") return;
        if (optsRef.current.serverOperationsEnabled === false) {
          setError("Save your draft before applying a timing change.");
          return;
        }
        const revision = optsRef.current.speechCutRevision;
        if (!revision || serverRendering) {
          setError("This cut is out of date. Kria is refreshing the review.");
          refreshReview();
          return;
        }
        pendingServerSuggestionRef.current = suggestion;
        setServerDispatchPending(true);
        setError(null);
        void applySpeechCutCandidate(
          optsRef.current.itemId,
          optsRef.current.variantId,
          op.candidate_id,
          revision,
        )
          .then(async (response) => {
            pendingServerOperationIdRef.current = String(
              response.request.operation_id || "",
            );
            removeSuggestion(suggestion.id);
            await optsRef.current.onServerRenderStarted?.();
          })
          .catch(() => {
            pendingServerSuggestionRef.current = null;
            pendingServerOperationIdRef.current = "";
            setServerDispatchPending(false);
            setError("Kria couldn’t apply that cut. Retry the change. The current video is unchanged.");
            refreshReview();
          });
        return;
      }
      if (suggestion.apply_mode === "omni_async") {
        if (!suggestion.omni || generation || omniDispatchPendingRef.current) return;
        // The visible AlertDialog owns the explicit cost confirmation. Keep a
        // second guard here so a future caller cannot start an Omni request by
        // invoking the hook directly without that confirmation.
        if (options?.omniCostConfirmed !== true) return;
        const source = sourceSnapshotRef.current;
        const sourceRevision = sourceRevisionRef.current;
        const current = tryBuildDirectorSnapshot(optsRef.current.buildSnapshot);
        if (!current.snapshot) {
          setError(current.error);
          return;
        }
        if (!source || directorSnapshotRevision(current.snapshot) !== sourceRevision) {
          setError("The draft changed. Kria is refreshing this suggestion.");
          refreshReview();
          return;
        }
        generationTokenRef.current += 1;
        const token = generationTokenRef.current;
        const itemId = optsRef.current.itemId;
        const variantId = optsRef.current.variantId;
        let startedAssetId = "";
        let contextSnapshotError: string | null = null;
        const identityIsCurrent = () =>
          token === generationTokenRef.current &&
          optsRef.current.itemId === itemId &&
          optsRef.current.variantId === variantId;
        const contextIsCurrent = () => {
          if (!identityIsCurrent()) return false;
          const currentSnapshot = tryBuildDirectorSnapshot(optsRef.current.buildSnapshot);
          if (!currentSnapshot.snapshot) {
            contextSnapshotError = currentSnapshot.error;
            setError(currentSnapshot.error);
            return false;
          }
          contextSnapshotError = null;
          return directorSnapshotRevision(currentSnapshot.snapshot) === sourceRevision;
        };
        const abandonAsset = (assetId = startedAssetId) => {
          if (assetId) {
            void cancelOmniAsset(itemId, variantId, assetId).catch(() => {});
          }
          if (identityIsCurrent()) {
            setGeneration(null);
            setError(
              contextSnapshotError ??
                "The generated clip is ready, but the draft changed, so Kria didn’t insert it. Review the latest draft and try again.",
            );
          }
        };
        setError(null);
        if (omniMaxCostPerSecondUsd === null) {
          setError("Kria couldn’t confirm the provider cost. Review the draft again before generating.");
          return;
        }
        // Consent must never understate the server's four-decimal estimate.
        // Ceiling to a cent so fractional durations cannot be rejected after
        // the creator confirms the displayed maximum.
        const estimatedMaxCostUsd = Math.ceil(
          suggestion.omni.duration_s * omniMaxCostPerSecondUsd * 100,
        ) / 100;
        omniDispatchPendingRef.current = true;
        setOmniDispatchPending(true);
        void startOmniAsset(
          itemId,
          variantId,
          {
            suggestion_id: suggestion.id,
            draft_revision: sourceRevision,
            ...suggestion.omni,
            estimated_max_cost_usd: estimatedMaxCostUsd,
            cost_confirmed: true,
          },
        )
          .then(async (started) => {
            startedAssetId = started.asset_id;
            if (!contextIsCurrent()) {
              omniDispatchPendingRef.current = false;
              setOmniDispatchPending(false);
              abandonAsset(started.asset_id);
              return;
            }
            setGeneration({
              suggestionId: suggestion.id,
              assetId: started.asset_id,
              status: started.status,
              progress: started.progress,
            });
            omniDispatchPendingRef.current = false;
            setOmniDispatchPending(false);
            let current = started;
            while (
              contextIsCurrent() &&
              !["ready", "failed", "cancelled"].includes(current.status)
            ) {
              await new Promise((resolve) => window.setTimeout(resolve, 2000));
              if (!contextIsCurrent()) {
                abandonAsset(started.asset_id);
                return;
              }
              current = await getOmniAsset(
                itemId,
                variantId,
                started.asset_id,
              );
              if (!contextIsCurrent()) {
                abandonAsset(started.asset_id);
                return;
              }
              setGeneration({
                suggestionId: suggestion.id,
                assetId: started.asset_id,
                status: current.status,
                progress: current.progress,
              });
            }
            if (!contextIsCurrent()) {
              abandonAsset(started.asset_id);
              return;
            }
            if (current.status !== "ready") {
              setError(
                friendlyOmniError(current.status),
              );
              setGeneration(null);
              return;
            }
            current = await claimOmniAsset(
              itemId,
              variantId,
              started.asset_id,
              sourceRevision,
            );
            if (!contextIsCurrent()) {
              abandonAsset();
              return;
            }
            if (!current.operation) {
              setError("The generated clip is ready, but Kria couldn’t add it to this draft. Review the latest draft and try again.");
              setGeneration(null);
              return;
            }
            const result = optsRef.current.applyOpsAtomic([current.operation], source);
            if (!contextIsCurrent()) {
              abandonAsset();
              return;
            }
            if (result.rejected.length > 0 || result.applied.length === 0) {
              abandonAsset();
              setError(
                result.rejected[0]?.detail ??
                  "The generated clip is ready, but it no longer fits this draft.",
              );
              setGeneration(null);
              return;
            }
            // Local application is the acceptance boundary. Remove the
            // cancellation affordance before mutating the draft so a slow
            // candidate refresh cannot release the asset underneath it.
            setGeneration(null);
            if (!completeAcceptance(suggestion, result)) {
              return;
            }
            try {
              await optsRef.current.onGeneratedAssetReady?.();
            } catch {
              setError("The generated clip was added, but its preview could not refresh. Save or Undo still work.");
            }
          })
          .catch(() => {
            if (token !== generationTokenRef.current) return;
            omniDispatchPendingRef.current = false;
            setOmniDispatchPending(false);
            setGeneration(null);
            setError("Kria couldn’t start generating that video. Retry the request. Your draft is unchanged.");
          });
        return;
      }
      const source = sourceSnapshotRef.current;
      if (!source) {
        setError("The draft changed. Kria is refreshing this suggestion.");
        refreshReview();
        return;
      }
      const localOps = suggestion.ops.filter(
        (op): op is CopilotOp => op.op !== "apply_speech_cut_candidate",
      );
      const result = optsRef.current.applyOpsAtomic(localOps, source);
      if (result.rejected.length > 0 || result.applied.length === 0) {
        setError(
          result.rejected[0]?.detail ??
            "That suggestion no longer fits the current draft.",
        );
        refreshReview();
        return;
      }
      completeAcceptance(suggestion, result);
    },
    [
      completeAcceptance,
      generation,
      omniMaxCostPerSecondUsd,
      refreshReview,
      removeSuggestion,
      serverRendering,
    ],
  );

  const revealApplied = useCallback((receipt: DirectorAppliedReceipt) => {
    if (receipt.previewFocus) optsRef.current.onRevealApplied?.(receipt.previewFocus);
  }, []);

  const cancelGeneration = useCallback(() => {
    if (!generation) return;
    generationTokenRef.current += 1;
    const active = generation;
    setGeneration({
      ...active,
      status: "cancellation_requested",
    });
    void cancelOmniAsset(
      optsRef.current.itemId,
      optsRef.current.variantId,
      active.assetId,
    )
      .then((response) => {
        setGeneration(null);
        if (response.status !== "cancelled") {
          setError("Cancellation requested. Your draft remains unchanged.");
        }
      })
      .catch(() => {
        // Keep the provider attempt addressable so Cancel remains retryable.
        // The original generation poll was fenced above; start a replacement
        // observer until a second cancel click supersedes this token.
        const observerToken = generationTokenRef.current;
        setGeneration(active);
        setError("Kria couldn’t confirm the cancellation. Retry the cancellation. Your draft is unchanged.");
        void (async () => {
          while (generationTokenRef.current === observerToken) {
            await new Promise((resolve) => window.setTimeout(resolve, 2000));
            if (generationTokenRef.current !== observerToken) return;
            try {
              const current = await getOmniAsset(
                optsRef.current.itemId,
                optsRef.current.variantId,
                active.assetId,
              );
              if (generationTokenRef.current !== observerToken) return;
              if (["failed", "cancelled", "ready"].includes(current.status)) {
                setGeneration(null);
                return;
              }
              setGeneration({
                suggestionId: active.suggestionId,
                assetId: active.assetId,
                status: current.status,
                progress: current.progress,
              });
            } catch {
              // Keep the retry control visible; the next poll may recover.
            }
          }
        })();
      });
  }, [generation]);

  const restoreOriginalTiming = useCallback(() => {
    if (!optsRef.current.canRestoreOriginalTiming || serverRendering) return;
    if (optsRef.current.serverOperationsEnabled === false) {
      setError("Save your draft before restoring the original timing.");
      return;
    }
    const revision = optsRef.current.speechCutRevision;
    if (!revision) return;
    setServerDispatchPending(true);
    setError(null);
    void restoreOriginalSpeechTiming(
      optsRef.current.itemId,
      optsRef.current.variantId,
      revision,
    )
      .then(async (response) => {
        pendingServerOperationIdRef.current = String(
          response.request.operation_id || "",
        );
        await optsRef.current.onServerRenderStarted?.();
      })
      .catch(() => {
        pendingServerOperationIdRef.current = "";
        setServerDispatchPending(false);
        setError("Kria couldn’t restore the original timing. Retry the change. The current video is unchanged.");
        refreshReview();
      });
  }, [refreshReview, serverRendering]);

  return useMemo(
    () => ({
      suggestions,
      appliedReceipts,
      reviewed,
      loading: loading || reviewQueued,
      error,
      unavailable,
      reviewBlocked: Boolean(
        reviewBlockedUntil && Date.parse(reviewBlockedUntil) > Date.now()
      ),
      modelUsed,
      omniMaxCostPerSecondUsd,
      omniDispatchPending,
      generation,
      serverRendering,
      // Explicit refresh stays armed through snapshot-hydration aborts, so the
      // visible button always produces a replacement review.
      refresh: refreshReview,
      accept,
      dismiss,
      revealApplied,
      cancelGeneration,
      restoreOriginalTiming,
      canRestoreOriginalTiming,
    }),
    [
      suggestions,
      appliedReceipts,
      reviewed,
      loading,
      reviewQueued,
      error,
      unavailable,
      reviewBlockedUntil,
      modelUsed,
      omniMaxCostPerSecondUsd,
      omniDispatchPending,
      generation,
      serverRendering,
      refreshReview,
      accept,
      dismiss,
      revealApplied,
      cancelGeneration,
      restoreOriginalTiming,
      canRestoreOriginalTiming,
    ],
  );
}
