import { act, renderHook, waitFor } from "@testing-library/react";
import { useEditorConversation } from "@/lib/editor-chat/useEditorConversation";
import type { CreationThread } from "@/lib/creation-thread-api";
import type { EditorChatState } from "@/lib/editor-chat/protocol";

const thread = { id: "thread-one", active_plan_item_id: "item-one", events: [] } as unknown as CreationThread;
const editor = { target: { itemId: "item-one", variantId: "variant", generationId: "gen", instanceId: "mount", revision: "rev" },
  messages: [{ id: "request:reply", role: "assistant", text: "Staged text", applied: ["Text changed"] }], director: null,
} as EditorChatState;
beforeEach(() => sessionStorage.clear());

test("persistence retries append the same acknowledged receipt without replaying an edit", async () => {
  const persist = jest.fn().mockRejectedValueOnce(new Error("offline")).mockResolvedValue(undefined);
  const { result, rerender } = renderHook(({ state }) => useEditorConversation(thread, state, persist), { initialProps: { state: editor } });
  await waitFor(() => expect(result.current.error).toMatch(/history hasn’t saved/));
  const batch = persist.mock.calls[0][1];
  rerender({ state: { ...editor } });
  expect(persist).toHaveBeenCalledTimes(1);
  await act(async () => { await result.current.retry(); });
  expect(persist).toHaveBeenCalledTimes(2);
  expect(persist.mock.calls[1]).toEqual([thread.id, batch]);
  expect(result.current.error).toBeNull();
});

test("failed receipts survive reload and stay scoped to their conversation", async () => {
  const fail = jest.fn().mockRejectedValue(new Error("offline"));
  const first = renderHook(() => useEditorConversation(thread, editor, fail));
  await waitFor(() => expect(first.result.current.error).not.toBeNull());
  first.unmount();
  const persist = jest.fn().mockResolvedValue(undefined);
  const { result, rerender } = renderHook(({ current, state }) => useEditorConversation(current, state, persist), {
    initialProps: { current: thread, state: null as EditorChatState | null },
  });
  expect(result.current.unsaved.map((row) => row.text)).toEqual(["Staged text"]);
  await act(async () => { await result.current.retry(); });
  expect(persist).toHaveBeenCalledTimes(1);
  rerender({ current: { ...thread, id: "other", active_plan_item_id: "other-item" }, state: editor });
  expect(result.current.unsaved).toEqual([]);
});

test("persisted message ids and repeated editor state never duplicate history", async () => {
  const persist = jest.fn();
  const current = { ...thread, events: [{ payload: { editor_message_id: "request:reply" } }] } as unknown as CreationThread;
  const { result, rerender } = renderHook(({ state }) => useEditorConversation(current, state, persist), { initialProps: { state: editor } });
  rerender({ state: { ...editor } });
  expect(persist).not.toHaveBeenCalled();
  expect(result.current.unsaved).toEqual([]);
});

test("an old project's failed receipt does not block the new project's history", async () => {
  const persist = jest.fn((id: string) => id === thread.id ? Promise.reject(new Error("archived")) : Promise.resolve());
  const { result, rerender } = renderHook(({ current, state }) => useEditorConversation(current, state, persist), { initialProps: { current: thread, state: editor } });
  await waitFor(() => expect(result.current.error).not.toBeNull());
  rerender({ current: { ...thread, id: "new-thread", active_plan_item_id: "new-item" }, state: { ...editor, target: { ...editor.target, itemId: "new-item" } } });
  await waitFor(() => expect(persist).toHaveBeenCalledWith("new-thread", expect.anything()));
  await waitFor(() => expect(result.current.error).toBeNull());
});
