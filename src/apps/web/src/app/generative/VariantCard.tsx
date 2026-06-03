"use client";

import { useEffect, useRef, useState } from "react";
import {
  INTRO_SIZE_MAX,
  INTRO_SIZE_MIN,
  INTRO_SIZE_STEP,
  type GenerativeStyleSet,
  type GenerativeVariant,
} from "@/lib/generative-api";
import type { MusicTrackSummary } from "@/lib/music-api";
import { downloadVideo } from "@/lib/download-video";

export const TEXT_MODE_LABEL: Record<string, string> = {
  lyrics: "Lyrics",
  agent_text: "AI text",
  none: "No text",
};

/**
 * One generative-edit variant: video preview + the re-render controls (edit text,
 * remove text, swap song, change style). Shared by the public generative page and
 * the admin generative detail page — both drive the same public endpoints, so the
 * card stays presentation-only and takes the actions as callbacks.
 */
export function VariantCard({
  variant,
  tracks,
  styleSets,
  onSwap,
  onRetext,
  onRemoveText,
  onChangeStyle,
  onResize,
  onSetMix,
}: {
  variant: GenerativeVariant;
  tracks: MusicTrackSummary[];
  styleSets: GenerativeStyleSet[];
  onSwap: (trackId: string) => Promise<void>;
  onRetext: (text: string) => Promise<void>;
  onRemoveText: () => Promise<void>;
  onChangeStyle: (styleSetId: string) => Promise<void>;
  onResize?: (textSizePx: number) => Promise<void>;
  onSetMix?: (mix: number) => Promise<void>;
}) {
  const [busy, setBusy] = useState(false);
  const rendering = variant.render_status === "rendering" || busy;
  const failed = variant.render_status === "failed";

  // Voice/footage mix for voiceover variants. The slider updates local state
  // immediately (responsive) but only fires the re-render after a debounce so a
  // drag doesn't enqueue a render per step. `music_track_id` decides whether the
  // "Footage" side of the mix is the original audio or the matched music bed.
  const isVoiceover = variant.variant_id.startsWith("voiceover");
  const [mix, setMix] = useState<number>(variant.mix ?? 1);
  const mixTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  // Re-sync local mix when the server reports a new value (e.g. after a render).
  useEffect(() => {
    setMix(variant.mix ?? 1);
  }, [variant.mix]);
  useEffect(() => {
    return () => {
      if (mixTimer.current) clearTimeout(mixTimer.current);
    };
  }, []);
  const bedLabel = variant.music_track_id !== null ? "Music" : "Footage";
  // The ±size nudge applies only to the AI-intro text variants, and only once a
  // size exists to nudge from (set on first render). curPx is the current pinned
  // or agent-decided size; null hides the control.
  const curPx =
    variant.text_mode === "agent_text" ? variant.intro_text_size_px : null;

  const run = async (fn: () => Promise<void>) => {
    setBusy(true);
    try {
      await fn();
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="rounded-lg border border-zinc-800 bg-zinc-950 p-3">
      <div className="mb-2 flex items-center justify-between">
        <span className="rounded bg-zinc-800 px-2 py-0.5 text-xs text-zinc-300">
          {TEXT_MODE_LABEL[variant.text_mode] ?? variant.text_mode}
          {variant.track_title ? ` · ${variant.track_title}` : " · Original audio"}
        </span>
      </div>

      <div className="aspect-[9/16] w-full overflow-hidden rounded bg-black">
        {rendering ? (
          <div className="flex h-full items-center justify-center text-sm text-zinc-500">
            Rendering…
          </div>
        ) : failed ? (
          <div className="flex h-full items-center justify-center px-3 text-center text-sm text-red-300">
            {variant.error ?? "Render failed"}
          </div>
        ) : variant.output_url ? (
          <video src={variant.output_url} controls className="h-full w-full object-contain" />
        ) : (
          <div className="flex h-full items-center justify-center text-sm text-zinc-600">
            No preview
          </div>
        )}
      </div>

      <div className="mt-3 flex flex-wrap gap-2">
        {!rendering && !failed && variant.output_url && (
          <button
            onClick={() =>
              downloadVideo(variant.output_url!, `nova-${variant.variant_id}.mp4`)
            }
            className="rounded border border-zinc-700 px-2 py-1 text-xs text-zinc-300 disabled:opacity-40"
          >
            Download
          </button>
        )}
        <button
          disabled={rendering}
          onClick={() => {
            const next = prompt("New intro text:");
            if (next && next.trim()) run(() => onRetext(next.trim()));
          }}
          className="rounded border border-zinc-700 px-2 py-1 text-xs text-zinc-300 disabled:opacity-40"
        >
          Edit text
        </button>
        <button
          disabled={rendering}
          onClick={() => run(onRemoveText)}
          className="rounded border border-zinc-700 px-2 py-1 text-xs text-zinc-300 disabled:opacity-40"
        >
          Remove text
        </button>
        {/* ±size nudge — re-renders the AI intro at a bigger/smaller font. Hidden
            on lyrics / no-text variants (no resizable overlay) and until a size
            exists to nudge from. Clamped both client- and server-side. */}
        {onResize && curPx != null && (
          <div className="flex items-center overflow-hidden rounded border border-zinc-700">
            <button
              disabled={rendering || curPx <= INTRO_SIZE_MIN}
              onClick={() =>
                run(() => onResize(Math.max(INTRO_SIZE_MIN, curPx - INTRO_SIZE_STEP)))
              }
              aria-label="Smaller intro text"
              className="px-2.5 py-1 text-xs text-zinc-300 hover:bg-zinc-800 disabled:opacity-40"
            >
              A−
            </button>
            <span
              title={
                variant.intro_size_source === "user"
                  ? `Your size · ${curPx}px`
                  : `Auto-sized · ${curPx}px`
              }
              className="select-none border-x border-zinc-700 px-2 py-1 text-xs tabular-nums text-zinc-500"
            >
              {variant.intro_size_source === "user" ? `${curPx}` : `${curPx} auto`}
            </span>
            <button
              disabled={rendering || curPx >= INTRO_SIZE_MAX}
              onClick={() =>
                run(() => onResize(Math.min(INTRO_SIZE_MAX, curPx + INTRO_SIZE_STEP)))
              }
              aria-label="Bigger intro text"
              className="px-2.5 py-1 text-xs text-zinc-300 hover:bg-zinc-800 disabled:opacity-40"
            >
              A+
            </button>
          </div>
        )}
        {/* Change text style — applies to ALL variants (the set governs the AI
            intro on text variants and the lyric typography on the lyrics variant). */}
        {styleSets.length > 0 && (
          <select
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
        {tracks.length > 0 && variant.music_track_id !== null && (
          <select
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

      {/* Voice / bed mix — voiceover variants only. Debounced re-render; disabled
          while the variant is rendering, mirroring the other controls. */}
      {isVoiceover && onSetMix && (
        <div className="mt-3">
          <div className="mb-1 flex items-center justify-between text-xs text-zinc-400">
            <label htmlFor={`mix-${variant.variant_id}`}>Voice / {bedLabel}</label>
            <span className="tabular-nums text-zinc-500">{Math.round(mix * 100)}% voice</span>
          </div>
          <input
            id={`mix-${variant.variant_id}`}
            type="range"
            min={0}
            max={1}
            step={0.05}
            value={mix}
            disabled={rendering}
            aria-label={`Voice versus ${bedLabel.toLowerCase()} mix`}
            onChange={(e) => {
              const next = Number(e.target.value);
              setMix(next);
              if (mixTimer.current) clearTimeout(mixTimer.current);
              mixTimer.current = setTimeout(() => {
                run(() => onSetMix(next));
              }, 600);
            }}
            className="w-full accent-white disabled:opacity-40"
          />
        </div>
      )}
    </div>
  );
}
