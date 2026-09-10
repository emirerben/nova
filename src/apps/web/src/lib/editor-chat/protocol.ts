import type { EditorOverlaySuggestionsState } from "@/app/plan/items/[id]/_editor/useEditorOverlaySuggestions";
import type { CopilotMessage } from "@/lib/edit-copilot/useEditCopilot";
import type { UseEditDirectorResult } from "@/lib/edit-copilot/useEditDirector";
import type { PoolAsset, EditCopilotTurn } from "@/lib/plan-api";
import type { NovaStep } from "@/lib/job-phases";

export const EDITOR_CHAT_PROTOCOL = "kria:editor-chat:v1";
export const EDITOR_CHAT_MESSAGE_MAX_LENGTH = 2000;

/** A command is valid only in the exact editor mount and draft it observed. */
export interface EditorChatTarget {
  itemId: string;
  variantId: string;
  generationId: string | null;
  instanceId: string;
  revision: string;
}

export interface EditorConfirmation {
  id: string;
  title: string;
  revision: string;
}

export type EditorDirectorState = Omit<UseEditDirectorResult,
  "refresh" | "accept" | "dismiss" | "revealApplied" | "cancelGeneration" | "restoreOriginalTiming">;

export interface EditorChatState {
  target: EditorChatTarget;
  messages: CopilotMessage[];
  sending: boolean;
  error: string | null;
  unavailable: boolean;
  dirty: boolean;
  saving: boolean;
  readOnly: boolean;
  historyVersion: number;
  canUndo: boolean;
  director: EditorDirectorState | null;
  visuals?: Omit<EditorOverlaySuggestionsState, "start" | "removeRow" | "clearLocal"> & { readyAssets: number; assets?: Array<Pick<PoolAsset, "id" | "source_filename" | "subject" | "display_url">> };
  confirmation: EditorConfirmation | null;
  renderActive: boolean;
  renderSteps: NovaStep[] | null;
}

export type EditorChatAction =
  | { kind: "send"; text: string; turns: EditCopilotTurn[] }
  | { kind: "review" }
  | { kind: "visuals-start" }
  | { kind: "visuals-accept" | "visuals-dismiss" | "visuals-reveal"; suggestionId: string }
  | { kind: "accept"; suggestionId: string; omniCostConfirmed?: boolean }
  | { kind: "dismiss"; suggestionId: string }
  | { kind: "reveal"; receiptId: string }
  | { kind: "undo" }
  | { kind: "stop" }
  | { kind: "cancel-generation" }
  | { kind: "restore-timing" }
  | { kind: "confirm"; confirmationId: string; approved: boolean };

export interface EditorChatCommand {
  protocol: typeof EDITOR_CHAT_PROTOCOL;
  type: "command";
  connectionId: string;
  requestId: string;
  target: EditorChatTarget;
  action: EditorChatAction;
}

export type EditorChatReply =
  | { protocol: typeof EDITOR_CHAT_PROTOCOL; type: "state"; connectionId: string; state: EditorChatState }
  | { protocol: typeof EDITOR_CHAT_PROTOCOL; type: "result"; connectionId: string; requestId: string; target: EditorChatTarget; error?: string };

export function sameEditorTarget(a: EditorChatTarget, b: EditorChatTarget): boolean {
  return a.itemId === b.itemId && a.variantId === b.variantId
    && a.generationId === b.generationId && a.instanceId === b.instanceId
    && a.revision === b.revision;
}

function record(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}
function identifier(value: unknown): value is string {
  return typeof value === "string" && value.length > 0 && value.length <= 128;
}

export function isEditorChatTarget(value: unknown): value is EditorChatTarget {
  return record(value) && identifier(value.itemId) && identifier(value.variantId)
    && identifier(value.instanceId) && identifier(value.revision)
    && (value.generationId === null || identifier(value.generationId));
}

/** Structured cloning is transport, not input validation. */
export function isEditorChatCommand(value: unknown): value is EditorChatCommand {
  if (!record(value) || value.protocol !== EDITOR_CHAT_PROTOCOL || value.type !== "command"
    || !identifier(value.connectionId) || !identifier(value.requestId)
    || !isEditorChatTarget(value.target) || !record(value.action)) return false;
  const action = value.action;
  switch (action.kind) {
    case "send":
      return typeof action.text === "string" && action.text.trim().length > 0 && action.text.length <= EDITOR_CHAT_MESSAGE_MAX_LENGTH
        && Array.isArray(action.turns) && action.turns.length <= 12
        && action.turns.every((turn) => record(turn) && ["user", "assistant"].includes(String(turn.role))
          && typeof turn.content === "string" && turn.content.length <= EDITOR_CHAT_MESSAGE_MAX_LENGTH);
    case "accept":
      return identifier(action.suggestionId)
        && (action.omniCostConfirmed === undefined || typeof action.omniCostConfirmed === "boolean");
    case "visuals-accept": case "visuals-dismiss": case "visuals-reveal":
    case "dismiss": return identifier(action.suggestionId);
    case "reveal": return identifier(action.receiptId);
    case "confirm": return identifier(action.confirmationId) && typeof action.approved === "boolean";
    case "visuals-start": case "review": case "undo": case "stop": case "cancel-generation": case "restore-timing": return true;
    default: return false;
  }
}
