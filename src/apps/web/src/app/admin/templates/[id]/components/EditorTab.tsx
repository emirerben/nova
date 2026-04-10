"use client";

import { useCallback, useEffect, useReducer, useRef } from "react";
import { adminGetRecipe, adminSaveRecipe } from "@/lib/admin-api";
import type { AdminTemplate } from "@/lib/admin-api";
import type {
  EditorAction,
  EditorSelection,
  EditorState,
  Recipe,
  RecipeSlot,
} from "./recipe-types";
import { EMPTY_INTERSTITIAL, EMPTY_OVERLAY } from "./recipe-types";
import { PropertyPanel } from "./PropertyPanel";

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

// ── EditorTab ───────────────────────────────────────────────────────────────

export function EditorTab({ template }: { template: AdminTemplate }) {
  const [state, dispatch] = useReducer(editorReducer, initialState);
  const savingRef = useRef(false);

  // Fetch recipe on mount
  useEffect(() => {
    let cancelled = false;
    adminGetRecipe(template.id)
      .then((res) => {
        if (cancelled) return;
        dispatch({ type: "LOAD_RECIPE", recipe: res.recipe as unknown as Recipe });
        // Store version metadata outside reducer (simple refs)
        stateRef.current = {
          versionId: res.version_id,
          versionNumber: res.version_number,
        };
      })
      .catch((err) => {
        if (!cancelled) {
          dispatch({
            type: "LOAD_RECIPE",
            recipe: null as unknown as Recipe,
          });
          // We'll show error via the state.recipe being null
        }
        console.error("Failed to load recipe:", err);
      });
    return () => { cancelled = true; };
  }, [template.id]);

  const stateRef = useRef({ versionId: "", versionNumber: 0 });

  const isDirty =
    state.recipe !== null &&
    state.savedRecipe !== null &&
    JSON.stringify(state.recipe) !== JSON.stringify(state.savedRecipe);

  const handleSave = useCallback(async () => {
    if (!state.recipe || savingRef.current) return;
    savingRef.current = true;

    try {
      const res = await adminSaveRecipe(template.id, {
        recipe: state.recipe as unknown as Record<string, unknown>,
        base_version_id: stateRef.current.versionId || null,
      });
      dispatch({
        type: "RESET_TO_SAVED",
        recipe: res.recipe as unknown as Recipe,
      });
      stateRef.current = {
        versionId: res.version_id,
        versionNumber: res.version_number,
      };
    } catch (err) {
      alert(err instanceof Error ? err.message : "Save failed");
    } finally {
      savingRef.current = false;
    }
  }, [state.recipe, template.id]);

  const handleReset = useCallback(() => {
    if (!state.savedRecipe) return;
    if (!confirm("Discard unsaved changes?")) return;
    dispatch({ type: "RESET_TO_SAVED", recipe: state.savedRecipe });
  }, [state.savedRecipe]);

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

  return (
    <div className="space-y-4">
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
              onSelect={() =>
                dispatch({
                  type: "SET_SELECTED",
                  selection: { type: "slot", slotIndex: i },
                })
              }
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
            disabled={!isDirty || savingRef.current}
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
          {isDirty && (
            <span className="text-xs text-amber-400">Unsaved changes</span>
          )}
        </div>

        <span className="text-xs text-zinc-500">
          Version {stateRef.current.versionNumber}
          {stateRef.current.versionId && (
            <> &middot; {stateRef.current.versionId.slice(0, 8)}</>
          )}
        </span>
      </div>
    </div>
  );
}

// ── Slot bar component ──────────────────────────────────────────────────────

function SlotBar({
  slot,
  index,
  totalDuration,
  isSelected,
  onSelect,
}: {
  slot: RecipeSlot;
  index: number;
  totalDuration: number;
  isSelected: boolean;
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
