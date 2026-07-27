"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  cancelOmniAsset,
  claimOmniAsset,
  editDirectorFeedback,
  editDirectorSuggestions,
  getOmniAsset,
  startOmniAsset,
  type EditorSuggestion,
  type OmniAssetResponse,
} from "@/lib/plan-api";
import type { ApplyCopilotOpsResult } from "./apply-ops";
import type { CopilotSnapshot } from "./snapshot";
import { isFeatureUnavailable } from "./availability";

export interface UseEditDirectorOptions {
  enabled: boolean;
  omniEnabled: boolean;
  itemId: string;
  variantId: string;
  materialRevision: number;
  buildSnapshot: () => CopilotSnapshot;
  applyOpsAtomic: (
    ops: EditorSuggestion["ops"],
    snapshot: CopilotSnapshot,
  ) => ApplyCopilotOpsResult;
  onApplied: (result: ApplyCopilotOpsResult) => { undoVersion?: number } | void;
  onGeneratedAssetReady?: () => void | Promise<void>;
}

export interface DirectorGenerationState {
  suggestionId: string;
  assetId: string;
  status: OmniAssetResponse["status"];
  progress: number;
}

export interface UseEditDirectorResult {
  suggestions: EditorSuggestion[];
  loading: boolean;
  error: string | null;
  /** The API cannot serve director reviews at all; polling has been stopped. */
  unavailable: boolean;
  modelUsed: string;
  fallbackReason: string | null;
  generation: DirectorGenerationState | null;
  refresh: () => void;
  accept: (suggestion: EditorSuggestion) => void;
  dismiss: (suggestion: EditorSuggestion) => void;
  cancelGeneration: () => void;
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

function friendlyDirectorError(caught: unknown): string {
  if (caught instanceof DOMException && caught.name === "AbortError") return "";
  if (isFeatureUnavailable(caught)) return DIRECTOR_UNAVAILABLE_MESSAGE;
  return "Nova couldn't review this draft just now. Your edit is unchanged.";
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
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [unavailable, setUnavailable] = useState(false);
  const [modelUsed, setModelUsed] = useState("");
  const [fallbackReason, setFallbackReason] = useState<string | null>(null);
  const [generation, setGeneration] = useState<DirectorGenerationState | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);
  const requestIdRef = useRef(0);
  const sourceSnapshotRef = useRef<CopilotSnapshot | null>(null);
  const sourceRevisionRef = useRef("");
  const firstLoadRef = useRef(true);
  const optsRef = useRef(opts);
  const generationTokenRef = useRef(0);
  optsRef.current = opts;

  useEffect(() => {
    setSuggestions([]);
    setError(null);
    setUnavailable(false);
    setModelUsed("");
    setFallbackReason(null);
    firstLoadRef.current = true;
    generationTokenRef.current += 1;
    setGeneration(null);
  }, [opts.itemId, opts.variantId]);

  useEffect(() => () => {
    generationTokenRef.current += 1;
  }, []);

  useEffect(() => {
    if (!opts.enabled || !opts.itemId || !opts.variantId) return;
    // This effect re-runs on every material revision. Without this guard a
    // flag-off API re-fires a doomed request after each keystroke-sized edit
    // and repaints the failure, which is what put an error in the drawer
    // before the user had typed anything.
    if (unavailable) return;
    const controller = new AbortController();
    const delay = firstLoadRef.current ? 250 : 1200;
    const timer = window.setTimeout(() => {
      firstLoadRef.current = false;
      const snapshot = optsRef.current.buildSnapshot();
      if (snapshot.allowed_op_families.length === 0) {
        setSuggestions([]);
        return;
      }
      const revision = directorSnapshotRevision(snapshot);
      requestIdRef.current += 1;
      const requestId = requestIdRef.current;
      setLoading(true);
      setError(null);
      void editDirectorSuggestions(optsRef.current.itemId, optsRef.current.variantId, {
        snapshot,
        snapshot_revision: revision,
        dismissed_suggestion_ids: readDismissed(
          optsRef.current.itemId,
          optsRef.current.variantId,
        ),
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
          setSuggestions(
            optsRef.current.omniEnabled
              ? response.suggestions
              : response.suggestions.filter((item) => item.apply_mode !== "omni_async"),
          );
          setModelUsed(response.model_used);
          setFallbackReason(response.fallback_reason ?? null);
        })
        .catch((caught) => {
          if (requestId !== requestIdRef.current || controller.signal.aborted) return;
          if (isFeatureUnavailable(caught)) setUnavailable(true);
          setError(friendlyDirectorError(caught));
        })
        .finally(() => {
          if (requestId === requestIdRef.current) setLoading(false);
        });
    }, delay);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
      requestIdRef.current += 1;
    };
  }, [
    opts.enabled,
    opts.itemId,
    opts.variantId,
    opts.materialRevision,
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

  const dismiss = useCallback(
    (suggestion: EditorSuggestion) => {
      setSuggestions((current) => current.filter((item) => item.id !== suggestion.id));
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
    [feedback],
  );

  const accept = useCallback(
    (suggestion: EditorSuggestion) => {
      if (suggestion.apply_mode === "omni_async") {
        if (!suggestion.omni || generation) return;
        const source = sourceSnapshotRef.current;
        const sourceRevision = sourceRevisionRef.current;
        if (!source || directorSnapshotRevision(optsRef.current.buildSnapshot()) !== sourceRevision) {
          setError("The draft changed. Nova is refreshing this suggestion.");
          setRefreshKey((value) => value + 1);
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
            optsRef.current.onApplied(result);
            try {
              await optsRef.current.onGeneratedAssetReady?.();
            } catch {
              setError("The generated clip was added, but its preview could not refresh. Save or Undo still work.");
            }
            setSuggestions((items) => items.filter((item) => item.id !== suggestion.id));
            feedback(suggestion, "accepted");
          })
          .catch(() => {
            if (token !== generationTokenRef.current) return;
            setGeneration(null);
            setError("Nova couldn't start generated video just now. Your draft is unchanged.");
          });
        return;
      }
      const source = sourceSnapshotRef.current;
      const currentRevision = directorSnapshotRevision(optsRef.current.buildSnapshot());
      if (!source || currentRevision !== sourceRevisionRef.current) {
        setError("The draft changed. Nova is refreshing this suggestion.");
        setRefreshKey((value) => value + 1);
        return;
      }
      const result = optsRef.current.applyOpsAtomic(suggestion.ops, source);
      if (result.rejected.length > 0 || result.applied.length === 0) {
        setError(
          result.rejected[0]?.detail ??
            "That suggestion no longer fits the current draft.",
        );
        setRefreshKey((value) => value + 1);
        return;
      }
      optsRef.current.onApplied(result);
      setSuggestions((current) => current.filter((item) => item.id !== suggestion.id));
      feedback(suggestion, "accepted");
    },
    [feedback, generation],
  );

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

  return useMemo(
    () => ({
      suggestions,
      loading,
      error,
      unavailable,
      modelUsed,
      fallbackReason,
      generation,
      // Clearing the latch keeps the visible Refresh button honest: the guard
      // exists to stop AUTOMATIC re-firing on every material revision, not to
      // veto one deliberate click. Also lets a mid-session API deploy recover
      // without reopening the editor.
      refresh: () => {
        setUnavailable(false);
        setRefreshKey((value) => value + 1);
      },
      accept,
      dismiss,
      cancelGeneration,
    }),
    [
      suggestions,
      loading,
      error,
      unavailable,
      modelUsed,
      fallbackReason,
      generation,
      accept,
      dismiss,
      cancelGeneration,
    ],
  );
}
