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
import { type GenerativeStyleSet } from "@/lib/generative-api";
import { formatTimecode } from "@/lib/timeline/time-format";
import { DEFAULT_TEXT_PRESET, TEXT_PRESETS, type TextPreset } from "@/lib/text-presets";
import {
  initTextEditorState,
  textReducer,
  type TextElementBar,
} from "@/lib/timeline/text-timeline-reducer";
import { InkButton } from "@/components/ui/InkButton";
import { ConfirmDialog } from "@/components/ui/ConfirmDialog";
import { useFocusTrap } from "@/components/ui/useFocusTrap";
import UnifiedTimeline from "@/app/plan/_components/UnifiedTimeline";
import { useClipTimeline } from "@/app/plan/_components/useClipTimeline";
import { type DraftSlot } from "@/app/generative/timeline-math";
import { barsToTextElements, seedBarsFromVariant } from "./editor-bars";
import { splitSlotAt, deleteSlotEnforceFloor, activeSlotCount } from "./slot-split";
import TransportBar from "./TransportBar";
import type { EditorTimelineBodyProps } from "./EditorTimelineBody";
import EditorCanvas from "./EditorCanvas";
import InspectorPanel from "./InspectorPanel";
import InspectorRail, { type InspectorTab } from "./InspectorRail";
import ToolDrawer from "./ToolDrawer";
import ToolRail, { type EditorTool } from "./ToolRail";
import PresetGrid, { presetMatchesFields } from "./PresetGrid";
import { useEditorLayoutMode } from "./useEditorLayoutMode";
import {
  deleteKeyAllowed,
  escapeAction,
  nudgeBarStart,
  type EditorSelectionKind,
  useEditorSelection,
} from "./useEditorSelection";
import {
  draftKey,
  deserializeDraft,
  serializeDraft,
  useEditorHistory,
  type EditorDocument,
} from "./useEditorHistory";

const ZOOM_OPTIONS = [100, 125, 150] as const;

/** Default duration + look of a freshly added text bar (plan §2). */
const NEW_TEXT_DURATION_S = 2.0;
const NEW_TEXT_CONTENT = "Add a title";
const NEW_TEXT_Y_FRAC = 0.4;
const NEW_TEXT_SIZE_PX = 64;

function spaceShortcutAllowed(target: HTMLElement | null): boolean {
  if (!deleteKeyAllowed(target)) return false;
  return (target?.tagName ?? "").toUpperCase() !== "BUTTON";
}

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
  // Last style-set applied via restyle-all — drives the StyleChip ring.
  const [appliedStyleSetId, setAppliedStyleSetId] = useState<string | null>(null);

  useEffect(() => {
    if (!variant || seededVariantIdRef.current === variant.variant_id) return;
    seededVariantIdRef.current = variant.variant_id;
    originalsRef.current = new Map(
      (variant.text_elements ?? []).map((el) => [el.id, el]),
    );
    dispatch({ type: "RESET", bars: seedBarsFromVariant(variant) });
    setAppliedStyleSetId(null);
  }, [variant]);

  // ── View state ──────────────────────────────────────────────────────────────
  const layoutMode = useEditorLayoutMode();
  const { selection, select, clear } = useEditorSelection();
  const [activeTool, setActiveTool] = useState<EditorTool | null>(null); // drawer CLOSED at first paint
  const [inspectorTab, setInspectorTab] = useState<InspectorTab>("basic");
  const [lightSheetOpen, setLightSheetOpen] = useState(false);
  const [canvasTool, setCanvasTool] = useState<"select" | "pan">("select");
  const [zoomPct, setZoomPct] = useState<number>(100);
  const [currentTime, setCurrentTime] = useState(0);
  const [duration, setDuration] = useState(0);
  const videoRef = useRef<HTMLVideoElement>(null);
  const contentRef = useRef<HTMLTextAreaElement>(null);

  // ── Timeline view state (plan §6) ───────────────────────────────────────────
  const [playing, setPlaying] = useState(false);
  const [zoom, setZoom] = useState(1); // 1 = fit-to-width
  const [videoMuted, setVideoMuted] = useState(false);
  const [soundMuted, setSoundMuted] = useState(false);
  const [toast, setToast] = useState<string | null>(null);
  const [timelineDirty, setTimelineDirty] = useState(false);

  // Clip slots — the shell's local working state for split/delete (seeded from
  // the shared clip-timeline handle, then edited locally; persisted via
  // editor-commit `timeline_slots`).
  const clip = useClipTimeline(itemId, variant?.variant_id ?? "", "plan-item");
  const [localSlots, setLocalSlots] = useState<DraftSlot[] | null>(null);
  const slotsSeededRef = useRef<string | null>(null);
  useEffect(() => {
    if (clip.loadState !== "ready") return;
    if (slotsSeededRef.current === variant?.variant_id) return;
    slotsSeededRef.current = variant?.variant_id ?? null;
    setLocalSlots(clip.state.slots.map((s) => ({ ...s })));
    setTimelineDirty(false);
  }, [clip.loadState, clip.state.slots, variant]);
  const slots = localSlots ?? clip.state.slots;

  // Toast auto-clear.
  useEffect(() => {
    if (!toast) return;
    const t = window.setTimeout(() => setToast(null), 2600);
    return () => window.clearTimeout(t);
  }, [toast]);

  // Live audio: mute the preview element when either channel is toggled off
  // (the preview is a single mixed element; the render honors the split via mix).
  useEffect(() => {
    if (videoRef.current) videoRef.current.muted = videoMuted || soundMuted;
  }, [videoMuted, soundMuted, variant]);

  // ── Read-only capability gate (plan §9 / E4) ────────────────────────────────
  // A variant whose editor_capabilities are ALL false is read-only: banner +
  // Save disabled + every mutating command no-ops. The server's honest reason
  // is surfaced verbatim.
  const capabilities = (variant as unknown as {
    editor_capabilities?: {
      text_elements?: boolean;
      timeline?: boolean;
      split_clips?: boolean;
      mix?: boolean;
      reason?: string;
    };
  } | null)?.editor_capabilities;
  const readOnly =
    !!capabilities &&
    capabilities.text_elements === false &&
    capabilities.timeline === false &&
    capabilities.split_clips === false &&
    capabilities.mix === false;
  const readOnlyReason =
    capabilities?.reason ?? "This version can't be edited.";

  // ── Unified undo/redo (plan §7, task T8) ────────────────────────────────────
  const getCurrent = useCallback(
    (): EditorDocument => ({
      bars: state.bars,
      slots: localSlots,
      videoMuted,
      soundMuted,
      title,
    }),
    [state.bars, localSlots, videoMuted, soundMuted, title],
  );

  const applyDocument = useCallback(
    (doc: EditorDocument) => {
      const beforeIds = new Set(state.bars.map((b) => b.id));
      dispatch({ type: "RESET", bars: doc.bars });
      setLocalSlots(doc.slots);
      setVideoMuted(doc.videoMuted);
      setSoundMuted(doc.soundMuted);
      setTitle(doc.title);
      // Undo of a delete (or redo of an add) resurrects a bar → re-select it
      // (plan §5 — the one selection rule that reaches into undo).
      const resurrected = doc.bars.find((b) => !beforeIds.has(b.id));
      if (resurrected) {
        select("text", resurrected.id);
        setInspectorTab("basic");
      }
    },
    [state.bars, select],
  );

  const history = useEditorHistory({ getCurrent, apply: applyDocument });

  // Every mutation (text, slots, mutes, title) records into the stack, so the
  // stack IS the dirty signal — and Save's history.clear() makes it read clean
  // (so the draft mirror doesn't immediately re-write the just-saved state).
  const dirty = history.canUndo || history.canRedo;

  // ── Save / cancel state ─────────────────────────────────────────────────────
  // saveState: idle → saving → {conflict | error | partial} (all preserve
  // working state); full success navigates away.
  const [saveState, setSaveState] = useState<
    "idle" | "saving" | "conflict" | "error" | "partial"
  >("idle");
  const [saveMessage, setSaveMessage] = useState<string | null>(null);
  const saving = saveState === "saving";
  const [confirmLeave, setConfirmLeave] = useState(false);
  // Resume-draft notice (plan §9 crash recovery). Non-null → show the notice.
  const [draftDoc, setDraftDoc] = useState<EditorDocument | null>(null);

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
      setLightSheetOpen(false);
    }
  }, [selection, state.bars, clear]);

  useEffect(() => {
    if (layoutMode === "light") {
      setActiveTool(null);
      setCanvasTool("select");
    } else {
      setLightSheetOpen(false);
    }
  }, [layoutMode]);

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

  const selectElement = useCallback(
    (kind: EditorSelectionKind, id: string) => {
      select(kind, id);
      if (layoutMode === "overlay") setActiveTool(null);
      if (kind === "text") {
        setInspectorTab("basic"); // selecting anything activates + switches to Basic (D6)
        if (layoutMode === "light") setLightSheetOpen(true);
      }
    },
    [layoutMode, select],
  );

  const selectText = useCallback(
    (id: string) => selectElement("text", id),
    [selectElement],
  );

  const patchBar = useCallback(
    (id: string, patch: Partial<Omit<TextElementBar, "id" | "role">>) => {
      if (readOnly) return;
      history.record();
      dispatch({ type: "PATCH_BAR", id, patch });
    },
    [readOnly, history],
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
      if (readOnly) return;
      history.record();
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
    [currentTime, duration, selectText, readOnly, history],
  );

  // Restyle ALL text bars with a style set — ONE undoable command with instant
  // canvas update (plan §2 Styles v1, task wiring). record() once, then patch
  // every bar (each PATCH_BAR is a reducer dispatch; the single record collapses
  // them into one undo step).
  const restyleAll = useCallback(
    (styleSet: GenerativeStyleSet) => {
      if (readOnly) return;
      if (state.bars.length === 0) {
        setToast("Add text first, then apply a style.");
        return;
      }
      const patch: Partial<Omit<TextElementBar, "id" | "role">> = {
        font_family: styleSet.font_family ?? styleSet.intro?.font_family ?? undefined,
        color: styleSet.text_color ?? styleSet.intro?.text_color ?? undefined,
        highlight_color:
          styleSet.highlight_color ?? styleSet.intro?.highlight_color ?? undefined,
        stroke_width: styleSet.intro?.stroke_width ?? undefined,
        effect: styleSet.effect ?? styleSet.intro?.effect ?? undefined,
      };
      history.record();
      state.bars.forEach((b) => dispatch({ type: "PATCH_BAR", id: b.id, patch }));
      setAppliedStyleSetId(styleSet.id);
    },
    [readOnly, state.bars, history],
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

  // Clip-split capability gate (plan §7): missing capabilities → allowed for
  // montage agent_text variants (song_text / original_text), disabled otherwise.
  const splitClipsAllowed =
    capabilities?.split_clips !== undefined
      ? capabilities.split_clips !== false
      : variant?.text_mode === "agent_text";

  const deleteSelected = useCallback(() => {
    if (!selection || readOnly) return;
    if (selection.kind === "text") {
      history.record();
      dispatch({ type: "DELETE_BAR", id: selection.id });
      clear();
    } else if (selection.kind === "clip") {
      const res = deleteSlotEnforceFloor(slots, selection.id);
      if (res.didDelete) {
        history.record();
        setLocalSlots(res.slots);
        setTimelineDirty(true);
        clear();
      } else {
        setToast("Keep at least one clip.");
      }
    }
  }, [selection, clear, slots, readOnly, history]);

  const splitAtPlayhead = useCallback(() => {
    if (!selection || readOnly) return;
    if (selection.kind === "text") {
      // Guard before recording so an out-of-bounds split (reducer no-op) never
      // pushes a spurious undo step.
      const bar = state.bars.find((b) => b.id === selection.id);
      if (!bar) return;
      const at = Math.round(currentTime * 10) / 10;
      const MIN = 0.2;
      if (at <= bar.start_s + MIN - 1e-9 || at >= bar.end_s - MIN + 1e-9) {
        setToast("Move the playhead over the text to split it.");
        return;
      }
      history.record();
      dispatch({
        type: "SPLIT_BAR",
        id: selection.id,
        at_s: currentTime,
        newId: crypto.randomUUID(),
      });
    } else if (selection.kind === "clip") {
      if (!splitClipsAllowed) return;
      const res = splitSlotAt(
        slots,
        clip.state.grid,
        selection.id,
        currentTime,
        `split-${crypto.randomUUID()}`,
      );
      if (res.didSplit) {
        history.record();
        setLocalSlots(res.slots);
        setTimelineDirty(true);
      } else {
        setToast("Move the playhead over the clip to split it.");
      }
    }
  }, [
    selection,
    currentTime,
    slots,
    clip.state.grid,
    splitClipsAllowed,
    readOnly,
    state.bars,
    history,
  ]);

  const togglePlay = useCallback(() => {
    const v = videoRef.current;
    if (!v) return;
    if (v.paused) void v.play();
    else v.pause();
  }, []);

  const seekTo = useCallback((sec: number) => {
    const v = videoRef.current;
    if (v) {
      if (!v.paused) v.pause();
      v.currentTime = sec;
    }
    setCurrentTime(sec);
  }, []);

  const nudgeSelectedText = useCallback(
    (deltaS: number) => {
      if (readOnly || selection?.kind !== "text") return;
      const bar = state.bars.find((b) => b.id === selection.id);
      if (!bar) return;
      const start_s = nudgeBarStart(bar, deltaS, duration);
      if (start_s === bar.start_s) return;
      history.record();
      dispatch({ type: "MOVE_BAR", id: bar.id, start_s });
    },
    [duration, history, readOnly, selection, state.bars],
  );

  // Transport enablement (plan §6).
  const canSplit =
    selection?.kind === "text" ||
    (selection?.kind === "clip" && splitClipsAllowed);
  const splitReason =
    selection?.kind === "music"
      ? "Music fits the cut automatically"
      : selection?.kind === "clip" && !splitClipsAllowed
        ? "This variant's clips can't be split"
        : undefined;
  const canDelete =
    selection?.kind === "text" ||
    (selection?.kind === "clip" && activeSlotCount(slots) > 1);

  // ── Keyboard: Escape ladder + Delete with focus guard (plan §5/§9) ──────────
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      // ⌘Z / ⇧⌘Z (⌃Z / ⌃⇧Z, ⌘Y) — document undo/redo. Guarded: when focus is
      // in a text field, let the browser's native text undo win.
      if ((e.metaKey || e.ctrlKey) && (e.key === "z" || e.key === "Z")) {
        if (!deleteKeyAllowed(e.target as HTMLElement | null)) return;
        e.preventDefault();
        if (e.shiftKey) history.redo();
        else history.undo();
        return;
      }
      if ((e.metaKey || e.ctrlKey) && (e.key === "y" || e.key === "Y")) {
        if (!deleteKeyAllowed(e.target as HTMLElement | null)) return;
        e.preventDefault();
        history.redo();
        return;
      }
      if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
        if (!deleteKeyAllowed(e.target as HTMLElement | null)) return;
        if (selection?.kind !== "text") return;
        e.preventDefault();
        const step = e.shiftKey ? 1 : 0.1;
        nudgeSelectedText(e.key === "ArrowLeft" ? -step : step);
        return;
      }
      if (e.key === " " || e.key === "Spacebar") {
        if (!spaceShortcutAllowed(e.target as HTMLElement | null)) return;
        e.preventDefault();
        togglePlay();
        return;
      }
      if (e.key === "Escape") {
        if (layoutMode === "light" && lightSheetOpen) {
          e.preventDefault();
          setLightSheetOpen(false);
          return;
        }
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
  }, [
    activeTool,
    selection,
    clear,
    deleteSelected,
    history,
    layoutMode,
    lightSheetOpen,
    nudgeSelectedText,
    togglePlay,
  ]);

  // ── Save / leave ────────────────────────────────────────────────────────────

  const clearDraft = useCallback(() => {
    if (!variant) return;
    try {
      window.sessionStorage.removeItem(draftKey(variant.variant_id));
    } catch {
      /* privacy mode / quota — nothing to clean up */
    }
  }, [variant]);

  const handleSave = useCallback(async () => {
    if (!variant || saveState === "saving" || readOnly) return;
    setSaveState("saving");
    setSaveMessage(null);
    try {
      const res = await commitEditorSession(itemId, variant.variant_id, {
        text_elements: barsToTextElements(state.bars, originalsRef.current),
        // Clip-slot overrides only when the timeline was actually edited
        // (split / delete / mute) — omit otherwise so an untouched section
        // isn't rewritten.
        timeline_slots: timelineDirty
          ? slots.map((s, i) => ({
              slot_index: i,
              in_s: s.inS,
              duration_s: s.durationS,
              removed: s.removed,
            }))
          : undefined,
        // Music mute → bed level 0.0 (the editor-commit client carries `mix`).
        mix: soundMuted ? 0.0 : null,
        title: title.trim() !== "" ? title.trim() : null,
        base_generation: variant.render_finished_at ?? null,
      });
      // Partial: persist landed (we got a 2xx) but the render kick failed —
      // the response's `ok` flag tells us. Working state stays, Retry re-kicks.
      if (res && res.ok === false) {
        setSaveState("partial");
        setSaveMessage("Saved, but rendering didn't start.");
        return;
      }
      // Full success: the stack is void (no undoing into a pre-persist world),
      // the draft is spent, and the item-page hero shows the rendering state.
      history.clear();
      clearDraft();
      setSaveState("idle");
      setSaveMessage("Saved — rendering your latest version");
      router.push(`/plan/items/${itemId}`);
    } catch (err) {
      if (err instanceof EditorCommitConflictError) {
        setSaveState("conflict");
        setSaveMessage(err.message);
      } else {
        setSaveState("error");
        setSaveMessage(
          err instanceof Error ? err.message : "Couldn't save your edits.",
        );
      }
    }
  }, [
    variant,
    saveState,
    readOnly,
    itemId,
    state.bars,
    title,
    router,
    timelineDirty,
    slots,
    soundMuted,
    history,
    clearDraft,
  ]);

  // ── Draft recovery (plan §9) ────────────────────────────────────────────────
  // Mirror the working document to sessionStorage on every command push (any
  // document change while dirty). Failures degrade draft safety silently.
  useEffect(() => {
    if (!variant || !dirty) return;
    try {
      window.sessionStorage.setItem(
        draftKey(variant.variant_id),
        serializeDraft(variant.variant_id, getCurrent()),
      );
    } catch {
      /* quota full / privacy mode — editing continues, draft safety only */
    }
  }, [variant, dirty, state.bars, localSlots, videoMuted, soundMuted, title, getCurrent]);

  // On open, surface a matching unsaved draft as a quiet Resume/Discard notice
  // (once per variant, after seeding so a Resume overrides the seeded bars).
  const draftCheckedRef = useRef<string | null>(null);
  useEffect(() => {
    if (!variant) return;
    if (draftCheckedRef.current === variant.variant_id) return;
    draftCheckedRef.current = variant.variant_id;
    try {
      const parsed = deserializeDraft(
        window.sessionStorage.getItem(draftKey(variant.variant_id)),
      );
      if (parsed && parsed.variantId === variant.variant_id) {
        setDraftDoc(parsed.doc);
      }
    } catch {
      /* unreadable draft — skip the notice */
    }
  }, [variant]);

  const resumeDraft = useCallback(() => {
    if (!draftDoc) return;
    // Record the seeded baseline first so Resume itself is undoable, then
    // restore the draft as the working document.
    history.record();
    applyDocument(draftDoc);
    setDraftDoc(null);
  }, [draftDoc, history, applyDocument]);

  const discardDraft = useCallback(() => {
    clearDraft();
    setDraftDoc(null);
  }, [clearDraft]);

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
                  className="min-h-11 rounded-full border border-zinc-200 px-4 text-[13px] text-[#3f3f46] hover:border-zinc-400 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500"
                >
                  Retry
                </button>
              )}
              <button
                type="button"
                onClick={() => router.push(`/plan/items/${itemId}`)}
                className="min-h-11 rounded-full bg-[#0c0c0e] px-4 text-[13px] font-semibold text-white hover:opacity-80 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500"
              >
                Back to the video
              </button>
            </div>
          </div>
        </div>
      </Frame>
    );
  }

  const editorModeProps: EditorTimelineBodyProps = {
    durationS: duration,
    currentTimeS: currentTime,
    zoom,
    selection,
    onSelect: (kind, id) => {
      selectElement(kind, id);
    },
    onClear: clear,
    textBars: state.bars,
    slots,
    grid: clip.state.grid,
    clipsLoading: clip.loadState === "loading",
    filmstripSrc: variant.base_video_url ?? variant.output_url ?? null,
    sfx: (variant.sound_effects ?? []).map((p) => ({
      id: p.id,
      at_s: p.at_s ?? 0,
      label: p.label ?? null,
    })),
    hasMusic: !!variant.music_track_id,
    musicLabel: variant.track_title ?? "Music",
    videoMuted,
    onToggleVideoMute: () => {
      if (readOnly) return;
      history.record();
      setVideoMuted((m) => !m);
    },
    soundMuted,
    onToggleSoundMute: () => {
      if (readOnly) return;
      history.record();
      setSoundMuted((m) => !m);
      setTimelineDirty(true);
    },
    overlays: (variant.media_overlays ?? []).map((o) => ({
      id: o.id,
      start_s: o.start_s,
      end_s: o.end_s,
      label: o.kind === "video" ? "Video" : "Image",
    })),
    onScrub: seekTo,
    onScrubStart: () => {
      const v = videoRef.current;
      if (v && !v.paused) v.pause();
    },
  };

  return (
    <div
      className={
        layoutMode === "light"
          ? "fixed inset-0 z-50 grid grid-rows-[minmax(0,1fr)_auto] overflow-hidden bg-[#fafaf8]"
          : "fixed inset-0 z-50 grid grid-rows-[56px_minmax(480px,1fr)_260px] overflow-hidden bg-[#fafaf8]"
      }
    >
      <style dangerouslySetInnerHTML={{ __html: FONT_FACES }} />

      {/* ── Top bar (plan §1) ── */}
      {layoutMode !== "light" && (
        <header className="flex items-center border-b border-zinc-200 bg-white px-4">
          <div className="flex flex-1 items-center gap-3">
            <button
              type="button"
              aria-label="Back to the video page"
              onClick={requestLeave}
              className="flex h-11 w-11 items-center justify-center rounded-full border border-zinc-200 pb-0.5 text-[15px] text-[#3f3f46] hover:border-zinc-400 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500"
            >
              ‹
            </button>
            <input
              type="text"
              value={title}
              onChange={(e) => {
                if (readOnly) return;
                // Coalesce typing bursts into one undo step.
                history.record("title");
                setTitle(e.target.value);
              }}
              readOnly={readOnly}
              placeholder="add title for your video"
              aria-label="Video title"
              className="min-h-11 w-[240px] rounded-md border border-transparent bg-transparent px-2 py-1 text-[13px] text-[#0c0c0e] placeholder:text-[#a1a1aa] focus:border-lime-500 focus:bg-white focus:outline-none focus:ring-2 focus:ring-lime-500/25"
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
              className={`flex h-11 w-11 items-center justify-center rounded-lg text-[13px] focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500 ${
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
              className={`flex h-11 w-11 items-center justify-center rounded-lg text-[13px] focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500 ${
                canvasTool === "pan"
                  ? "bg-[#0c0c0e] text-white"
                  : "text-[#3f3f46] hover:bg-zinc-100"
              }`}
            >
              ✋
            </button>
            {/* Undo/redo — unified document command stack (plan §7). */}
            <button
              type="button"
              aria-label="Undo"
              title="Undo (⌘Z)"
              disabled={!history.canUndo}
              onClick={history.undo}
              className="flex h-11 w-11 items-center justify-center rounded-lg text-[14px] text-[#3f3f46] hover:bg-zinc-100 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500 disabled:opacity-40 disabled:hover:bg-transparent"
            >
              ↺
            </button>
            <button
              type="button"
              aria-label="Redo"
              title="Redo (⇧⌘Z)"
              disabled={!history.canRedo}
              onClick={history.redo}
              className="flex h-11 w-11 items-center justify-center rounded-lg text-[14px] text-[#3f3f46] hover:bg-zinc-100 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500 disabled:opacity-40 disabled:hover:bg-transparent"
            >
              ↻
            </button>
            <select
              aria-label="Canvas zoom"
              value={zoomPct}
              onChange={(e) => setZoomPct(Number(e.target.value))}
              className="ml-1 h-11 rounded-lg border border-zinc-200 bg-white px-2 text-[12px] text-[#3f3f46] focus:border-lime-500 focus:outline-none focus:ring-2 focus:ring-lime-500/25"
            >
              {ZOOM_OPTIONS.map((z) => (
                <option key={z} value={z}>
                  {z}%
                </option>
              ))}
            </select>
          </div>

          <div className="flex flex-1 items-center justify-end gap-2">
            {saveState === "idle" && saveMessage && (
              <span className="max-w-[280px] truncate rounded-lg border border-zinc-200 bg-white px-3 py-1.5 text-[12px] text-[#3f3f46]">
                {saveMessage}
              </span>
            )}
            <InkButton
              variant="ghost"
              className="min-h-11 text-[13px] focus-visible:!outline-lime-500"
              onClick={requestLeave}
            >
              Cancel
            </InkButton>
            <InkButton
              className="min-h-11 px-6 py-2.5 text-[13px] focus-visible:!outline-lime-500"
              disabled={!dirty || saving || readOnly}
              onClick={() => void handleSave()}
            >
              {saving ? "Saving…" : "Save"}
            </InkButton>
          </div>
        </header>
      )}

      {/* ── Middle row: rail · drawer · canvas · inspector · edge rail ── */}
      {layoutMode === "light" ? (
        <div className="min-h-0">
          <EditorCanvas
            variant={variant}
            elements={elements}
            bars={state.bars}
            selectedTextId={selection?.kind === "text" ? selection.id : null}
            currentTime={currentTime}
            zoomPct={100}
            tool="select"
            videoRef={videoRef}
            onSelectText={selectText}
            onClearSelection={() => {
              clear();
              setLightSheetOpen(false);
            }}
            onPatchBar={patchBar}
            onFocusContent={() => setLightSheetOpen(true)}
            onTimeUpdate={setCurrentTime}
            onDuration={setDuration}
            onPlayingChange={setPlaying}
            onReloadSource={() => setLoadNonce((n) => n + 1)}
            allowManipulation={false}
            stageHeightCss="100dvh - 96px"
          />
        </div>
      ) : (
        <div
          className={[
            "relative grid min-h-0",
            layoutMode === "full"
              ? "grid-cols-[auto_auto_1fr_auto_auto]"
              : "grid-cols-[auto_1fr_auto_auto]",
          ].join(" ")}
        >
        <ToolRail
          activeTool={activeTool}
          onToggleTool={(tool) => setActiveTool((cur) => (cur === tool ? null : tool))}
        />
        {layoutMode === "full" &&
          (activeTool !== null ? (
            <ToolDrawer
              tool={activeTool}
              sampleWord={sampleWord}
              appliedPresetId={appliedPresetId}
              onAddText={() => addTextAtPlayhead()}
              onPickPreset={pickPreset}
              appliedStyleSetId={appliedStyleSetId}
              onRestyleAll={restyleAll}
              onClose={() => setActiveTool(null)}
            />
          ) : (
            <div />
          ))}
        {layoutMode === "overlay" && activeTool !== null && (
          <div className="absolute bottom-0 left-[92px] top-0 z-40 shadow-[18px_0_36px_rgba(12,12,14,0.16)]">
            <ToolDrawer
              tool={activeTool}
              sampleWord={sampleWord}
              appliedPresetId={appliedPresetId}
              onAddText={() => addTextAtPlayhead()}
              onPickPreset={pickPreset}
              appliedStyleSetId={appliedStyleSetId}
              onRestyleAll={restyleAll}
              onClose={() => setActiveTool(null)}
            />
          </div>
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
          onPlayingChange={setPlaying}
          onReloadSource={() => setLoadNonce((n) => n + 1)}
        />
        <InspectorPanel
          selection={selection}
          bar={selectedBar}
          tab={inspectorTab}
          sampleWord={sampleWord}
          appliedPresetId={appliedPresetId}
          contentRef={contentRef}
          onEditText={(text) => {
            if (selectedBar && !readOnly) {
              // Coalesce keystrokes on one bar into a single undo step.
              history.record(`text:${selectedBar.id}`);
              dispatch({ type: "EDIT_TEXT", id: selectedBar.id, text });
            }
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
      )}

      {/* ── Timeline region (260px): TransportBar + scale-driven editor
             timeline (Text → Video → Sound → Overlays), plan §6. ── */}
      {layoutMode === "light" ? (
        <LightTransport
          playing={playing}
          currentTime={currentTime}
          duration={duration}
          onPlayPause={togglePlay}
          onScrub={seekTo}
        />
      ) : (
      <div
        data-region="timeline"
        className="relative flex min-h-0 flex-col border-t border-zinc-200 bg-white"
      >
        <TransportBar
          playing={playing}
          currentTime={currentTime}
          duration={duration}
          onPlayPause={togglePlay}
          canSplit={canSplit}
          splitReason={splitReason}
          onSplit={splitAtPlayhead}
          canDelete={canDelete}
          onDelete={deleteSelected}
          zoom={zoom}
          onZoom={setZoom}
          onFit={() => setZoom(1)}
        />
        <div className="min-h-0 flex-1">
          <UnifiedTimeline
            totalDurationS={duration}
            currentTimeS={currentTime}
            // Item-page-only props — unused in editor mode (UnifiedTimeline
            // early-returns on `editorMode`); passed as inert defaults so the
            // shared component's required contract stays satisfied.
            sfxPlacements={[]}
            sfxGlossaryEffects={[]}
            sfxGlossaryLoading={false}
            sfxRendering={false}
            sfxUploading={false}
            onSfxChange={() => {}}
            onSfxUploadRequest={async () => {}}
            overlayCards={[]}
            overlaysEnabled={false}
            overlayUploading={false}
            localPreviewUrls={{}}
            onOverlayUploadRequest={() => {}}
            onUpdateCard={() => {}}
            onRemoveCard={() => {}}
            onClearOverlays={() => {}}
            editorMode={editorModeProps}
          />
        </div>
        {toast && (
          <div className="pointer-events-none absolute bottom-4 left-1/2 -translate-x-1/2 rounded-lg bg-[#0c0c0e] px-3 py-1.5 text-[12px] text-white shadow-lg">
            {toast}
          </div>
        )}
      </div>
      )}

      <LightEditSheet
        open={layoutMode === "light" && lightSheetOpen && !!selectedBar}
        bar={selectedBar}
        sampleWord={sampleWord}
        appliedPresetId={appliedPresetId}
        saveState={saveState}
        saving={saving}
        dirty={dirty}
        readOnly={readOnly}
        onClose={() => setLightSheetOpen(false)}
        onEditText={(text) => {
          if (selectedBar && !readOnly) {
            history.record(`text:${selectedBar.id}`);
            dispatch({ type: "EDIT_TEXT", id: selectedBar.id, text });
          }
        }}
        onPickPreset={pickPreset}
        onSave={() => void handleSave()}
      />

      {/* ── Read-only banner (ineligible variant, plan §9 / E4) ── */}
      {readOnly && (
        <div className="pointer-events-none absolute left-1/2 top-[68px] z-[60] w-[min(560px,90vw)] -translate-x-1/2">
          <div className="rounded-lg border border-zinc-200 bg-white/95 px-4 py-2.5 text-center text-[12px] text-[#3f3f46] shadow-sm">
            This version can&apos;t be edited. {readOnlyReason}
          </div>
        </div>
      )}

      {/* ── Save micro-states (plan §9): conflict / error / partial tiles.
             All preserve working state; only Reload/Retry act. ── */}
      {(saveState === "conflict" || saveState === "error" || saveState === "partial") && (
        <div className="absolute left-1/2 top-[68px] z-[70] w-[min(520px,90vw)] -translate-x-1/2">
          <div className="flex items-center justify-between gap-3 rounded-lg border border-dashed border-zinc-300 bg-white px-4 py-3 shadow-sm">
            <p className="text-[12px] text-[#3f3f46]">
              {saveState === "conflict"
                ? "This video changed in another tab — reload to continue."
                : saveState === "partial"
                  ? "Saved, but rendering didn't start."
                  : (saveMessage ?? "Couldn't save your edits.")}
            </p>
            {saveState === "conflict" ? (
              <button
                type="button"
                onClick={() => {
                  setSaveState("idle");
                  setSaveMessage(null);
                  setLoadNonce((n) => n + 1);
                }}
                className="min-h-11 flex-shrink-0 rounded-full bg-[#0c0c0e] px-4 text-[12px] font-semibold text-white hover:opacity-80 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500"
              >
                Reload
              </button>
            ) : (
              <button
                type="button"
                onClick={() => void handleSave()}
                className="min-h-11 flex-shrink-0 rounded-full border border-zinc-200 px-4 text-[12px] text-[#3f3f46] hover:border-zinc-400 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500"
              >
                Retry
              </button>
            )}
          </div>
        </div>
      )}

      {/* ── Resume-draft notice (plan §9): quiet, not a modal. ── */}
      {draftDoc && saveState === "idle" && (
        <div className="absolute left-1/2 top-[68px] z-[65] w-[min(480px,90vw)] -translate-x-1/2">
          <div className="flex items-center justify-between gap-3 rounded-lg border border-zinc-200 bg-white px-4 py-2.5 shadow-sm">
            <p className="text-[12px] text-[#3f3f46]">Resume your unsaved edits?</p>
            <div className="flex flex-shrink-0 items-center gap-2">
              <button
                type="button"
                onClick={discardDraft}
                className="min-h-11 rounded-full px-3 text-[12px] text-[#71717a] hover:text-[#0c0c0e] focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500"
              >
                Discard
              </button>
              <button
                type="button"
                onClick={resumeDraft}
                className="min-h-11 rounded-full bg-[#0c0c0e] px-4 text-[12px] font-semibold text-white hover:opacity-80 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500"
              >
                Resume
              </button>
            </div>
          </div>
        </div>
      )}

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

function LightTransport({
  playing,
  currentTime,
  duration,
  onPlayPause,
  onScrub,
}: {
  playing: boolean;
  currentTime: number;
  duration: number;
  onPlayPause: () => void;
  onScrub: (seconds: number) => void;
}) {
  const safeDuration = Math.max(0, duration);
  const safeTime = Math.min(safeDuration || currentTime, Math.max(0, currentTime));
  return (
    <div className="border-t border-zinc-200 bg-white px-4 pb-[max(16px,env(safe-area-inset-bottom))] pt-3">
      <div className="mx-auto flex max-w-[720px] items-center gap-3">
        <button
          type="button"
          aria-label={playing ? "Pause video" : "Play video"}
          aria-pressed={playing}
          onClick={onPlayPause}
          className="flex h-11 w-11 flex-none items-center justify-center rounded-full bg-[#0c0c0e] text-[13px] text-white hover:opacity-80 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500"
        >
          {playing ? "❚❚" : "▶"}
        </button>
        <input
          type="range"
          aria-label="Scrub video"
          min={0}
          max={safeDuration || 0}
          step={0.1}
          value={safeDuration > 0 ? safeTime : 0}
          disabled={safeDuration <= 0}
          onChange={(e) => onScrub(Number(e.target.value))}
          className="h-11 min-w-0 flex-1 cursor-pointer accent-lime-500 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500 disabled:cursor-not-allowed disabled:opacity-40"
        />
        <span
          aria-label="Playback position"
          className="w-[92px] flex-none text-right text-[12px] tabular-nums text-[#3f3f46]"
        >
          {formatTimecode(currentTime)}{" "}
          <span className="text-[#a1a1aa]">/ {formatTimecode(duration)}</span>
        </span>
      </div>
    </div>
  );
}

function LightEditSheet({
  open,
  bar,
  sampleWord,
  appliedPresetId,
  saveState,
  saving,
  dirty,
  readOnly,
  onClose,
  onEditText,
  onPickPreset,
  onSave,
}: {
  open: boolean;
  bar: TextElementBar | null;
  sampleWord: string | null;
  appliedPresetId: string | null;
  saveState: "idle" | "saving" | "conflict" | "error" | "partial";
  saving: boolean;
  dirty: boolean;
  readOnly: boolean;
  onClose: () => void;
  onEditText: (text: string) => void;
  onPickPreset: (preset: TextPreset) => void;
  onSave: () => void;
}) {
  const trapRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  useFocusTrap(trapRef, open);

  useEffect(() => {
    if (!open) return;
    const id = window.requestAnimationFrame(() => {
      textareaRef.current?.focus();
      textareaRef.current?.select();
    });
    return () => window.cancelAnimationFrame(id);
  }, [open, bar?.id]);

  if (!open || !bar) return null;

  return (
    <div
      ref={trapRef}
      role="dialog"
      aria-modal="true"
      aria-labelledby="light-edit-title"
      className="fixed inset-0 z-[90] flex flex-col bg-white"
    >
      <div className="flex items-center justify-between border-b border-zinc-200 px-4 py-3">
        <div>
          <h2 id="light-edit-title" className="font-display text-[18px] text-[#0c0c0e]">
            Edit text
          </h2>
          <p className="mt-0.5 text-[12px] text-[#71717a]">Full timeline editing on desktop</p>
        </div>
        <button
          type="button"
          aria-label="Close text editor"
          onClick={onClose}
          className="flex h-11 w-11 items-center justify-center rounded-lg text-[14px] text-[#71717a] hover:bg-zinc-100 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500"
        >
          ✕
        </button>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto px-4 py-5">
        <label className="block text-[12px] font-semibold text-[#3f3f46]" htmlFor="light-edit-textarea">
          Content
        </label>
        <textarea
          id="light-edit-textarea"
          ref={textareaRef}
          value={bar.text}
          readOnly={readOnly}
          onChange={(e) => onEditText(e.target.value)}
          rows={5}
          className="mt-2 w-full resize-none rounded-lg border border-zinc-200 px-3 py-3 text-[15px] text-[#0c0c0e] outline-none focus:border-lime-500 focus:ring-2 focus:ring-lime-500/25"
        />
        <p className="mb-3 mt-6 text-[12px] font-semibold text-[#3f3f46]">Presets</p>
        <PresetGrid
          presets={TEXT_PRESETS}
          sampleWord={sampleWord}
          appliedPresetId={appliedPresetId}
          onPick={onPickPreset}
        />
      </div>
      <div className="flex items-center justify-end gap-2 border-t border-zinc-200 px-4 pb-[max(16px,env(safe-area-inset-bottom))] pt-3">
        <button
          type="button"
          onClick={onClose}
          className="min-h-11 rounded-full px-4 text-[13px] font-semibold text-[#71717a] hover:underline focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500"
        >
          Close
        </button>
        <button
          type="button"
          disabled={!dirty || saving || readOnly}
          onClick={onSave}
          className="min-h-11 rounded-full bg-[#0c0c0e] px-6 text-[13px] font-semibold text-white hover:opacity-80 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500 disabled:opacity-40"
        >
          {saveState === "saving" ? "Saving..." : "Save"}
        </button>
      </div>
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
