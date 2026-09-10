"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { CreationThread, EditorConversationBatch } from "@/lib/creation-thread-api";
import type { CopilotMessage } from "@/lib/edit-copilot/useEditCopilot";
import type { EditorChatState } from "./protocol";

interface QueuedEvent {
  batch: EditorConversationBatch;
  threadId: string;
}

/** Persistence retries only append receipts; they never replay editor commands. */
export function useEditorConversation(thread: CreationThread | null, editor: EditorChatState | null,
  persist: (threadId: string, batch: EditorConversationBatch) => Promise<unknown>) {
  const [error, setError] = useState<string | null>(null);
  const [extra, setExtra] = useState<Array<CopilotMessage & { threadId: string }>>([]);
  const pending = useRef(new Map<string, QueuedEvent>());
  const seen = useRef(new Set<string>());
  const inFlight = useRef(false);
  const retryAfter = useRef(0);
  const [outbox, setOutbox] = useState<QueuedEvent[]>([]);
  const latest = useRef({ thread, editor, persist });
  latest.current = { thread, editor, persist };
  const currentThreadId = useRef(thread?.id);
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);
  useEffect(() => {
    if (currentThreadId.current !== thread?.id) {
      currentThreadId.current = thread?.id;
      setExtra([]);
      setError(null);
      retryAfter.current = 0;
    }
  }, [thread?.id]);

  const saveOutbox = useCallback(() => {
    setOutbox([...pending.current.values()]);
    const ids = new Set([...pending.current.values()].map((event) => event.threadId));
    if (latest.current.thread) ids.add(latest.current.thread.id);
    for (const id of ids) {
      try {
        sessionStorage.setItem(`kria:editor-outbox:${id}`, JSON.stringify(
          [...pending.current.values()].filter((event) => event.threadId === id),
        ));
      } catch { /* The in-memory retry remains usable when browser storage is full. */ }
    }
  }, []);

  useEffect(() => {
    if (!thread) return;
    try {
      const restored: QueuedEvent[] = JSON.parse(sessionStorage.getItem(`kria:editor-outbox:${thread.id}`) ?? "[]");
      for (const event of restored) {
        if (event.threadId !== thread.id || event.batch?.item_id !== thread.active_plan_item_id || !Array.isArray(event.batch.messages)) continue;
        for (const message of event.batch.messages) {
          if (typeof message.id !== "string" || typeof message.text !== "string") continue;
          const key = `${thread.id}:${message.id}`;
          seen.current.add(key);
          if (!thread.events.some((row) => row.payload?.editor_message_id === message.id)) pending.current.set(key, event);
        }
      }
      saveOutbox();
    } catch { /* Ignore malformed recovery data. */ }
  // Reload recovery is scoped to the selected conversation, not each server poll.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [thread?.id, saveOutbox]);

  const flush = useCallback(async () => {
    if (inFlight.current || Date.now() < retryAfter.current) return;
    inFlight.current = true;
    try {
      let failed = false;
      for (const [key, event] of pending.current) {
        try {
          await latest.current.persist(event.threadId, event.batch);
        } catch {
          // An archived/offline old project must not block another project's receipts.
          failed = true;
          continue;
        }
        pending.current.delete(key);
        try { sessionStorage.setItem(`kria:editor-outbox:${event.threadId}`, JSON.stringify(
          [...pending.current.values()].filter((queued) => queued.threadId === event.threadId),
        )); } catch { /* Server persistence already succeeded. */ }
        if (mounted.current) saveOutbox();
      }
      retryAfter.current = failed ? Date.now() + 30_000 : 0;
      if (mounted.current) setError([...pending.current.values()].some((event) => event.threadId === latest.current.thread?.id)
        ? "Your editor changes are still here, but chat history hasn’t saved. Retry saving the conversation." : null);
    } finally {
      inFlight.current = false;
    }
  }, [saveOutbox]);

  const remember = useCallback((message: CopilotMessage, state: EditorChatState) => {
    const current = latest.current.thread;
    if (!current || current.active_plan_item_id !== state.target.itemId) return;
    const key = `${current.id}:${message.id}`;
    if (seen.current.has(key)) return;
    seen.current.add(key);
    if (current.events.some((event) => event.payload?.editor_message_id === message.id)) return;
    pending.current.set(key, {
      threadId: current.id,
      batch: {
        item_id: state.target.itemId, variant_id: state.target.variantId,
        generation_id: state.target.generationId,
        messages: [{ id: message.id, role: message.role, text: message.text,
          applied: message.applied ?? [], rejected: message.rejected ?? [],
          clarification_context: message.clarification_context ?? null,
          pending_actions: message.pending_actions ?? [],
        }],
      },
    });
    saveOutbox();
  }, [saveOutbox]);

  useEffect(() => {
    if (!editor || editor.target.itemId !== thread?.active_plan_item_id) return;
    editor.messages.forEach((message) => remember(message, editor));
    const additions: CopilotMessage[] = [];
    const review = editor.director;
    if (review?.reviewed && !review.loading && !review.error) {
      const reviewId = `${editor.target.instanceId}:review:${review.reviewVersion ?? 0}`;
      if (!seen.current.has(`${thread.id}:${reviewId}`)) additions.push({ id: reviewId, role: "assistant",
        text: review.suggestions.length
          ? `I reviewed your edit. ${review.suggestions.map((row) => row.title).join(". ")}.`
          : "I reviewed your edit and found no further changes to suggest.",
      });
    }
    for (const receipt of review?.appliedReceipts ?? []) {
      const id = (receipt.status === "applied" ? receipt.id : `${editor.target.instanceId}:applied:${receipt.id}`).slice(0, 128);
      if (!seen.current.has(`${thread.id}:${id}`)) additions.push({
        id, role: "assistant", text: receipt.status === "applied" ? `${receipt.title}.`
          : receipt.status === "rendering" ? `${receipt.title}: the new video version is rendering.`
          : `Staged: ${receipt.title}. Save to render the new video.`,
        applied: receipt.changes.map((change) => `${change.label}: ${change.to}`),
        undoVersion: receipt.undoVersion,
      });
    }
    if (additions.length) {
      additions.forEach((message) => remember(message, editor));
      setExtra((rows) => [...rows, ...additions.map((message) => ({ ...message, threadId: thread.id }))]);
    }
    void flush();
  }, [editor, flush, remember, thread?.active_plan_item_id, thread?.id]);

  const unsaved = useMemo(() => {
    const persisted = new Set(thread?.events.map((event) => event.payload?.editor_message_id) ?? []);
    const queued = outbox.filter((event) => event.threadId === thread?.id).flatMap((event) => event.batch.messages);
    const unique = new Map<string, CopilotMessage>();
    [...queued, ...(editor?.target.itemId === thread?.active_plan_item_id ? editor?.messages ?? [] : []), ...extra.filter((message) => message.threadId === thread?.id)].forEach((message) => unique.set(message.id, message));
    return [...unique.values()].filter((message) => !persisted.has(message.id));
  }, [editor, extra, outbox, thread]);

  return { unsaved, error, retry: () => { retryAfter.current = 0; return flush(); } };
}
