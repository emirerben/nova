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
  applied?: string[];
  rejected?: string[];
  suggestions?: string[];
}

export interface QueuedCopilotMessage {
  id: string;
  text: string;
}

export interface UseEditCopilotOptions {
  itemId: string;
  variantId: string;
  buildSnapshot: () => CopilotSnapshot;
  applyOps: (ops: CopilotOp[], snapshot: CopilotSnapshot) => ApplyCopilotOpsResult;
  onApplied?: (result: ApplyCopilotOpsResult, response: EditCopilotTurnResponse) => void;
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
  clear: () => void;
  clearRestoredInput: () => void;
}

export function editCopilotStorageKey(variantId: string): string {
  return `nova-edit-copilot-thread:${variantId}`;
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

function readThread(variantId: string): CopilotMessage[] {
  const raw = storage()?.getItem(editCopilotStorageKey(variantId));
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

function writeThread(variantId: string, messages: CopilotMessage[]) {
  storage()?.setItem(
    editCopilotStorageKey(variantId),
    JSON.stringify({ v: 1, messages }),
  );
}

function removeThread(variantId: string) {
  storage()?.removeItem(editCopilotStorageKey(variantId));
}

function summaries(result: ApplyCopilotOpsResult): {
  applied: string[];
  rejected: string[];
} {
  return {
    applied: result.applied.map((chip) => `${chip.label}: ${chip.from} -> ${chip.to}`),
    rejected: result.rejected.map((op) => `${op.label}: ${op.detail}`),
  };
}

function appendRejectionSuffix(reply: string, rejected: string[]): string {
  if (rejected.length === 0) return reply;
  return `${reply}\n\nCouldn't apply: ${rejected.join("; ")}`;
}

export function messagesToCopilotTurns(messages: CopilotMessage[]): EditCopilotTurn[] {
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

export function useEditCopilot(opts: UseEditCopilotOptions): UseEditCopilotResult {
  const [messages, setMessages] = useState<CopilotMessage[]>(() => readThread(opts.variantId));
  const [sending, setSending] = useState(false);
  const [queued, setQueued] = useState<QueuedCopilotMessage | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [restoredInput, setRestoredInput] = useState("");
  const [suggestions, setSuggestions] = useState<string[]>([]);

  const optsRef = useRef(opts);
  const messagesRef = useRef(messages);
  const sendingRef = useRef(false);
  const queuedRef = useRef<QueuedCopilotMessage | null>(null);
  const runTurnRef = useRef<(text: string) => Promise<void>>(async () => {});
  const skipNextPersistRef = useRef(false);

  optsRef.current = opts;
  messagesRef.current = messages;
  sendingRef.current = sending;
  queuedRef.current = queued;

  useEffect(() => {
    const restored = readThread(opts.variantId);
    setMessages(restored);
    messagesRef.current = restored;
    setQueued(null);
    queuedRef.current = null;
    setError(null);
    setRestoredInput("");
  }, [opts.variantId]);

  useEffect(() => {
    if (skipNextPersistRef.current) {
      skipNextPersistRef.current = false;
      return;
    }
    writeThread(opts.variantId, messages);
  }, [messages, opts.variantId]);

  const runTurn = useCallback(async (text: string): Promise<void> => {
    const trimmed = text.trim();
    if (!trimmed) return;

    setSending(true);
    sendingRef.current = true;
    setError(null);
    setRestoredInput("");

    const snapshot = optsRef.current.buildSnapshot();
    const priorTurns = messagesToCopilotTurns(messagesRef.current);
    let succeeded = false;

    try {
      const response = await editCopilotTurn(optsRef.current.itemId, optsRef.current.variantId, {
        message: trimmed,
        turns: priorTurns,
        snapshot,
      });
      const applyResult = response.needs_clarification
        ? { textActions: [], nextSlots: null, applied: [], rejected: [] }
        : optsRef.current.applyOps(response.ops, snapshot);
      optsRef.current.onApplied?.(applyResult, response);
      const outcome = summaries(applyResult);
      const assistantText = appendRejectionSuffix(response.reply, outcome.rejected);
      const nextMessages: CopilotMessage[] = [
        ...messagesRef.current,
        { id: nextMessageId("user"), role: "user", text: trimmed },
        {
          id: nextMessageId("assistant"),
          role: "assistant",
          text: assistantText,
          applied: outcome.applied,
          rejected: outcome.rejected,
          suggestions: response.suggestions,
        },
      ];
      messagesRef.current = nextMessages;
      setMessages(nextMessages);
      setSuggestions(response.suggestions);
      succeeded = true;
    } catch (err) {
      setError(
        err instanceof Error
          ? err.message
          : "I couldn't reach Nova just now. Your edit is untouched - try again.",
      );
      setRestoredInput(trimmed);
    } finally {
      setSending(false);
      sendingRef.current = false;
    }

    const pending = queuedRef.current;
    if (succeeded && pending) {
      queuedRef.current = null;
      setQueued(null);
      await Promise.resolve();
      await runTurnRef.current(pending.text);
    }
  }, []);

  runTurnRef.current = runTurn;

  const send = useCallback(async (text: string) => {
    const trimmed = text.trim();
    if (!trimmed) return;
    if (sendingRef.current) {
      const next = { id: queuedRef.current?.id ?? nextMessageId("queued"), text: trimmed };
      queuedRef.current = next;
      setQueued(next);
      return;
    }
    await runTurn(trimmed);
  }, [runTurn]);

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
    const next = { id: queuedRef.current?.id ?? nextMessageId("queued"), text: trimmed };
    queuedRef.current = next;
    setQueued(next);
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
    removeThread(optsRef.current.variantId);
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
      clear,
      clearRestoredInput,
    ],
  );
}
