"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  editCopilotTurn,
  type EditCopilotTurn,
  type EditCopilotTurnResponse,
} from "@/lib/plan-api";
import type { CopilotOp } from "./ops";
import type { ApplyCopilotOpsResult } from "./apply-ops";
import type { CopilotSnapshot } from "./snapshot";
import { isFeatureUnavailable } from "./availability";

/** Shown once when the API has no copilot route, in place of a dead retry. */
export const COPILOT_UNAVAILABLE_MESSAGE =
  "Nova editing isn't enabled on this server yet.";

/** Fallback copy for failures with nothing worth showing the user. */
export const COPILOT_GENERIC_ERROR =
  "I couldn't reach Nova just now. Your edit is untouched — try again.";

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
}

export interface QueuedCopilotMessage {
  id: string;
  text: string;
}

export interface UseEditCopilotOptions {
  itemId: string;
  variantId: string;
  buildSnapshot: () => CopilotSnapshot;
  applyOps: (
    ops: CopilotOp[],
    snapshot: CopilotSnapshot,
  ) => ApplyCopilotOpsResult;
  onApplied?: (
    result: ApplyCopilotOpsResult,
    response: EditCopilotTurnResponse,
    snapshot: CopilotSnapshot,
  ) => { undoVersion?: number } | void;
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
        `${chip.label}: ${chip.from} → ${chip.to}${(chip.count ?? 1) > 1 ? ` (×${chip.count})` : ""}`,
    ),
    rejected: result.rejected.map((op) => `${op.label}: ${op.detail}`),
  };
}

export function outcomeAuthoritativeReply({
  modelReply,
  intent,
  needsClarification,
  applied,
  rejected,
}: {
  modelReply: string;
  intent: string;
  needsClarification: boolean;
  applied: string[];
  rejected: string[];
}): string {
  if (intent !== "edit" || needsClarification) {
    if (rejected.length === 0) return modelReply;
    return `${modelReply}\n\nCouldn't apply: ${rejected.join("; ")}`;
  }
  if (applied.length === 0) {
    return rejected.length > 0
      ? `I didn't change the draft. ${rejected.join("; ")}`
      : "I didn't change the draft.";
  }
  const appliedReceipt = `Applied: ${applied.join("; ")}.`;
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

    const snapshot = optsRef.current.buildSnapshot();
    let succeeded = false;

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
      const applyResult = response.needs_clarification
        ? { textActions: [], nextSlots: null, applied: [], rejected: [] }
        : optsRef.current.applyOps(response.ops, snapshot);
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
      const assistantText = outcomeAuthoritativeReply({
        modelReply: response.reply,
        intent: response.intent,
        needsClarification: response.needs_clarification,
        applied: outcome.applied,
        rejected: outcome.rejected,
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
        },
      ];
      messagesRef.current = nextMessages;
      setMessages(nextMessages);
      setSuggestions(response.suggestions);
      succeeded = true;
    } catch (err) {
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
