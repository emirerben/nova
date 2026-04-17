"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import {
  adminListMusicTracks,
  adminCreateMusicTrack,
  type MusicTrackDetail,
} from "@/lib/music-api";

const STATUS_COLORS: Record<string, string> = {
  queued: "bg-zinc-700 text-zinc-300",
  analyzing: "bg-blue-900 text-blue-300",
  ready: "bg-green-900 text-green-300",
  failed: "bg-red-900 text-red-300",
};

export default function AdminMusicPage() {
  const [tracks, setTracks] = useState<MusicTrackDetail[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [url, setUrl] = useState("");
  const [title, setTitle] = useState("");
  const [artist, setArtist] = useState("");
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);

  async function loadTracks() {
    setLoading(true);
    try {
      const data = await adminListMusicTracks(50, 0);
      setTracks(data.tracks);
      setTotal(data.total);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Failed to load tracks");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    loadTracks();
  }, []);

  async function handleCreate(e: React.FormEvent) {
    e.preventDefault();
    setCreating(true);
    setCreateError(null);
    try {
      await adminCreateMusicTrack(url, title || undefined, artist || undefined);
      setUrl("");
      setTitle("");
      setArtist("");
      await loadTracks();
    } catch (e: unknown) {
      setCreateError(e instanceof Error ? e.message : "Failed to create track");
    } finally {
      setCreating(false);
    }
  }

  return (
    <div className="min-h-screen bg-zinc-950 text-zinc-100 p-6 max-w-4xl mx-auto">
      <div className="flex items-center gap-4 mb-6">
        <Link href="/admin" className="text-zinc-400 hover:text-zinc-200 text-sm">
          ← Admin
        </Link>
        <h1 className="text-2xl font-bold">Music Tracks</h1>
        <span className="text-zinc-500 text-sm ml-auto">{total} total</span>
      </div>

      {/* Add track form */}
      <form
        onSubmit={handleCreate}
        className="bg-zinc-900 rounded-xl border border-zinc-700 p-5 mb-8"
      >
        <h2 className="font-semibold mb-4">Add track from URL</h2>
        <div className="space-y-3">
          <input
            required
            type="url"
            placeholder="YouTube or SoundCloud URL"
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            className="w-full bg-zinc-800 border border-zinc-600 rounded-lg px-3 py-2 text-sm font-mono text-zinc-100 placeholder:text-zinc-600 focus:outline-none focus:border-violet-500"
          />
          <div className="flex gap-3">
            <input
              type="text"
              placeholder="Title (optional — auto-detected)"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              className="flex-1 bg-zinc-800 border border-zinc-600 rounded-lg px-3 py-2 text-sm text-zinc-100 placeholder:text-zinc-600 focus:outline-none focus:border-violet-500"
            />
            <input
              type="text"
              placeholder="Artist (optional)"
              value={artist}
              onChange={(e) => setArtist(e.target.value)}
              className="flex-1 bg-zinc-800 border border-zinc-600 rounded-lg px-3 py-2 text-sm text-zinc-100 placeholder:text-zinc-600 focus:outline-none focus:border-violet-500"
            />
          </div>
        </div>
        {createError && <p className="text-red-400 text-sm mt-2">{createError}</p>}
        <button
          type="submit"
          disabled={creating || !url.trim()}
          className="mt-4 bg-violet-600 hover:bg-violet-500 disabled:opacity-40 disabled:cursor-not-allowed text-white text-sm font-semibold px-5 py-2 rounded-lg transition-colors"
        >
          {creating ? "Downloading…" : "Add track"}
        </button>
      </form>

      {/* Track list */}
      {loading ? (
        <p className="text-zinc-400">Loading…</p>
      ) : error ? (
        <p className="text-red-400">{error}</p>
      ) : tracks.length === 0 ? (
        <p className="text-zinc-500">No tracks yet.</p>
      ) : (
        <div className="space-y-3">
          {tracks.map((t) => (
            <Link
              key={t.id}
              href={`/admin/music/${t.id}`}
              className="flex items-center gap-4 bg-zinc-900 hover:bg-zinc-800 border border-zinc-700 rounded-xl p-4 transition-colors"
            >
              {t.thumbnail_url ? (
                <img
                  src={t.thumbnail_url}
                  alt={t.title}
                  className="w-14 h-14 rounded-lg object-cover shrink-0"
                />
              ) : (
                <div className="w-14 h-14 rounded-lg bg-zinc-800 flex items-center justify-center shrink-0">
                  <span className="text-2xl">🎵</span>
                </div>
              )}
              <div className="flex-1 min-w-0">
                <p className="font-semibold truncate">{t.title}</p>
                <p className="text-sm text-zinc-400 truncate">
                  {t.artist || "Unknown artist"} · {t.beat_count} beats
                </p>
              </div>
              <div className="flex flex-col items-end gap-1 shrink-0">
                <span
                  className={`text-xs font-semibold px-2 py-0.5 rounded-full ${
                    STATUS_COLORS[t.analysis_status] ?? STATUS_COLORS.queued
                  }`}
                >
                  {t.analysis_status}
                </span>
                {t.published_at && (
                  <span className="text-xs text-green-500">published</span>
                )}
                {t.archived_at && (
                  <span className="text-xs text-zinc-500">archived</span>
                )}
              </div>
            </Link>
          ))}
        </div>
      )}
    </div>
  );
}
