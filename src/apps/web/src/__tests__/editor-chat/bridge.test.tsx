import { act, renderHook, waitFor } from "@testing-library/react";
import { useEditorChatBridge } from "@/lib/editor-chat/useEditorChatBridge";
import { useEditorConfirmation } from "@/lib/editor-chat/useEditorConfirmation";
import { EDITOR_CHAT_PROTOCOL, isEditorChatCommand, type EditorChatState, type EditorChatCommand } from "@/lib/editor-chat/protocol";
import type { UseEditCopilotResult } from "@/lib/edit-copilot/useEditCopilot";
import type { UseEditDirectorResult } from "@/lib/edit-copilot/useEditDirector";

const target = { itemId: "item", variantId: "variant", generationId: "generation", instanceId: "mount", revision: "draft-1" };
const parent = { postMessage: jest.fn() };
const originalParent = window.parent;
const state: EditorChatState = {
  target, messages: [], sending: false, error: null, unavailable: false, dirty: false,
  saving: false, readOnly: false, historyVersion: 1, canUndo: true, director: null,
  confirmation: null, renderActive: false, renderSteps: null,
};
function emit(data: unknown, origin = window.location.origin, source: unknown = parent) {
  window.dispatchEvent(new MessageEvent("message", { data, origin, source: source as Window }));
}
function connect() {
  emit({ protocol: EDITOR_CHAT_PROTOCOL, type: "connect", connectionId: "chat", itemId: "item", variantId: "variant" });
}
function command(overrides: Partial<EditorChatCommand> = {}): EditorChatCommand {
  return { protocol: EDITOR_CHAT_PROTOCOL, type: "command", connectionId: "chat", requestId: "request", target,
    action: { kind: "send", text: "Make the text smaller", turns: [] }, ...overrides };
}
function setup(overrides: Partial<Parameters<typeof useEditorChatBridge>[0]> = {}) {
  const send = jest.fn().mockResolvedValue(undefined);
  const accept = jest.fn();
  const undo = jest.fn();
  const options = { enabled: true, state,
    copilot: { send, stop: jest.fn() } as unknown as UseEditCopilotResult,
    director: { suggestions: [], accept, refresh: jest.fn() } as unknown as UseEditDirectorResult,
    undo, confirm: jest.fn().mockResolvedValue(true), decide: jest.fn(), ...overrides };
  const hook = renderHook((props) => useEditorChatBridge(props), { initialProps: options });
  act(connect);
  return { ...hook, send, accept, undo, options };
}

beforeEach(() => {
  jest.clearAllMocks();
  Object.defineProperty(window, "parent", { configurable: true, value: parent });
});
afterEach(() => Object.defineProperty(window, "parent", { configurable: true, value: originalParent }));

test("validates commands before dispatch and rejects unknown actions", () => {
  expect(isEditorChatCommand(command())).toBe(true);
  expect(isEditorChatCommand(command({ action: { kind: "send", text: "x".repeat(2001), turns: [] } }))).toBe(false);
  expect(isEditorChatCommand({ ...command(), action: { kind: "render", approved: true } })).toBe(false);
  expect(isEditorChatCommand({ ...command(), target: { ...target, revision: null } })).toBe(false);
});

test("ignores other origins, other frames, and other conversations", async () => {
  const { send } = setup();
  await act(async () => {
    emit(command(), "https://elsewhere.example");
    emit(command(), window.location.origin, {});
    emit(command({ connectionId: "another-chat" }));
  });
  expect(send).not.toHaveBeenCalled();
});

test("duplicate transport delivery executes once and returns the same acknowledgement", async () => {
  const { send } = setup();
  await act(async () => { emit(command()); emit(command()); });
  expect(send).toHaveBeenCalledTimes(1);
  expect(send).toHaveBeenCalledWith("Make the text smaller", { requestId: "request", turns: [] });
  expect(parent.postMessage).toHaveBeenCalledWith(expect.objectContaining({ type: "result", requestId: "request", target }), window.location.origin);
});

test.each(["revision", "generationId", "variantId", "instanceId"] as const)("rejects a stale %s without applying or rendering", async (field) => {
  const { send } = setup();
  await act(async () => emit(command({ target: { ...target, [field]: "old" } })));
  expect(send).not.toHaveBeenCalled();
  expect(parent.postMessage).toHaveBeenCalledWith(expect.objectContaining({ type: "result", error: expect.stringContaining("editor changed") }), window.location.origin);
});

test("a second request cannot unlock an already running request", async () => {
  let finish!: () => void;
  const send = jest.fn(() => new Promise<void>((resolve) => { finish = resolve; }));
  setup({ copilot: { send } as unknown as UseEditCopilotResult });
  await act(async () => { emit(command()); emit(command({ requestId: "second" })); emit(command({ requestId: "third" })); });
  expect(send).toHaveBeenCalledTimes(1);
  await act(async () => finish());
});

test("server suggestions require confirmation against the same draft", async () => {
  let approve!: (value: boolean) => void;
  const accept = jest.fn();
  const confirm = jest.fn(() => new Promise<boolean>((resolve) => { approve = resolve; }));
  const { rerender, options } = setup({ confirm, director: {
    suggestions: [{ id: "cut", title: "Remove silence", apply_mode: "server_async", ops: [] }], accept,
  } as unknown as UseEditDirectorResult });
  await act(async () => emit(command({ action: { kind: "accept", suggestionId: "cut" } })));
  expect(confirm).toHaveBeenCalledTimes(1);
  expect(accept).not.toHaveBeenCalled();
  rerender({ ...options, state: { ...state, target: { ...target, revision: "draft-2" } } });
  await act(async () => approve(true));
  expect(accept).not.toHaveBeenCalled();
});

test("dirty drafts cannot dispatch server suggestions", async () => {
  const confirm = jest.fn();
  const accept = jest.fn();
  setup({ state: { ...state, dirty: true }, confirm, director: {
    suggestions: [{ id: "cut", title: "Remove silence", apply_mode: "server_async", ops: [] }], accept,
  } as unknown as UseEditDirectorResult });
  await act(async () => emit(command({ action: { kind: "accept", suggestionId: "cut" } })));
  expect(confirm).not.toHaveBeenCalled();
  expect(accept).not.toHaveBeenCalled();
});

test("changing the draft invalidates pending confirmation", async () => {
  const { result, rerender } = renderHook(({ revision }) => useEditorConfirmation(revision), { initialProps: { revision: "one" } });
  let decision!: Promise<boolean>;
  act(() => { decision = result.current.request("Render this layout?"); });
  expect(result.current.pending).not.toBeNull();
  rerender({ revision: "two" });
  await expect(decision).resolves.toBe(false);
  await waitFor(() => expect(result.current.pending).toBeNull());
});

test("visual suggestion commands use the same target fence and deduplicate acceptance", async () => {
  const visualAction = jest.fn();
  setup({ state: { ...state, visuals: { phase: "ready", rows: [], wishlist: [], staleNotice: false, stillWorking: false, unavailable: false, readyAssets: 1 } }, visualAction });
  const accept = command({ action: { kind: "visuals-accept", suggestionId: "visual-one" } });
  await act(async () => { emit(accept); emit(accept); });
  expect(visualAction).toHaveBeenCalledTimes(1);
  expect(visualAction).toHaveBeenCalledWith("visuals-accept", "visual-one");
  await act(async () => emit(command({ requestId: "stale-visual", target: { ...target, revision: "old" }, action: { kind: "visuals-accept", suggestionId: "visual-one" } })));
  expect(visualAction).toHaveBeenCalledTimes(1);
});
