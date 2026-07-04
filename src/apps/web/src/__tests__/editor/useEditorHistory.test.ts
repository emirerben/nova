import { act, renderHook } from "@testing-library/react";
import {
  EDITOR_HISTORY_DEPTH,
  deserializeDraft,
  draftKey,
  initEditorHistoryState,
  recordSnapshot,
  redoSnapshot,
  serializeDraft,
  undoSnapshot,
  useEditorHistory,
  type EditorDocument,
} from "@/app/plan/items/[id]/_editor/useEditorHistory";
import type { MediaOverlay } from "@/lib/plan-api";
import type { TextElementBar } from "@/lib/timeline/text-timeline-reducer";

function bar(id: string, text = "hi"): TextElementBar {
  return { id, text, start_s: 0, end_s: 2, role: "generative_intro" };
}

function doc(bars: TextElementBar[], over: Partial<EditorDocument> = {}): EditorDocument {
  return { bars, slots: null, videoMuted: false, soundMuted: false, title: "", ...over };
}

function overlay(overrides: Partial<MediaOverlay> = {}): MediaOverlay {
  return {
    id: "ov-1",
    kind: "image",
    src_gcs_path: "users/u/plan/i/overlays/card.png",
    position: "custom",
    x_frac: 0.24,
    y_frac: 0.68,
    scale: 0.72,
    start_s: 0.4,
    end_s: 2.4,
    z: 0,
    ...overrides,
  };
}

// ── Pure stack ────────────────────────────────────────────────────────────────

describe("recordSnapshot", () => {
  it("pushes the prior document and clears redo", () => {
    let h = initEditorHistoryState();
    h.future = [doc([bar("z")])]; // pretend there was a redo available
    h = recordSnapshot(h, doc([bar("a")]));
    expect(h.past).toHaveLength(1);
    expect(h.past[0].bars[0].id).toBe("a");
    expect(h.future).toHaveLength(0);
  });

  it("coalesces consecutive records that share a non-null tag", () => {
    let h = initEditorHistoryState();
    h = recordSnapshot(h, doc([bar("a")], { title: "H" }), "title");
    h = recordSnapshot(h, doc([bar("a")], { title: "He" }), "title");
    h = recordSnapshot(h, doc([bar("a")], { title: "Hel" }), "title");
    // A typing burst collapses to ONE restore point (the pre-typing snapshot).
    expect(h.past).toHaveLength(1);
    expect(h.past[0].title).toBe("H");
  });

  it("does not coalesce across different tags or null tags", () => {
    let h = initEditorHistoryState();
    h = recordSnapshot(h, doc([bar("a")]), "title");
    h = recordSnapshot(h, doc([bar("a")]), null);
    h = recordSnapshot(h, doc([bar("a")]), "text:1");
    expect(h.past).toHaveLength(3);
  });

  it("caps the stack at EDITOR_HISTORY_DEPTH (dropping the oldest)", () => {
    let h = initEditorHistoryState();
    for (let i = 0; i < EDITOR_HISTORY_DEPTH + 10; i++) {
      h = recordSnapshot(h, doc([bar(`b${i}`)]));
    }
    expect(h.past).toHaveLength(EDITOR_HISTORY_DEPTH);
    // Oldest ten were shifted off; the first retained snapshot is b10.
    expect(h.past[0].bars[0].id).toBe("b10");
  });
});

describe("undoSnapshot / redoSnapshot", () => {
  it("undo returns the prior doc and moves current onto future", () => {
    let h = initEditorHistoryState();
    h = recordSnapshot(h, doc([bar("a")])); // pre-change snapshot
    const res = undoSnapshot(h, doc([bar("a"), bar("b")]));
    expect(res).not.toBeNull();
    expect(res!.doc.bars.map((x) => x.id)).toEqual(["a"]);
    expect(res!.history.past).toHaveLength(0);
    expect(res!.history.future).toHaveLength(1);
    expect(res!.history.future[0].bars.map((x) => x.id)).toEqual(["a", "b"]);
  });

  it("undo of nothing returns null", () => {
    expect(undoSnapshot(initEditorHistoryState(), doc([]))).toBeNull();
  });

  it("redo replays the undone doc", () => {
    let h = initEditorHistoryState();
    h = recordSnapshot(h, doc([bar("a")]));
    const undone = undoSnapshot(h, doc([bar("a"), bar("b")]))!;
    const redone = redoSnapshot(undone.history, undone.doc);
    expect(redone).not.toBeNull();
    expect(redone!.doc.bars.map((x) => x.id)).toEqual(["a", "b"]);
    expect(redone!.history.future).toHaveLength(0);
  });

  it("undo of a delete resurrects the removed element in the restored doc", () => {
    // Model a delete: pre-change had [a,b]; after delete current is [a].
    let h = initEditorHistoryState();
    h = recordSnapshot(h, doc([bar("a"), bar("b")]));
    const current = doc([bar("a")]);
    const res = undoSnapshot(h, current)!;
    // The reselect signal the shell uses: an id present in the restored doc but
    // absent from the current doc = the resurrected element to re-select.
    const currentIds = new Set(current.bars.map((x) => x.id));
    const resurrected = res.doc.bars.find((x) => !currentIds.has(x.id));
    expect(resurrected?.id).toBe("b");
  });
});

// ── Draft (de)serialization round-trip ────────────────────────────────────────

describe("serializeDraft / deserializeDraft", () => {
  it("round-trips a full document", () => {
    const d = doc([bar("a"), bar("b")], {
      slots: [{ key: "s0", inS: 0, durationS: 3, removed: false } as never],
      overlays: [overlay({ x_frac: 0.8, y_frac: 0.2, scale: 0.55 })],
      videoMuted: true,
      soundMuted: true,
      title: "My clip",
    });
    const parsed = deserializeDraft(serializeDraft("v1", d));
    expect(parsed).not.toBeNull();
    expect(parsed!.variantId).toBe("v1");
    expect(parsed!.doc).toEqual(d);
  });

  it("returns null for malformed / foreign input", () => {
    expect(deserializeDraft(null)).toBeNull();
    expect(deserializeDraft("")).toBeNull();
    expect(deserializeDraft("{not json")).toBeNull();
    expect(deserializeDraft(JSON.stringify({ v: 2, variantId: "x", doc: {} }))).toBeNull();
    expect(deserializeDraft(JSON.stringify({ v: 1, variantId: "x" }))).toBeNull();
    expect(deserializeDraft(JSON.stringify({ v: 1, variantId: "x", doc: { bars: "no" } }))).toBeNull();
  });

  it("coerces missing optional fields to safe defaults", () => {
    const parsed = deserializeDraft(
      JSON.stringify({ v: 1, variantId: "x", doc: { bars: [] } }),
    );
    expect(parsed!.doc).toEqual({
      bars: [],
      slots: null,
      videoMuted: false,
      soundMuted: false,
      title: "",
    });
  });

  it("preserves overlay move and scale fields in draft recovery", () => {
    const moved = overlay({
      position: "custom",
      x_frac: 0.12,
      y_frac: 0.88,
      scale: 0.91,
      start_s: 1.2,
      end_s: 4.8,
    });
    const parsed = deserializeDraft(serializeDraft("v1", doc([], { overlays: [moved] })));
    expect(parsed?.doc.overlays?.[0]).toEqual(moved);
  });

  it("keys drafts per variant id", () => {
    expect(draftKey("abc")).toBe("nova-editor-draft:abc");
  });
});

// ── Hook integration ──────────────────────────────────────────────────────────

describe("useEditorHistory (hook)", () => {
  it("push → undo → redo drives canUndo/canRedo and calls apply", () => {
    let current: EditorDocument = doc([bar("a")]);
    const applied: EditorDocument[] = [];
    const { result } = renderHook(() =>
      useEditorHistory({
        getCurrent: () => current,
        apply: (d) => {
          applied.push(d);
          current = d;
        },
      }),
    );

    expect(result.current.canUndo).toBe(false);
    expect(result.current.canRedo).toBe(false);

    // Command: record the pre-change doc, then mutate current.
    act(() => result.current.record());
    current = doc([bar("a"), bar("b")]);
    expect(result.current.canUndo).toBe(true);

    act(() => result.current.undo());
    expect(applied[applied.length - 1].bars.map((x) => x.id)).toEqual(["a"]);
    expect(result.current.canUndo).toBe(false);
    expect(result.current.canRedo).toBe(true);

    act(() => result.current.redo());
    expect(applied[applied.length - 1].bars.map((x) => x.id)).toEqual(["a", "b"]);
    expect(result.current.canRedo).toBe(false);
  });

  it("clear empties the stack (Save contract)", () => {
    let current: EditorDocument = doc([bar("a")]);
    const { result } = renderHook(() =>
      useEditorHistory({ getCurrent: () => current, apply: (d) => (current = d) }),
    );
    act(() => result.current.record());
    current = doc([bar("a"), bar("b")]);
    expect(result.current.canUndo).toBe(true);
    act(() => result.current.clear());
    expect(result.current.canUndo).toBe(false);
    expect(result.current.canRedo).toBe(false);
  });
});
