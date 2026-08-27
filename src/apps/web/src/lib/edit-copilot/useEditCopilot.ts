"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  editCopilotTurn,
  executeEditCopilotReceipt,
  type EditCopilotTurn,
  type EditCopilotExecutionOutcome,
  type EditCopilotExecutionReceiptBody,
  type EditCopilotTurnResponse,
} from "@/lib/plan-api";
import type { CopilotOp } from "./ops";
import type { ApplyCopilotOpsResult } from "./apply-ops";
import type {
  CopilotRenderStepSummaryItem,
  CopilotSnapshot,
  CopilotSnapshotContext,
} from "./snapshot";
import { COPILOT_RECENT_EDIT_HISTORY_MAX } from "./snapshot";
import { isFeatureUnavailable } from "./availability";

/** Shown once when the API has no copilot route, in place of a dead retry. */
export const COPILOT_UNAVAILABLE_MESSAGE =
  "Kria editing isn’t available right now. Your video is unchanged.";

/** Fallback copy for failures with nothing worth showing the user. */
export const COPILOT_GENERIC_ERROR =
  "Kria couldn’t connect just now. Your edit is unchanged. Retry the request.";

/**
 * Pick what the drawer actually shows.
 *
 * The route's own 404s are human copy worth surfacing verbatim ("Plan item not
 * found", "No render to edit yet", "Variant not found"). Two things are NOT:
 * snake_case sentinels the backend uses as diagnostics (`edit_copilot_failed`
 * from the 502 in routes/_copilot.py) and the `Request failed (NNN)` fallback
 * request() writes when the body carries no `detail` at all — which is exactly
 * what a slowapi 429 does, since it returns `{"error": ...}`, not `{"detail": ...}`.
 * Showing either to a creator is worse than saying nothing useful.
 */
export function copilotErrorMessage(caught: unknown): string {
  if (!(caught instanceof Error) || !caught.message) return COPILOT_GENERIC_ERROR;
  const opaque =
    /^[a-z0-9_]+$/.test(caught.message) ||
    caught.message.startsWith("Request failed (");
  return opaque ? COPILOT_GENERIC_ERROR : caught.message;
}

export type CopilotMessageRole = "user" | "assistant";

export interface CopilotMessage {
  id: string;
  role: CopilotMessageRole;
  text: string;
  pending?: boolean;
  applied?: string[];
  rejected?: string[];
  suggestions?: string[];
  undoVersion?: number;
  /** Chat steps feed (PR4, NEXT_PUBLIC_NOVA_STEPS_FEED_ENABLED): this turn
   *  dispatched a server render (today: set_intro_layout). CopilotDrawer
   *  shows the disclosure + live NovaActivityFeed for these turns instead of
   *  receipt rows, and never renders an Undo affordance (non-undoable
   *  contract — undoVersion is never set alongside this). */
  isRenderTurn?: boolean;
  /** Structured referent captured by a clarification turn (for example,
   * `images`), so a later “all of them” cannot drift to all clips. */
  clarification_context?: Record<string, unknown> | null;
  pending_actions?: Array<Record<string, unknown>>;
}

export interface QueuedCopilotMessage {
  id: string;
  text: string;
}

export interface UseEditCopilotOptions {
  itemId: string;
  variantId: string;
  /** Accepts an optional context the hook threads through on every turn:
   * renderStepSummary (below) and the hook's own message-derived
   * recentEditHistory. The caller's buildSnapshot forwards these into
   * buildCopilotSnapshot's options — see snapshot.ts. */
  buildSnapshot: (context?: CopilotSnapshotContext) => CopilotSnapshot;
  applyOps: (
    ops: CopilotOp[],
    snapshot: CopilotSnapshot,
  ) => ApplyCopilotOpsResult;
  /** Bulk/Director edits are all-or-zero: the hook uses this when present. */
  applyOpsAtomic?: (
    ops: CopilotOp[],
    snapshot: CopilotSnapshot,
  ) => ApplyCopilotOpsResult;
  onApplied?: (
    result: ApplyCopilotOpsResult,
    response: EditCopilotTurnResponse,
    snapshot: CopilotSnapshot,
  ) => { undoVersion?: number; isRenderTurn?: boolean; assistantText?: string } | void;
  /** Called only for a locally applied, undoable turn with durable proposal identity. */
  onReceiptStaged?: (receiptId: string, undoVersion: number) => void;
  /** Last ≤8 humanized render steps for the current job (PR1's status-route
   * `steps` field, once a parallel PR lands it — the caller is responsible
   * for sourcing this, e.g. from a polled job-status hook). Undefined/empty
   * omits the render_step_summary snapshot section entirely. */
  renderStepSummary?: CopilotRenderStepSummaryItem[];
}

export interface UseEditCopilotResult {
  messages: CopilotMessage[];
  sending: boolean;
  queued: QueuedCopilotMessage | null;
  error: string | null;
  /** The API cannot serve copilot turns at all; retrying will not help. */
  unavailable: boolean;
  restoredInput: string;
  suggestions: string[];
  send: (text: string) => Promise<void>;
  cancelQueued: () => void;
  editQueued: (text: string) => void;
  stop: () => void;
  clear: () => void;
  clearRestoredInput: () => void;
}

export function editCopilotStorageKey(
  itemId: string,
  variantId: string,
): string {
  return `nova-edit-copilot-thread:${itemId}:${variantId}`;
}

let messageCounter = 0;

function nextMessageId(prefix: string): string {
  messageCounter += 1;
  return `${prefix}-${Date.now()}-${messageCounter}`;
}

function storage(): Storage | null {
  if (typeof window === "undefined") return null;
  try {
    return window.sessionStorage;
  } catch {
    return null;
  }
}

function readThread(itemId: string, variantId: string): CopilotMessage[] {
  if (!itemId || !variantId) return [];
  const raw = storage()?.getItem(editCopilotStorageKey(itemId, variantId));
  if (!raw) return [];
  try {
    const parsed = JSON.parse(raw) as { v?: number; messages?: unknown };
    if (parsed?.v !== 1 || !Array.isArray(parsed.messages)) return [];
    return parsed.messages.filter(isCopilotMessage);
  } catch {
    return [];
  }
}

function isCopilotMessage(value: unknown): value is CopilotMessage {
  if (!value || typeof value !== "object") return false;
  const msg = value as Partial<CopilotMessage>;
  return (
    typeof msg.id === "string" &&
    (msg.role === "user" || msg.role === "assistant") &&
    typeof msg.text === "string"
  );
}

function writeThread(
  itemId: string,
  variantId: string,
  messages: CopilotMessage[],
) {
  if (!itemId || !variantId) return;
  // undoVersion is meaningless across mounts: the history counter restarts at 0
  // per editor session, so a persisted version could collide with a fresh
  // counter and revive a stale Undo chip against unrelated edits (review F3).
  const persistable = messages
    .filter((message) => !message.pending)
    .map(({ undoVersion: _dropUndo, pending: _dropPending, ...rest }) => rest);
  storage()?.setItem(
    editCopilotStorageKey(itemId, variantId),
    JSON.stringify({ v: 1, messages: persistable }),
  );
}

function removeThread(itemId: string, variantId: string) {
  if (!itemId || !variantId) return;
  storage()?.removeItem(editCopilotStorageKey(itemId, variantId));
}

function summaries(result: ApplyCopilotOpsResult): {
  applied: string[];
  rejected: string[];
} {
  return {
    applied: result.applied.map(
      (chip) =>
        `${chip.label}: ${chip.from}, now ${chip.to}${(chip.count ?? 1) > 1 ? ` (×${chip.count})` : ""}`,
    ),
    rejected: result.rejected.map((op) => `${op.label}: ${op.detail}`),
  };
}

function newClientEventId(): string {
  const randomUUID = globalThis.crypto?.randomUUID;
  if (randomUUID) return randomUUID.call(globalThis.crypto);
  return `copilot-execution-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function executionOutcome(
  response: EditCopilotTurnResponse,
  result: ApplyCopilotOpsResult,
): EditCopilotExecutionOutcome {
  if (response.outcome === "stale") return "stale";
  if (response.outcome === "failed") return "failed";
  if (result.applied.length > 0) return "staged";
  if (result.rejected.length > 0 || response.outcome === "unsupported") return "rejected";
  return "no_effect";
}

function snapshotRevisionHash(snapshot: CopilotSnapshot): string | null {
  return (
    snapshot.guided_revision?.state_hash ||
    (snapshot.guided_revision
      ? `${snapshot.guided_revision.revision_number}:${snapshot.guided_revision.base_generation}`.slice(0, 128)
      : null)
  );
}

function executionReceiptBody(
  response: EditCopilotTurnResponse,
  result: ApplyCopilotOpsResult,
  snapshot: CopilotSnapshot,
): EditCopilotExecutionReceiptBody {
  const beforeRevisionHash = snapshotRevisionHash(snapshot);
  const outcome = executionOutcome(response, result);
  return {
    client_event_id: newClientEventId(),
    outcome,
    rejection_reasons: [
      ...(response.rejection_reasons ?? []).map((reason) => ({
        op: reason.op,
        reason: reason.reason,
        detail: reason.detail,
      })),
      ...result.rejected.map((reason) => ({
        op: reason.op,
        reason: reason.reason,
        detail: reason.detail,
      })),
    ],
    before_revision_hash: beforeRevisionHash,
    // Applied changes are still a local, unsaved draft at this point. Claiming
    // the old server revision as the after hash would be false; the eventual
    // saved artifact can link this receipt once it has a canonical hash.
    after_revision_hash: outcome === "staged" ? null : beforeRevisionHash,
  };
}

/** Best-effort audit delivery. A stable client_event_id is reused across
 * retries so the append-only server endpoint can return the original row.
 * Receipt persistence must never delay or undo a successful local edit. */
export async function reportCopilotExecution(
  itemId: string,
  variantId: string,
  receiptId: string,
  body: EditCopilotExecutionReceiptBody,
  retryDelaysMs: readonly number[] = [0, 250, 1000],
): Promise<boolean> {
  for (let attempt = 0; attempt < retryDelaysMs.length; attempt += 1) {
    if (retryDelaysMs[attempt] > 0) {
      await new Promise((resolve) => setTimeout(resolve, retryDelaysMs[attempt]));
    }
    try {
      await executeEditCopilotReceipt(itemId, variantId, receiptId, body);
      return true;
    } catch {
      // Continue with the same idempotency key. There is deliberately no UI
      // error: the creator's staged edit already succeeded.
    }
  }
  return false;
}

/** One line per applied turn: distinct op labels from the summary chip strings,
 * plus a total edit count — e.g.
 * "Text color, Font size (3 edits)". Mirrors the "+N more" collapse the
 * receipt chips already use in the drawer. Exported so EditorShell can build
 * the same one-line shape for `history_state.last_turn_summary` (PR7)
 * without duplicating the label-collapsing logic. */
export function summarizeAppliedTurn(applied: string[]): string {
  const labels: string[] = [];
  for (const entry of applied) {
    const label = entry.split(":")[0]?.trim();
    if (label && !labels.includes(label)) labels.push(label);
  }
  const shown = labels.slice(0, 3);
  const suffix = labels.length > shown.length ? ", …" : "";
  const count = applied.length;
  return `${shown.join(", ")}${suffix} (${count} edit${count === 1 ? "" : "s"})`;
}

/** Derives recent_edit_history from the hook's own message log — the last
 * ≤6 turns that actually applied a change (rejected-only or non-edit turns
 * are not "edit history"). Purely local; nothing here reaches the network
 * except via the buildSnapshot() context passed on the next turn. */
export function deriveRecentEditHistory(messages: CopilotMessage[]): string[] {
  const summaries: string[] = [];
  for (const message of messages) {
    if (message.role !== "assistant" || message.pending) continue;
    if (!message.applied || message.applied.length === 0) continue;
    summaries.push(summarizeAppliedTurn(message.applied));
  }
  return summaries.slice(-COPILOT_RECENT_EDIT_HISTORY_MAX);
}

export function outcomeAuthoritativeReply({
  modelReply,
  intent,
  needsClarification,
  applied,
  rejected,
  outcome,
  rejectionReasons,
}: {
  modelReply: string;
  intent: string;
  needsClarification: boolean;
  applied: string[];
  rejected: string[];
  outcome?: EditCopilotTurnResponse["outcome"];
  rejectionReasons?: EditCopilotTurnResponse["rejection_reasons"];
}): string {
  if (outcome === "clarification" || (outcome === undefined && needsClarification)) {
    const explanation = modelReply.trim();
    const claimsSuccess =
      /\b(done|stored|changed|updated|applied|edited|trimmed|removed|swapped|punchier)\b/i.test(explanation) &&
      !/\b(already|unchanged|cannot|can't|couldn't|unable|not|no longer)\b/i.test(explanation);
    return claimsSuccess || !explanation
      ? "I need one detail before changing the draft."
      : explanation;
  }
  if (outcome === "unsupported") {
    return rejectionReasons?.find((reason) => reason.detail)?.detail ?? "That kind of edit isn't available for this draft yet.";
  }
  if (outcome === "stale") {
    return "That edit is based on an older draft. Refresh the editor and try again.";
  }
  if (outcome === "failed") {
    return "I couldn't build a valid draft change for that request. Try again.";
  }
  if (intent !== "edit") {
    if (rejected.length === 0) return modelReply;
    return `${modelReply}\n\nCouldn't apply: ${rejected.join("; ")}`;
  }
  if (outcome === "no_effect" || applied.length === 0) {
    const truth = rejected.length > 0
      ? `No change made. ${rejected.join("; ")}`
      : "No change needed — the draft already reflects that request.";
    const explanation = modelReply.trim();
    const claimsSuccess =
      /\b(done|stored|changed|updated|applied|edited|trimmed|removed|swapped|punchier)\b/i.test(
        explanation,
      ) &&
      !/\b(already|unchanged|cannot|can't|couldn't|unable|not|no longer)\b/i.test(explanation);
    return rejected.length === 0 && explanation && explanation !== truth && !claimsSuccess
      ? explanation
      : truth;
  }
  const appliedReceipt = `Staged: ${applied.join("; ")}. Save to render the new video.`;
  return rejected.length > 0
    ? `${appliedReceipt}\n\nCouldn't apply: ${rejected.join("; ")}`
    : appliedReceipt;
}

export function messagesToCopilotTurns(
  messages: CopilotMessage[],
): EditCopilotTurn[] {
  return messages.slice(-12).map((message) => ({
    role: message.role,
    content: message.text,
    ...(message.role === "assistant" && message.applied?.length
      ? { applied: message.applied }
      : {}),
    ...(message.role === "assistant" && message.rejected?.length
      ? { rejected: message.rejected }
      : {}),
    ...(message.clarification_context ? { clarification_context: message.clarification_context } : {}),
    ...(message.pending_actions?.length ? { pending_actions: message.pending_actions } : {}),
  }));
}

export function useEditCopilot(
  opts: UseEditCopilotOptions,
): UseEditCopilotResult {
  const [messages, setMessages] = useState<CopilotMessage[]>(() =>
    readThread(opts.itemId, opts.variantId),
  );
  const [sending, setSending] = useState(false);
  const [queued, setQueued] = useState<QueuedCopilotMessage | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [unavailable, setUnavailable] = useState(false);
  const [restoredInput, setRestoredInput] = useState("");
  const [suggestions, setSuggestions] = useState<string[]>([]);

  const optsRef = useRef(opts);
  const messagesRef = useRef(messages);
  const sendingRef = useRef(false);
  const queuedRef = useRef<QueuedCopilotMessage | null>(null);
  const activeTurnRef = useRef<{
    id: number;
    text: string;
    userMessageId: string;
  } | null>(null);
  const abandonedTurnsRef = useRef(new Set<number>());
  const unavailableRef = useRef(false);
  const turnIdRef = useRef(0);
  const runTurnRef = useRef<(text: string) => Promise<void>>(async () => {});
  const skipNextPersistRef = useRef(false);

  optsRef.current = opts;
  messagesRef.current = messages;
  sendingRef.current = sending;
  queuedRef.current = queued;

  useEffect(() => {
    const restored = readThread(opts.itemId, opts.variantId);
    // Prevent the A->B key-change commit from persisting A's still-rendered
    // messages into B's bucket before the restored B thread lands.
    skipNextPersistRef.current = true;
    setMessages(restored);
    messagesRef.current = restored;
    setQueued(null);
    queuedRef.current = null;
    setError(null);
    // Availability is a property of the API, not of one item — but re-probing
    // once per item is cheap and keeps a mid-session deploy from leaving the
    // drawer latched off.
    setUnavailable(false);
    unavailableRef.current = false;
    setRestoredInput("");
  }, [opts.itemId, opts.variantId]);

  useEffect(() => {
    if (skipNextPersistRef.current) {
      skipNextPersistRef.current = false;
      return;
    }
    writeThread(opts.itemId, opts.variantId, messages);
  }, [messages, opts.itemId, opts.variantId]);

  const runTurn = useCallback(async (text: string): Promise<void> => {
    const trimmed = text.trim();
    if (!trimmed) return;
    if (!optsRef.current.itemId || !optsRef.current.variantId) return;
    // Latched off: the route 404s for this API build, so a turn would only
    // re-render the same failure and drop the user's text.
    if (unavailableRef.current) return;

    setSending(true);
    sendingRef.current = true;
    turnIdRef.current += 1;
    const turnId = turnIdRef.current;
    setError(null);
    setRestoredInput("");

    const priorTurns = messagesToCopilotTurns(messagesRef.current);
    const userMessageId = nextMessageId("user");
    const optimisticUserMessage: CopilotMessage = {
      id: userMessageId,
      role: "user",
      text: trimmed,
      pending: true,
    };
    const optimisticMessages = [...messagesRef.current, optimisticUserMessage];
    messagesRef.current = optimisticMessages;
    setMessages(optimisticMessages);
    activeTurnRef.current = { id: turnId, text: trimmed, userMessageId };

    // messagesRef.current at this point already includes the optimistic
    // pending user message set above — deriveRecentEditHistory only reads
    // assistant turns, so the in-flight turn itself is naturally excluded.
    const snapshot = optsRef.current.buildSnapshot({
      renderStepSummary: optsRef.current.renderStepSummary,
      recentEditHistory: deriveRecentEditHistory(messagesRef.current),
    });
    let succeeded = false;
    let receiptResponse: EditCopilotTurnResponse | null = null;
    let receiptReported = false;

    try {
      const response = await editCopilotTurn(
        optsRef.current.itemId,
        optsRef.current.variantId,
        {
          message: trimmed,
          turns: priorTurns,
          snapshot,
        },
      );
      receiptResponse = response;
      const shouldClarify = response.outcome
        ? response.outcome === "clarification"
        : response.needs_clarification;
      const applyResult = shouldClarify
        ? { textActions: [], nextSlots: null, applied: [], rejected: [] }
      : (optsRef.current.applyOpsAtomic ?? optsRef.current.applyOps)(response.ops, snapshot);
      if (abandonedTurnsRef.current.has(turnId)) {
        abandonedTurnsRef.current.delete(turnId);
        return;
      }
      const applyMeta = optsRef.current.onApplied?.(
        applyResult,
        response,
        snapshot,
      );
      const outcome = summaries(applyResult);
      if (response.receipt_id) {
        receiptReported = true;
        void reportCopilotExecution(
          optsRef.current.itemId,
          optsRef.current.variantId,
          response.receipt_id,
          executionReceiptBody(response, applyResult, snapshot),
        );
        if (applyMeta?.undoVersion != null && (response.outcome === "proposed" || response.outcome === "staged" || response.outcome === "applied")) {
          optsRef.current.onReceiptStaged?.(response.receipt_id, applyMeta.undoVersion);
        }
      }
      const assistantText =
        applyMeta?.assistantText ??
        outcomeAuthoritativeReply({
          modelReply: response.reply,
          intent: response.intent,
          needsClarification: response.needs_clarification,
          applied: outcome.applied,
          rejected: outcome.rejected,
          outcome: response.outcome,
          rejectionReasons: response.rejection_reasons,
        });
      const nextMessages: CopilotMessage[] = [
        ...messagesRef.current.map((message) => {
          if (message.id !== userMessageId) return message;
          const { pending: _dropPending, ...rest } = message;
          return rest;
        }),
        {
          id: nextMessageId("assistant"),
          role: "assistant",
          text: assistantText,
          applied: outcome.applied,
          rejected: outcome.rejected,
          suggestions: response.suggestions,
          undoVersion: applyMeta?.undoVersion,
          isRenderTurn: applyMeta?.isRenderTurn,
          clarification_context: response.clarification_context ?? null,
          pending_actions: response.pending_actions ?? [],
        },
      ];
      messagesRef.current = nextMessages;
      setMessages(nextMessages);
      setSuggestions(response.suggestions);
      succeeded = true;
    } catch (err) {
      if (receiptResponse?.receipt_id && !receiptReported) {
        const beforeRevisionHash = snapshotRevisionHash(snapshot);
        receiptReported = true;
        void reportCopilotExecution(
          optsRef.current.itemId,
          optsRef.current.variantId,
          receiptResponse.receipt_id,
          {
            client_event_id: newClientEventId(),
            outcome: "failed",
            rejection_reasons: [
              ...(receiptResponse.rejection_reasons ?? []),
              {
                op: "client_apply",
                reason: "failed",
                detail: "The local editor could not execute the proposed operations.",
              },
            ],
            before_revision_hash: beforeRevisionHash,
            after_revision_hash: beforeRevisionHash,
          },
        );
      }
      if (abandonedTurnsRef.current.has(turnId)) {
        abandonedTurnsRef.current.delete(turnId);
        return;
      }
      const nextMessages = messagesRef.current.filter(
        (message) => message.id !== userMessageId,
      );
      messagesRef.current = nextMessages;
      setMessages(nextMessages);
      if (isFeatureUnavailable(err)) {
        unavailableRef.current = true;
        setUnavailable(true);
        setError(COPILOT_UNAVAILABLE_MESSAGE);
        // The composer is about to go inert, so a queued follow-up can never be
        // sent. Drop it instead of leaving a chip the user can only cancel.
        queuedRef.current = null;
        setQueued(null);
      } else {
        setError(copilotErrorMessage(err));
      }
      setRestoredInput(trimmed);
    } finally {
      if (activeTurnRef.current?.id === turnId) {
        activeTurnRef.current = null;
        setSending(false);
        sendingRef.current = false;
      }
    }

    const pending = queuedRef.current;
    if (succeeded && pending) {
      queuedRef.current = null;
      setQueued(null);
      // Fire via effect, NOT inline: the applied turn's state updates (bars,
      // slots) have not committed yet, so an inline runTurn would build the
      // queued turn's snapshot from the PRE-apply draft and every op touching
      // a field the prior turn changed would fingerprint-fail (review F1).
      // The effect below runs after React commits, when buildSnapshot's
      // re-created closure sees the post-apply draft.
      setFireQueued(pending);
    }
  }, []);

  runTurnRef.current = runTurn;

  const [fireQueued, setFireQueued] = useState<QueuedCopilotMessage | null>(
    null,
  );
  useEffect(() => {
    if (!fireQueued) return;
    setFireQueued(null);
    void runTurnRef.current(fireQueued.text);
  }, [fireQueued]);

  const send = useCallback(
    async (text: string) => {
      const trimmed = text.trim();
      if (!trimmed) return;
      if (!optsRef.current.itemId || !optsRef.current.variantId) return;
      if (sendingRef.current) {
        const next = {
          id: queuedRef.current?.id ?? nextMessageId("queued"),
          text: trimmed,
        };
        queuedRef.current = next;
        setQueued(next);
        return;
      }
      await runTurn(trimmed);
    },
    [runTurn],
  );

  const cancelQueued = useCallback(() => {
    queuedRef.current = null;
    setQueued(null);
  }, []);

  const editQueued = useCallback((text: string) => {
    const trimmed = text.trim();
    if (!trimmed) {
      queuedRef.current = null;
      setQueued(null);
      return;
    }
    const next = {
      id: queuedRef.current?.id ?? nextMessageId("queued"),
      text: trimmed,
    };
    queuedRef.current = next;
    setQueued(next);
  }, []);

  const stop = useCallback(() => {
    const active = activeTurnRef.current;
    if (!active) return;
    abandonedTurnsRef.current.add(active.id);
    activeTurnRef.current = null;
    const nextMessages = messagesRef.current.filter(
      (message) => message.id !== active.userMessageId,
    );
    messagesRef.current = nextMessages;
    setMessages(nextMessages);
    setRestoredInput(active.text);
    setSending(false);
    sendingRef.current = false;
    queuedRef.current = null;
    setQueued(null);
  }, []);

  const clear = useCallback(() => {
    messagesRef.current = [];
    setMessages([]);
    queuedRef.current = null;
    setQueued(null);
    setError(null);
    setRestoredInput("");
    setSuggestions([]);
    skipNextPersistRef.current = true;
    removeThread(optsRef.current.itemId, optsRef.current.variantId);
  }, []);

  const clearRestoredInput = useCallback(() => {
    setRestoredInput("");
  }, []);

  return useMemo(
    () => ({
      messages,
      sending,
      queued,
      error,
      unavailable,
      restoredInput,
      suggestions,
      send,
      cancelQueued,
      editQueued,
      stop,
      clear,
      clearRestoredInput,
    }),
    [
      messages,
      sending,
      queued,
      error,
      unavailable,
      restoredInput,
      suggestions,
      send,
      cancelQueued,
      editQueued,
      stop,
      clear,
      clearRestoredInput,
    ],
  );
}
