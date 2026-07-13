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

function appendRejectionSuffix(reply: string, rejected: string[]): string {
  if (rejected.length === 0) return reply;
  return `${reply}\n\nCouldn't apply: ${rejected.join("; ")}`;
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
      const assistantText = appendRejectionSuffix(
        response.reply,
        outcome.rejected,
      );
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
      setError(
        err instanceof Error
          ? err.message
          : "I couldn't reach Nova just now. Your edit is untouched - try again.",
      );
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
