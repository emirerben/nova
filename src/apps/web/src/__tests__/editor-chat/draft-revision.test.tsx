import { act, renderHook } from "@testing-library/react";
import { editorDraftRevision } from "@/lib/editor-chat/draft-revision";
import { useEditorConfirmation } from "@/lib/editor-chat/useEditorConfirmation";

const state = { target: "item:variant:generation", historyVersion: 1,
  bars: [{ text: "hello", start: 0 }], capabilities: { trim: true },
  sources: [{ id: "source", signed_url: "https://example.com/old" }],
};

test("equivalent polling data and refreshed delivery URLs preserve the revision", () => {
  expect(editorDraftRevision({ ...state, sources: [{ signed_url: "https://example.com/new", id: "source" }] }))
    .toBe(editorDraftRevision(state));
});

test.each([
  { bars: [{ text: "changed", start: 0 }] },
  { bars: [{ text: "hello", start: 1 }] },
  { historyVersion: 2 }, { target: "item:variant:new-generation" },
  { capabilities: { trim: false } },
  { musicStartS: 4 }, { backgroundMusic: { track_id: "new" } },
  { orientation: "landscape" }, { lyricsEnabled: false },
  { videoMuted: true }, { soundMuted: true },
])("fences meaningful mutations %j", (change) => {
  expect(editorDraftRevision({ ...state, ...change })).not.toBe(editorDraftRevision(state));
});

test("confirmation survives refresh and expires on an actual edit", async () => {
  const { result, rerender } = renderHook(({ draft }) => useEditorConfirmation(editorDraftRevision(draft)), {
    initialProps: { draft: state },
  });
  let pending!: Promise<boolean>;
  act(() => { pending = result.current.request("Render?"); });
  const id = result.current.pending?.id;
  rerender({ draft: { ...state, sources: [{ id: "source", signed_url: "new" }] } });
  expect(result.current.pending?.id).toBe(id);
  rerender({ draft: { ...state, historyVersion: 2 } });
  expect(await pending).toBe(false);
  expect(result.current.pending).toBeNull();
});
