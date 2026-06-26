/**
 * Instant-edit session semantics:
 * - draft seeds from the variant on enterEdit and polls never clobber it
 * - commit builds a MINIMAL payload (only changed fields) and fires ONCE
 * - edits while a render is in flight coalesce into one follow-up commit
 * - a no-change Done fires nothing (no wasted render)
 */

import { act, renderHook } from "@testing-library/react";
import {
  buildEditPayload,
  useVariantEditSession,
  type EditDraft,
} from "@/lib/variant-editor/useVariantEditSession";
import type { EditVariantPayload, GenerativeVariant } from "@/lib/generative-api";

function makeVariant(over: Partial<GenerativeVariant> = {}): GenerativeVariant {
  return {
    variant_id: "song_text",
    rank: 1,
    text_mode: "agent_text",
    music_track_id: "t1",
    track_title: "Track",
    style_set_id: "travel_editorial",
    output_url: "https://x/out.mp4",
    video_path: "generative-jobs/j/v.mp4",
    render_status: "ready",
    ok: true,
    error: null,
    intro_text_size_px: 56,
    intro_size_source: "computed",
    intro_text: "original hook",
    base_video_url: "https://x/base.mp4?sig=1",
    ...over,
  } as GenerativeVariant;
}

const baselineDraft: EditDraft = {
  text: "original hook",
  removed: false,
  styleSetId: "travel_editorial",
  sizePx: 56,
  layout: null,
  fontFamily: null,
  animation: null,
  textColor: null,
};

describe("buildEditPayload", () => {
  it("sends only changed fields", () => {
    expect(
      buildEditPayload({ ...baselineDraft, styleSetId: "word_reveal" }, baselineDraft),
    ).toEqual({ style_set_id: "word_reveal" });
    expect(buildEditPayload({ ...baselineDraft, text: "new" }, baselineDraft)).toEqual({
      text: "new",
    });
    expect(buildEditPayload({ ...baselineDraft, sizePx: 70 }, baselineDraft)).toEqual({
      text_size_px: 70,
    });
  });

  it("maps removal to remove_text and ignores size while removed", () => {
    expect(
      buildEditPayload({ ...baselineDraft, removed: true, sizePx: 70 }, baselineDraft),
    ).toEqual({ remove_text: true });
  });

  it("re-adding text after removal sends the text even when unchanged", () => {
    const removedBaseline = { ...baselineDraft, removed: true };
    expect(
      buildEditPayload({ ...baselineDraft, removed: false }, removedBaseline),
    ).toEqual({ text: "original hook" });
  });

  it("returns empty payload for no changes", () => {
    expect(buildEditPayload(baselineDraft, baselineDraft)).toEqual({});
  });

  it("treats deleted-to-empty text as removal (no silent no-op)", () => {
    expect(buildEditPayload({ ...baselineDraft, text: "   " }, baselineDraft)).toEqual({
      remove_text: true,
    });
  });
});

describe("useVariantEditSession", () => {
  it("seeds the draft on enterEdit and tracks dirtiness", () => {
    const { result } = renderHook(() => useVariantEditSession(makeVariant(), jest.fn()));

    act(() => result.current.enterEdit());
    expect(result.current.isEditing).toBe(true);
    expect(result.current.draft.text).toBe("original hook");
    expect(result.current.isDirty).toBe(false);

    act(() => result.current.setText("typed live"));
    expect(result.current.isDirty).toBe(true);
  });

  it("polls do not clobber the draft while editing", () => {
    const { result, rerender } = renderHook(
      ({ v }) => useVariantEditSession(v, jest.fn()),
      { initialProps: { v: makeVariant() } },
    );

    act(() => result.current.enterEdit());
    act(() => result.current.setText("my edit"));

    // A poll delivers a refreshed variant (new signed URLs etc.).
    rerender({ v: makeVariant({ base_video_url: "https://x/base.mp4?sig=2" }) });
    expect(result.current.draft.text).toBe("my edit");
    expect(result.current.isEditing).toBe(true);
  });

  it("commits once with the combined minimal payload", async () => {
    const commits: EditVariantPayload[] = [];
    const onCommit = jest.fn(async (p: EditVariantPayload) => {
      commits.push(p);
    });
    const { result } = renderHook(() => useVariantEditSession(makeVariant(), onCommit));

    act(() => result.current.enterEdit());
    act(() => {
      result.current.setText("new text");
      result.current.setStyle("word_reveal");
      result.current.setSize(64);
    });
    await act(async () => result.current.commit());

    expect(commits).toEqual([
      { text: "new text", style_set_id: "word_reveal", text_size_px: 64 },
    ]);
    expect(result.current.isEditing).toBe(false);
    expect(result.current.isSaving).toBe(true); // preview stays up until ready
  });

  it("a no-change Done fires nothing", async () => {
    const onCommit = jest.fn(async () => {});
    const { result } = renderHook(() => useVariantEditSession(makeVariant(), onCommit));

    act(() => result.current.enterEdit());
    await act(async () => result.current.commit());

    expect(onCommit).not.toHaveBeenCalled();
    expect(result.current.isSaving).toBe(false);
  });

  it("settles when the committed render comes back ready", async () => {
    const onCommit = jest.fn(async () => {});
    const { result, rerender } = renderHook(
      ({ v }) => useVariantEditSession(v, onCommit),
      { initialProps: { v: makeVariant() } },
    );

    act(() => result.current.enterEdit());
    act(() => result.current.setText("new text"));
    await act(async () => result.current.commit());
    expect(result.current.isSaving).toBe(true);

    rerender({ v: makeVariant({ render_status: "rendering" }) });
    expect(result.current.isSaving).toBe(true);

    rerender({ v: makeVariant({ render_status: "ready", intro_text: "new text" }) });
    expect(result.current.isSaving).toBe(false);
    expect(result.current.isActive).toBe(false);
    expect(onCommit).toHaveBeenCalledTimes(1);
  });

  it("shows a brief 'Saved' state (not a blocking spinner) when a text edit settles, then recedes", async () => {
    jest.useFakeTimers();
    try {
      const onCommit = jest.fn(async () => {});
      const { result, rerender } = renderHook(
        ({ v }) => useVariantEditSession(v, onCommit),
        { initialProps: { v: makeVariant() } },
      );

      act(() => result.current.enterEdit());
      act(() => result.current.setText("new text"));
      await act(async () => result.current.commit());

      // The preview stays up while the reburn runs — no blocking spinner state
      // beyond the lightweight isSaving flag.
      rerender({ v: makeVariant({ render_status: "rendering" }) });
      expect(result.current.justSaved).toBe(false);

      // On settle: isSaving drops to false (non-blocking) and the quiet "Saved"
      // pulse engages — the live WYSIWYG preview is the result, not a flash to output.
      rerender({ v: makeVariant({ render_status: "ready", intro_text: "new text" }) });
      expect(result.current.isSaving).toBe(false);
      expect(result.current.justSaved).toBe(true);

      // The pulse recedes after a short beat (no lingering blocking state).
      act(() => {
        jest.advanceTimersByTime(2000);
      });
      expect(result.current.justSaved).toBe(false);
    } finally {
      jest.useRealTimers();
    }
  });

  it("isDirty resets to false on commit so LiveEditPreview can show the burned output_url at rest", async () => {
    // Contract that LiveEditPreview relies on: after commit() fires, isDirty → false
    // (because fireCommit calls setBaseline(toCommit)) and stays false through
    // settlement. Only a NEW keystroke makes it true again.
    const onCommit = jest.fn(async () => {});
    const { result, rerender } = renderHook(
      ({ v }) => useVariantEditSession(v, onCommit),
      { initialProps: { v: makeVariant() } },
    );

    act(() => result.current.enterEdit());
    act(() => result.current.setText("edited hook"));
    expect(result.current.isDirty).toBe(true); // unsaved edit → DOM overlay shown

    await act(async () => result.current.commit());
    expect(result.current.isDirty).toBe(false); // baseline = committed draft → burned output eligible
    expect(result.current.isSaving).toBe(true); // render still in flight

    rerender({ v: makeVariant({ render_status: "rendering" }) });
    rerender({ v: makeVariant({ render_status: "ready", intro_text: "edited hook" }) });
    expect(result.current.isDirty).toBe(false); // still clean → burned output shown
    expect(result.current.isSaving).toBe(false); // settled

    // A new edit switches back to DOM overlay mode.
    act(() => result.current.enterEdit());
    act(() => result.current.setText("another change"));
    expect(result.current.isDirty).toBe(true);
  });

  it("does NOT pulse 'Saved' when a render fails (failure path is preserved)", async () => {
    const onCommit = jest.fn(async () => {});
    const { result, rerender } = renderHook(
      ({ v }) => useVariantEditSession(v, onCommit),
      { initialProps: { v: makeVariant() } },
    );

    act(() => result.current.enterEdit());
    act(() => result.current.setText("doomed"));
    await act(async () => result.current.commit());
    rerender({ v: makeVariant({ render_status: "failed", error_class: "render_error" }) });

    expect(result.current.justSaved).toBe(false);
    expect(result.current.isActive).toBe(false);
  });

  it("coalesces edits made while a render is in flight into ONE follow-up", async () => {
    const commits: EditVariantPayload[] = [];
    const onCommit = jest.fn(async (p: EditVariantPayload) => {
      commits.push(p);
    });
    const { result, rerender } = renderHook(
      ({ v }) => useVariantEditSession(v, onCommit),
      { initialProps: { v: makeVariant() } },
    );

    act(() => result.current.enterEdit());
    act(() => result.current.setText("first"));
    await act(async () => result.current.commit());
    rerender({ v: makeVariant({ render_status: "rendering" }) });

    // User edits again mid-render and hits Done — must queue, not fire.
    act(() => result.current.enterEdit());
    act(() => result.current.setText("second"));
    await act(async () => result.current.commit());
    expect(commits).toHaveLength(1);

    // First render completes → the queued edit fires exactly once.
    await act(async () => {
      rerender({ v: makeVariant({ render_status: "ready", intro_text: "first" }) });
    });
    expect(commits).toHaveLength(2);
    expect(commits[1]).toEqual({ text: "second" });
  });

  it("does NOT retry-storm on commit failure — one call, error surfaced, editor reopens", async () => {
    // Regression: the old failure path queued the draft as pending while the
    // render fingerprint still looked fresh → the watcher refired immediately
    // → unbounded request loop during an API outage (26 POSTs in one flush).
    const onCommit = jest.fn(async () => {
      throw new Error("boom");
    });
    const { result, rerender } = renderHook(
      ({ v }) => useVariantEditSession(v, onCommit),
      { initialProps: { v: makeVariant({ render_finished_at: "2026-06-09T00:00:00Z" }) } },
    );

    act(() => result.current.enterEdit());
    act(() => result.current.setText("new text"));
    await act(async () => result.current.commit());

    // Several poll cycles pass — no further attempts may fire.
    rerender({
      v: makeVariant({ render_finished_at: "2026-06-09T00:00:00Z", output_url: "https://x/2" }),
    });
    rerender({
      v: makeVariant({ render_finished_at: "2026-06-09T00:00:00Z", output_url: "https://x/3" }),
    });

    expect(onCommit).toHaveBeenCalledTimes(1);
    expect(result.current.commitError).toBe("boom");
    expect(result.current.isEditing).toBe(true); // editor reopened, draft intact
    expect(result.current.draft.text).toBe("new text");
    expect(result.current.isSaving).toBe(false);
  });

  it("commit during an EXTERNAL render queues and stays in saving state", async () => {
    // Regression: a render started elsewhere (admin page / second tab) while
    // editing meant the queued draft stranded silently — awaitingRender never
    // engaged the watcher and the session closed.
    const commits: EditVariantPayload[] = [];
    const onCommit = jest.fn(async (p: EditVariantPayload) => {
      commits.push(p);
    });
    const { result, rerender } = renderHook(
      ({ v }) => useVariantEditSession(v, onCommit),
      { initialProps: { v: makeVariant() } },
    );

    act(() => result.current.enterEdit());
    // External actor flips the variant to rendering mid-edit.
    rerender({ v: makeVariant({ render_status: "rendering" }) });
    act(() => result.current.setText("queued edit"));
    await act(async () => result.current.commit());

    expect(commits).toHaveLength(0);
    expect(result.current.isSaving).toBe(true); // watcher engaged

    await act(async () => {
      rerender({
        v: makeVariant({ render_status: "ready", render_finished_at: "2026-06-09T01:00:00Z" }),
      });
    });
    expect(commits).toEqual([{ text: "queued edit" }]);
  });

  it("drops out of the preview when the committed render fails", async () => {
    const onCommit = jest.fn(async () => {});
    const { result, rerender } = renderHook(
      ({ v }) => useVariantEditSession(v, onCommit),
      { initialProps: { v: makeVariant() } },
    );

    act(() => result.current.enterEdit());
    act(() => result.current.setText("doomed"));
    await act(async () => result.current.commit());

    rerender({ v: makeVariant({ render_status: "failed", error_class: "render_error" }) });
    expect(result.current.isActive).toBe(false); // failure card takes over
  });
});
