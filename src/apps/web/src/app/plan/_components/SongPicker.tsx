"use client";

import { useEffect, useRef, useState } from "react";
import { cn } from "@/lib/cn";
import type { MusicTrackSummary } from "@/lib/music-api";

/**
 * Song picker for a plan variant — light editorial canvas (D20/D21).
 * Shows the current track and, on "Change", a scrollable list of every
 * published track with album art and a play button that previews the hook.
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

  useEffect(() => {
    if (!open) stop();
    return stop;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  function stop() {
    const audio = audioRef.current;
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
    audio.onloadedmetadata = () => {
      try {
        audio.currentTime = t.preview_start_s ?? 0;
      } catch {
        /* best-effort */
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
      <audio ref={audioRef} className="hidden" onError={() => playingId && markFailed(playingId)} />

      {/* Current track row */}
      <div className="flex items-center gap-3 rounded-lg border border-zinc-200 bg-white p-2">
        <Art track={current} />
        <div className="min-w-0 flex-1">
          <p className="truncate text-sm text-[#0c0c0e]">{current?.title ?? "—"}</p>
          <p className="truncate text-xs text-[#71717a]">{current?.artist ?? ""}</p>
        </div>
        <button
          type="button"
          disabled={disabled}
          onClick={() => setOpen((o) => !o)}
          className="rounded-full border border-zinc-200 px-3 py-2 text-sm text-[#3f3f46] transition-colors hover:border-zinc-400 disabled:cursor-not-allowed disabled:opacity-40"
        >
          {open ? "Close" : "Change"}
        </button>
      </div>

      {open && (
        <div className="mt-2 max-h-72 overflow-y-auto rounded-lg border border-zinc-200">
          <ul className="divide-y divide-zinc-100">
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
                    className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full border border-zinc-200 text-[#3f3f46] hover:border-zinc-400 disabled:opacity-30"
                  >
                    {playingId === t.id ? "❚❚" : "►"}
                  </button>
                  <Art track={t} />
                  <div className="min-w-0 flex-1">
                    <p className="truncate text-sm text-[#0c0c0e]">{t.title}</p>
                    <p className="truncate text-xs text-[#71717a]">
                      {t.artist}
                      {!canPlay && <span className="ml-1 text-[#a1a1aa]">· preview unavailable</span>}
                    </p>
                    {/* Prevention-first (P6): say it BEFORE they swap, not after a dead render. */}
                    {t.has_lyrics && t.lyrics_variant_supported === false && (
                      <p className="truncate text-xs text-[#71717a]">
                        No lyric variant — language not supported yet
                      </p>
                    )}
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
                        ? "bg-zinc-100 text-[#a1a1aa]"
                        : "bg-[#0c0c0e] text-white hover:opacity-80",
                    )}
                  >
                    {isCurrent ? "Current" : "Use"}
                  </button>
                </li>
              );
            })}
          </ul>
          <p className="px-3 py-2 text-center text-xs text-[#a1a1aa]">More songs coming soon.</p>
        </div>
      )}
    </div>
  );
}

function Art({ track }: { track: MusicTrackSummary | null }) {
  if (track?.thumbnail_url) {
    return (
      // eslint-disable-next-line @next/next/no-img-element
      <img src={track.thumbnail_url} alt="" className="h-9 w-9 shrink-0 rounded object-cover" />
    );
  }
  const initial = (track?.title ?? "?").trim().charAt(0).toUpperCase() || "?";
  return (
    <div
      aria-hidden="true"
      className="flex h-9 w-9 shrink-0 items-center justify-center rounded bg-gradient-to-br from-zinc-100 to-zinc-200 text-xs font-semibold text-[#71717a]"
    >
      {initial}
    </div>
  );
}
