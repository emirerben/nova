"use client";

import { useState } from "react";
import {
  adminExtractLyrics,
  adminGetMusicTrack,
  adminUpdateMusicTrack,
  type LyricsConfig,
  type LyricsStatus,
  type MusicTrackDetail,
} from "@/lib/music-api";

const STATUS_COLORS: Record<LyricsStatus, string> = {
  pending: "bg-zinc-700 text-zinc-300",
  extracting: "bg-blue-900 text-blue-300",
  ready: "bg-green-900 text-green-300",
  failed: "bg-red-900 text-red-300",
  unavailable: "bg-amber-900 text-amber-300",
};

const STYLE_OPTIONS: { value: LyricsConfig["style"]; label: string }[] = [
  { value: "karaoke", label: "Karaoke (sing-along highlight)" },
  { value: "per-word-pop", label: "Per-word pop-in (TikTok style)" },
];

const POSITION_OPTIONS: { value: string; label: string }[] = [
  { value: "bottom", label: "Bottom" },
  { value: "center", label: "Center" },
  { value: "top", label: "Top" },
];

function defaultConfig(): LyricsConfig {
  return {
    enabled: false,
    style: "karaoke",
    position: "bottom",
    text_color: "#FFFFFF",
    highlight_color: "#FFFF00",
    font_style: "sans",
    text_size: "medium",
    outline_px: 2,
  };
}

export default function LyricsSection({
  track,
  onTrackUpdated,
}: {
  track: MusicTrackDetail;
  onTrackUpdated: (t: MusicTrackDetail) => void;
}) {
  const existing = track.track_config?.lyrics_config ?? defaultConfig();
  const [cfg, setCfg] = useState<LyricsConfig>(existing);
  const [saving, setSaving] = useState(false);
  const [extracting, setExtracting] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const [previewExpanded, setPreviewExpanded] = useState(false);

  const status = track.lyrics_status;
  const cache = track.lyrics_cached;

  async function handleSave() {
    setSaving(true);
    setMsg(null);
    try {
      const updated = await adminUpdateMusicTrack(track.id, {
        track_config: {
          ...(track.track_config ?? {}),
          lyrics_config: cfg,
        },
      });
      onTrackUpdated(updated);
      setMsg("Saved.");
    } catch (err) {
      setMsg(err instanceof Error ? err.message : "Save failed");
    } finally {
      setSaving(false);
    }
  }

  async function handleExtract() {
    setExtracting(true);
    setMsg(null);
    try {
      await adminExtractLyrics(track.id);
      // Poll until status leaves "extracting".
      const start = Date.now();
      while (Date.now() - start < 120_000) {
        await new Promise((r) => setTimeout(r, 2500));
        const fresh = await adminGetMusicTrack(track.id);
        onTrackUpdated(fresh);
        if (fresh.lyrics_status !== "extracting") return;
      }
      setMsg("Extraction is still running — refresh in a minute.");
    } catch (err) {
      setMsg(err instanceof Error ? err.message : "Extract failed");
    } finally {
      setExtracting(false);
    }
  }

  return (
    <div className="bg-zinc-900 rounded-xl border border-zinc-700 p-5 mb-6">
      <div className="flex items-center justify-between mb-4">
        <h2 className="font-semibold">Lyrics</h2>
        <span
          className={`text-xs font-semibold px-2 py-1 rounded-full ${
            STATUS_COLORS[status] ?? STATUS_COLORS.pending
          }`}
        >
          {status}
        </span>
      </div>

      <div className="grid grid-cols-2 gap-x-8 gap-y-2 text-sm mb-4">
        <div>
          <span className="text-zinc-500">Source </span>
          <span className="text-zinc-100">{track.lyrics_source ?? "—"}</span>
        </div>
        <div>
          <span className="text-zinc-500">Language </span>
          <span className="text-zinc-100">{cache?.language || "—"}</span>
        </div>
        <div>
          <span className="text-zinc-500">Lines </span>
          <span className="text-zinc-100">{cache?.lines?.length ?? 0}</span>
        </div>
        <div>
          <span className="text-zinc-500">Confidence </span>
          <span className="text-zinc-100">
            {cache?.confidence != null ? cache.confidence.toFixed(2) : "—"}
          </span>
        </div>
        {cache?.genius_url && (
          <div className="col-span-2">
            <span className="text-zinc-500">Matched on Genius </span>
            <a
              href={cache.genius_url}
              target="_blank"
              rel="noopener noreferrer"
              className="text-violet-400 hover:text-violet-300 text-xs break-all"
            >
              {cache.track_title_matched} — {cache.artist_matched}
            </a>
          </div>
        )}
        {track.lyrics_error_detail && (
          <div className="col-span-2 text-red-400 text-xs break-words">
            {track.lyrics_error_detail}
          </div>
        )}
      </div>

      {/* Cached preview */}
      {cache?.lines && cache.lines.length > 0 && (
        <div className="bg-zinc-950 rounded-lg border border-zinc-800 p-3 mb-4">
          <button
            onClick={() => setPreviewExpanded(!previewExpanded)}
            className="text-xs text-zinc-400 hover:text-zinc-200 mb-2"
          >
            {previewExpanded
              ? "▼ Hide preview"
              : `▶ Preview (${cache.lines.length} lines)`}
          </button>
          {previewExpanded && (
            <ol className="text-xs text-zinc-300 space-y-1 max-h-64 overflow-y-auto font-mono">
              {cache.lines.map((line, i) => (
                <li key={i}>
                  <span className="text-zinc-600 tabular-nums">
                    {line.start_s.toFixed(2)}s
                  </span>{" "}
                  {line.text}
                </li>
              ))}
            </ol>
          )}
        </div>
      )}

      {/* Visual config form */}
      <div className="space-y-3 mb-4">
        <label className="flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            checked={cfg.enabled}
            onChange={(e) => setCfg({ ...cfg, enabled: e.target.checked })}
            className="rounded"
          />
          <span>Enable lyrics in music jobs using this track</span>
        </label>

        <div className="grid grid-cols-2 gap-4">
          <label className="block">
            <span className="text-xs text-zinc-400 mb-1 block">Animation style</span>
            <select
              value={cfg.style}
              onChange={(e) =>
                setCfg({ ...cfg, style: e.target.value as LyricsConfig["style"] })
              }
              className="w-full bg-zinc-800 border border-zinc-600 rounded-lg px-3 py-2 text-sm text-zinc-100 focus:outline-none focus:border-violet-500"
            >
              {STYLE_OPTIONS.map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </select>
          </label>
          <label className="block">
            <span className="text-xs text-zinc-400 mb-1 block">Position</span>
            <select
              value={cfg.position ?? "bottom"}
              onChange={(e) => setCfg({ ...cfg, position: e.target.value })}
              className="w-full bg-zinc-800 border border-zinc-600 rounded-lg px-3 py-2 text-sm text-zinc-100 focus:outline-none focus:border-violet-500"
            >
              {POSITION_OPTIONS.map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </select>
          </label>
        </div>

        <div className="grid grid-cols-2 gap-4">
          <label className="block">
            <span className="text-xs text-zinc-400 mb-1 block">Text color</span>
            <input
              type="color"
              value={cfg.text_color ?? "#FFFFFF"}
              onChange={(e) => setCfg({ ...cfg, text_color: e.target.value })}
              className="w-full h-9 bg-zinc-800 border border-zinc-600 rounded-lg"
            />
          </label>
          <label className="block">
            <span className="text-xs text-zinc-400 mb-1 block">
              Highlight color (karaoke only)
            </span>
            <input
              type="color"
              value={cfg.highlight_color ?? "#FFFF00"}
              onChange={(e) =>
                setCfg({ ...cfg, highlight_color: e.target.value })
              }
              disabled={cfg.style !== "karaoke"}
              className="w-full h-9 bg-zinc-800 border border-zinc-600 rounded-lg disabled:opacity-40"
            />
          </label>
        </div>
      </div>

      {msg && (
        <p
          className={`text-sm mb-3 ${
            msg === "Saved." ? "text-green-400" : "text-red-400"
          }`}
        >
          {msg}
        </p>
      )}

      <div className="flex flex-wrap gap-3">
        <button
          onClick={handleSave}
          disabled={saving}
          className="bg-violet-600 hover:bg-violet-500 disabled:opacity-40 text-white text-sm font-semibold px-4 py-2 rounded-lg transition-colors"
        >
          {saving ? "Saving…" : "Save lyrics config"}
        </button>
        <button
          onClick={handleExtract}
          disabled={extracting || status === "extracting"}
          className="bg-zinc-700 hover:bg-zinc-600 disabled:opacity-40 text-zinc-100 text-sm font-semibold px-4 py-2 rounded-lg transition-colors"
        >
          {extracting || status === "extracting"
            ? "Extracting…"
            : "Re-extract lyrics"}
        </button>
      </div>
    </div>
  );
}
