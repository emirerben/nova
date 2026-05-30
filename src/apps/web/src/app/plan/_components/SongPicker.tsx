"use client";

import { useEffect, useRef, useState } from "react";
import { cn } from "@/lib/cn";
import type { MusicTrackSummary } from "@/lib/music-api";

/**
 * Song picker for a plan variant: shows the current track and, on "Change",
 * a scrollable list of every published track with album art and a play button
 * that previews the matched hook (seeks to `preview_start_s`). Picking a track
 * fires `onSelect`; the actual swap + re-render is the parent's job.
 *
 * Audio is one shared `<audio>` element — only one preview plays at a time, and
 * a failed/expired signed URL just disables that track's play button (the list
 * still works). All preview playback is client-side; no server round-trip until
 * the user commits a swap.
 */
export default function SongPicker({
  tracks,
  currentTrackId,
  disabled,
  onSelect,
}: {
  tracks: MusicTrackSummary[];
  currentTrackId: string | null;
  disabled?: boolean;
  onSelect: (trackId: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [playingId, setPlayingId] = useState<string | null>(null);
  const [failed, setFailed] = useState<Set<string>>(new Set());
  const audioRef = useRef<HTMLAudioElement | null>(null);

  const current = tracks.find((t) => t.id === currentTrackId) ?? null;

  // Stop playback when the picker closes or the component unmounts so audio
  // never keeps playing after the user moves on.
  useEffect(() => {
    if (!open) stop();
    return stop;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  function stop() {
    const audio = audioRef.current;
    // Only touch the element if we actually started playback (it has a src) —
    // avoids calling pause() on an idle element (jsdom has no media impl and
    // logs/throws there; real browsers no-op). Belt-and-braces try/catch too.
    if (audio && audio.src) {
      try {
        audio.pause();
      } catch {
        /* no-op */
      }
      audio.removeAttribute("src");
    }
    setPlayingId(null);
  }

  function markFailed(id: string) {
    setFailed((prev) => new Set(prev).add(id));
    setPlayingId((cur) => (cur === id ? null : cur));
  }

  function togglePlay(t: MusicTrackSummary) {
    const audio = audioRef.current;
    if (!audio || !t.preview_audio_url || failed.has(t.id)) return;
    if (playingId === t.id) {
      stop();
      return;
    }
    audio.src = t.preview_audio_url;
    // Seek to the hook once the file is far enough loaded.
    audio.onloadedmetadata = () => {
      try {
        audio.currentTime = t.preview_start_s ?? 0;
      } catch {
        /* seeking is best-effort */
      }
    };
    audio.onended = () => setPlayingId((cur) => (cur === t.id ? null : cur));
    audio.play().then(
      () => setPlayingId(t.id),
      () => markFailed(t.id),
    );
  }

  return (
    <div>
      {/* hidden shared audio element */}
      <audio ref={audioRef} className="hidden" onError={() => playingId && markFailed(playingId)} />

      {/* Current track row */}
      <div className="flex items-center gap-3 rounded-lg border border-zinc-800 bg-zinc-950/40 p-2">
        <Art track={current} />
        <div className="min-w-0 flex-1">
          <p className="truncate text-sm text-zinc-100">{current?.title ?? "—"}</p>
          <p className="truncate text-xs text-zinc-400">{current?.artist ?? ""}</p>
        </div>
        <button
          type="button"
          disabled={disabled}
          onClick={() => setOpen((o) => !o)}
          className="rounded-full border border-zinc-700 px-3 py-2 text-sm text-zinc-200 transition-colors hover:bg-zinc-800 disabled:cursor-not-allowed disabled:opacity-40"
        >
          {open ? "Close" : "Change"}
        </button>
      </div>

      {open && (
        <div className="mt-2 max-h-72 overflow-y-auto rounded-lg border border-zinc-800">
          <ul className="divide-y divide-zinc-800">
            {tracks.map((t) => {
              const isCurrent = t.id === currentTrackId;
              const canPlay = !!t.preview_audio_url && !failed.has(t.id);
              return (
                <li key={t.id} className="flex items-center gap-3 p-2">
                  <button
                    type="button"
                    aria-label={
                      canPlay
                        ? playingId === t.id
                          ? `Pause preview of ${t.title}`
                          : `Play preview of ${t.title}`
                        : "Preview unavailable"
                    }
                    disabled={!canPlay}
                    onClick={() => togglePlay(t)}
                    className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full border border-zinc-700 text-zinc-200 hover:bg-zinc-800 disabled:opacity-30"
                  >
                    {playingId === t.id ? "❚❚" : "►"}
                  </button>
                  <Art track={t} />
                  <div className="min-w-0 flex-1">
                    <p className="truncate text-sm text-zinc-100">{t.title}</p>
                    <p className="truncate text-xs text-zinc-400">
                      {t.artist}
                      {!canPlay && <span className="ml-1 text-zinc-600">· preview unavailable</span>}
                    </p>
                  </div>
                  <button
                    type="button"
                    disabled={disabled || isCurrent}
                    onClick={() => {
                      stop();
                      onSelect(t.id);
                      setOpen(false);
                    }}
                    className={cn(
                      "rounded-full px-3 py-2 text-xs font-medium transition-colors disabled:cursor-not-allowed",
                      isCurrent
                        ? "bg-zinc-800 text-zinc-500"
                        : "bg-amber-400 text-black hover:bg-amber-300",
                    )}
                  >
                    {isCurrent ? "Current" : "Use"}
                  </button>
                </li>
              );
            })}
          </ul>
          <p className="px-3 py-2 text-center text-xs text-zinc-500">More songs coming soon.</p>
        </div>
      )}
    </div>
  );
}

/** Square album art with a gradient-initial fallback when there's no thumbnail. */
function Art({ track }: { track: MusicTrackSummary | null }) {
  if (track?.thumbnail_url) {
    return (
      // eslint-disable-next-line @next/next/no-img-element -- tiny album-art thumb; next/image is overkill for a remote signed URL
      <img src={track.thumbnail_url} alt="" className="h-9 w-9 shrink-0 rounded object-cover" />
    );
  }
  const initial = (track?.title ?? "?").trim().charAt(0).toUpperCase() || "?";
  return (
    <div
      aria-hidden="true"
      className="flex h-9 w-9 shrink-0 items-center justify-center rounded bg-gradient-to-br from-zinc-700 to-zinc-900 text-xs font-semibold text-zinc-300"
    >
      {initial}
    </div>
  );
}
