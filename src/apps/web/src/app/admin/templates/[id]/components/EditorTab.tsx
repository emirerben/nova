"use client";

import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from "react";
import {
  adminCreateRerenderJob,
  adminCreateTestJob,
  adminGetRecipe,
  adminSaveRecipe,
  adminTextPreview,
  type TextPreviewParams,
} from "@/lib/admin-api";
import type { AdminTemplate, LatestTestJob } from "@/lib/admin-api";
import type {
  EditorAction,
  EditorSelection,
  EditorState,
  Recipe,
  RecipeSlot,
} from "./recipe-types";
import { EMPTY_INTERSTITIAL, EMPTY_OVERLAY } from "./recipe-types";
import { PropertyPanel } from "./PropertyPanel";
import type { TemplateJobStatusResponse } from "@/lib/api";
import { getTemplateJobStatus } from "@/lib/api";
import { useJobPoller } from "@/hooks/useJobPoller";
import { OverlayPreview } from "./OverlayPreview";
import { OverlayTimeline } from "./OverlayTimeline";

// ── Reducer ─────────────────────────────────────────────────────────────────

function editorReducer(state: EditorState, action: EditorAction): EditorState {
  switch (action.type) {
    case "LOAD_RECIPE":
      return {
        ...state,
        recipe: action.recipe,
        savedRecipe: action.recipe,
        loading: false,
        error: null,
      };

    case "UPDATE_SLOT_FIELD": {
      if (!state.recipe) return state;
      const slots = state.recipe.slots.map((s, i) =>
        i === action.slotIndex ? { ...s, [action.field]: action.value } : s,
      );
      return { ...state, recipe: { ...state.recipe, slots } };
    }

    case "UPDATE_OVERLAY_FIELD": {
      if (!state.recipe) return state;
      const slots = state.recipe.slots.map((s, si) => {
        if (si !== action.slotIndex) return s;
        const overlays = s.text_overlays.map((o, oi) =>
          oi === action.overlayIndex
            ? { ...o, [action.field]: action.value }
            : o,
        );
        return { ...s, text_overlays: overlays };
      });
      return { ...state, recipe: { ...state.recipe, slots } };
    }

    case "UPDATE_INTERSTITIAL_FIELD": {
      if (!state.recipe) return state;
      const interstitials = state.recipe.interstitials.map((inter, i) =>
        i === action.interstitialIndex
          ? { ...inter, [action.field]: action.value }
          : inter,
      );
      return { ...state, recipe: { ...state.recipe, interstitials } };
    }

    case "UPDATE_GLOBAL_FIELD": {
      if (!state.recipe) return state;
      return {
        ...state,
        recipe: { ...state.recipe, [action.field]: action.value },
      };
    }

    case "ADD_OVERLAY": {
      if (!state.recipe) return state;
      const slots = state.recipe.slots.map((s, i) => {
        if (i !== action.slotIndex) return s;
        return {
          ...s,
          text_overlays: [...s.text_overlays, { ...EMPTY_OVERLAY }],
        };
      });
      return { ...state, recipe: { ...state.recipe, slots } };
    }

    case "REMOVE_OVERLAY": {
      if (!state.recipe) return state;
      const slots = state.recipe.slots.map((s, si) => {
        if (si !== action.slotIndex) return s;
        return {
          ...s,
          text_overlays: s.text_overlays.filter(
            (_, oi) => oi !== action.overlayIndex,
          ),
        };
      });
      return { ...state, recipe: { ...state.recipe, slots } };
    }

    case "ADD_INTERSTITIAL": {
      if (!state.recipe) return state;
      const slotCount = state.recipe.slots.length;
      return {
        ...state,
        recipe: {
          ...state.recipe,
          interstitials: [
            ...state.recipe.interstitials,
            { ...EMPTY_INTERSTITIAL, after_slot: Math.min(1, slotCount) },
          ],
        },
      };
    }

    case "REMOVE_INTERSTITIAL": {
      if (!state.recipe) return state;
      return {
        ...state,
        recipe: {
          ...state.recipe,
          interstitials: state.recipe.interstitials.filter(
            (_, i) => i !== action.interstitialIndex,
          ),
        },
      };
    }

    case "SET_SELECTED":
      return { ...state, selection: action.selection };

    case "RESET_TO_SAVED":
      return {
        ...state,
        recipe: action.recipe,
        savedRecipe: action.recipe,
        selection: null,
      };

    case "SET_VERSION":
      return {
        ...state,
        versionId: action.versionId,
        versionNumber: action.versionNumber,
      };

    default:
      return state;
  }
}

const initialState: EditorState = {
  recipe: null,
  savedRecipe: null,
  selection: null,
  versionId: "",
  versionNumber: 0,
  loading: true,
  saving: false,
  error: null,
};

// ── Slot bar colors by type ─────────────────────────────────────────────────

const SLOT_COLORS: Record<string, string> = {
  hook: "bg-amber-700/60 border-amber-600",
  broll: "bg-blue-700/40 border-blue-600",
  outro: "bg-purple-700/40 border-purple-600",
};

const TERMINAL_STATUSES = new Set(["template_ready", "processing_failed"]);

// ── EditorTab ───────────────────────────────────────────────────────────────

interface EditorTabProps {
  template: AdminTemplate;
  latestTestJob: LatestTestJob | null;
  onTestJobComplete?: (job: LatestTestJob) => void;
}

export function EditorTab({ template, latestTestJob, onTestJobComplete }: EditorTabProps) {
  const [state, dispatch] = useReducer(editorReducer, initialState);
  const [saving, setSaving] = useState(false);
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const [currentTime, setCurrentTime] = useState<number | null>(null);
  const [videoError, setVideoError] = useState(false);
  const [savedSinceLastTest, setSavedSinceLastTest] = useState(false);
  const [previewSubject, setPreviewSubject] = useState("");

  // Re-run polling state
  const [rerunJobId, setRerunJobId] = useState<string | null>(null);
  const rerunPoller = useJobPoller<TemplateJobStatusResponse>(rerunJobId, {
    fetchStatus: getTemplateJobStatus,
    isTerminal: (d) => TERMINAL_STATUSES.has(d.status),
  });

  // When re-run completes, push result to parent
  useEffect(() => {
    if (
      rerunJobId &&
      rerunPoller.data?.status === "template_ready" &&
      rerunPoller.data.job_id === rerunJobId &&
      rerunPoller.data.assembly_plan?.output_url &&
      latestTestJob
    ) {
      onTestJobComplete?.({
        job_id: rerunPoller.data.job_id,
        output_url: rerunPoller.data.assembly_plan.output_url,
        base_output_url: rerunPoller.data.assembly_plan.base_output_url ?? null,
        clip_paths: latestTestJob.clip_paths,
        has_rerender_data: true,
        created_at: rerunPoller.data.created_at,
      });
      setRerunJobId(null);
      setVideoError(false);
      setSavedSinceLastTest(false);
    }
  }, [rerunJobId, rerunPoller.data, onTestJobComplete, latestTestJob]);

  // Fetch recipe on mount
  useEffect(() => {
    let cancelled = false;
    adminGetRecipe(template.id)
      .then((res) => {
        if (cancelled) return;
        // Migrate legacy overlays: if sample_text is empty but text has content, copy text → sample_text
        if (res.recipe) {
          for (const slot of (res.recipe as any).slots || []) {
            for (const overlay of slot.text_overlays || []) {
              if (!overlay.sample_text && overlay.text) {
                overlay.sample_text = overlay.text;
              }
            }
          }
        }
        dispatch({ type: "LOAD_RECIPE", recipe: res.recipe as unknown as Recipe });
        dispatch({ type: "SET_VERSION", versionId: res.version_id, versionNumber: res.version_number });
      })
      .catch((err) => {
        if (!cancelled) {
          dispatch({
            type: "LOAD_RECIPE",
            recipe: null as unknown as Recipe,
          });
        }
        console.error("Failed to load recipe:", err);
      });
    return () => { cancelled = true; };
  }, [template.id]);

  const isDirty =
    state.recipe !== null &&
    state.savedRecipe !== null &&
    JSON.stringify(state.recipe) !== JSON.stringify(state.savedRecipe);

  // ── Cumulative slot timing ──────────────────────────────────────────────

  const slotStartTimes = useMemo(() => {
    if (!state.recipe) return [];
    const { slots, interstitials } = state.recipe;

    // Build a map: slot position → total interstitial hold_s after it
    const interstitialHoldMap = new Map<number, number>();
    for (const inter of interstitials) {
      const existing = interstitialHoldMap.get(inter.after_slot) ?? 0;
      interstitialHoldMap.set(inter.after_slot, existing + inter.hold_s);
    }

    const starts: number[] = [];
    let cumulative = 0;
    for (let i = 0; i < slots.length; i++) {
      starts.push(cumulative);
      cumulative += slots[i].target_duration_s;
      const holdAfter = interstitialHoldMap.get(slots[i].position) ?? 0;
      cumulative += holdAfter;
    }
    return starts;
  }, [state.recipe]);

  // ── Active slot from playhead ──────────────────────────────────────────

  const activeSlotIndex = useMemo(() => {
    if (currentTime == null || slotStartTimes.length === 0) return -1;
    for (let i = slotStartTimes.length - 1; i >= 0; i--) {
      if (currentTime >= slotStartTimes[i]) return i;
    }
    return -1;
  }, [currentTime, slotStartTimes]);

  // ── Slot click → video seek ───────────────────────────────────────────

  const handleSlotSelect = useCallback(
    (index: number) => {
      dispatch({ type: "SET_SELECTED", selection: { type: "slot", slotIndex: index } });
      if (videoRef.current && slotStartTimes[index] != null) {
        videoRef.current.currentTime = slotStartTimes[index];
      }
    },
    [slotStartTimes],
  );

  // ── Save ──────────────────────────────────────────────────────────────

  const handleSave = useCallback(async () => {
    if (!state.recipe || saving) return;
    setSaving(true);

    try {
      // Sync text from sample_text for backward compatibility
      const recipeToSave = JSON.parse(JSON.stringify(state.recipe));
      for (const slot of recipeToSave.slots) {
        for (const overlay of slot.text_overlays) {
          overlay.text = overlay.sample_text;
        }
      }

      const res = await adminSaveRecipe(template.id, {
        recipe: recipeToSave as unknown as Record<string, unknown>,
        base_version_id: state.versionId || null,
      });
      dispatch({
        type: "RESET_TO_SAVED",
        recipe: res.recipe as unknown as Recipe,
      });
      dispatch({ type: "SET_VERSION", versionId: res.version_id, versionNumber: res.version_number });
      setSavedSinceLastTest(true);
    } catch (err) {
      alert(err instanceof Error ? err.message : "Save failed");
    } finally {
      setSaving(false);
    }
  }, [state.recipe, saving, template.id]);

  const handleReset = useCallback(() => {
    if (!state.savedRecipe) return;
    if (!confirm("Discard unsaved changes?")) return;
    dispatch({ type: "RESET_TO_SAVED", recipe: state.savedRecipe });
  }, [state.savedRecipe]);

  // ── Re-run test ───────────────────────────────────────────────────────

  const runFullPipeline = useCallback(async () => {
    if (!latestTestJob?.clip_paths.length) return;
    const res = await adminCreateTestJob(template.id, {
      clip_gcs_paths: latestTestJob.clip_paths,
    });
    setRerunJobId(res.job_id);
  }, [template.id, latestTestJob]);

  const handleRerun = useCallback(async () => {
    if (!latestTestJob?.clip_paths.length) return;
    try {
      if (latestTestJob.has_rerender_data) {
        // Fast path: re-render with locked clip assignments (~1 min)
        const res = await adminCreateRerenderJob(template.id, latestTestJob.job_id);
        setRerunJobId(res.job_id);
      } else {
        // No rerender data: full pipeline
        await runFullPipeline();
      }
    } catch (err) {
      // Only fall back to full pipeline on 409 (slot count changed) or 422 (missing data)
      const msg = err instanceof Error ? err.message : "";
      if (msg.includes("Slot count changed") || msg.includes("clip_gcs_path")) {
        try {
          await runFullPipeline();
        } catch (fallbackErr) {
          alert(fallbackErr instanceof Error ? fallbackErr.message : "Re-run failed");
        }
      } else {
        alert(msg || "Re-run failed");
      }
    }
  }, [template.id, latestTestJob, runFullPipeline]);

  // ── Recipe changed since last test? ───────────────────────────────────

  const recipeChangedSinceTest = latestTestJob != null && savedSinceLastTest;

  // ── Loading / Error states ──────────────────────────────────────────────

  if (state.loading) {
    return (
      <div className="flex items-center justify-center py-12">
        <div className="w-6 h-6 border-2 border-zinc-600 border-t-white rounded-full animate-spin" />
        <span className="ml-3 text-zinc-400 text-sm">Loading recipe...</span>
      </div>
    );
  }

  if (!state.recipe) {
    return (
      <div className="py-12 text-center">
        <p className="text-zinc-500 text-sm">
          {template.analysis_status !== "ready"
            ? "Template analysis must complete before editing the recipe."
            : "No recipe data available."}
        </p>
      </div>
    );
  }

  const { recipe, selection } = state;
  const totalDuration = recipe.slots.reduce(
    (sum, s) => sum + s.target_duration_s,
    0,
  );

  // Editor shows the base video (without burned overlays) so the
  // OverlayPreview HTML layer is the sole source of overlay display.
  // Falls back to output_url for jobs rendered before this change.
  const editorVideoUrl = latestTestJob?.base_output_url || latestTestJob?.output_url;
  const hasVideo = editorVideoUrl && !videoError;
  const isRerunning = rerunJobId !== null && rerunPoller.polling;

  // ── Layout: side-by-side with video, or vertical without ──────────────

  const editorContent = (
    <div className="flex-1 space-y-4 min-w-0">
      {/* Mini-timeline: slot bars */}
      <div className="border border-zinc-800 rounded p-3">
        <div className="flex items-center gap-1 mb-2">
          <span className="text-xs text-zinc-500 mr-2">Timeline</span>
          <div className="flex items-center gap-2 ml-auto">
            <span className="text-xs text-zinc-500">Preview Subject</span>
            <input
              type="text"
              value={previewSubject}
              onChange={(e) => setPreviewSubject(e.target.value)}
              placeholder="e.g. Puerto Rico"
              className="bg-zinc-900 border border-zinc-700 rounded px-2 py-1 text-sm text-white focus:outline-none focus:border-zinc-500 w-40"
            />
          </div>
          <button
            onClick={() =>
              dispatch({
                type: "SET_SELECTED",
                selection: { type: "global", slotIndex: 0 },
              })
            }
            className={`text-xs px-2 py-0.5 rounded transition-colors ${
              selection?.type === "global"
                ? "bg-zinc-600 text-white"
                : "bg-zinc-800 text-zinc-400 hover:text-white"
            }`}
          >
            Global
          </button>
        </div>

        <div className="flex gap-1 h-10">
          {recipe.slots.map((slot, i) => (
            <SlotBar
              key={i}
              slot={slot}
              index={i}
              totalDuration={totalDuration}
              isSelected={
                selection?.type === "slot" && selection.slotIndex === i
              }
              isActive={hasVideo ? activeSlotIndex === i : false}
              onSelect={() => handleSlotSelect(i)}
            />
          ))}

          {/* Interstitial markers */}
          {recipe.interstitials.map((inter, ii) => (
            <button
              key={`i-${ii}`}
              onClick={() =>
                dispatch({
                  type: "SET_SELECTED",
                  selection: {
                    type: "interstitial",
                    slotIndex: inter.after_slot - 1,
                    interstitialIndex: ii,
                  },
                })
              }
              className={`flex-shrink-0 w-3 rounded border transition-colors flex items-center justify-center ${
                selection?.type === "interstitial" &&
                selection.interstitialIndex === ii
                  ? "bg-zinc-500 border-zinc-400"
                  : "bg-zinc-800 border-zinc-700 hover:border-zinc-500"
              }`}
              title={`Interstitial: ${inter.type} after slot ${inter.after_slot}`}
            >
              <span className="text-[8px] text-zinc-400">I</span>
            </button>
          ))}
        </div>
      </div>

      {/* Property panel */}
      <div className="border border-zinc-800 rounded p-4 min-h-[300px]">
        <PropertyPanel
          recipe={recipe}
          selection={selection}
          dispatch={dispatch}
          previewSubject={previewSubject}
        />
      </div>

      {/* Text Tuning */}
      <TextTuningPanel templateId={template.id} />

      {/* Action bar */}
      <div className="flex items-center justify-between border-t border-zinc-800 pt-4">
        <div className="flex items-center gap-3">
          <button
            onClick={handleSave}
            disabled={!isDirty || saving}
            className="px-4 py-2 text-sm bg-white text-black rounded hover:bg-zinc-200 disabled:opacity-40 disabled:cursor-not-allowed"
          >
            Save Recipe
          </button>
          <button
            onClick={handleReset}
            disabled={!isDirty}
            className="px-4 py-2 text-sm bg-zinc-800 hover:bg-zinc-700 text-white rounded disabled:opacity-40 disabled:cursor-not-allowed"
          >
            Reset
          </button>
          {latestTestJob?.clip_paths.length ? (
            <button
              onClick={handleRerun}
              disabled={isRerunning || isDirty}
              className="px-4 py-2 text-sm bg-blue-700 hover:bg-blue-600 text-white rounded disabled:opacity-50 disabled:cursor-not-allowed flex items-center gap-2"
              title={
                isDirty
                  ? "Save first, then re-run"
                  : latestTestJob.has_rerender_data
                    ? "Re-render with same clips (fast)"
                    : "Re-run test with same clips (full pipeline)"
              }
            >
              {isRerunning ? (
                <>
                  <span className="w-3 h-3 border-2 border-blue-300 border-t-white rounded-full animate-spin" />
                  {latestTestJob.has_rerender_data ? "Re-rendering..." : "Processing..."}
                </>
              ) : latestTestJob.has_rerender_data ? (
                "Re-render"
              ) : (
                "Re-run Test"
              )}
            </button>
          ) : null}
          {latestTestJob?.output_url && !isDirty && !isRerunning && (
            <a
              href={latestTestJob.output_url}
              download
              className="px-4 py-2 text-sm bg-zinc-800 hover:bg-zinc-700 text-white rounded"
            >
              Download Final
            </a>
          )}
          {isDirty && (
            <span className="text-xs text-amber-400">Unsaved changes</span>
          )}
          {recipeChangedSinceTest && !isDirty && (
            <span className="text-xs text-yellow-500/80">
              Recipe edited since last test — re-run to see changes
            </span>
          )}
          {rerunPoller.data?.status === "processing_failed" && (
            <span className="text-xs text-red-400">
              Re-run failed: {rerunPoller.data.error_detail ?? "Unknown error"}
            </span>
          )}
        </div>

        <span className="text-xs text-zinc-500">
          Version {state.versionNumber}
          {state.versionId && (
            <> &middot; {state.versionId.slice(0, 8)}</>
          )}
        </span>
      </div>
    </div>
  );

  // Compute slot-relative time for overlays
  const activeSlot = activeSlotIndex >= 0 ? recipe.slots[activeSlotIndex] : null;
  const currentTimeInSlot =
    currentTime != null && activeSlotIndex >= 0
      ? currentTime - slotStartTimes[activeSlotIndex]
      : 0;

  // Side-by-side layout when video is available
  if (hasVideo) {
    return (
      <div className="flex gap-6">
        <div className="flex-shrink-0 space-y-2">
          <SyncVideoPlayer
            url={editorVideoUrl!}
            videoRef={videoRef}
            onTimeUpdate={setCurrentTime}
            onError={() => setVideoError(true)}
          >
            {activeSlot && activeSlotIndex >= 0 && (
              <OverlayPreview
                slot={activeSlot}
                slotIndex={activeSlotIndex}
                currentTimeInSlot={currentTimeInSlot}
                selection={selection}
                dispatch={dispatch}
                previewSubject={previewSubject}
              />
            )}
          </SyncVideoPlayer>
          {activeSlot && activeSlotIndex >= 0 && (
            <OverlayTimeline
              slot={activeSlot}
              slotIndex={activeSlotIndex}
              currentTimeInSlot={currentTimeInSlot}
              selection={selection}
              dispatch={dispatch}
            />
          )}
        </div>
        {editorContent}
      </div>
    );
  }

  // Video error state (expired URL, CORS, etc.)
  if (videoError && latestTestJob?.output_url) {
    return (
      <div className="flex gap-6">
        <div className="w-[280px] aspect-[9/16] bg-zinc-900 rounded flex flex-col items-center justify-center gap-3 flex-shrink-0 border border-zinc-800">
          <p className="text-zinc-400 text-sm text-center px-4">
            Video unavailable
          </p>
          <p className="text-zinc-600 text-xs text-center px-4">
            The signed URL may have expired.
          </p>
          {latestTestJob.clip_paths.length > 0 && (
            <button
              onClick={handleRerun}
              disabled={isRerunning}
              className="px-3 py-1.5 text-xs bg-blue-700 hover:bg-blue-600 text-white rounded disabled:opacity-50 flex items-center gap-2"
            >
              {isRerunning ? (
                <>
                  <span className="w-3 h-3 border-2 border-blue-300 border-t-white rounded-full animate-spin" />
                  Processing...
                </>
              ) : (
                "Re-run Test"
              )}
            </button>
          )}
        </div>
        {editorContent}
      </div>
    );
  }

  // Vertical layout when no video
  return (
    <div className="space-y-4">
      <div className="bg-zinc-900 border border-zinc-800 rounded p-6 text-center">
        <p className="text-zinc-500 text-sm">
          Run a test in the Test tab to preview video here.
        </p>
      </div>
      {editorContent}
    </div>
  );
}

// ── SyncVideoPlayer ─────────────────────────────────────────────────────────

function SyncVideoPlayer({
  url,
  videoRef,
  onTimeUpdate,
  onError,
  children,
}: {
  url: string;
  videoRef?: React.RefCallback<HTMLVideoElement> | React.MutableRefObject<HTMLVideoElement | null>;
  onTimeUpdate?: (time: number) => void;
  onError?: () => void;
  children?: React.ReactNode;
}) {
  const [loading, setLoading] = useState(true);
  const [errored, setErrored] = useState(false);

  return (
    <div className="w-[280px] aspect-[9/16] bg-black rounded overflow-hidden relative">
      {loading && !errored && (
        <div className="absolute inset-0 flex flex-col items-center justify-center gap-2 z-20">
          <div className="w-6 h-6 border-2 border-zinc-600 border-t-white rounded-full animate-spin" />
          <span className="text-zinc-500 text-xs">Loading video...</span>
        </div>
      )}
      <video
        ref={videoRef}
        src={url}
        controls
        className="w-full h-full object-contain"
        playsInline
        onLoadedData={() => setLoading(false)}
        onTimeUpdate={(e) => onTimeUpdate?.(e.currentTarget.currentTime)}
        onError={() => {
          setLoading(false);
          setErrored(true);
          onError?.();
        }}
      />
      {children}
    </div>
  );
}

// ── Slot bar component ──────────────────────────────────────────────────────

function SlotBar({
  slot,
  index,
  totalDuration,
  isSelected,
  isActive,
  onSelect,
}: {
  slot: RecipeSlot;
  index: number;
  totalDuration: number;
  isSelected: boolean;
  isActive: boolean;
  onSelect: () => void;
}) {
  const widthPct = totalDuration > 0
    ? Math.max((slot.target_duration_s / totalDuration) * 100, 3)
    : 100 / Math.max(index + 1, 1);

  const colorClass = SLOT_COLORS[slot.slot_type] ?? "bg-zinc-700/40 border-zinc-600";

  return (
    <button
      onClick={onSelect}
      style={{ width: `${widthPct}%` }}
      className={`relative rounded border transition-all overflow-hidden flex-shrink-0 ${colorClass} ${
        isSelected
          ? "ring-2 ring-white/50 border-white/60"
          : isActive
            ? "ring-2 ring-blue-400/60 border-blue-400/60"
            : "hover:border-zinc-400"
      }`}
      title={`Slot ${slot.position}: ${slot.target_duration_s.toFixed(1)}s (${slot.slot_type})`}
    >
      <div className="absolute inset-0 flex items-center justify-center">
        <span className="text-[10px] font-medium text-white/80 truncate px-1">
          S{slot.position}
        </span>
      </div>
      <div className="absolute bottom-0 left-0 right-0 text-[8px] text-white/50 text-center">
        {slot.target_duration_s.toFixed(1)}s
      </div>
    </button>
  );
}

// ── Text Tuning Panel ─────────────────────────────────────────────────────────

function TextTuningPanel({ templateId }: { templateId: string }) {
  const [subjectSize, setSubjectSize] = useState(199);
  const [subjectY, setSubjectY] = useState(0.45);
  const [prefixSize, setPrefixSize] = useState(36);
  const [prefixY, setPrefixY] = useState(0.472);
  const [previewImg, setPreviewImg] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [applying, setApplying] = useState(false);
  const [applied, setApplied] = useState(false);

  // Load current values from recipe on mount
  useEffect(() => {
    adminGetRecipe(templateId)
      .then((r) => {
        const slots = (r.recipe as { slots?: Array<{ text_overlays?: Array<Record<string, unknown>>; overlays?: Array<Record<string, unknown>> }> }).slots;
        if (!slots) return;
        const sizeMap: Record<string, number> = {
          small: 36, medium: 72, large: 120, xlarge: 150, xxlarge: 250, jumbo: 199,
        };
        for (const slot of slots) {
          const overlays = slot.text_overlays || slot.overlays;
          if (!overlays || overlays.length < 2) continue;
          const subject = overlays.find(
            (o) => o.effect === "font-cycle" || (o.text_size as string)?.match(/jumbo|xxlarge|xlarge/),
          );
          const prefix = overlays.find(
            (o) => o !== subject && typeof o.sample_text === "string",
          );
          if (subject && prefix) {
            const sz = (subject.text_size_px as number) || sizeMap[subject.text_size as string];
            if (sz) setSubjectSize(sz);
            const psz = (prefix.text_size_px as number) || sizeMap[prefix.text_size as string];
            if (psz) setPrefixSize(psz);
            if (typeof subject.position_y_frac === "number") setSubjectY(subject.position_y_frac);
            if (typeof prefix.position_y_frac === "number") setPrefixY(prefix.position_y_frac);
            break;
          }
        }
      })
      .catch(() => {});
  }, [templateId]);

  const handlePreview = useCallback(async () => {
    setLoading(true);
    try {
      const res = await adminTextPreview(templateId, {
        subject_size_px: subjectSize,
        subject_y_frac: subjectY,
        prefix_size_px: prefixSize,
        prefix_y_frac: prefixY,
      } as TextPreviewParams);
      setPreviewImg(`data:image/png;base64,${res.image_base64}`);
    } catch (err) {
      // Preview is non-critical — silently ignore (endpoint may not be deployed yet)
      console.warn("Text preview failed:", err instanceof Error ? err.message : err);
    } finally {
      setLoading(false);
    }
  }, [templateId, subjectSize, subjectY, prefixSize, prefixY]);

  // Auto-preview on slider change (debounced)
  useEffect(() => {
    const timer = setTimeout(() => {
      handlePreview();
    }, 300);
    return () => clearTimeout(timer);
  }, [handlePreview]);

  const handleApply = useCallback(async () => {
    setApplying(true);
    setApplied(false);
    try {
      const current = await adminGetRecipe(templateId);
      const recipe = current.recipe as Record<string, unknown>;
      const slots = recipe.slots as Array<{ text_overlays?: Array<Record<string, unknown>>; overlays?: Array<Record<string, unknown>> }> | undefined;

      if (slots) {
        for (const slot of slots) {
          const overlays = slot.text_overlays || slot.overlays;
          if (!overlays || overlays.length < 2) continue;
          const subject = overlays.find(
            (o) => o.effect === "font-cycle" || (o.text_size as string)?.match(/jumbo|xxlarge|xlarge/),
          );
          const prefix = overlays.find(
            (o) => o !== subject && typeof o.sample_text === "string",
          );
          if (subject && prefix) {
            subject.text_size_px = subjectSize;
            subject.position_y_frac = subjectY;
            prefix.text_size_px = prefixSize;
            prefix.position_y_frac = prefixY;
          }
        }
      }

      await adminSaveRecipe(templateId, {
        recipe,
        base_version_id: current.version_id,
      });
      setApplied(true);
      setTimeout(() => setApplied(false), 2000);
    } catch (err) {
      alert(err instanceof Error ? err.message : "Apply failed");
    } finally {
      setApplying(false);
    }
  }, [templateId, subjectSize, subjectY, prefixSize, prefixY]);

  return (
    <div className="border border-zinc-800 rounded p-5 space-y-5">
      <div className="flex items-center justify-between">
        <div>
          <h3 className="text-sm font-medium text-white">Text Tuning</h3>
          <p className="text-xs text-zinc-500 mt-0.5">Adjust size &amp; position, preview live, then apply.</p>
        </div>
        <div className="flex items-center gap-3">
          <button
            onClick={handleApply}
            disabled={applying}
            className="px-4 py-2 text-sm bg-green-700 hover:bg-green-600 text-white rounded disabled:opacity-50"
          >
            {applying ? "Applying..." : "Apply to Recipe"}
          </button>
          {applied && <span className="text-green-400 text-sm">Applied!</span>}
        </div>
      </div>

      <div className="grid grid-cols-2 gap-4">
        {/* Subject (PERU) */}
        <div className="bg-zinc-900/50 rounded-lg p-3 space-y-2 border border-zinc-800">
          <p className="text-xs text-yellow-400 font-medium uppercase tracking-wide">Subject (PERU)</p>
          <div className="flex items-center gap-2">
            <label className="text-xs text-zinc-500 w-8 shrink-0">Size</label>
            <input type="range" min={80} max={400} step={5} value={subjectSize}
              onChange={(e) => setSubjectSize(Number(e.target.value))}
              className="flex-1 accent-yellow-400 h-1" />
            <code className="text-xs text-yellow-400 font-bold w-12 text-right">{subjectSize}px</code>
          </div>
          <div className="flex gap-1">
            {[150, 199, 250, 320].map((s) => (
              <button key={s} onClick={() => setSubjectSize(s)}
                className={`px-2 py-0.5 text-xs rounded ${subjectSize === s ? "bg-yellow-400 text-black" : "bg-zinc-800 text-zinc-400 hover:bg-zinc-700"}`}>
                {s}
              </button>
            ))}
          </div>
          <div className="flex items-center gap-2">
            <label className="text-xs text-zinc-500 w-8 shrink-0">Y</label>
            <input type="range" min={0.2} max={0.7} step={0.001} value={subjectY}
              onChange={(e) => setSubjectY(Number(e.target.value))}
              className="flex-1 accent-yellow-400 h-1" />
            <code className="text-xs text-yellow-400 font-bold w-12 text-right">{subjectY.toFixed(4)}</code>
          </div>
        </div>

        {/* Prefix (Welcome to) */}
        <div className="bg-zinc-900/50 rounded-lg p-3 space-y-2 border border-zinc-800">
          <p className="text-xs text-white font-medium uppercase tracking-wide">Prefix (Welcome to)</p>
          <div className="flex items-center gap-2">
            <label className="text-xs text-zinc-500 w-8 shrink-0">Size</label>
            <input type="range" min={16} max={96} step={2} value={prefixSize}
              onChange={(e) => setPrefixSize(Number(e.target.value))}
              className="flex-1 accent-white h-1" />
            <code className="text-xs text-white font-bold w-12 text-right">{prefixSize}px</code>
          </div>
          <div className="flex items-center gap-2">
            <label className="text-xs text-zinc-500 w-8 shrink-0">Y</label>
            <input type="range" min={0.2} max={0.7} step={0.001} value={prefixY}
              onChange={(e) => setPrefixY(Number(e.target.value))}
              className="flex-1 accent-white h-1" />
            <code className="text-xs text-white font-bold w-12 text-right">{prefixY.toFixed(4)}</code>
          </div>
        </div>
      </div>

      {/* Preview */}
      <div className="flex justify-center">
        {loading && !previewImg && (
          <div className="w-[320px] aspect-[9/16] bg-zinc-900 rounded-lg flex items-center justify-center">
            <div className="w-6 h-6 border-2 border-zinc-600 border-t-white rounded-full animate-spin" />
          </div>
        )}
        {previewImg && (
          <div className="relative">
            <img
              src={previewImg}
              alt="Text preview"
              className="w-[320px] rounded-lg border border-zinc-700 shadow-lg"
            />
            {loading && (
              <div className="absolute inset-0 bg-black/40 rounded-lg flex items-center justify-center">
                <div className="w-6 h-6 border-2 border-zinc-600 border-t-white rounded-full animate-spin" />
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
