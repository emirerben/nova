"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { StableVideo } from "@/components/StableVideo";
import { cn } from "@/lib/cn";
import { fmtTime, useAudioRecorder, type AudioTake } from "@/hooks/useAudioRecorder";
import { uploadVoiceover } from "@/lib/generative-api";
import { setItemVoiceover } from "@/lib/plan-api";
import { markTranscriptRecorded } from "@/lib/transcript-api";
import type { ScriptState } from "./ScriptStep";

const FONT_SIZES = [16, 18, 20, 24, 28, 32] as const;
const DEFAULT_SIZE_INDEX = 2; // 20px

/**
 * Step 4 (hero) — Teleprompter recorder.
 *
 * Desktop (sm+): muted footage video left (~40%), scrollable transcript right,
 * record/stop bar below. Mobile (<sm): pinned muted video strip on top,
 * transcript full-width, record bar docked at the bottom (44px targets).
 *
 * CRITICAL: the footage video is MUTED — playing its audio would bleed into the
 * mic and corrupt alignment.
 *
 * The line nearest the viewport center gets a subtle reading highlight
 * (bg-lime-50 + lime-600 left-border). It is SCROLL-driven, not time-driven —
 * no karaoke, no auto-advance. A−/A+ controls resize the transcript for a11y.
 *
 * On stop: upload the take, attach it to the item, mark the recorded version,
 * then advance to Review.
 */
export default function TeleprompterRecorder({
  itemId,
  script,
  footageSrc,
  footageIdentity,
  onRecorded,
  onError,
}: {
  itemId: string;
  script: ScriptState;
  /** Signed URL of a muted footage reference (latest rendered variant), or null. */
  footageSrc: string | null;
  footageIdentity: string | null;
  onRecorded: (take: AudioTake) => void;
  onError: (message: string) => void;
}) {
  const [sizeIndex, setSizeIndex] = useState(DEFAULT_SIZE_INDEX);
  const [activeLine, setActiveLine] = useState(0);
  const [saving, setSaving] = useState(false);

  const scrollRef = useRef<HTMLDivElement | null>(null);
  const lineRefs = useRef<Array<HTMLParagraphElement | null>>([]);

  const rec = useAudioRecorder({
    onTake: (take) => void finishTake(take),
  });

  // Scroll-driven highlight: the line whose center is nearest the scroll
  // container's center wins. Pure reading aid — never time-driven.
  const recomputeActive = useCallback(() => {
    const container = scrollRef.current;
    if (!container) return;
    const rect = container.getBoundingClientRect();
    const mid = rect.top + rect.height / 2;
    let best = 0;
    let bestDist = Infinity;
    lineRefs.current.forEach((el, i) => {
      if (!el) return;
      const r = el.getBoundingClientRect();
      const c = r.top + r.height / 2;
      const dist = Math.abs(c - mid);
      if (dist < bestDist) {
        bestDist = dist;
        best = i;
      }
    });
    setActiveLine(best);
  }, []);

  useEffect(() => {
    recomputeActive();
  }, [recomputeActive, sizeIndex, script.lines]);

  // Upload → attach → mark recorded → advance.
  const finishTake = useCallback(
    async (take: AudioTake) => {
      setSaving(true);
      try {
        const uploaded = await uploadVoiceover(take.blob, take.filename);
        await setItemVoiceover(itemId, uploaded.gcs_path);
        await markTranscriptRecorded(itemId, script.version).catch(() => {
          // Provenance link is best-effort — the take is already attached.
        });
        onRecorded(take);
      } catch (e) {
        onError(e instanceof Error ? e.message : "Couldn't save your take.");
      } finally {
        setSaving(false);
      }
    },
    [itemId, onError, onRecorded, script.version],
  );

  // Spacebar toggles record/stop (unless typing in a control).
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.code !== "Space") return;
      const target = e.target as HTMLElement | null;
      const tag = target?.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA" || target?.isContentEditable) return;
      e.preventDefault();
      if (saving) return;
      if (rec.phase === "recording") rec.stop();
      else if (rec.phase === "idle") void rec.start();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [rec, saving]);

  const fontSize = FONT_SIZES[sizeIndex];

  const recordingLabel =
    rec.phase === "recording"
      ? `Recording — ${fmtTime(rec.elapsed)}`
      : saving
        ? "Saving your take…"
        : "Ready to record";

  const videoPane = footageSrc ? (
    <StableVideo
      src={footageSrc}
      identity={footageIdentity ?? undefined}
      muted
      loop
      autoPlay
      playsInline
      aria-hidden
      className="h-full w-full rounded-[14px] bg-black object-contain"
    />
  ) : (
    <div className="flex h-full w-full items-center justify-center rounded-[14px] border border-dashed border-zinc-200 bg-zinc-50 px-4 text-center">
      <p className="text-sm text-[#71717a]">
        Your footage will play here once a first cut has rendered — read from the
        script on the right.
      </p>
    </div>
  );

  const fontControls = (
    <div className="flex items-center gap-1.5" role="group" aria-label="Transcript text size">
      <button
        type="button"
        onClick={() => setSizeIndex((i) => Math.max(0, i - 1))}
        disabled={sizeIndex === 0}
        aria-label="Smaller text"
        className="flex h-9 w-9 items-center justify-center rounded-full border border-zinc-200 bg-white text-sm text-[#3f3f46] hover:border-zinc-400 disabled:opacity-40"
      >
        A−
      </button>
      <button
        type="button"
        onClick={() => setSizeIndex((i) => Math.min(FONT_SIZES.length - 1, i + 1))}
        disabled={sizeIndex === FONT_SIZES.length - 1}
        aria-label="Larger text"
        className="flex h-9 w-9 items-center justify-center rounded-full border border-zinc-200 bg-white text-sm text-[#3f3f46] hover:border-zinc-400 disabled:opacity-40"
      >
        A+
      </button>
    </div>
  );

  const transcript = (
    <div
      ref={scrollRef}
      onScroll={recomputeActive}
      className="h-full overflow-y-auto px-1 py-6 sm:px-2"
    >
      <div className="space-y-4" style={{ fontSize }}>
        {script.lines.map((line, i) => (
          <p
            key={i}
            ref={(el) => {
              lineRefs.current[i] = el;
            }}
            className={cn(
              "rounded-r-md py-1 pl-3 leading-relaxed transition-colors",
              i === activeLine
                ? "border-l-[3px] border-lime-600 bg-lime-50 text-[#0c0c0e]"
                : "border-l-[3px] border-transparent text-[#3f3f46]",
            )}
          >
            {line}
          </p>
        ))}
      </div>
    </div>
  );

  const recordControls = (
    <div className="flex items-center gap-3">
      {rec.phase === "recording" ? (
        <button
          type="button"
          onClick={rec.stop}
          disabled={saving}
          aria-label="Stop recording"
          aria-pressed
          className="inline-flex min-h-[44px] items-center gap-2 rounded-full bg-[#0c0c0e] px-6 py-2 text-sm font-semibold text-white transition-opacity hover:opacity-80 disabled:opacity-40"
        >
          <span aria-hidden className="h-2.5 w-2.5 rounded-sm bg-white" />
          Stop
        </button>
      ) : (
        <button
          type="button"
          onClick={() => void rec.start()}
          disabled={saving || !rec.recordSupported}
          aria-label="Start recording"
          aria-pressed={false}
          className="inline-flex min-h-[44px] items-center gap-2 rounded-full bg-[#0c0c0e] px-6 py-2 text-sm font-semibold text-white transition-opacity hover:opacity-80 disabled:opacity-40"
        >
          <span aria-hidden className="h-2.5 w-2.5 rounded-full bg-red-500" />
          {saving ? "Saving…" : "Record"}
        </button>
      )}
      {rec.phase === "recording" && (
        <span className="inline-flex items-center gap-2 text-sm tabular-nums text-[#71717a]">
          <span
            aria-hidden
            className="h-2 w-2 motion-safe:animate-pulse rounded-full bg-red-500"
          />
          {fmtTime(rec.elapsed)}
        </span>
      )}
      <span className="text-xs text-[#a1a1aa]">Space to start / stop</span>
    </div>
  );

  return (
    <div className="flex h-full min-h-0 flex-col">
      <p aria-live="polite" className="sr-only">
        {recordingLabel}
      </p>

      {rec.micBlocked && (
        <div className="mb-3 rounded border border-zinc-200 bg-[#fafaf8] px-3 py-2 text-sm text-[#3f3f46]">
          Mic blocked — enable microphone access in your browser settings, then try
          again.
        </div>
      )}

      {/* ── Mobile (<sm): pinned video strip on top ── */}
      <div className="mb-4 h-40 shrink-0 sm:hidden">{videoPane}</div>

      {/* ── Body ── */}
      <div className="flex min-h-0 flex-1 flex-col gap-6 sm:flex-row">
        {/* Desktop video (~40%) */}
        <div className="hidden min-h-0 sm:block sm:w-2/5">
          <div className="sticky top-0 aspect-[9/16] max-h-[70vh]">{videoPane}</div>
        </div>

        {/* Transcript column */}
        <div className="flex min-h-0 flex-1 flex-col">
          <div className="mb-2 flex items-center justify-between">
            <p className="text-xs font-medium uppercase tracking-wide text-lime-700">
              Read this
            </p>
            {fontControls}
          </div>
          <div className="min-h-0 flex-1 rounded-2xl border border-zinc-200 bg-white">
            {transcript}
          </div>
        </div>
      </div>

      {/* ── Record bar ── (docked at bottom on mobile) */}
      <div className="mt-4 shrink-0 border-t border-zinc-100 bg-[#fafaf8] pt-4">
        {recordControls}
      </div>
    </div>
  );
}
