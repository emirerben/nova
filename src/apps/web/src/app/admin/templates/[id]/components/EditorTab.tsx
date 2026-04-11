"use client";

import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from "react";
import {
  adminCreateRerenderJob,
  adminCreateTestJob,
  adminGetRecipe,
  adminSaveRecipe,
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
      const res = await adminSaveRecipe(template.id, {
        recipe: state.recipe as unknown as Record<string, unknown>,
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

  const hasVideo = latestTestJob?.output_url && !videoError;
  const isRerunning = rerunJobId !== null && rerunPoller.polling;

  // ── Layout: side-by-side with video, or vertical without ──────────────

  const editorContent = (
    <div className="flex-1 space-y-4 min-w-0">
      {/* Mini-timeline: slot bars */}
      <div className="border border-zinc-800 rounded p-3">
        <div className="flex items-center gap-1 mb-2">
          <span className="text-xs text-zinc-500 mr-2">Timeline</span>
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
        />
      </div>

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
            url={latestTestJob!.output_url!}
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
