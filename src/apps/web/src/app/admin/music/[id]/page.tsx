"use client";

import { use, useEffect, useState } from "react";
import Link from "next/link";
import {
  adminGetMusicTrack,
  adminUpdateMusicTrack,
  adminReanalyzeMusicTrack,
  adminArchiveMusicTrack,
  type MusicTrackDetail,
  type TrackConfig,
} from "@/lib/music-api";

// ── Beat waveform ─────────────────────────────────────────────────────────────

function BeatWaveform({
  beats,
  duration,
  start,
  end,
}: {
  beats: number[];
  duration: number;
  start: number;
  end: number;
}) {
  if (!beats.length || !duration) return null;

  const W = 600;
  const H = 40;
  const barW = 2;

  return (
    <div className="mt-3 overflow-x-auto">
      <svg width={W} height={H} className="bg-zinc-800 rounded">
        {/* Selected window highlight */}
        <rect
          x={(start / duration) * W}
          y={0}
          width={Math.max(0, ((end - start) / duration) * W)}
          height={H}
          fill="rgba(139,92,246,0.2)"
        />
        {/* Beat markers */}
        {beats.map((b, i) => {
          const x = (b / duration) * W;
          const inWindow = b >= start && b <= end;
          return (
            <rect
              key={i}
              x={x}
              y={inWindow ? 4 : 10}
              width={barW}
              height={inWindow ? H - 8 : H - 20}
              fill={inWindow ? "#8b5cf6" : "#52525b"}
            />
          );
        })}
      </svg>
      <p className="text-xs text-zinc-500 mt-1">
        {beats.length} beats · window {start.toFixed(1)}s–{end.toFixed(1)}s highlighted
      </p>
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
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  const [track, setTrack] = useState<MusicTrackDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

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
    const updated = await adminUpdateMusicTrack(id, {
      publish: track.published_at === null,
    });
    setTrack(updated);
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
  const beats: number[] = [];
  // beats are stored on track but not directly in MusicTrackDetail — we derive from beat_count
  // The waveform uses track_config window bounds over beat_count as proxy

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

      {/* Beat waveform placeholder — real waveform needs beat array from API */}
      {track.analysis_status === "ready" && track.beat_count > 0 && (
        <div className="bg-zinc-900 rounded-xl border border-zinc-700 p-5 mb-6">
          <h2 className="font-semibold mb-1 text-sm text-zinc-400 uppercase tracking-wide">
            Beat map ({track.beat_count} beats)
          </h2>
          <p className="text-xs text-zinc-500">
            Waveform renders from beat timestamps. Fetch the full track JSON for a detailed view.
          </p>
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
