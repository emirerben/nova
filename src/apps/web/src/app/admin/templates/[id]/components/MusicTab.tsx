"use client";

import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import {
  type AdminTemplate,
  type ChildTemplate,
  type MusicTrackPickerItem,
  adminCreateChildTemplate,
  adminListChildren,
  adminListPublishedMusicTracks,
  adminRemergeChildren,
} from "@/lib/admin-api";

// ── Music Tab ───────────────────────────────────────────────────────────────

export function MusicTab({ template }: { template: AdminTemplate }) {
  const router = useRouter();
  const [children, setChildren] = useState<ChildTemplate[]>([]);
  const [loading, setLoading] = useState(true);
  const [showPicker, setShowPicker] = useState(false);
  const [remerging, setRemerging] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const fetchChildren = useCallback(async () => {
    try {
      const res = await adminListChildren(template.id);
      setChildren(res.children);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load children");
    } finally {
      setLoading(false);
    }
  }, [template.id]);

  useEffect(() => {
    fetchChildren();
  }, [fetchChildren]);

  const handleRemerge = useCallback(async () => {
    if (!confirm("Re-merge all children with the parent's latest recipe? This overwrites their current recipes.")) return;
    setRemerging(true);
    setError(null);
    try {
      const res = await adminRemergeChildren(template.id);
      alert(`Updated ${res.updated} sub-template(s)`);
      fetchChildren();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Remerge failed");
    } finally {
      setRemerging(false);
    }
  }, [template.id, fetchChildren]);

  const handleChildCreated = useCallback(() => {
    setShowPicker(false);
    fetchChildren();
  }, [fetchChildren]);

  if (loading) {
    return (
      <div className="flex items-center justify-center py-12">
        <div className="w-6 h-6 border-2 border-zinc-600 border-t-white rounded-full animate-spin" />
      </div>
    );
  }

  return (
    <div className="space-y-6 max-w-2xl">
      {/* Header */}
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-semibold text-white">Music Variants</h2>
        <button
          onClick={handleRemerge}
          disabled={remerging || children.length === 0}
          className="px-3 py-1.5 text-sm bg-zinc-800 hover:bg-zinc-700 text-white rounded disabled:opacity-50"
        >
          {remerging ? "Re-merging..." : "Re-merge all"}
        </button>
      </div>

      {error && <p className="text-red-400 text-sm">{error}</p>}

      {/* Children list */}
      {children.length > 0 ? (
        <div className="space-y-2">
          {children.map((child) => (
            <button
              key={child.id}
              onClick={() => router.push(`/admin/templates/${child.id}`)}
              className="w-full text-left border border-zinc-800 rounded-lg p-4 hover:border-zinc-600 transition-colors group"
            >
              <div className="flex items-center justify-between">
                <div>
                  <div className="flex items-center gap-2">
                    <span className="text-white font-medium">
                      {child.track_title}
                    </span>
                    {child.track_artist && (
                      <span className="text-zinc-500">— {child.track_artist}</span>
                    )}
                    <StatusDot status={child.analysis_status} />
                  </div>
                  <p className="text-xs text-zinc-500 mt-1">
                    {child.beat_count} beats
                    {child.published_at ? " · Published" : ""}
                  </p>
                </div>
                <span className="text-zinc-600 group-hover:text-zinc-400 transition-colors">
                  →
                </span>
              </div>
            </button>
          ))}
        </div>
      ) : (
        <div className="border border-dashed border-zinc-700 rounded-lg p-8 text-center">
          <p className="text-zinc-500 text-sm">No music variants yet</p>
          <p className="text-zinc-600 text-xs mt-1">
            Add a song to create a beat-synced sub-template
          </p>
        </div>
      )}

      {/* Add Song button */}
      <button
        onClick={() => setShowPicker(true)}
        className="px-4 py-2 text-sm bg-blue-700 hover:bg-blue-600 text-white rounded"
      >
        + Add Song
      </button>

      {/* Track picker modal */}
      {showPicker && (
        <TrackPicker
          parentId={template.id}
          existingTrackIds={children.map((c) => c.music_track_id)}
          onSelect={handleChildCreated}
          onClose={() => setShowPicker(false)}
        />
      )}
    </div>
  );
}

// ── Track Picker Modal ──────────────────────────────────────────────────────

function TrackPicker({
  parentId,
  existingTrackIds,
  onSelect,
  onClose,
}: {
  parentId: string;
  existingTrackIds: string[];
  onSelect: () => void;
  onClose: () => void;
}) {
  const [tracks, setTracks] = useState<MusicTrackPickerItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [creating, setCreating] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    adminListPublishedMusicTracks()
      .then(setTracks)
      .catch((err) => setError(err instanceof Error ? err.message : "Failed to load tracks"))
      .finally(() => setLoading(false));
  }, []);

  const handlePick = useCallback(
    async (trackId: string) => {
      setCreating(trackId);
      setError(null);
      try {
        await adminCreateChildTemplate(parentId, trackId);
        onSelect();
      } catch (err) {
        setError(err instanceof Error ? err.message : "Failed to create sub-template");
        setCreating(null);
      }
    },
    [parentId, onSelect],
  );

  const availableTracks = tracks.filter((t) => !existingTrackIds.includes(t.id));

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60">
      <div className="bg-zinc-900 border border-zinc-700 rounded-lg w-full max-w-lg max-h-[70vh] flex flex-col">
        <div className="flex items-center justify-between px-4 py-3 border-b border-zinc-800">
          <h3 className="text-sm font-medium text-white">Select a Song</h3>
          <button onClick={onClose} className="text-zinc-500 hover:text-white text-sm">
            Close
          </button>
        </div>

        <div className="flex-1 overflow-auto p-4">
          {error && <p className="text-red-400 text-sm mb-3">{error}</p>}

          {loading ? (
            <div className="flex justify-center py-8">
              <div className="w-6 h-6 border-2 border-zinc-600 border-t-white rounded-full animate-spin" />
            </div>
          ) : availableTracks.length === 0 ? (
            <p className="text-zinc-500 text-sm text-center py-8">
              {tracks.length === 0
                ? "No published tracks available. Add tracks in the Music section first."
                : "All published tracks are already added."}
            </p>
          ) : (
            <div className="space-y-2">
              {availableTracks.map((track) => (
                <button
                  key={track.id}
                  onClick={() => handlePick(track.id)}
                  disabled={creating !== null}
                  className="w-full text-left border border-zinc-800 rounded p-3 hover:border-zinc-600 transition-colors disabled:opacity-50"
                >
                  <div className="flex items-center justify-between">
                    <div>
                      <span className="text-white text-sm">{track.title}</span>
                      {track.artist && (
                        <span className="text-zinc-500 text-sm ml-2">— {track.artist}</span>
                      )}
                      <p className="text-xs text-zinc-600 mt-0.5">
                        {track.beat_count} beats
                        {track.duration_s ? ` · ${track.duration_s.toFixed(1)}s` : ""}
                      </p>
                    </div>
                    {creating === track.id ? (
                      <div className="w-4 h-4 border-2 border-zinc-600 border-t-white rounded-full animate-spin" />
                    ) : (
                      <span className="text-xs text-zinc-600">Select</span>
                    )}
                  </div>
                </button>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

// ── Helpers ──────────────────────────────────────────────────────────────────

function StatusDot({ status }: { status: string }) {
  const color =
    status === "ready"
      ? "bg-green-500"
      : status === "analyzing"
        ? "bg-amber-500"
        : status === "failed"
          ? "bg-red-500"
          : "bg-zinc-500";
  return <span className={`inline-block w-2 h-2 rounded-full ${color}`} />;
}
