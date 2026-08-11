"use client";

import { useEffect, useRef } from "react";
import type { EditorSuggestion, SuggestionCategory } from "@/lib/plan-api";
import type {
  DirectorAppliedReceipt,
  DirectorGenerationState,
} from "@/lib/edit-copilot/useEditDirector";

const CATEGORY_LABEL: Record<SuggestionCategory, string> = {
  hook_pacing: "Hook & pacing",
  text: "Text",
  audio: "Sound",
  effect: "Effect",
  transition: "Transition",
};

function formatTimeRange(startS: number, endS: number): string {
  const start = startS.toFixed(1);
  const end = endS.toFixed(1);
  return start === end ? `${start}s` : `${start}-${end}s`;
}

export function directorWillChange(suggestion: EditorSuggestion): string[] {
  const labels = new Set<string>();
  for (const operation of suggestion.ops) {
    if (operation.op === "apply_speech_cut_candidate") {
      labels.add("Remove the reviewed speech span and retime captions, text, and effects");
    } else if (["edit_text", "add_text", "remove_text", "patch_text_style", "set_text_timing"].includes(operation.op)) {
      labels.add("Text layer");
    } else if (operation.op.includes("caption")) {
      labels.add("Captions");
    } else if (operation.op.includes("sfx")) {
      labels.add("Sound effects");
    } else if (operation.op.includes("overlay")) {
      labels.add("Visual overlays");
    } else if (operation.op === "set_look_preset") {
      labels.add("Clip look");
    } else if (operation.op.includes("clip") || operation.op === "set_transition") {
      labels.add("Clip timing");
    } else if (operation.op.includes("camera_effect") || operation.op === "set_visual_fade") {
      labels.add("Visual effects");
    } else if (operation.op === "swap_music" || operation.op === "set_mix") {
      labels.add("Audio mix");
    } else {
      labels.add("Editor draft");
    }
  }
  return Array.from(labels);
}

function AppliedReceipt({
  receipt,
  historyVersion,
  onReveal,
}: {
  receipt: DirectorAppliedReceipt;
  historyVersion: number;
  onReveal: (receipt: DirectorAppliedReceipt) => void;
}) {
  const isCurrent =
    receipt.undoVersion === undefined || receipt.undoVersion === historyVersion;
  return (
    <article className="rounded-xl border border-lime-200 bg-lime-50/70 px-3 py-2.5">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="text-[10px] font-semibold uppercase tracking-[0.12em] text-lime-800">
            {isCurrent ? "Applied" : "Changed since"}
          </p>
          <p className="mt-0.5 truncate text-[12px] font-semibold text-[#27272a]">
            {receipt.title}
          </p>
        </div>
        <span className="shrink-0 text-[11px] tabular-nums text-[#71717a]">
          {formatTimeRange(receipt.startS, receipt.endS)}
        </span>
      </div>
      <div className="mt-1.5 space-y-1">
        {receipt.changes.map((change, index) => (
          <p key={`${change.label}-${index}`} className="text-[11px] leading-4 text-[#52525b]">
            <span className="font-medium text-[#3f3f46]">{change.label}</span>
            {change.count && change.count > 1 ? ` ×${change.count}` : ""}: {change.from} → {change.to}
          </p>
        ))}
      </div>
      {receipt.previewFocus && isCurrent ? (
        <div className="mt-1.5 flex items-center justify-between gap-2">
          <p className="text-[11px] text-lime-900">Showing this moment in preview.</p>
          <button
            type="button"
            onClick={() => onReveal(receipt)}
            className="min-h-11 shrink-0 rounded-lg px-2.5 text-[11px] font-semibold text-lime-900 hover:bg-lime-100 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500"
          >
            Show again
          </button>
        </div>
      ) : receipt.previewFocus ? (
        <p className="mt-1.5 text-[11px] text-[#71717a]">
          The preview has changed since this edit.
        </p>
      ) : null}
    </article>
  );
}

export default function DirectorSuggestions({
  suggestions,
  appliedReceipts,
  historyVersion,
  loading,
  error,
  modelUsed,
  fallbackReason,
  generation,
  serverRendering = false,
  onAccept,
  onDismiss,
  onRevealApplied,
  onRefresh,
  onCancelGeneration,
  canRestoreOriginalTiming = false,
  onRestoreOriginalTiming = () => {},
}: {
  suggestions: EditorSuggestion[];
  appliedReceipts: DirectorAppliedReceipt[];
  historyVersion: number;
  loading: boolean;
  error: string | null;
  modelUsed: string;
  fallbackReason: string | null;
  generation: DirectorGenerationState | null;
  serverRendering?: boolean;
  onAccept: (suggestion: EditorSuggestion) => void;
  onDismiss: (suggestion: EditorSuggestion) => void;
  onRevealApplied: (receipt: DirectorAppliedReceipt) => void;
  onRefresh: () => void;
  onCancelGeneration: () => void;
  canRestoreOriginalTiming?: boolean;
  onRestoreOriginalTiming?: () => void;
}) {
  const firstSuggestionRef = useRef<HTMLElement>(null);
  const firstSuggestionId = suggestions[0]?.id ?? null;

  useEffect(() => {
    if (!firstSuggestionId) return;
    firstSuggestionRef.current?.scrollIntoView?.({ block: "nearest" });
  }, [firstSuggestionId]);

  return (
    <section aria-label="Nova suggestions" className="space-y-2.5">
      <div className="flex items-center justify-between">
        <div>
          <h3 className="text-[12px] font-semibold uppercase tracking-[0.12em] text-[#3f3f46]">
            Nova suggests
          </h3>
          {modelUsed && (
            <p className="mt-0.5 text-[11px] text-[#71717a]">
              {fallbackReason ? "Fast fallback review" : "Deep creative review"}
            </p>
          )}
        </div>
        <button
          type="button"
          onClick={onRefresh}
          disabled={loading}
          className="min-h-11 rounded-full px-3 text-[11px] font-medium text-[#3f3f46] hover:bg-zinc-100 disabled:opacity-40 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500"
        >
          {loading ? "Reviewing…" : "Refresh"}
        </button>
      </div>

      {canRestoreOriginalTiming && (
        <button
          type="button"
          onClick={onRestoreOriginalTiming}
          disabled={serverRendering}
          className="min-h-11 w-full rounded-lg border border-zinc-200 bg-white px-3 text-[12px] font-medium text-[#3f3f46] hover:bg-zinc-50 disabled:opacity-40 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500"
        >
          Restore original timing
        </button>
      )}

      {loading && suggestions.length === 0 && (
        <div className="rounded-xl border border-zinc-200 bg-zinc-50 px-3 py-3 text-[12px] text-[#71717a]">
          Reading the hook, rhythm, sound, and visual treatment…
        </div>
      )}

      {error && (
        <div role="status" className="rounded-xl border border-zinc-200 bg-zinc-50 px-3 py-2 text-[12px] text-[#52525b]">
          {error}
        </div>
      )}

      {!loading && !error && suggestions.length === 0 && (
        <div className="rounded-xl border border-zinc-200 bg-zinc-50 px-3 py-3">
          <p className="text-[12px] font-semibold text-[#27272a]">No changes recommended</p>
          <p className="mt-0.5 text-[11px] leading-4 text-[#71717a]">
            Nova did not find a clear improvement for this draft. Refresh after your next edit.
          </p>
        </div>
      )}

      {serverRendering && (
        <div role="status" className="rounded-xl border border-zinc-200 bg-zinc-50 px-3 py-3 text-[12px] text-[#3f3f46]">
          Rebuilding the video with the reviewed cut. The current preview stays available until it succeeds.
        </div>
      )}

      {generation && (
        <div
          role="status"
          className="rounded-xl border border-zinc-200 bg-zinc-50 px-3 py-3"
        >
          <div className="flex items-center justify-between gap-3">
            <div>
              <p className="text-[12px] font-semibold text-[#27272a]">
                {generation.status === "normalizing"
                  ? "Preparing generated clip…"
                  : generation.status === "cancellation_requested"
                    ? "Cancelling…"
                    : "Generating clip…"}
              </p>
              <p className="mt-0.5 text-[11px] text-[#52525b]">
                Your current draft stays unchanged until this finishes.
              </p>
            </div>
            <button
              type="button"
              onClick={onCancelGeneration}
              disabled={generation.status === "cancellation_requested"}
              className="min-h-11 rounded-lg px-2.5 text-[11px] font-medium text-[#3f3f46] hover:bg-zinc-200 disabled:opacity-40 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500"
            >
              Cancel
            </button>
          </div>
          <div
            aria-hidden="true"
            className="mt-2 h-1.5 overflow-hidden rounded-full bg-[linear-gradient(110deg,#e4e4e7,45%,#f4f4f5,55%,#e4e4e7)] bg-[length:200%_100%] motion-safe:animate-shimmer"
          >
          </div>
        </div>
      )}

      {suggestions.map((suggestion, index) => (
        <article
          key={suggestion.id}
          ref={index === 0 ? firstSuggestionRef : undefined}
          className="rounded-xl border border-zinc-200 bg-white p-3 shadow-[0_1px_2px_rgba(12,12,14,0.04)]"
        >
          <div className="flex items-center justify-between gap-3">
            <span className="rounded-full bg-lime-100 px-2 py-1 text-[10px] font-semibold text-lime-800">
              {CATEGORY_LABEL[suggestion.category]}
            </span>
            <span className="text-[11px] tabular-nums text-[#71717a]">
              {formatTimeRange(suggestion.start_s, suggestion.end_s)}
            </span>
          </div>
          <h4 className="mt-2 text-[13px] font-semibold text-[#0c0c0e]">
            {suggestion.title}
          </h4>
          <p className="mt-1 text-[12px] leading-4 text-[#52525b]">
            {suggestion.rationale}
          </p>
          <p className="mt-1.5 text-[11px] leading-4 text-[#71717a]">
            {suggestion.expected_benefit}
          </p>
          <div className="mt-2 rounded-lg bg-zinc-50 px-2.5 py-2">
            <p className="text-[10px] font-semibold uppercase tracking-[0.1em] text-[#71717a]">
              Will change
            </p>
            {directorWillChange(suggestion).map((label) => (
              <p key={label} className="mt-1 text-[11px] leading-4 text-[#3f3f46]">{label}</p>
            ))}
          </div>
          <div className="mt-3 flex items-center gap-2">
            <button
              type="button"
              onClick={() => onAccept(suggestion)}
              disabled={generation !== null || serverRendering}
              className="min-h-11 flex-1 rounded-lg bg-[#0c0c0e] px-3 text-[12px] font-semibold text-white hover:opacity-85 disabled:cursor-not-allowed disabled:opacity-40 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500"
            >
              {suggestion.apply_mode === "omni_async"
                ? "Generate & add"
                : suggestion.apply_mode === "server_async"
                  ? "Apply & rebuild"
                  : "Accept"}
            </button>
            <button
              type="button"
              aria-label={`Dismiss ${suggestion.title}`}
              onClick={() => onDismiss(suggestion)}
              className="min-h-11 rounded-lg px-3 text-[12px] text-[#71717a] hover:bg-zinc-100 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500"
            >
              Dismiss
            </button>
          </div>
        </article>
      ))}

      {appliedReceipts.length > 0 && (
        <div aria-label="Applied Nova suggestions" aria-live="polite" className="space-y-2">
          {appliedReceipts.map((receipt) => (
            <AppliedReceipt
              key={receipt.id}
              receipt={receipt}
              historyVersion={historyVersion}
              onReveal={onRevealApplied}
            />
          ))}
        </div>
      )}
    </section>
  );
}
