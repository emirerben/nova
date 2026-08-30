import { notifyEmbeddedEditorLeave, shouldNotifyEmbeddedEditor } from "@/app/plan/items/[id]/_editor/EditorShell";

describe("embedded editor leave contract", () => {
  it("only notifies a different parent from an embedded URL", () => {
    expect(shouldNotifyEmbeddedEditor("?embedded=1", false)).toBe(true);
    expect(shouldNotifyEmbeddedEditor("?embedded=1", true)).toBe(false);
    expect(shouldNotifyEmbeddedEditor("", false)).toBe(false);
  });

  it("posts a same-origin leave message instead of navigating", () => {
    const parent = window.parent;
    const postMessage = jest.fn();
    Object.defineProperty(window, "parent", { configurable: true, value: { postMessage } });
    window.history.replaceState({}, "", "/plan/items/item-1/edit?embedded=1");

    expect(notifyEmbeddedEditorLeave(true)).toBe(true);
    expect(postMessage).toHaveBeenCalledWith(
      { type: "nova:embedded-editor-leave", refresh: true },
      window.location.origin,
    );

    Object.defineProperty(window, "parent", { configurable: true, value: parent });
    window.history.replaceState({}, "", "/");
  });
});
