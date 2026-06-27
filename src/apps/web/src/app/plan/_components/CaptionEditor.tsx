"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  applyPlanItemCaptions,
  setPlanItemCaptionFont,
  setPlanItemCaptions,
  type CaptionCue,
} from "../../../lib/plan-api";
import { INTRO_FONTS } from "../../../lib/overlay-constants";

/**
 * On-video caption editor (paused "Edit captions" mode).
 *
 * Plays the caption-FREE base video and overlays the editable cues as DOM text
 * synced to playback — styled to approximate the libass burn (TikTok-Sans-ish
 * bold, bottom-centered, thick outline) so what you edit looks like what burns.
 * Pause + tap a caption to edit that line; edits persist instantly (debounced
 * PATCH, no re-render). "Apply" reburns the edited cues onto the base.
 *
 * The burned video is the source of truth — the overlay is a close, editable
 * preview, not a pixel-exact mirror.
 */

// libass Default style → CSS, in container-query units so the caption scales
// with the 9:16 video box. ASS PlayRes is 1080x1920; Fontsize 78, MarginV 180,
// MarginL/R 80, Outline 4 (see captions._ass_header_for).
const CAPTION_FONT =
  "'TikTok Sans', 'Inter', system-ui, -apple-system, 'Segoe UI', sans-serif";
// Approximate the libass Outline=4 (at PlayResY 1920) — a ~0.2cqh stroke, NOT the
// 2x-thicker shadow we shipped first. Tight offsets, small blur, on all sides.
const OUTLINE =
  "0 0.2cqh 0.2cqh #000, 0.2cqh 0 0.2cqh #000, 0 -0.2cqh 0.2cqh #000, -0.2cqh 0 0.2cqh #000, 0.15cqh 0.15cqh 0.2cqh #000, -0.15cqh -0.15cqh 0.2cqh #000";

const captionTextStyle: React.CSSProperties = {
  fontFamily: CAPTION_FONT,
  // Bold (700) — the burn uses the TikTok Sans Bold face; 800 faux-bolds in the browser.
  fontWeight: 700,
  fontSize: "4.1cqh", // 78 / 1920
  lineHeight: 1.18,
  color: "#ffffff",
  textAlign: "center",
  maxWidth: "85cqw", // ~ (1080 - 2*80) / 1080
  whiteSpace: "pre-wrap",
  textShadow: OUTLINE,
};

// Caption font choices: "Default" (TikTok Sans) + every non-deprecated editor font
// (the same INTRO_FONTS the montage Font tab offers). `name` is the font-registry
// key sent to the backend; null = reset to default.
const CAPTION_FONT_OPTIONS: Array<{
  name: string | null;
  label: string;
  cssFamily: string;
  weight: number;
}> = [
  { name: null, label: "Default", cssFamily: CAPTION_FONT, weight: 700 },
  ...INTRO_FONTS.map((f) => ({
    name: f.name,
    label: f.name,
    cssFamily: f.cssFamily,
    weight: f.weight,
  })),
];

export default function CaptionEditor({
  itemId,
  variantId,
  baseVideoUrl,
  initialCues,
  initialFont = null,
  rendering = false,
  onApplied,
}: {
  itemId: string;
  variantId: string;
  baseVideoUrl: string;
  initialCues: CaptionCue[];
  initialFont?: string | null;
  rendering?: boolean;
  onApplied?: () => void;
}) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const saveTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const fontTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  // Tracks an in-flight font PATCH so Apply can wait for it before writing the final
  // font — otherwise a debounced earlier PATCH could land last and revert the choice.
  const fontInFlight = useRef<Promise<void> | null>(null);
  const [cues, setCues] = useState<CaptionCue[]>(() => initialCues.map((c) => ({ ...c })));
  const [time, setTime] = useState(0);
  const [duration, setDuration] = useState(0);
  const [paused, setPaused] = useState(true);
  const [editing, setEditing] = useState<number | null>(null);
  // Which surface the user opened the editor from. Only that surface autoFocuses,
  // so the on-video textarea and the cue-list input never both grab focus on mount
  // (two autoFocus fields blur each other → editing collapses before you can type).
  const [editSource, setEditSource] = useState<"video" | "list" | null>(null);
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [applying, setApplying] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Caption font (font-registry key; null = default). Applies to every cue.
  const [font, setFont] = useState<string | null>(initialFont);

  // The chosen font as a CSS stack (for the live preview) — falls back to the
  // default TikTok-Sans stack for null/unknown. The burn weight is bold, so the
  // preview keeps fontWeight 700 regardless of the picked face.
  const fontStack = useMemo(() => {
    if (!font) return CAPTION_FONT;
    return INTRO_FONTS.find((f) => f.name === font)?.cssFamily ?? CAPTION_FONT;
  }, [font]);
  const activeCaptionStyle = useMemo<React.CSSProperties>(
    () => ({ ...captionTextStyle, fontFamily: fontStack }),
    [fontStack],
  );

  // Edits flow local → server (this is the only editor), so the cues are
  // authoritative for the lifetime of the mount: we do NOT re-pull from the
  // status poll, which could carry a stale snapshot (taken before our last PATCH
  // committed) and silently revert a just-fixed word. Reopening the editor
  // remounts and re-seeds from the freshest server cues via the useState init.
  useEffect(() => {
    return () => {
      if (saveTimer.current) clearTimeout(saveTimer.current);
      if (fontTimer.current) clearTimeout(fontTimer.current);
    };
  }, []);

  // Persist one font choice, recording the in-flight promise so Apply can await it.
  const sendFont = useCallback(
    (value: string | null) => {
      const p = setPlanItemCaptionFont(itemId, variantId, value)
        .then(() => {})
        .catch(() => {})
        .finally(() => {
          if (fontInFlight.current === p) fontInFlight.current = null;
        });
      fontInFlight.current = p;
      return p;
    },
    [itemId, variantId],
  );

  const chooseFont = useCallback(
    (name: string | null) => {
      setFont(name);
      if (fontTimer.current) clearTimeout(fontTimer.current);
      // Debounce; a failure is non-fatal — Apply re-sends the final font.
      fontTimer.current = setTimeout(() => void sendFont(name), 400);
    },
    [sendFont],
  );

  const activeIndex = useMemo(
    () => cues.findIndex((c) => time >= c.start_s && time < c.end_s),
    [cues, time],
  );

  const persistDebounced = useCallback(
    (next: CaptionCue[]) => {
      if (saveTimer.current) clearTimeout(saveTimer.current);
      saveTimer.current = setTimeout(async () => {
        setSaving(true);
        setError(null);
        try {
          await setPlanItemCaptions(itemId, variantId, next);
          setDirty(false);
        } catch {
          // Keep dirty; Apply re-sends the latest cues so nothing is lost.
        } finally {
          setSaving(false);
        }
      }, 600);
    },
    [itemId, variantId],
  );

  const updateCue = useCallback(
    (i: number, text: string) => {
      setCues((prev) => {
        const next = prev.map((c, idx) => (idx === i ? { ...c, text } : c));
        setDirty(true);
        persistDebounced(next);
        return next;
      });
    },
    [persistDebounced],
  );

  const stopEditing = useCallback(() => {
    setEditing(null);
    setEditSource(null);
  }, []);

  // Blur exits edit mode ONLY when focus leaves the caption editor entirely.
  // Moving focus between the two editors of the same cue (on-video textarea and
  // the cue-list input — both tagged data-caption-edit) must keep edit mode open;
  // otherwise the field you just left would clear `editing` and unmount the field
  // you just entered, so no keystroke ever lands.
  const handleEditorBlur = useCallback((e: React.FocusEvent) => {
    const next = e.relatedTarget as HTMLElement | null;
    if (next?.dataset?.captionEdit === "1") return;
    setEditing(null);
    setEditSource(null);
  }, []);

  const jumpToCue = useCallback(
    (i: number) => {
      const v = videoRef.current;
      if (!v || !cues[i]) return;
      v.pause();
      v.currentTime = Math.min(cues[i].start_s + 0.02, Math.max(0, (v.duration || 0) - 0.05));
      setEditing(i);
      setEditSource("list");
    },
    [cues],
  );

  const togglePlay = useCallback(() => {
    const v = videoRef.current;
    if (!v) return;
    if (v.paused) {
      stopEditing();
      void v.play();
    } else {
      v.pause();
    }
  }, [stopEditing]);

  const apply = useCallback(async () => {
    setApplying(true);
    setError(null);
    if (saveTimer.current) clearTimeout(saveTimer.current);
    if (fontTimer.current) clearTimeout(fontTimer.current);
    try {
      // Let any in-flight debounced font PATCH settle FIRST, then write the final
      // cues + font LAST, so the reburn always reads the user's latest font choice.
      await fontInFlight.current;
      await setPlanItemCaptions(itemId, variantId, cues);
      await sendFont(font);
      setDirty(false);
      await applyPlanItemCaptions(itemId, variantId);
      onApplied?.();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Couldn't apply caption changes");
    } finally {
      setApplying(false);
    }
  }, [itemId, variantId, cues, font, sendFont, onApplied]);

  const active = activeIndex >= 0 ? cues[activeIndex] : null;
  const busy = applying || rendering;

  return (
    <div className="space-y-3">
      <p className="text-xs text-[#71717a]">
        Pause and tap a caption to fix a word. Changes save as you type; hit{" "}
        <span className="font-medium">Apply</span> to bake them into the video.
      </p>

      <div className="mx-auto w-full max-w-[280px]">
        <div
          className="relative overflow-hidden rounded-2xl bg-black"
          style={{ aspectRatio: "9 / 16", containerType: "size" } as React.CSSProperties}
        >
          <video
            ref={videoRef}
            src={baseVideoUrl}
            playsInline
            preload="metadata"
            onLoadedMetadata={(e) => setDuration(e.currentTarget.duration || 0)}
            onTimeUpdate={(e) => setTime(e.currentTarget.currentTime)}
            onPlay={() => {
              setPaused(false);
              stopEditing();
            }}
            onPause={() => setPaused(true)}
            onClick={() => {
              // tap the frame (not a caption) → play/pause
              if (editing === null) togglePlay();
            }}
            className="absolute inset-0 h-full w-full object-contain"
          />

          {/* caption overlay — bottom-centered like the burn (MarginV 180/1920) */}
          {active && (
            <div
              className="absolute inset-x-0 flex justify-center"
              style={{ bottom: "9.4cqh" }}
            >
              {paused && editing === activeIndex ? (
                <textarea
                  autoFocus={editSource === "video"}
                  data-caption-edit="1"
                  rows={2}
                  value={active.text}
                  onChange={(e) => updateCue(activeIndex, e.target.value)}
                  onBlur={handleEditorBlur}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" && !e.shiftKey) {
                      e.preventDefault();
                      stopEditing();
                    }
                  }}
                  className="resize-none rounded-md border-2 border-lime-400 bg-black/60 px-2 text-center outline-none"
                  style={activeCaptionStyle}
                  aria-label="Edit caption line"
                />
              ) : (
                <span
                  role={paused ? "button" : undefined}
                  tabIndex={paused ? 0 : -1}
                  aria-label={paused ? `Edit caption: ${active.text}` : undefined}
                  onClick={() => {
                    if (paused) {
                      setEditing(activeIndex);
                      setEditSource("video");
                    }
                  }}
                  onKeyDown={(e) => {
                    if (paused && (e.key === "Enter" || e.key === " ")) {
                      e.preventDefault();
                      setEditing(activeIndex);
                      setEditSource("video");
                    }
                  }}
                  className={paused ? "cursor-text" : "pointer-events-none"}
                  style={activeCaptionStyle}
                >
                  {active.text}
                </span>
              )}
            </div>
          )}

          {busy && (
            <div className="absolute inset-0 flex items-center justify-center bg-black/40 text-sm text-white">
              {applying ? "Applying…" : "Rendering…"}
            </div>
          )}
        </div>

        {/* minimal transport: play/pause + scrubber */}
        <div className="mt-2 flex items-center gap-2">
          <button
            type="button"
            onClick={togglePlay}
            className="rounded-full border border-zinc-200 px-3 py-1 text-sm hover:border-lime-400"
            aria-label={paused ? "Play" : "Pause"}
          >
            {paused ? "►" : "❚❚"}
          </button>
          <input
            type="range"
            min={0}
            max={duration || 0}
            step={0.05}
            value={Math.min(time, duration || 0)}
            onChange={(e) => {
              const v = videoRef.current;
              if (v) {
                v.currentTime = Number(e.target.value);
                v.pause();
              }
            }}
            className="h-1 flex-1 cursor-pointer accent-lime-600"
            aria-label="Scrub video"
          />
        </div>
      </div>

      {/* Caption font — applies to every cue (both sentence + word styles). Reuses
          the editor's fonts; the preview updates live, Apply burns the choice. */}
      <div>
        <p className="mb-1.5 text-[11px] font-semibold uppercase tracking-[0.12em] text-[#a1a1aa]">
          Caption font
        </p>
        <div className="flex gap-2 overflow-x-auto pb-1">
          {CAPTION_FONT_OPTIONS.map((opt) => {
            const active = (font ?? null) === opt.name;
            return (
              <button
                key={opt.name ?? "__default__"}
                type="button"
                aria-pressed={active}
                disabled={busy}
                onClick={() => chooseFont(opt.name)}
                style={{ fontFamily: opt.cssFamily, fontWeight: opt.weight }}
                className={`shrink-0 whitespace-nowrap rounded-lg border px-3 py-1.5 text-sm transition-colors disabled:cursor-not-allowed disabled:opacity-60 ${
                  active
                    ? "border-lime-600 bg-lime-50 text-lime-900"
                    : "border-zinc-200 bg-white text-[#3f3f46] hover:border-zinc-400"
                }`}
              >
                {opt.label}
              </button>
            );
          })}
        </div>
      </div>

      {/* cue list — click a line to jump + edit it */}
      <ul className="max-h-56 space-y-1 overflow-y-auto rounded-xl border border-zinc-100 bg-white p-2">
        {cues.map((c, i) =>
          // Editing row: a plain container (NOT a <button>) so the text <input>
          // isn't nested inside an interactive element, which breaks click/keys.
          editing === i ? (
            <li
              key={i}
              className="flex w-full items-start gap-2 rounded-lg bg-lime-50 px-2 py-1.5 text-left text-sm"
            >
              <span className="mt-0.5 w-10 shrink-0 text-[11px] tabular-nums text-zinc-400">
                {formatTime(c.start_s)}
              </span>
              <input
                autoFocus={editSource === "list"}
                data-caption-edit="1"
                value={c.text}
                onChange={(e) => updateCue(i, e.target.value)}
                onBlur={handleEditorBlur}
                onKeyDown={(e) => {
                  if (e.key === "Enter") {
                    e.preventDefault();
                    stopEditing();
                  }
                }}
                className="flex-1 rounded border border-lime-400 px-1 py-0.5 text-[#18181b] outline-none"
                aria-label={`Edit caption at ${formatTime(c.start_s)}`}
              />
            </li>
          ) : (
            <li key={i}>
              <button
                type="button"
                onClick={() => jumpToCue(i)}
                className={`flex w-full items-start gap-2 rounded-lg px-2 py-1.5 text-left text-sm transition-colors ${
                  i === activeIndex ? "bg-lime-50 text-lime-900" : "hover:bg-zinc-50 text-[#3f3f46]"
                }`}
              >
                <span className="mt-0.5 w-10 shrink-0 text-[11px] tabular-nums text-zinc-400">
                  {formatTime(c.start_s)}
                </span>
                <span className="flex-1">{c.text}</span>
              </button>
            </li>
          ),
        )}
      </ul>

      <div className="flex items-center justify-between">
        <p className="text-xs text-zinc-400">
          {saving ? "Saving…" : dirty ? "Unsaved edits" : "Saved"}
        </p>
        <button
          type="button"
          onClick={apply}
          disabled={busy}
          className="rounded-lg bg-black px-4 py-2 text-sm text-white transition-opacity hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {applying ? "Applying…" : "Apply to video"}
        </button>
      </div>
      {error && <p className="text-xs text-red-600">{error}</p>}
    </div>
  );
}

function formatTime(s: number): string {
  const total = Math.max(0, Math.floor(s));
  const m = Math.floor(total / 60);
  const sec = total % 60;
  return `${m}:${sec.toString().padStart(2, "0")}`;
}
