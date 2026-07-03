"use client";

/**
 * EditorShell — the full-screen TikTok-parity editor at
 * /plan/items/[id]/edit?variant=<id> (plan §1, approved mockup Variant A).
 *
 * Full-viewport grid: 56px top bar / minmax(480px,1fr) canvas row / 260px
 * timeline region. Middle row: ToolRail · ToolDrawer · canvas · InspectorPanel
 * (~320px, PERMANENTLY reserved — the canvas never reflows on select/deselect,
 * D6) · InspectorRail (~72px).
 *
 * First paint: drawer closed, no selection, inspector empty state, Select
 * tool active, playhead 0:00, video paused on frame 0.
 *
 * Working state = local reducer bars (text-timeline-reducer) + title. No
 * mid-edit server writes; Save persists once via commitEditorSession
 * (lib/editor-commit.ts — endpoint lands with the API task; a local 404
 * surfaces as the quiet retry notice and working state is preserved).
 */

import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import {
  getPlanItem,
  getPlanItemJobStatus,
  NotAuthenticatedError,
  type PlanItem,
  type PlanItemVariant,
  type TextElement,
} from "@/lib/plan-api";
import {
  commitEditorSession,
  EditorCommitConflictError,
} from "@/lib/editor-commit";
import { FONT_FACES } from "@/lib/font-faces";
import { DEFAULT_TEXT_PRESET, TEXT_PRESETS, type TextPreset } from "@/lib/text-presets";
import {
  initTextEditorState,
  textReducer,
  type TextElementBar,
} from "@/lib/timeline/text-timeline-reducer";
import { InkButton } from "@/components/ui/InkButton";
import { ConfirmDialog } from "@/components/ui/ConfirmDialog";
import { barsToTextElements, seedBarsFromVariant } from "./editor-bars";
import EditorCanvas from "./EditorCanvas";
import InspectorPanel from "./InspectorPanel";
import InspectorRail, { type InspectorTab } from "./InspectorRail";
import ToolDrawer from "./ToolDrawer";
import ToolRail, { type EditorTool } from "./ToolRail";
import { presetMatchesFields } from "./PresetGrid";
import {
  deleteKeyAllowed,
  escapeAction,
  useEditorSelection,
} from "./useEditorSelection";

const ZOOM_OPTIONS = [100, 125, 150] as const;

/** Default duration + look of a freshly added text bar (plan §2). */
const NEW_TEXT_DURATION_S = 2.0;
const NEW_TEXT_CONTENT = "Add a title";
const NEW_TEXT_Y_FRAC = 0.4;
const NEW_TEXT_SIZE_PX = 64;

export default function EditorShell({
  itemId,
  variantParam,
}: {
  itemId: string;
  variantParam: string | null;
}) {
  const router = useRouter();

  // ── Data ────────────────────────────────────────────────────────────────────
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [needsAuth, setNeedsAuth] = useState(false);
  const [item, setItem] = useState<PlanItem | null>(null);
  const [variants, setVariants] = useState<PlanItemVariant[]>([]);
  const [loadNonce, setLoadNonce] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setLoadError(null);
    (async () => {
      try {
        const it = await getPlanItem(itemId);
        const job = it.current_job_id
          ? await getPlanItemJobStatus(it.current_job_id)
          : null;
        if (cancelled) return;
        setItem(it);
        setVariants(job?.variants ?? []);
        setLoading(false);
      } catch (err) {
        if (cancelled) return;
        if (err instanceof NotAuthenticatedError) setNeedsAuth(true);
        else setLoadError(err instanceof Error ? err.message : "Couldn't load this video.");
        setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [itemId, loadNonce]);

  const variant = useMemo(() => {
    if (variants.length === 0) return null;
    return (
      variants.find((v) => v.variant_id === variantParam) ??
      variants.find((v) => v.output_url || v.base_video_url) ??
      variants[0]
    );
  }, [variants, variantParam]);

  // ── Working state ───────────────────────────────────────────────────────────
  const [state, dispatch] = useReducer(textReducer, initTextEditorState([]));
  // Originals by id — Save merges bar edits OVER these so fields the editor
  // doesn't model (reveal_s, word_timings, …) survive untouched.
  const originalsRef = useRef<Map<string, TextElement>>(new Map());
  const seededVariantIdRef = useRef<string | null>(null);
  const [title, setTitle] = useState("");

  useEffect(() => {
    if (!variant || seededVariantIdRef.current === variant.variant_id) return;
    seededVariantIdRef.current = variant.variant_id;
    originalsRef.current = new Map(
      (variant.text_elements ?? []).map((el) => [el.id, el]),
    );
    dispatch({ type: "RESET", bars: seedBarsFromVariant(variant) });
  }, [variant]);

  const dirty = state.past.length > 0 || title.trim() !== "";

  // ── View state ──────────────────────────────────────────────────────────────
  const { selection, select, clear } = useEditorSelection();
  const [activeTool, setActiveTool] = useState<EditorTool | null>(null); // drawer CLOSED at first paint
  const [inspectorTab, setInspectorTab] = useState<InspectorTab>("basic");
  const [canvasTool, setCanvasTool] = useState<"select" | "pan">("select");
  const [zoomPct, setZoomPct] = useState<number>(100);
  const [currentTime, setCurrentTime] = useState(0);
  const [duration, setDuration] = useState(0);
  const videoRef = useRef<HTMLVideoElement>(null);
  const contentRef = useRef<HTMLTextAreaElement>(null);

  // ── Save / cancel state ─────────────────────────────────────────────────────
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [confirmLeave, setConfirmLeave] = useState(false);

  // ── Derived ─────────────────────────────────────────────────────────────────
  const elements = useMemo(
    () => barsToTextElements(state.bars, originalsRef.current),
    [state.bars],
  );

  const selectedBar = useMemo(
    () =>
      selection?.kind === "text"
        ? (state.bars.find((b) => b.id === selection.id) ?? null)
        : null,
    [selection, state.bars],
  );

  // Selection on a deleted/vanished bar clears itself.
  useEffect(() => {
    if (selection?.kind === "text" && !state.bars.some((b) => b.id === selection.id)) {
      clear();
    }
  }, [selection, state.bars, clear]);

  const sampleWord = useMemo(() => {
    const first = selectedBar?.text.trim().split(/\s+/)[0];
    return first && first.length > 0 ? first.slice(0, 8).toUpperCase() : null;
  }, [selectedBar]);

  // "Applied" is DERIVED (field comparison), not bookkept — a preset ring
  // stays honest even after manual tweaks diverge from the preset.
  const appliedPresetId = useMemo(() => {
    if (!selectedBar) return null;
    return TEXT_PRESETS.find((p) => presetMatchesFields(p, selectedBar))?.id ?? null;
  }, [selectedBar]);

  // ── Actions ─────────────────────────────────────────────────────────────────

  const selectText = useCallback(
    (id: string) => {
      select("text", id);
      setInspectorTab("basic"); // selecting anything activates + switches to Basic (D6)
    },
    [select],
  );

  const patchBar = useCallback(
    (id: string, patch: Partial<Omit<TextElementBar, "id" | "role">>) => {
      dispatch({ type: "PATCH_BAR", id, patch });
    },
    [],
  );

  const focusContent = useCallback(() => {
    // Double-click contract: focus the inspector textarea with select-all.
    // Deferred a frame so the inspector has populated for a fresh selection.
    requestAnimationFrame(() => {
      contentRef.current?.focus();
      contentRef.current?.select();
    });
  }, []);

  const addTextAtPlayhead = useCallback(
    (preset: TextPreset = DEFAULT_TEXT_PRESET) => {
      const start = Math.max(0, Math.round(currentTime * 10) / 10);
      const end =
        duration > 0
          ? Math.min(duration, start + NEW_TEXT_DURATION_S)
          : start + NEW_TEXT_DURATION_S;
      const bar: TextElementBar = {
        id: crypto.randomUUID(),
        text: NEW_TEXT_CONTENT,
        start_s: start,
        end_s: Math.max(end, start + 0.5),
        role: "generative_intro",
        x_frac: 0.5,
        y_frac: NEW_TEXT_Y_FRAC,
        position: "custom",
        size_px: NEW_TEXT_SIZE_PX,
        alignment: "center",
        font_family: preset.fields.font_family ?? undefined,
        color: preset.fields.color ?? undefined,
        highlight_color: preset.fields.highlight_color ?? undefined,
        stroke_width: preset.fields.stroke_width ?? undefined,
        effect: preset.fields.effect ?? undefined,
      };
      dispatch({ type: "ADD_TEXT", bar });
      selectText(bar.id);
    },
    [currentTime, duration, selectText],
  );

  const pickPreset = useCallback(
    (preset: TextPreset) => {
      if (selectedBar) {
        // Apply to the selected element.
        patchBar(selectedBar.id, {
          font_family: preset.fields.font_family ?? undefined,
          color: preset.fields.color ?? undefined,
          highlight_color: preset.fields.highlight_color ?? undefined,
          stroke_width: preset.fields.stroke_width ?? 0,
          effect: preset.fields.effect ?? undefined,
        });
      } else {
        // No selection → create a text element at the playhead with this
        // preset and select it (D6).
        addTextAtPlayhead(preset);
      }
    },
    [selectedBar, patchBar, addTextAtPlayhead],
  );

  const deleteSelected = useCallback(() => {
    if (selection?.kind !== "text") return;
    dispatch({ type: "DELETE_BAR", id: selection.id });
    clear();
  }, [selection, clear]);

  // ── Keyboard: Escape ladder + Delete with focus guard (plan §5/§9) ──────────
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        const target = e.target as HTMLElement | null;
        // One press, one effect: leaving a text field is that effect.
        if (target && !deleteKeyAllowed(target)) {
          target.blur();
          return;
        }
        const action = escapeAction({
          drawerOpen: activeTool !== null,
          hasSelection: selection !== null,
        });
        if (action === "close-drawer") setActiveTool(null);
        else if (action === "clear-selection") clear();
      } else if (e.key === "Delete" || e.key === "Backspace") {
        if (!deleteKeyAllowed(e.target as HTMLElement | null)) return;
        if (selection?.kind === "text") {
          e.preventDefault();
          deleteSelected();
        }
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [activeTool, selection, clear, deleteSelected]);

  // ── Save / leave ────────────────────────────────────────────────────────────

  const handleSave = useCallback(async () => {
    if (!variant || saving) return;
    setSaving(true);
    setSaveError(null);
    try {
      await commitEditorSession(itemId, variant.variant_id, {
        text_elements: barsToTextElements(state.bars, originalsRef.current),
        title: title.trim() !== "" ? title.trim() : null,
        base_generation: variant.render_finished_at ?? null,
      });
      // "Saved — rendering your latest version" lives on the item page hero.
      router.push(`/plan/items/${itemId}`);
    } catch (err) {
      setSaving(false);
      if (err instanceof EditorCommitConflictError) setSaveError(err.message);
      else setSaveError(err instanceof Error ? err.message : "Couldn't save your edits.");
    }
  }, [variant, saving, itemId, state.bars, title, router]);

  const requestLeave = useCallback(() => {
    if (dirty) setConfirmLeave(true);
    else router.push(`/plan/items/${itemId}`);
  }, [dirty, router, itemId]);

  // ── Render ──────────────────────────────────────────────────────────────────

  if (needsAuth) {
    return (
      <Frame>
        <div className="flex flex-1 items-center justify-center">
          <p className="text-sm text-[#3f3f46]">
            Please{" "}
            <a href="/api/auth/signin" className="underline underline-offset-4">
              sign in
            </a>{" "}
            to edit this video.
          </p>
        </div>
      </Frame>
    );
  }

  if (loading) {
    return (
      <Frame>
        <div className="grid min-h-0 flex-1 grid-cols-[92px_1fr_320px_72px]">
          <div className="border-r border-zinc-200 bg-white" />
          <div className="flex items-center justify-center">
            <div className="h-[70%] w-auto rounded-xl border border-zinc-200 bg-zinc-100 motion-safe:animate-pulse" style={{ aspectRatio: "9 / 16" }} />
          </div>
          <div className="border-l border-zinc-200 bg-white" />
          <div className="border-l border-zinc-200 bg-white" />
        </div>
        <div className="h-[260px] border-t border-zinc-200 bg-white" />
      </Frame>
    );
  }

  if (loadError || !variant) {
    return (
      <Frame>
        <div className="flex flex-1 items-center justify-center p-8">
          <div className="max-w-[420px] rounded-xl border border-dashed border-zinc-300 bg-white p-6 text-center">
            <p className="text-sm text-[#3f3f46]">
              {loadError ?? "This video doesn't have an editable version yet."}
            </p>
            <div className="mt-4 flex items-center justify-center gap-3">
              {loadError && (
                <button
                  type="button"
                  onClick={() => setLoadNonce((n) => n + 1)}
                  className="rounded-full border border-zinc-200 px-4 py-1.5 text-[13px] text-[#3f3f46] hover:border-zinc-400"
                >
                  Retry
                </button>
              )}
              <button
                type="button"
                onClick={() => router.push(`/plan/items/${itemId}`)}
                className="rounded-full bg-[#0c0c0e] px-4 py-1.5 text-[13px] font-semibold text-white hover:opacity-80"
              >
                Back to the video
              </button>
            </div>
          </div>
        </div>
      </Frame>
    );
  }

  return (
    <div className="fixed inset-0 z-50 grid grid-rows-[56px_minmax(480px,1fr)_260px] overflow-hidden bg-[#fafaf8]">
      <style dangerouslySetInnerHTML={{ __html: FONT_FACES }} />

      {/* ── Top bar (plan §1) ── */}
      <header className="flex items-center border-b border-zinc-200 bg-white px-4">
        <div className="flex flex-1 items-center gap-3">
          <button
            type="button"
            aria-label="Back to the video page"
            onClick={requestLeave}
            className="flex h-8 w-8 items-center justify-center rounded-full border border-zinc-200 pb-0.5 text-[15px] text-[#3f3f46] hover:border-zinc-400 focus-visible:outline-2 focus-visible:outline-[#0c0c0e]"
          >
            ‹
          </button>
          <input
            type="text"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            placeholder="add title for your video"
            aria-label="Video title"
            className="w-[240px] rounded-md border border-transparent bg-transparent px-2 py-1 text-[13px] text-[#0c0c0e] placeholder:text-[#a1a1aa] focus:border-zinc-200 focus:bg-white focus:outline-none"
          />
        </div>

        {/* Center cluster — visually quiet; ink chip only on the active tool */}
        <div className="flex items-center gap-1.5">
          <button
            type="button"
            aria-pressed={canvasTool === "select"}
            aria-label="Select tool"
            title="Select"
            onClick={() => setCanvasTool("select")}
            className={`flex h-8 w-8 items-center justify-center rounded-lg text-[13px] ${
              canvasTool === "select"
                ? "bg-[#0c0c0e] text-white"
                : "text-[#3f3f46] hover:bg-zinc-100"
            }`}
          >
            ➤
          </button>
          <button
            type="button"
            aria-pressed={canvasTool === "pan"}
            aria-label="Pan tool"
            title="Pan (when zoomed in)"
            onClick={() => setCanvasTool("pan")}
            className={`flex h-8 w-8 items-center justify-center rounded-lg text-[13px] ${
              canvasTool === "pan" ? "bg-[#0c0c0e] text-white" : "text-[#3f3f46] hover:bg-zinc-100"
            }`}
          >
            ✋
          </button>
          {/* Undo/redo: no-op stubs — the unified history task wires these. */}
          <button
            type="button"
            aria-label="Undo"
            title="Undo arrives with a later update"
            disabled
            className="flex h-8 w-8 items-center justify-center rounded-lg text-[14px] text-[#3f3f46] disabled:opacity-40"
          >
            ↺
          </button>
          <button
            type="button"
            aria-label="Redo"
            title="Redo arrives with a later update"
            disabled
            className="flex h-8 w-8 items-center justify-center rounded-lg text-[14px] text-[#3f3f46] disabled:opacity-40"
          >
            ↻
          </button>
          <select
            aria-label="Canvas zoom"
            value={zoomPct}
            onChange={(e) => setZoomPct(Number(e.target.value))}
            className="ml-1 h-8 rounded-lg border border-zinc-200 bg-white px-2 text-[12px] text-[#3f3f46] focus:border-lime-500/60 focus:outline-none"
          >
            {ZOOM_OPTIONS.map((z) => (
              <option key={z} value={z}>
                {z}%
              </option>
            ))}
          </select>
        </div>

        <div className="flex flex-1 items-center justify-end gap-2">
          {saveError && (
            <span className="max-w-[280px] truncate rounded-lg border border-zinc-200 bg-white px-3 py-1.5 text-[12px] text-[#3f3f46]">
              {saveError}
            </span>
          )}
          <InkButton variant="ghost" className="text-[13px]" onClick={requestLeave}>
            Cancel
          </InkButton>
          <InkButton
            className="px-6 py-2.5 text-[13px]"
            disabled={!dirty || saving}
            onClick={() => void handleSave()}
          >
            {saving ? "Saving…" : "Save"}
          </InkButton>
        </div>
      </header>

      {/* ── Middle row: rail · drawer · canvas · inspector · edge rail ── */}
      <div className="grid min-h-0 grid-cols-[auto_auto_1fr_auto_auto]">
        <ToolRail
          activeTool={activeTool}
          onToggleTool={(tool) => setActiveTool((cur) => (cur === tool ? null : tool))}
        />
        {activeTool !== null ? (
          <ToolDrawer
            tool={activeTool}
            sampleWord={sampleWord}
            appliedPresetId={appliedPresetId}
            onAddText={() => addTextAtPlayhead()}
            onPickPreset={pickPreset}
            onClose={() => setActiveTool(null)}
          />
        ) : (
          <div />
        )}
        <EditorCanvas
          variant={variant}
          elements={elements}
          bars={state.bars}
          selectedTextId={selection?.kind === "text" ? selection.id : null}
          currentTime={currentTime}
          zoomPct={zoomPct}
          tool={canvasTool}
          videoRef={videoRef}
          onSelectText={selectText}
          onClearSelection={clear}
          onPatchBar={patchBar}
          onFocusContent={focusContent}
          onTimeUpdate={setCurrentTime}
          onDuration={setDuration}
        />
        <InspectorPanel
          selection={selection}
          bar={selectedBar}
          tab={selection === null && inspectorTab === "basic" ? "basic" : inspectorTab}
          sampleWord={sampleWord}
          appliedPresetId={appliedPresetId}
          contentRef={contentRef}
          onEditText={(text) => {
            if (selectedBar) dispatch({ type: "EDIT_TEXT", id: selectedBar.id, text });
          }}
          onPatch={(patch) => {
            if (selectedBar) patchBar(selectedBar.id, patch);
          }}
          onClose={clear}
          onPickPreset={pickPreset}
        />
        <InspectorRail
          tab={inspectorTab}
          hasSelection={selection !== null}
          onTab={setInspectorTab}
        />
      </div>

      {/* ── Timeline region (260px) — the multi-track timeline lands with the
             timeline task; the selection store above is what it will consume. ── */}
      <div
        data-region="timeline"
        className="flex items-center justify-center border-t border-zinc-200 bg-white"
      >
        <p className="text-[12px] text-[#a1a1aa]">
          Timeline editing arrives with the next update
        </p>
      </div>

      <ConfirmDialog
        open={confirmLeave}
        question="Discard your edits?"
        detail="Your changes haven't been saved. Leaving now throws them away."
        confirmLabel="Discard"
        cancelLabel="Keep editing"
        onConfirm={() => {
          setConfirmLeave(false);
          router.push(`/plan/items/${itemId}`);
        }}
        onCancel={() => setConfirmLeave(false)}
      />
    </div>
  );
}

/** Chrome-less frame for loading / error / auth states (keeps the shell's
 * grid footprint so the transition to the loaded editor doesn't jump). */
function Frame({ children }: { children: React.ReactNode }) {
  return (
    <div className="fixed inset-0 z-50 flex flex-col overflow-hidden bg-[#fafaf8]">
      <div className="h-14 flex-none border-b border-zinc-200 bg-white" />
      {children}
    </div>
  );
}
