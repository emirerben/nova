"use client";

import { useMemo, useRef, useState } from "react";

export interface SongWindowState {
  startS: number;
  videoDurationS: number;
  trackDurationS: number;
  recommendedStartS: number;
  beatTimestampsS: number[];
  editable: boolean;
  reason: string | null;
}

export interface SongWindowControl {
  value: SongWindowState;
  onPreview: (startS: number) => void;
  onChange: (startS: number) => void;
  onBegin: () => void;
}

function formatTime(value: number): string {
  const safe = Math.max(0, value);
  const minutes = Math.floor(safe / 60);
  const seconds = safe - minutes * 60;
  return `${minutes}:${seconds.toFixed(1).padStart(4, "0")}`;
}

export function snapSongWindowStart(
  startS: number,
  trackDurationS: number,
  videoDurationS: number,
  beats: number[],
): number {
  const maxStart = Math.max(0, trackDurationS - videoDurationS);
  const clamped = Math.max(0, Math.min(maxStart, startS));
  const candidates = beats.filter(
    (beat) => Number.isFinite(beat) && beat >= 0 && beat <= maxStart,
  );
  if (candidates.length === 0) return clamped;
  return candidates.reduce((best, beat) => {
    const error = Math.abs(beat - clamped);
    const bestError = Math.abs(best - clamped);
    return error < bestError || (error === bestError && beat < best) ? beat : best;
  }, candidates[0]);
}

function disabledCopy(reason: string | null): string {
  switch (reason) {
    case "song_shorter_than_video":
      return "This song is shorter than your video, so its section can’t be moved.";
    case "timing_metadata_unavailable":
      return "Beat timing isn’t available for this song yet.";
    case "track_duration_unknown":
      return "The song duration isn’t available yet.";
    case "video_duration_unknown":
      return "The video duration isn’t available yet.";
    case "track_unavailable":
      return "This song is no longer available for preview.";
    default:
      return "This song section can’t be changed.";
  }
}

export default function SongWindowSelector({
  value,
  onPreview,
  onChange,
  onBegin,
}: SongWindowControl) {
  const maxStart = Math.max(0, value.trackDurationS - value.videoDurationS);
  const startS = Math.max(0, Math.min(maxStart, value.startS));
  const endS = startS + value.videoDurationS;
  const leftPct = value.trackDurationS > 0 ? (startS / value.trackDurationS) * 100 : 0;
  const widthPct =
    value.trackDurationS > 0
      ? Math.min(100, (value.videoDurationS / value.trackDurationS) * 100)
      : 100;
  const commitSnapped = (candidate = startS) => {
    onChange(
      snapSongWindowStart(
        candidate,
        value.trackDurationS,
        value.videoDurationS,
        value.beatTimestampsS,
      ),
    );
  };
  const marks = useMemo(() => {
    const usable = value.beatTimestampsS.filter(
      (beat) => beat >= 0 && beat <= value.trackDurationS,
    );
    const stride = Math.max(1, Math.ceil(usable.length / 180));
    return usable.filter(
      (_beat, index) => index % stride === 0 || index === usable.length - 1,
    );
  }, [value.beatTimestampsS, value.trackDurationS]);
  const bandRef = useRef<HTMLDivElement>(null);
  const bandRectRef = useRef<{ left: number; width: number } | null>(null);
  const draggingBand = useRef(false);
  const keyboardEditing = useRef(false);
  const [dragging, setDragging] = useState(false);
  const bandStartForPointer = (clientX: number): number => {
    const rect = bandRectRef.current ?? bandRef.current?.getBoundingClientRect();
    if (!rect || rect.width <= 0) return startS;
    const centerS = ((clientX - rect.left) / rect.width) * value.trackDurationS;
    return Math.max(0, Math.min(maxStart, centerS - value.videoDurationS / 2));
  };

  return (
    <section className="rounded-xl border border-zinc-200 bg-zinc-50 p-3">
      <div className="flex items-center justify-between">
        <p className="text-[12px] font-semibold text-[#3f3f46]">Song section</p>
        <span className="text-[11px] tabular-nums text-[#71717a]">
          {formatTime(value.videoDurationS)} selected
        </span>
      </div>
      <div
        ref={bandRef}
        data-testid="song-window-band"
        className={`relative mt-3 h-11 rounded-md bg-zinc-200 ${
          value.editable ? "cursor-grab touch-none active:cursor-grabbing" : ""
        }`}
        aria-hidden
        onPointerDown={(event) => {
          if (!value.editable) return;
          draggingBand.current = true;
          setDragging(true);
          const rect = event.currentTarget.getBoundingClientRect();
          bandRectRef.current = { left: rect.left, width: rect.width };
          event.currentTarget.setPointerCapture?.(event.pointerId);
          onBegin();
          onPreview(bandStartForPointer(event.clientX));
        }}
        onPointerMove={(event) => {
          if (!draggingBand.current || !value.editable) return;
          onPreview(bandStartForPointer(event.clientX));
        }}
        onPointerUp={(event) => {
          if (!draggingBand.current || !value.editable) return;
          draggingBand.current = false;
          setDragging(false);
          const candidate = bandStartForPointer(event.clientX);
          bandRectRef.current = null;
          commitSnapped(candidate);
        }}
        onPointerCancel={() => {
          draggingBand.current = false;
          setDragging(false);
          bandRectRef.current = null;
          commitSnapped();
        }}
      >
        {marks.map((beat) => (
          <span
            key={beat}
            className="absolute top-1/2 h-2 w-px -translate-y-1/2 bg-zinc-400"
            style={{ left: `${(beat / Math.max(value.trackDurationS, 0.001)) * 100}%` }}
          />
        ))}
        <span
          className={`absolute inset-y-0 rounded-md border-2 border-lime-600 bg-lime-600/65 transition-transform ${
            dragging ? "scale-y-105" : ""
          }`}
          style={{ left: `${leftPct}%`, width: `${widthPct}%` }}
        />
        {dragging && (
          <span
            className="pointer-events-none absolute -top-7 z-10 -translate-x-1/2 rounded bg-[#0c0c0e] px-2 py-1 text-[11px] tabular-nums text-white"
            style={{ left: `${Math.max(8, Math.min(92, leftPct + widthPct / 2))}%` }}
          >
            {formatTime(startS)}
          </span>
        )}
      </div>
      <input
        aria-label="Song section start"
        aria-valuetext={`${formatTime(startS)} start, ${formatTime(endS)} end`}
        className="mt-2 h-11 w-full accent-[#0c0c0e] disabled:cursor-not-allowed disabled:opacity-40"
        type="range"
        min={0}
        max={Math.max(0, maxStart)}
        step={0.01}
        value={startS}
        disabled={!value.editable}
        onChange={(event) => onPreview(Number(event.currentTarget.value))}
        onPointerDown={onBegin}
        onKeyDown={(event) => {
          if (!["ArrowLeft", "ArrowRight", "Home", "End", "PageUp", "PageDown"].includes(event.key)) {
            return;
          }
          if (!keyboardEditing.current) {
            keyboardEditing.current = true;
            onBegin();
          }
        }}
        onPointerUp={() => commitSnapped()}
        onKeyUp={(event) => {
          if (!keyboardEditing.current) return;
          keyboardEditing.current = false;
          commitSnapped(Number(event.currentTarget.value));
        }}
        onBlur={(event) => {
          if (!keyboardEditing.current) return;
          keyboardEditing.current = false;
          commitSnapped(Number(event.currentTarget.value));
        }}
      />
      <div className="mt-1 flex justify-between text-[11px] tabular-nums text-[#52525b]">
        <span>{formatTime(startS)}</span>
        <span>{formatTime(endS)}</span>
      </div>
      {value.editable ? (
        <button
          type="button"
          className="mt-3 min-h-11 w-full rounded-lg border border-zinc-300 bg-white px-3 text-[12px] font-semibold text-[#0c0c0e] hover:border-zinc-500 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500"
          onClick={() => {
            onBegin();
            commitSnapped(value.recommendedStartS);
          }}
        >
          Reset to recommended section
        </button>
      ) : (
        <p className="mt-2 text-[11px] leading-relaxed text-[#71717a]">
          {disabledCopy(value.reason)}
        </p>
      )}
    </section>
  );
}
