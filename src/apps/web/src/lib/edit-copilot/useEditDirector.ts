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
import type { CopilotSnapshot } from "./snapshot";
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
  loading: boolean;
  error: string | null;
  /** The API cannot serve director reviews at all; polling has been stopped. */
  unavailable: boolean;
  modelUsed: string;
  fallbackReason: string | null;
  generation: DirectorGenerationState | null;
  serverRendering: boolean;
  refresh: () => void;
  accept: (suggestion: EditorSuggestion) => void;
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
  "Nova's proactive review isn't enabled on this server yet.";
export const DIRECTOR_CAPABILITY_MISMATCH_MESSAGE =
  "Nova's proactive review is updating. Try again shortly.";
const DIRECTOR_REVIEW_DEBOUNCE_MS = 1200;
const MAX_APPLIED_RECEIPTS = 8;

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
  if (status === null) {
    return "Nova couldn't reach the review service. Check your connection; your draft is unchanged.";
  }
  if (
    status === 502 ||
    code.includes("director_failed") ||
    code.includes("model") ||
    stage.includes("model")
  ) {
    return "Nova's review model couldn't complete this pass. Your draft is unchanged.";
  }
  if (status >= 500) {
    return "Nova's review service had a server error. Your draft is unchanged.";
  }
  return "Nova couldn't review this draft. Your draft is unchanged.";
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
  return "Nova couldn't generate that clip. Your draft was not changed.";
}

export function directorSnapshotRevision(snapshot: CopilotSnapshot): string {
  const value = JSON.stringify(snapshot);
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
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [unavailable, setUnavailable] = useState(false);
  const [modelUsed, setModelUsed] = useState("");
  const [fallbackReason, setFallbackReason] = useState<string | null>(null);
  const [generation, setGeneration] = useState<DirectorGenerationState | null>(null);
  const [serverDispatchPending, setServerDispatchPending] = useState(false);
  const [refreshKey, setRefreshKey] = useState(0);
  const requestIdRef = useRef(0);
  const sourceSnapshotRef = useRef<CopilotSnapshot | null>(null);
  const sourceRevisionRef = useRef("");
  const optsRef = useRef(opts);
  const generationTokenRef = useRef(0);
  const receiptSequenceRef = useRef(0);
  const lastServerReceiptIdRef = useRef("");
  const lastServerFailureIdRef = useRef("");
  const pendingServerOperationIdRef = useRef("");
  const pendingServerSuggestionRef = useRef<EditorSuggestion | null>(null);
  // Stays armed across abort/restart cycles until a replacement review either
  // lands or fails. A one-render latch loses refreshes during async hydration.
  const forceRefreshRef = useRef(false);
  optsRef.current = opts;
  // Unlike history.version, this includes async editor hydration (asset pool,
  // captions, capabilities, overlays). A review started against a partial
  // snapshot is cancelled and restarted once that real input settles.
  const directorEnabled = opts.enabled;
  const canRestoreOriginalTiming = opts.canRestoreOriginalTiming === true;
  const buildSnapshot = opts.buildSnapshot;
  const currentSnapshotRevision = useMemo(
    () => directorEnabled ? directorSnapshotRevision(buildSnapshot()) : "",
    [directorEnabled, buildSnapshot],
  );

  useEffect(() => {
    suggestionsRef.current = [];
    setSuggestions([]);
    setAppliedReceipts([]);
    setError(null);
    setUnavailable(false);
    setModelUsed("");
    setFallbackReason(null);
    sourceRevisionRef.current = "";
    forceRefreshRef.current = false;
    generationTokenRef.current += 1;
    setGeneration(null);
    setServerDispatchPending(false);
    lastServerReceiptIdRef.current = "";
    lastServerFailureIdRef.current = "";
    pendingServerOperationIdRef.current = "";
    pendingServerSuggestionRef.current = null;
  }, [opts.itemId, opts.variantId]);

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
    // This effect re-runs on every complete snapshot revision. Without this guard a
    // flag-off API re-fires a doomed request after each keystroke-sized edit
    // and repaints the failure, which is what put an error in the drawer
    // before the user had typed anything.
    if (unavailable) return;
    const forceRefresh = forceRefreshRef.current;
    // Keep a returned review stable while the user works through it. Director
    // suggestions are server-validated into sequentially compatible edit
    // domains, and applyOpsAtomic rejects a card if its own target changed.
    if (suggestionsRef.current.length > 0 && !forceRefresh) return;
    if (sourceRevisionRef.current === currentSnapshotRevision && !forceRefresh) return;
    const controller = new AbortController();
    let activeRequestId = 0;
    const timer = window.setTimeout(() => {
      const snapshot = optsRef.current.buildSnapshot();
      if (snapshot.allowed_op_families.length === 0) {
        forceRefreshRef.current = false;
        suggestionsRef.current = [];
        setSuggestions([]);
        return;
      }
      const revision = directorSnapshotRevision(snapshot);
      requestIdRef.current += 1;
      const requestId = requestIdRef.current;
      activeRequestId = requestId;
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
          const currentRevision = directorSnapshotRevision(optsRef.current.buildSnapshot());
          if (
            response.snapshot_revision !== revision ||
            currentRevision !== revision
          ) {
            return;
          }
          sourceSnapshotRef.current = snapshot;
          sourceRevisionRef.current = revision;
          const nextSuggestions = optsRef.current.omniEnabled
            ? response.suggestions
            : response.suggestions.filter((item) => item.apply_mode !== "omni_async");
          suggestionsRef.current = nextSuggestions;
          setSuggestions(nextSuggestions);
          forceRefreshRef.current = false;
          setModelUsed(response.model_used);
          setFallbackReason(response.fallback_reason ?? null);
          if (nextSuggestions.length === 0 && response.suggestions.length > 0) {
            setError(DIRECTOR_CAPABILITY_MISMATCH_MESSAGE);
          }
        })
        .catch((caught) => {
          if (requestId !== requestIdRef.current || controller.signal.aborted) return;
          if (directorErrorStatus(caught) === 409) {
            // The server evaluated a newer editor revision. Rebuild the
            // snapshot and retry without painting a failure over the draft.
            forceRefreshRef.current = true;
            sourceRevisionRef.current = "";
            setError(null);
            setRefreshKey((value) => value + 1);
            return;
          }
          forceRefreshRef.current = false;
          if (isFeatureUnavailable(caught)) setUnavailable(true);
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
    currentSnapshotRevision,
    refreshKey,
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
    forceRefreshRef.current = true;
    setUnavailable(false);
    setRefreshKey((value) => value + 1);
  }, []);

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
    setError("Nova couldn't complete that timing change. The current video is unchanged.");
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
        setError("Nova couldn't confirm that change. Check the preview or Undo before retrying.");
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
    (suggestion: EditorSuggestion) => {
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
          setError("This cut is stale. Nova is refreshing the review.");
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
            setError("Nova couldn't apply that cut. The current video is unchanged.");
            refreshReview();
          });
        return;
      }
      if (suggestion.apply_mode === "omni_async") {
        if (!suggestion.omni || generation) return;
        const source = sourceSnapshotRef.current;
        const sourceRevision = sourceRevisionRef.current;
        if (!source || directorSnapshotRevision(optsRef.current.buildSnapshot()) !== sourceRevision) {
          setError("The draft changed. Nova is refreshing this suggestion.");
          refreshReview();
          return;
        }
        generationTokenRef.current += 1;
        const token = generationTokenRef.current;
        const itemId = optsRef.current.itemId;
        const variantId = optsRef.current.variantId;
        let startedAssetId = "";
        const identityIsCurrent = () =>
          token === generationTokenRef.current &&
          optsRef.current.itemId === itemId &&
          optsRef.current.variantId === variantId;
        const contextIsCurrent = () =>
          identityIsCurrent() &&
          directorSnapshotRevision(optsRef.current.buildSnapshot()) === sourceRevision;
        const abandonAsset = (assetId = startedAssetId) => {
          if (assetId) {
            void cancelOmniAsset(itemId, variantId, assetId).catch(() => {});
          }
          if (identityIsCurrent()) {
            setGeneration(null);
            setError("The generated clip is ready, but the draft changed, so Nova did not insert it.");
          }
        };
        setError(null);
        void startOmniAsset(
          itemId,
          variantId,
          {
            suggestion_id: suggestion.id,
            draft_revision: sourceRevision,
            ...suggestion.omni,
          },
        )
          .then(async (started) => {
            startedAssetId = started.asset_id;
            if (!contextIsCurrent()) {
              abandonAsset(started.asset_id);
              return;
            }
            setGeneration({
              suggestionId: suggestion.id,
              assetId: started.asset_id,
              status: started.status,
              progress: started.progress,
            });
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
              setError("The generated clip is ready, but Nova couldn't add it to this draft.");
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
            setGeneration(null);
            setError("Nova couldn't start generated video just now. Your draft is unchanged.");
          });
        return;
      }
      const source = sourceSnapshotRef.current;
      if (!source) {
        setError("The draft changed. Nova is refreshing this suggestion.");
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
    [completeAcceptance, generation, refreshReview, removeSuggestion, serverRendering],
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
        setGeneration(null);
        setError("Nova couldn't confirm cancellation. Your draft remains unchanged.");
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
        setError("Nova couldn't restore the original timing. The current video is unchanged.");
        refreshReview();
      });
  }, [refreshReview, serverRendering]);

  return useMemo(
    () => ({
      suggestions,
      appliedReceipts,
      loading,
      error,
      unavailable,
      modelUsed,
      fallbackReason,
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
      loading,
      error,
      unavailable,
      modelUsed,
      fallbackReason,
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
