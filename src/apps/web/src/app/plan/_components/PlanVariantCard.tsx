"use client";

import { useState } from "react";
import type { PlanItemVariant } from "@/lib/plan-api";
import type { GenerativeStyleSet } from "@/lib/generative-api";
import type { MusicTrackSummary } from "@/lib/music-api";

const TEXT_MODE_LABEL: Record<string, string> = {
  lyrics: "Lyrics",
  agent_text: "AI text",
  none: "No text",
};

/**
 * One plan-item render variant: the video preview plus plan-styled re-render
 * controls (edit text inline, remove text, swap song, change style). The heavy
 * lifting is server-side — this card is presentation-only and takes each action
 * as a callback, mirroring the generative VariantCard but matched to the plan
 * page's aesthetic (inline text field instead of a browser prompt()).
 */
export default function PlanVariantCard({
  variant,
  tracks,
  styleSets,
  onSwap,
  onRetext,
  onRemoveText,
  onChangeStyle,
}: {
  variant: PlanItemVariant;
  tracks: MusicTrackSummary[];
  styleSets: GenerativeStyleSet[];
  onSwap: (trackId: string) => Promise<void>;
  onRetext: (text: string) => Promise<void>;
  onRemoveText: () => Promise<void>;
  onChangeStyle: (styleSetId: string) => Promise<void>;
}) {
  const [busy, setBusy] = useState(false);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const rendering = variant.render_status === "rendering" || busy;
  const failed = variant.render_status === "failed";
  // Swap only applies to song variants — the original-audio edit has no track.
  const canSwap = tracks.length > 0 && variant.music_track_id != null;

  const run = async (fn: () => Promise<void>) => {
    setBusy(true);
    try {
      await fn();
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="rounded-lg border border-zinc-800 bg-zinc-950/40 p-3">
      <div className="mb-2 flex items-center gap-2">
        <span className="rounded bg-zinc-800 px-2 py-0.5 text-xs text-zinc-300">
          {TEXT_MODE_LABEL[variant.text_mode ?? ""] ?? "Edit"}
        </span>
        <span className="truncate text-xs text-zinc-500">
          {variant.track_title ?? "Original audio"}
        </span>
      </div>

      <div className="aspect-[9/16] w-full overflow-hidden rounded bg-black">
        {rendering ? (
          <div className="flex h-full items-center justify-center text-sm text-amber-300">
            Rendering…
          </div>
        ) : failed ? (
          <div className="flex h-full items-center justify-center px-3 text-center text-sm text-red-300">
            This variant failed — try editing again.
          </div>
        ) : variant.output_url ? (
          <video src={variant.output_url} controls className="h-full w-full object-contain" />
        ) : (
          <div className="flex h-full items-center justify-center text-sm text-zinc-600">
            No preview yet
          </div>
        )}
      </div>

      {/* Inline text editor (no browser prompt) */}
      {editing ? (
        <form
          className="mt-3 flex flex-col gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            const next = draft.trim();
            if (!next) return;
            setEditing(false);
            void run(() => onRetext(next));
          }}
        >
          <input
            autoFocus
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            placeholder="New intro text…"
            className="rounded border border-zinc-700 bg-zinc-900 px-2 py-1 text-sm text-white placeholder:text-zinc-600 focus:border-amber-400 focus:outline-none"
          />
          <div className="flex gap-2">
            <button
              type="submit"
              disabled={rendering || !draft.trim()}
              className="rounded-full bg-amber-400 px-3 py-1 text-xs font-medium text-black transition-colors hover:bg-amber-300 disabled:cursor-not-allowed disabled:bg-zinc-700 disabled:text-zinc-400"
            >
              Save
            </button>
            <button
              type="button"
              onClick={() => setEditing(false)}
              className="rounded-full border border-zinc-700 px-3 py-1 text-xs text-zinc-300 hover:bg-zinc-800"
            >
              Cancel
            </button>
          </div>
        </form>
      ) : (
        <div className="mt-3 flex flex-wrap gap-2">
          <button
            disabled={rendering}
            onClick={() => {
              setDraft("");
              setEditing(true);
            }}
            className="rounded border border-zinc-700 px-2 py-1 text-xs text-zinc-300 hover:bg-zinc-800 disabled:opacity-40"
          >
            Edit text
          </button>
          <button
            disabled={rendering}
            onClick={() => run(onRemoveText)}
            className="rounded border border-zinc-700 px-2 py-1 text-xs text-zinc-300 hover:bg-zinc-800 disabled:opacity-40"
          >
            Remove text
          </button>
          {/* Text style — governs the AI intro on text variants + lyric typography. */}
          {styleSets.length > 0 && (
            <select
              aria-label="Text style"
              disabled={rendering}
              value={variant.style_set_id ?? ""}
              onChange={(e) => {
                if (e.target.value && e.target.value !== variant.style_set_id) {
                  run(() => onChangeStyle(e.target.value));
                }
              }}
              className="rounded border border-zinc-700 bg-zinc-900 px-2 py-1 text-xs text-zinc-300 disabled:opacity-40"
            >
              <option value="" disabled>
                Style…
              </option>
              {styleSets.map((s) => (
                <option key={s.id} value={s.id}>
                  {s.label}
                </option>
              ))}
            </select>
          )}
          {canSwap && (
            <select
              aria-label="Swap song"
              disabled={rendering}
              value=""
              onChange={(e) => {
                if (e.target.value) run(() => onSwap(e.target.value));
              }}
              className="rounded border border-zinc-700 bg-zinc-900 px-2 py-1 text-xs text-zinc-300 disabled:opacity-40"
            >
              <option value="">Swap song…</option>
              {tracks.map((t) => (
                <option key={t.id} value={t.id}>
                  {t.title}
                </option>
              ))}
            </select>
          )}
        </div>
      )}
    </div>
  );
}
