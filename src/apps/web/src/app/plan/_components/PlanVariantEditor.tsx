"use client";

import { useState } from "react";
import type { PlanItemVariant } from "@/lib/plan-api";
import type { GenerativeStyleSet } from "@/lib/generative-api";
import type { MusicTrackSummary } from "@/lib/music-api";
import SongPicker from "./SongPicker";
import StyleChip from "./StyleChip";

/**
 * Edit controls for the focused plan-item variant: caption (inline edit /
 * remove), text style (real-font preview chips), and song (art + audio preview).
 * Presentation-only — every mutation is a callback; the heavy lifting (re-render)
 * is server-side. Replaces the cramped per-card control row from #389 with a
 * legible, grouped column matched to the plan page aesthetic.
 *
 * All controls disable while the variant is re-rendering (the parent flips
 * `render_status` to "rendering" optimistically); the hero shows the spinner.
 */
export default function PlanVariantEditor({
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
    <div className="space-y-6">
      {/* ── Caption ─────────────────────────────────────────────── */}
      <section>
        <h3 className="mb-2 text-sm font-semibold text-zinc-200">Caption</h3>
        {editing ? (
          <form
            className="flex flex-col gap-2"
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
              className="rounded border border-zinc-700 bg-zinc-900 px-3 py-2 text-sm text-white placeholder:text-zinc-600 focus:border-amber-400 focus:outline-none"
            />
            <div className="flex gap-2">
              <button
                type="submit"
                disabled={rendering || !draft.trim()}
                className="rounded-full bg-amber-400 px-4 py-2 text-sm font-medium text-black transition-colors hover:bg-amber-300 disabled:cursor-not-allowed disabled:bg-zinc-700 disabled:text-zinc-400"
              >
                Save
              </button>
              <button
                type="button"
                onClick={() => setEditing(false)}
                className="rounded-full border border-zinc-700 px-4 py-2 text-sm text-zinc-300 hover:bg-zinc-800"
              >
                Cancel
              </button>
            </div>
          </form>
        ) : (
          <div className="flex flex-wrap gap-2">
            <button
              type="button"
              disabled={rendering}
              onClick={() => {
                setDraft("");
                setEditing(true);
              }}
              className="rounded-full border border-zinc-700 px-4 py-2 text-sm text-zinc-200 transition-colors hover:bg-zinc-800 disabled:cursor-not-allowed disabled:opacity-40"
            >
              Edit text
            </button>
            <button
              type="button"
              disabled={rendering}
              onClick={() => run(onRemoveText)}
              className="rounded-full border border-zinc-700 px-4 py-2 text-sm text-zinc-200 transition-colors hover:bg-zinc-800 disabled:cursor-not-allowed disabled:opacity-40"
            >
              Remove text
            </button>
          </div>
        )}
      </section>

      {/* ── Text style ──────────────────────────────────────────── */}
      {styleSets.length > 0 && (
        <section>
          <h3 className="mb-2 text-sm font-semibold text-zinc-200">Style</h3>
          <div role="radiogroup" aria-label="Text style" className="flex flex-wrap gap-2">
            {styleSets.map((s) => (
              <StyleChip
                key={s.id}
                styleSet={s}
                selected={s.id === variant.style_set_id}
                disabled={rendering}
                onSelect={() => {
                  if (s.id !== variant.style_set_id) run(() => onChangeStyle(s.id));
                }}
              />
            ))}
          </div>
        </section>
      )}

      {/* ── Song ────────────────────────────────────────────────── */}
      {canSwap && (
        <section>
          <h3 className="mb-2 text-sm font-semibold text-zinc-200">Song</h3>
          <SongPicker
            tracks={tracks}
            currentTrackId={variant.music_track_id ?? null}
            disabled={rendering}
            onSelect={(trackId) => run(() => onSwap(trackId))}
          />
        </section>
      )}
    </div>
  );
}
