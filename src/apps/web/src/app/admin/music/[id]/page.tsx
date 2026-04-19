"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import {
  adminGetMusicTrack,
  adminGetAudioUrl,
  adminUpdateMusicTrack,
  adminReanalyzeMusicTrack,
  adminArchiveMusicTrack,
  type MusicTrackDetail,
  type TrackConfig,
} from "@/lib/music-api";
import { adminCreateTemplateFromMusicTrack } from "@/lib/admin-api";

// ── Audio player with interactive waveform ────────────────────────────────────

type SelectionMode = "start" | "end" | null;

function AudioPlayer({
  trackId,
  beats,
  duration,
  start,
  end,
  onStartChange,
  onEndChange,
}: {
  trackId: string;
  beats: number[];
  duration: number;
  start: number;
  end: number;
  onStartChange: (s: number) => void;
  onEndChange: (s: number) => void;
}) {
  const audioRef = useRef<HTMLAudioElement>(null);
  const [audioUrl, setAudioUrl] = useState<string | null>(null);
  const [audioError, setAudioError] = useState<string | null>(null);
  const [playing, setPlaying] = useState(false);
  const [currentTime, setCurrent] = useState(0);
  const [selectMode, setSelectMode] = useState<SelectionMode>(null);
  const rafRef = useRef<number>(0);

  // Fetch signed audio URL
  useEffect(() => {
    let cancelled = false;
    adminGetAudioUrl(trackId)
      .then((url) => { if (!cancelled) setAudioUrl(url); })
      .catch((e) => { if (!cancelled) setAudioError(e.message); });
    return () => { cancelled = true; };
  }, [trackId]);

  // Animation frame loop for playhead tracking
  useEffect(() => {
    const audio = audioRef.current;
    if (!audio) return;
    function tick() {
      if (audio) setCurrent(audio.currentTime);
      rafRef.current = requestAnimationFrame(tick);
    }
    rafRef.current = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(rafRef.current);
  }, [audioUrl]);

  const togglePlay = useCallback(() => {
    const audio = audioRef.current;
    if (!audio) return;
    if (audio.paused) { audio.play(); setPlaying(true); }
    else { audio.pause(); setPlaying(false); }
  }, []);

  // Play just the selected section
  const playSection = useCallback(() => {
    const audio = audioRef.current;
    if (!audio) return;
    audio.currentTime = start;
    audio.play();
    setPlaying(true);
    const checkEnd = () => {
      if (audio.currentTime >= end) {
        audio.pause();
        setPlaying(false);
        audio.removeEventListener("timeupdate", checkEnd);
      }
    };
    audio.addEventListener("timeupdate", checkEnd);
  }, [start, end]);

  const W = 700;
  const H = 56;
  const barW = 2;

  function handleWaveformClick(e: React.MouseEvent<SVGSVGElement>) {
    const svg = e.currentTarget;
    const rect = svg.getBoundingClientRect();
    const x = e.clientX - rect.left;
    const t = (x / rect.width) * duration;

    if (selectMode === "start") {
      onStartChange(Math.round(t * 10) / 10);
      setSelectMode(null);
    } else if (selectMode === "end") {
      onEndChange(Math.round(t * 10) / 10);
      setSelectMode(null);
    } else {
      // Default: seek audio to clicked position
      const audio = audioRef.current;
      if (audio) audio.currentTime = t;
    }
  }

  if (audioError) {
    return <p className="text-sm text-red-400">Could not load audio: {audioError}</p>;
  }
  if (!audioUrl) {
    return <p className="text-sm text-zinc-500">Loading audio...</p>;
  }

  const playheadX = duration > 0 ? (currentTime / duration) * W : 0;

  return (
    <div>
      <audio
        ref={audioRef}
        src={audioUrl}
        preload="auto"
        onEnded={() => setPlaying(false)}
      />

      {/* Transport controls */}
      <div className="flex items-center gap-3 mb-3">
        <button
          onClick={togglePlay}
          className="bg-zinc-700 hover:bg-zinc-600 text-white text-sm font-semibold px-3 py-1.5 rounded-lg transition-colors"
        >
          {playing ? "⏸ Pause" : "▶ Play"}
        </button>
        <button
          onClick={playSection}
          className="bg-violet-700 hover:bg-violet-600 text-white text-sm font-semibold px-3 py-1.5 rounded-lg transition-colors"
        >
          ▶ Play section ({start.toFixed(1)}s – {end.toFixed(1)}s)
        </button>
        <span className="text-xs text-zinc-400 font-mono tabular-nums">
          {currentTime.toFixed(1)}s / {duration.toFixed(1)}s
        </span>
      </div>

      {/* Interactive waveform */}
      <svg
        width={W}
        height={H}
        className={`bg-zinc-800 rounded cursor-${selectMode ? "crosshair" : "pointer"} block`}
        style={{ cursor: selectMode ? "crosshair" : "pointer" }}
        onClick={handleWaveformClick}
      >
        {/* Selected window highlight */}
        <rect
          x={(start / duration) * W}
          y={0}
          width={Math.max(0, ((end - start) / duration) * W)}
          height={H}
          fill="rgba(139,92,246,0.15)"
        />

        {/* Beat markers */}
        {beats.map((b, i) => {
          const x = (b / duration) * W;
          const inWindow = b >= start && b <= end;
          return (
            <rect
              key={i}
              x={x}
              y={inWindow ? 4 : 14}
              width={barW}
              height={inWindow ? H - 8 : H - 28}
              fill={inWindow ? "#8b5cf6" : "#52525b"}
              rx={1}
            />
          );
        })}

        {/* Start marker */}
        <line
          x1={(start / duration) * W}
          y1={0}
          x2={(start / duration) * W}
          y2={H}
          stroke="#22c55e"
          strokeWidth={2}
        />
        {/* End marker */}
        <line
          x1={(end / duration) * W}
          y1={0}
          x2={(end / duration) * W}
          y2={H}
          stroke="#ef4444"
          strokeWidth={2}
        />

        {/* Playhead */}
        <line
          x1={playheadX}
          y1={0}
          x2={playheadX}
          y2={H}
          stroke="#ffffff"
          strokeWidth={1.5}
          opacity={0.8}
        />
      </svg>

      {/* Section selection buttons */}
      <div className="flex items-center gap-3 mt-2">
        <button
          onClick={() => setSelectMode(selectMode === "start" ? null : "start")}
          className={`text-xs font-semibold px-3 py-1 rounded-lg transition-colors ${
            selectMode === "start"
              ? "bg-green-600 text-white"
              : "bg-zinc-700 hover:bg-zinc-600 text-zinc-300"
          }`}
        >
          {selectMode === "start" ? "Click waveform to set start..." : "Set start"}
        </button>
        <button
          onClick={() => setSelectMode(selectMode === "end" ? null : "end")}
          className={`text-xs font-semibold px-3 py-1 rounded-lg transition-colors ${
            selectMode === "end"
              ? "bg-red-600 text-white"
              : "bg-zinc-700 hover:bg-zinc-600 text-zinc-300"
          }`}
        >
          {selectMode === "end" ? "Click waveform to set end..." : "Set end"}
        </button>
        <span className="text-xs text-zinc-500">
          {beats.length} beats · <span className="text-green-400">green</span> = start · <span className="text-red-400">red</span> = end · white = playhead
        </span>
      </div>
    </div>
  );
}

// ── Main Page ─────────────────────────────────────────────────────────────────

const STATUS_COLORS: Record<string, string> = {
  queued: "bg-zinc-700 text-zinc-300",
  analyzing: "bg-blue-900 text-blue-300",
  ready: "bg-green-900 text-green-300",
  failed: "bg-red-900 text-red-300",
};

export default function AdminMusicTrackPage({
  params,
}: {
  params: { id: string };
}) {
  const { id } = params;
  const router = useRouter();
  const [track, setTrack] = useState<MusicTrackDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [creatingTemplate, setCreatingTemplate] = useState(false);

  // Config form state
  const [bestStart, setBestStart] = useState("");
  const [bestEnd, setBestEnd] = useState("");
  const [slotEveryN, setSlotEveryN] = useState("");
  const [saving, setSaving] = useState(false);
  const [saveMsg, setSaveMsg] = useState<string | null>(null);

  // Reanalyze
  const [reanalyzing, setReanalyzing] = useState(false);

  // Poll while analyzing
  useEffect(() => {
    let interval: ReturnType<typeof setInterval>;
    if (track?.analysis_status === "analyzing" || track?.analysis_status === "queued") {
      interval = setInterval(async () => {
        try {
          const fresh = await adminGetMusicTrack(id);
          setTrack(fresh);
          syncFormFromTrack(fresh);
          if (fresh.analysis_status === "ready" || fresh.analysis_status === "failed") {
            clearInterval(interval);
          }
        } catch {
          // keep polling
        }
      }, 3000);
    }
    return () => clearInterval(interval);
  }, [id, track?.analysis_status]);

  function syncFormFromTrack(t: MusicTrackDetail) {
    const cfg = t.track_config;
    setBestStart(cfg?.best_start_s?.toString() ?? "");
    setBestEnd(cfg?.best_end_s?.toString() ?? "");
    setSlotEveryN(cfg?.slot_every_n_beats?.toString() ?? "8");
  }

  useEffect(() => {
    adminGetMusicTrack(id)
      .then((t) => {
        setTrack(t);
        syncFormFromTrack(t);
      })
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }, [id]);

  async function handleSaveConfig(e: React.FormEvent) {
    e.preventDefault();
    if (!track) return;
    setSaving(true);
    setSaveMsg(null);
    try {
      const updated = await adminUpdateMusicTrack(id, {
        track_config: {
          best_start_s: parseFloat(bestStart),
          best_end_s: parseFloat(bestEnd),
          slot_every_n_beats: parseInt(slotEveryN, 10),
        },
      });
      setTrack(updated);
      setSaveMsg("Saved.");
    } catch (e: unknown) {
      setSaveMsg(e instanceof Error ? e.message : "Save failed");
    } finally {
      setSaving(false);
    }
  }

  async function handleTogglePublish() {
    if (!track) return;
    try {
      const updated = await adminUpdateMusicTrack(id, {
        publish: track.published_at === null,
      });
      setTrack(updated);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to update publish status");
    }
  }

  async function handleReanalyze() {
    if (!track) return;
    setReanalyzing(true);
    try {
      await adminReanalyzeMusicTrack(id);
      const fresh = await adminGetMusicTrack(id);
      setTrack(fresh);
      syncFormFromTrack(fresh);
    } finally {
      setReanalyzing(false);
    }
  }

  async function handleCreateTemplate() {
    if (!track) return;
    setCreatingTemplate(true);
    try {
      const template = await adminCreateTemplateFromMusicTrack(track.id);
      router.push(`/admin/templates/${template.id}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to create template");
      setCreatingTemplate(false);
    }
  }

  async function handleArchive() {
    if (!track) return;
    if (!confirm("Archive this track? It will be hidden from the gallery.")) return;
    await adminArchiveMusicTrack(id);
    const fresh = await adminGetMusicTrack(id);
    setTrack(fresh);
  }

  if (loading) {
    return (
      <div className="min-h-screen bg-zinc-950 text-zinc-100 flex items-center justify-center">
        <p className="text-zinc-400">Loading…</p>
      </div>
    );
  }
  if (error || !track) {
    return (
      <div className="min-h-screen bg-zinc-950 text-zinc-100 flex items-center justify-center">
        <p className="text-red-400">{error ?? "Track not found"}</p>
      </div>
    );
  }

  const cfg = track.track_config ?? ({} as TrackConfig);

  return (
    <div className="min-h-screen bg-zinc-950 text-zinc-100 p-6 max-w-3xl mx-auto">
      {/* Header */}
      <div className="flex items-center gap-3 mb-6">
        <Link href="/admin/music" className="text-zinc-400 hover:text-zinc-200 text-sm">
          ← Music Tracks
        </Link>
        <h1 className="text-2xl font-bold flex-1 truncate">{track.title}</h1>
        <span
          className={`text-xs font-semibold px-2 py-1 rounded-full ${
            STATUS_COLORS[track.analysis_status] ?? STATUS_COLORS.queued
          }`}
        >
          {track.analysis_status}
        </span>
      </div>

      {/* Info card */}
      <div className="bg-zinc-900 rounded-xl border border-zinc-700 p-5 mb-6 grid grid-cols-2 gap-x-8 gap-y-2 text-sm">
        <Row label="Artist" value={track.artist || "—"} />
        <Row label="Duration" value={track.duration_s ? `${track.duration_s.toFixed(1)}s` : "—"} />
        <Row label="Beats detected" value={String(track.beat_count)} />
        <Row
          label="Best section"
          value={
            cfg.best_start_s != null
              ? `${cfg.best_start_s.toFixed(1)}s – ${cfg.best_end_s?.toFixed(1)}s`
              : "—"
          }
        />
        <Row label="Slot every N beats" value={cfg.slot_every_n_beats?.toString() ?? "—"} />
        <Row
          label="Required clips"
          value={
            cfg.required_clips_min != null
              ? `${cfg.required_clips_min} – ${cfg.required_clips_max}`
              : "—"
          }
        />
        <div className="col-span-2">
          <span className="text-zinc-500">Source URL </span>
          <a
            href={track.source_url}
            target="_blank"
            rel="noopener noreferrer"
            className="text-violet-400 hover:text-violet-300 font-mono text-xs break-all"
          >
            {track.source_url}
          </a>
        </div>
        {track.error_detail && (
          <div className="col-span-2 text-red-400 text-xs break-words">
            Error: {track.error_detail}
          </div>
        )}
      </div>

      {/* Audio player + beat waveform */}
      {track.analysis_status === "ready" && track.beat_timestamps_s && track.beat_timestamps_s.length > 0 && (
        <div className="bg-zinc-900 rounded-xl border border-zinc-700 p-5 mb-6">
          <h2 className="font-semibold mb-3 text-sm text-zinc-400 uppercase tracking-wide">
            Audio · {track.beat_count} beats
          </h2>
          <AudioPlayer
            trackId={id}
            beats={track.beat_timestamps_s}
            duration={track.duration_s ?? 0}
            start={parseFloat(bestStart) || (cfg.best_start_s ?? 0)}
            end={parseFloat(bestEnd) || (cfg.best_end_s ?? 0)}
            onStartChange={(s) => setBestStart(s.toString())}
            onEndChange={(s) => setBestEnd(s.toString())}
          />
        </div>
      )}

      {/* Config form */}
      <div className="bg-zinc-900 rounded-xl border border-zinc-700 p-5 mb-6">
        <h2 className="font-semibold mb-4">Timing config</h2>
        <form onSubmit={handleSaveConfig} className="space-y-4">
          <div className="grid grid-cols-2 gap-4">
            <label className="block">
              <span className="text-xs text-zinc-400 mb-1 block">Best section start (s)</span>
              <input
                type="number"
                step="0.1"
                min="0"
                value={bestStart}
                onChange={(e) => setBestStart(e.target.value)}
                className="w-full bg-zinc-800 border border-zinc-600 rounded-lg px-3 py-2 text-sm text-zinc-100 focus:outline-none focus:border-violet-500"
              />
            </label>
            <label className="block">
              <span className="text-xs text-zinc-400 mb-1 block">Best section end (s)</span>
              <input
                type="number"
                step="0.1"
                min="0"
                value={bestEnd}
                onChange={(e) => setBestEnd(e.target.value)}
                className="w-full bg-zinc-800 border border-zinc-600 rounded-lg px-3 py-2 text-sm text-zinc-100 focus:outline-none focus:border-violet-500"
              />
            </label>
          </div>
          <label className="block">
            <span className="text-xs text-zinc-400 mb-1 block">
              Slot every N beats (default: 8 = ~2 bars at 120 BPM)
            </span>
            <input
              type="number"
              step="1"
              min="1"
              max="32"
              value={slotEveryN}
              onChange={(e) => setSlotEveryN(e.target.value)}
              className="w-40 bg-zinc-800 border border-zinc-600 rounded-lg px-3 py-2 text-sm text-zinc-100 focus:outline-none focus:border-violet-500"
            />
          </label>
          {saveMsg && (
            <p
              className={`text-sm ${saveMsg === "Saved." ? "text-green-400" : "text-red-400"}`}
            >
              {saveMsg}
            </p>
          )}
          <button
            type="submit"
            disabled={saving}
            className="bg-violet-600 hover:bg-violet-500 disabled:opacity-40 text-white text-sm font-semibold px-5 py-2 rounded-lg transition-colors"
          >
            {saving ? "Saving…" : "Save config"}
          </button>
        </form>
      </div>

      {/* Actions */}
      <div className="flex flex-wrap gap-3">
        {track.analysis_status === "ready" && (
          <button
            onClick={handleCreateTemplate}
            disabled={creatingTemplate}
            className="text-sm font-semibold px-4 py-2 rounded-lg bg-blue-600 hover:bg-blue-500 disabled:opacity-40 text-white transition-colors"
          >
            {creatingTemplate ? "Creating…" : "Create Template"}
          </button>
        )}

        <button
          onClick={handleTogglePublish}
          className={`text-sm font-semibold px-4 py-2 rounded-lg transition-colors ${
            track.published_at
              ? "bg-zinc-700 hover:bg-zinc-600 text-zinc-100"
              : "bg-green-700 hover:bg-green-600 text-white"
          }`}
        >
          {track.published_at ? "Unpublish" : "Publish to gallery"}
        </button>

        <button
          onClick={handleReanalyze}
          disabled={reanalyzing || track.analysis_status === "analyzing"}
          className="text-sm font-semibold px-4 py-2 rounded-lg bg-zinc-700 hover:bg-zinc-600 disabled:opacity-40 transition-colors"
        >
          {reanalyzing ? "Re-analyzing…" : "Re-analyze beats"}
        </button>

        {!track.archived_at && (
          <button
            onClick={handleArchive}
            className="text-sm font-semibold px-4 py-2 rounded-lg bg-red-900 hover:bg-red-800 text-red-200 transition-colors ml-auto"
          >
            Archive track
          </button>
        )}
      </div>
    </div>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <span className="text-zinc-500">{label} </span>
      <span className="text-zinc-100">{value}</span>
    </div>
  );
}
