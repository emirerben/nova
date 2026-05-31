"use client";

import { useState } from "react";
import type { PlanItemVariant } from "@/lib/plan-api";
import {
  INTRO_SIZE_MAX,
  INTRO_SIZE_MIN,
  INTRO_SIZE_STEP,
  type GenerativeStyleSet,
} from "@/lib/generative-api";
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
  onResize,
}: {
  variant: PlanItemVariant;
  tracks: MusicTrackSummary[];
  styleSets: GenerativeStyleSet[];
  onSwap: (trackId: string) => Promise<void>;
  onRetext: (text: string) => Promise<void>;
  onRemoveText: () => Promise<void>;
  onChangeStyle: (styleSetId: string) => Promise<void>;
  onResize?: (textSizePx: number) => Promise<void>;
}) {
  const [busy, setBusy] = useState(false);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const rendering = variant.render_status === "rendering" || busy;
  // Swap only applies to song variants — the original-audio edit has no track.
  const canSwap = tracks.length > 0 && variant.music_track_id != null;
  // Text-size nudge: only the AI-intro variants have a resizable hero overlay,
  // and only once a size exists to nudge from (set on first render).
  const curPx =
    variant.text_mode === "agent_text" ? variant.intro_text_size_px ?? null : null;

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

      {/* ── Text size ───────────────────────────────────────────── */}
      {onResize && curPx != null && (
        <section>
          <h3 className="mb-2 text-sm font-semibold text-zinc-200">Text size</h3>
          <div className="flex items-center gap-3">
            <div className="flex items-center overflow-hidden rounded-full border border-zinc-700">
              <button
                type="button"
                disabled={rendering || curPx <= INTRO_SIZE_MIN}
                onClick={() =>
                  run(() => onResize(Math.max(INTRO_SIZE_MIN, curPx - INTRO_SIZE_STEP)))
                }
                aria-label="Smaller intro text"
                className="px-4 py-2 text-sm text-zinc-200 transition-colors hover:bg-zinc-800 disabled:cursor-not-allowed disabled:opacity-40"
              >
                A&minus;
              </button>
              <span className="border-x border-zinc-700 px-3 py-2 text-sm tabular-nums text-zinc-400">
                {variant.intro_size_source === "user" ? `${curPx}` : `${curPx} · auto`}
              </span>
              <button
                type="button"
                disabled={rendering || curPx >= INTRO_SIZE_MAX}
                onClick={() =>
                  run(() => onResize(Math.min(INTRO_SIZE_MAX, curPx + INTRO_SIZE_STEP)))
                }
                aria-label="Bigger intro text"
                className="px-4 py-2 text-base text-zinc-200 transition-colors hover:bg-zinc-800 disabled:cursor-not-allowed disabled:opacity-40"
              >
                A+
              </button>
            </div>
            <span className="text-xs text-zinc-500">
              {variant.intro_size_source === "user"
                ? "your size"
                : "auto-sized to the footage"}
            </span>
          </div>
        </section>
      )}

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
