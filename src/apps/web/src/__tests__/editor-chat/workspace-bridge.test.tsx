import { act, renderHook } from "@testing-library/react";
import { useWorkspaceEditorChat } from "@/lib/editor-chat/useWorkspaceEditorChat";
import { EDITOR_CHAT_PROTOCOL } from "@/lib/editor-chat/protocol";

const target = { itemId: "item", variantId: "variant", generationId: "generation", instanceId: "mount", revision: "revision" };
function setup() {
  const frame = { postMessage: jest.fn() };
  const props = { frameRef: { current: { contentWindow: frame } as unknown as HTMLIFrameElement }, threadId: "thread", itemId: "item", variantId: "variant", enabled: true };
  const hook = renderHook((options) => useWorkspaceEditorChat(options), { initialProps: props });
  const connectionId = frame.postMessage.mock.calls[0][0].connectionId;
  const emit = (data: object, source: unknown = frame) => window.dispatchEvent(new MessageEvent("message", { origin: location.origin, source: source as Window, data: { protocol: EDITOR_CHAT_PROTOCOL, connectionId, ...data } }));
  const publish = (instanceId = "mount") => emit({ type: "state", state: { target: { ...target, instanceId }, messages: [], sending: false } });
  return { ...hook, frame, props, emit, publish };
}

test("only the actual editor frame can connect and acknowledge a request", async () => {
  const { result, emit, publish, frame } = setup();
  act(() => emit({ type: "state", state: { target, messages: [], sending: false } }, {}));
  expect(result.current.state).toBeNull();
  act(() => publish());
  let request!: Promise<void>;
  act(() => { request = result.current.command({ kind: "undo" }, "request"); });
  expect(frame.postMessage).toHaveBeenLastCalledWith(expect.objectContaining({ type: "command", target }), location.origin);
  await act(async () => emit({ type: "result", requestId: "request", target }));
  await expect(request).resolves.toBeUndefined();
});

test("switching projects rejects an outstanding request and discards the old editor context", async () => {
  const { result, publish, rerender, props } = setup();
  act(() => publish());
  let failure!: Promise<string>;
  act(() => { failure = result.current.command({ kind: "send", text: "Edit", turns: [] }).then(() => "unexpected", (error: Error) => error.message); });
  rerender({ ...props, threadId: "other-thread", itemId: "other-item" });
  expect(result.current.state).toBeNull();
  await expect(failure).resolves.toMatch(/target changed/);
  await expect(result.current.command({ kind: "undo" })).rejects.toThrow(/connecting/);
});

test("an iframe reload rejects the old command without replaying it", async () => {
  const { result, publish, frame } = setup();
  act(() => publish());
  let failure!: Promise<string>;
  act(() => { failure = result.current.command({ kind: "undo" }).then(() => "unexpected", (error: Error) => error.message); });
  act(() => publish("new-mount"));
  await expect(failure).resolves.toMatch(/reloaded/);
  expect(frame.postMessage.mock.calls.filter(([message]) => message.type === "command")).toHaveLength(1);
});


test("oversized messages fail immediately without posting and the request ID remains reusable", async () => {
  const { result, publish, frame, emit } = setup();
  act(() => publish());
  await expect(result.current.command({ kind: "send", text: "x".repeat(2001), turns: [] }, "request"))
    .rejects.toThrow(/2,000 characters or fewer/);
  expect(frame.postMessage.mock.calls.filter(([message]) => message.type === "command")).toHaveLength(0);
  const request = result.current.command({ kind: "send", text: "x".repeat(2000), turns: [] }, "request");
  expect(frame.postMessage).toHaveBeenLastCalledWith(expect.objectContaining({ type: "command", requestId: "request" }), location.origin);
  await act(async () => emit({ type: "result", requestId: "request", target }));
  await expect(request).resolves.toBeUndefined();
});

test("invalid history fails locally instead of waiting for a missing acknowledgement", async () => {
  const { result, publish, frame } = setup();
  act(() => publish());
  await expect(result.current.command({ kind: "send", text: "Edit", turns: [{ role: "user", content: "x".repeat(2001) }] }))
    .rejects.toThrow(/request is invalid/);
  expect(frame.postMessage.mock.calls.filter(([message]) => message.type === "command")).toHaveLength(0);
});
