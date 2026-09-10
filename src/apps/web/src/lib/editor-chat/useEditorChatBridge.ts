"use client";

import { useEffect, useRef } from "react";
import type { UseEditCopilotResult } from "@/lib/edit-copilot/useEditCopilot";
import type { UseEditDirectorResult } from "@/lib/edit-copilot/useEditDirector";
import {
  EDITOR_CHAT_PROTOCOL, isEditorChatCommand, sameEditorTarget,
  type EditorChatCommand, type EditorChatReply, type EditorChatState,
} from "./protocol";

type Options = {
  enabled: boolean;
  state: EditorChatState;
  copilot: UseEditCopilotResult;
  director: UseEditDirectorResult;
  undo: () => void;
  confirm: (title: string) => Promise<boolean>;
  decide: (id: string, approved: boolean) => void;
  visualAction?: (kind: "visuals-start" | "visuals-accept" | "visuals-dismiss" | "visuals-reveal", suggestionId?: string) => void;
};

/** Keep execution in EditorShell: it owns the live draft, validation and history. */
export function useEditorChatBridge(options: Options) {
  const latest = useRef(options);
  latest.current = options;
  const connection = useRef<string | null>(null);
  const executions = useRef(new Map<string, Promise<EditorChatReply>>());
  const executing = useRef(false);

  useEffect(() => {
    if (!options.enabled || window.parent === window) return;
    let alive = true;
    const post = (reply: EditorChatReply) => {
      if (alive) window.parent.postMessage(reply, window.location.origin);
    };
    const publish = () => {
      if (connection.current) post({ protocol: EDITOR_CHAT_PROTOCOL, type: "state",
        connectionId: connection.current, state: latest.current.state });
    };
    const execute = async (command: EditorChatCommand): Promise<EditorChatReply> => {
      const result: EditorChatReply = { protocol: EDITOR_CHAT_PROTOCOL, type: "result",
        connectionId: command.connectionId, requestId: command.requestId, target: command.target };
      const { action } = command;
      const control = ["confirm", "stop", "cancel-generation"].includes(action.kind);
      let acquired = false;
      try {
        if (!sameEditorTarget(command.target, latest.current.state.target)) {
          throw new Error("The editor changed. Review the current draft and try again.");
        }
        if (!control && (executing.current || latest.current.state.sending || latest.current.state.confirmation)) {
          throw new Error("Finish the current request before starting another edit.");
        }
        if (!control && (latest.current.state.readOnly || latest.current.state.saving)) {
          throw new Error("Wait for the current save or render to finish before editing.");
        }
        if (!control) { executing.current = true; acquired = true; }
        const { copilot, director } = latest.current;
        switch (action.kind) {
          case "send":
            if (latest.current.state.unavailable) throw new Error("Kria editing isn’t available right now.");
            await copilot.send(action.text, { requestId: command.requestId, turns: action.turns });
            break;
          case "review": director.refresh(); break;
          case "visuals-start": case "visuals-accept": case "visuals-dismiss": case "visuals-reveal":
            if (!latest.current.state.visuals || latest.current.state.visuals.unavailable) throw new Error("Visual suggestions are unavailable for this edit.");
            latest.current.visualAction?.(action.kind, "suggestionId" in action ? action.suggestionId : undefined);
            break;
          case "accept": {
            const suggestion = director.suggestions.find((row) => row.id === action.suggestionId);
            if (!suggestion) throw new Error("That suggestion is no longer available. Review the edit again.");
            const renders = suggestion.apply_mode === "server_async"
              || suggestion.ops.some((op) => op.op === "set_intro_layout" || op.op === "apply_custom_effect");
            if (renders) {
              if (latest.current.state.dirty) throw new Error("Save your draft before applying a server timing or effect change.");
              if (!await latest.current.confirm(`${suggestion.title}. This processes a new video version.`)) break;
              if (!alive || !sameEditorTarget(command.target, latest.current.state.target)) {
                throw new Error("The draft changed. Review the edit again before applying this suggestion.");
              }
            }
            director.accept(suggestion, { omniCostConfirmed: action.omniCostConfirmed });
            break;
          }
          case "dismiss": {
            const suggestion = director.suggestions.find((row) => row.id === action.suggestionId);
            if (suggestion) director.dismiss(suggestion);
            break;
          }
          case "reveal": {
            const receipt = director.appliedReceipts.find((row) => row.id === action.receiptId);
            if (receipt?.undoVersion === latest.current.state.historyVersion) director.revealApplied(receipt);
            break;
          }
          case "undo":
            if (!latest.current.state.canUndo) throw new Error("There is no current edit to undo.");
            latest.current.undo();
            break;
          case "stop":
            if (latest.current.state.confirmation) latest.current.decide(latest.current.state.confirmation.id, false);
            copilot.stop();
            break;
          case "confirm": latest.current.decide(action.confirmationId, action.approved); break;
          case "cancel-generation": director.cancelGeneration(); break;
          case "restore-timing":
            if (latest.current.state.dirty) throw new Error("Save your draft before restoring the original timing.");
            if (await latest.current.confirm("Restore the original speech timing and render a new video version?")) {
              if (!alive || !sameEditorTarget(command.target, latest.current.state.target)) {
                throw new Error("The draft changed. Try restoring timing again.");
              }
              director.restoreOriginalTiming();
            }
            break;
        }
      } catch (error) {
        result.error = error instanceof Error ? error.message : "Kria couldn’t complete this editor action.";
      } finally {
        if (acquired) executing.current = false;
      }
      return result;
    };
    const onMessage = (event: MessageEvent) => {
      if (event.origin !== window.location.origin || event.source !== window.parent) return;
      const data = event.data;
      if (data?.protocol !== EDITOR_CHAT_PROTOCOL) return;
      if (data.type === "connect" && typeof data.connectionId === "string"
        && data.connectionId.length > 0 && data.connectionId.length <= 128
        && data.itemId === latest.current.state.target.itemId
        && data.variantId === latest.current.state.target.variantId) {
        // A new parent may observe a running request, but cannot hijack its acknowledgement.
        connection.current = data.connectionId;
        publish();
        return;
      }
      if (!isEditorChatCommand(data) || data.connectionId !== connection.current) return;
      const key = `${data.connectionId}:${data.requestId}`;
      const existing = executions.current.get(key);
      if (existing) { void existing.then(post); return; }
      const execution = execute(data);
      executions.current.set(key, execution);
      // Bound memory without permitting replay of a recent action.
      if (executions.current.size > 200) executions.current.delete(executions.current.keys().next().value!);
      void execution.then(post);
    };
    window.addEventListener("message", onMessage);
    return () => {
      alive = false;
      connection.current = null;
      window.removeEventListener("message", onMessage);
    };
  }, [options.enabled]);

  useEffect(() => {
    if (!options.enabled || !connection.current || window.parent === window) return;
    window.parent.postMessage({ protocol: EDITOR_CHAT_PROTOCOL, type: "state",
      connectionId: connection.current, state: options.state } satisfies EditorChatReply, window.location.origin);
  }, [options.enabled, options.state]);
}
