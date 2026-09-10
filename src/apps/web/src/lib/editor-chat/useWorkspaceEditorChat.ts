"use client";

import { useCallback, useEffect, useRef, useState, type RefObject } from "react";
import { EDITOR_CHAT_PROTOCOL, EDITOR_CHAT_MESSAGE_MAX_LENGTH, isEditorChatCommand, isEditorChatTarget, sameEditorTarget,
  type EditorChatAction, type EditorChatCommand, type EditorChatState, type EditorChatTarget } from "./protocol";

export function useWorkspaceEditorChat({ frameRef, threadId, itemId, variantId, enabled }: {
  frameRef: RefObject<HTMLIFrameElement | null>;
  threadId: string | null;
  itemId: string | null;
  variantId: string | null;
  enabled: boolean;
}) {
  const scope = `${threadId}:${itemId}:${variantId}:${enabled}`;
  const [state, setState] = useState<EditorChatState | null>(null);
  const [stateScope, setStateScope] = useState(scope);
  const identity = useRef({ threadId, itemId, variantId });
  identity.current = { threadId, itemId, variantId };
  const latest = useRef<EditorChatState | null>(null);
  const previousScope = useRef(scope);
  if (previousScope.current !== scope) {
    previousScope.current = scope;
    latest.current = null;
  }
  const connection = useRef("");
  const pending = useRef(new Map<string, {
    target: EditorChatTarget; resolve: () => void; reject: (reason: Error) => void;
    timer: ReturnType<typeof setTimeout>;
  }>());

  useEffect(() => {
    latest.current = null;
    setState(null);
    const connectionId = crypto.randomUUID();
    connection.current = connectionId;
    if (!enabled || !threadId || !itemId || !variantId) return;
    const connect = () => frameRef.current?.contentWindow?.postMessage({
      protocol: EDITOR_CHAT_PROTOCOL, type: "connect", connectionId, itemId, variantId,
    }, window.location.origin);
    const onMessage = (event: MessageEvent) => {
      if (event.origin !== window.location.origin || event.source !== frameRef.current?.contentWindow) return;
      const data = event.data;
      if (data?.protocol !== EDITOR_CHAT_PROTOCOL || data.connectionId !== connectionId) return;
      if (data.type === "state" && isEditorChatTarget(data.state?.target)
        && data.state.target.itemId === itemId && data.state.target.variantId === variantId
        && Array.isArray(data.state.messages) && typeof data.state.sending === "boolean") {
        const next = data.state as EditorChatState;
        if (latest.current && latest.current.target.instanceId !== next.target.instanceId) {
          for (const waiter of pending.current.values()) {
            clearTimeout(waiter.timer);
            waiter.reject(new Error("The editor reloaded. Check the draft before retrying the request."));
          }
          pending.current.clear();
        }
        latest.current = next;
        setStateScope(scope);
        setState(next);
      } else if (data.type === "result" && typeof data.requestId === "string") {
        const waiter = pending.current.get(data.requestId);
        if (!waiter || !isEditorChatTarget(data.target) || !sameEditorTarget(waiter.target, data.target)) return;
        clearTimeout(waiter.timer);
        pending.current.delete(data.requestId);
        if (typeof data.error === "string") waiter.reject(new Error(data.error));
        else waiter.resolve();
      }
    };
    window.addEventListener("message", onMessage);
    connect();
    // Also handles an iframe reload and React mounting after the native load event.
    const heartbeat = setInterval(connect, 1000);
    const requests = pending.current;
    return () => {
      clearInterval(heartbeat);
      window.removeEventListener("message", onMessage);
      latest.current = null;
      for (const waiter of requests.values()) {
        clearTimeout(waiter.timer);
        waiter.reject(new Error("The editor target changed. The request was not sent to another video."));
      }
      requests.clear();
    };
  }, [enabled, frameRef, itemId, scope, threadId, variantId]);

  const command = useCallback((action: EditorChatAction, requestId = crypto.randomUUID()): Promise<void> => {
    const current = latest.current;
    const frame = frameRef.current?.contentWindow;
    if (!current || !frame) return Promise.reject(new Error("The editor is connecting. Wait for it to load, then send again."));
    if (pending.current.has(requestId)) return Promise.reject(new Error("This request is already running."));
    if (action.kind === "send" && action.text.length > EDITOR_CHAT_MESSAGE_MAX_LENGTH) {
      return Promise.reject(new Error(`Keep editor messages to ${EDITOR_CHAT_MESSAGE_MAX_LENGTH.toLocaleString("en-US")} characters or fewer. Shorten your message and send again.`));
    }
    const message: EditorChatCommand = { protocol: EDITOR_CHAT_PROTOCOL, type: "command", connectionId: connection.current,
      requestId, target: current.target, action };
    if (!isEditorChatCommand(message)) {
      return Promise.reject(new Error("This editor request is invalid. Review your message and try again."));
    }
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        pending.current.delete(requestId);
        reject(new Error("The editor hasn’t acknowledged this request yet. Check its progress before retrying."));
      }, 180_000);
      pending.current.set(requestId, { target: current.target, resolve, reject, timer });
      frame.postMessage(message, window.location.origin);
    });
  }, [frameRef]);

  const waitForReady = useCallback(async (expectedThreadId: string, expectedItemId: string, expectedVariantId: string) => {
    const until = Date.now() + 15_000;
    while (Date.now() < until) {
      const current = identity.current;
      if (current.threadId !== expectedThreadId || current.itemId !== expectedItemId || current.variantId !== expectedVariantId) {
        throw new Error("The project changed before the editor was ready. Your request was not sent.");
      }
      if (latest.current) return latest.current;
      await new Promise((resolve) => window.setTimeout(resolve, 100));
    }
    throw new Error("The editor is still connecting. Wait for it to load, then send again.");
  }, []);

  return { state: stateScope === scope ? state : null, command, waitForReady };
}
