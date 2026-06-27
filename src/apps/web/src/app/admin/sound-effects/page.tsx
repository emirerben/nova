"use client";

import { useEffect, useState } from "react";
import {
  adminUploadSfx,
  listAdminSoundEffects,
  patchSoundEffect,
  archiveSoundEffect,
  getSfxAudioUrl,
  type SoundEffectSummary,
} from "@/lib/sfx-api";

const STATUS_COLORS: Record<string, string> = {
  pending: "bg-amber-900 text-amber-300",
  ready: "bg-green-900 text-green-300",
  failed: "bg-red-900 text-red-300",
};

type UploadStage = "uploading" | "confirming" | null;

export default function AdminSoundEffectsPage() {
  const [effects, setEffects] = useState<SoundEffectSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Upload form
  const [file, setFile] = useState<File | null>(null);
  const [name, setName] = useState("");
  const [uploading, setUploading] = useState(false);
  const [uploadStage, setUploadStage] = useState<UploadStage>(null);
  const [uploadPct, setUploadPct] = useState<number | null>(null);
  const [uploadError, setUploadError] = useState<string | null>(null);

  // Inline edit state
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editName, setEditName] = useState("");
  const [saving, setSaving] = useState(false);

  // Preview audio
  const [previewUrls, setPreviewUrls] = useState<Record<string, string>>({});

  async function loadEffects() {
    setLoading(true);
    try {
      const data = await listAdminSoundEffects(100, 0);
      setEffects(data.effects);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Failed to load sound effects");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { loadEffects(); }, []);

  async function handleUpload(e: React.FormEvent) {
    e.preventDefault();
    if (!file) return;
    setUploading(true);
    setUploadError(null);
    setUploadPct(null);
    try {
      await adminUploadSfx(file, name || undefined, (stage, pct) => {
        setUploadStage(stage);
        setUploadPct(pct ?? null);
      });
      setFile(null);
      setName("");
      setUploadStage(null);
      await loadEffects();
    } catch (e: unknown) {
      setUploadError(e instanceof Error ? e.message : "Upload failed");
    } finally {
      setUploading(false);
    }
  }

  async function handlePublishToggle(effect: SoundEffectSummary) {
    try {
      const updated = await patchSoundEffect(effect.id, {
        published: effect.published_at == null,
      });
      setEffects((prev) => prev.map((e) => (e.id === effect.id ? updated : e)));
    } catch (e: unknown) {
      alert(e instanceof Error ? e.message : "Failed to update");
    }
  }

  async function handleArchive(effect: SoundEffectSummary) {
    if (!confirm(`Archive "${effect.name}"? It will no longer appear in the picker.`)) return;
    try {
      await archiveSoundEffect(effect.id);
      setEffects((prev) => prev.filter((e) => e.id !== effect.id));
    } catch (e: unknown) {
      alert(e instanceof Error ? e.message : "Failed to archive");
    }
  }

  function startEdit(effect: SoundEffectSummary) {
    setEditingId(effect.id);
    setEditName(effect.name);
  }

  async function saveEdit(effect: SoundEffectSummary) {
    if (!editName.trim()) return;
    setSaving(true);
    try {
      const updated = await patchSoundEffect(effect.id, { name: editName.trim() });
      setEffects((prev) => prev.map((e) => (e.id === effect.id ? updated : e)));
      setEditingId(null);
    } catch (e: unknown) {
      alert(e instanceof Error ? e.message : "Failed to rename");
    } finally {
      setSaving(false);
    }
  }

  async function loadPreview(effectId: string) {
    if (previewUrls[effectId]) return;
    try {
      const url = await getSfxAudioUrl(effectId);
      setPreviewUrls((prev) => ({ ...prev, [effectId]: url }));
    } catch {
      // silent — preview is best-effort
    }
  }

  return (
    <div className="min-h-screen bg-zinc-950 text-white p-6 space-y-8 max-w-4xl mx-auto">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold">Sound Effects</h1>
        <a href="/admin" className="text-sm text-zinc-400 hover:text-white">← Admin</a>
      </div>

      {/* Upload form */}
      <form onSubmit={handleUpload} className="bg-zinc-900 border border-zinc-800 rounded-xl p-5 space-y-4">
        <h2 className="font-semibold text-lg">Add new effect</h2>
        <div className="space-y-3">
          <label className="block space-y-1">
            <span className="text-xs text-zinc-400 uppercase tracking-wide">Audio file</span>
            <input
              type="file"
              accept="audio/mpeg,audio/mp4,audio/wav,audio/aac,audio/ogg"
              onChange={(e) => {
                const f = e.target.files?.[0] ?? null;
                setFile(f);
                if (f && !name) setName(f.name.replace(/\.[^.]+$/, ""));
              }}
              className="block w-full text-sm text-zinc-400 file:mr-3 file:py-1.5 file:px-3 file:rounded file:border-0 file:bg-zinc-700 file:text-white file:text-sm hover:file:bg-zinc-600"
              required
            />
          </label>
          <label className="block space-y-1">
            <span className="text-xs text-zinc-400 uppercase tracking-wide">Name</span>
            <input
              type="text"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="e.g. Fah"
              className="w-full bg-zinc-800 border border-zinc-700 rounded px-3 py-2 text-sm focus:outline-none focus:border-lime-500"
            />
          </label>
        </div>

        {uploadStage && (
          <div className="text-sm text-zinc-400">
            {uploadStage === "uploading"
              ? `Uploading… ${uploadPct != null ? `${uploadPct}%` : ""}`
              : "Confirming upload…"}
          </div>
        )}
        {uploadError && <p className="text-sm text-red-400">{uploadError}</p>}

        <button
          type="submit"
          disabled={!file || uploading}
          className="w-full py-2 bg-lime-600 hover:bg-lime-500 text-black font-semibold rounded disabled:opacity-40"
        >
          {uploading ? "Uploading…" : "Upload Effect"}
        </button>
      </form>

      {/* Effect list */}
      <div className="space-y-3">
        <h2 className="font-semibold text-lg">
          Library ({effects.length})
        </h2>

        {loading && <p className="text-zinc-500">Loading…</p>}
        {error && <p className="text-red-400">{error}</p>}

        {effects.map((effect) => (
          <div
            key={effect.id}
            className="bg-zinc-900 border border-zinc-800 rounded-lg p-4 flex items-center gap-3 flex-wrap"
          >
            {/* Name / edit */}
            <div className="flex-1 min-w-0">
              {editingId === effect.id ? (
                <div className="flex gap-2 items-center">
                  <input
                    value={editName}
                    onChange={(e) => setEditName(e.target.value)}
                    onKeyDown={(e) => e.key === "Enter" && saveEdit(effect)}
                    autoFocus
                    className="bg-zinc-800 border border-zinc-600 rounded px-2 py-0.5 text-sm focus:outline-none focus:border-lime-500"
                  />
                  <button
                    onClick={() => saveEdit(effect)}
                    disabled={saving}
                    className="text-xs text-lime-400 hover:text-lime-300 disabled:opacity-40"
                  >
                    Save
                  </button>
                  <button
                    onClick={() => setEditingId(null)}
                    className="text-xs text-zinc-500 hover:text-white"
                  >
                    Cancel
                  </button>
                </div>
              ) : (
                <button
                  onClick={() => startEdit(effect)}
                  className="text-sm font-medium hover:text-lime-300 text-left truncate max-w-xs"
                >
                  {effect.name}
                </button>
              )}
              <p className="text-xs text-zinc-500 mt-0.5">
                {effect.duration_s != null ? `${effect.duration_s.toFixed(2)}s` : "—"}
                {effect.source_filename ? ` · ${effect.source_filename}` : ""}
              </p>
            </div>

            {/* Status badge */}
            <span
              className={`text-xs font-mono px-2 py-0.5 rounded ${STATUS_COLORS[effect.status] ?? "bg-zinc-700 text-zinc-300"}`}
            >
              {effect.status}
            </span>

            {/* Published badge */}
            {effect.published_at != null && (
              <span className="text-xs bg-lime-900 text-lime-300 px-2 py-0.5 rounded">
                published
              </span>
            )}

            {/* Preview */}
            {effect.status === "ready" && (
              previewUrls[effect.id] ? (
                <audio
                  src={previewUrls[effect.id]}
                  controls
                  className="h-8 w-36"
                />
              ) : (
                <button
                  onClick={() => loadPreview(effect.id)}
                  className="text-xs text-zinc-400 hover:text-white underline"
                >
                  Preview
                </button>
              )
            )}

            {/* Actions */}
            <div className="flex gap-2 text-xs">
              <button
                onClick={() => handlePublishToggle(effect)}
                className="px-2 py-1 rounded bg-zinc-700 hover:bg-zinc-600 text-zinc-300"
              >
                {effect.published_at != null ? "Unpublish" : "Publish"}
              </button>
              <button
                onClick={() => handleArchive(effect)}
                className="px-2 py-1 rounded bg-zinc-800 hover:bg-red-900 text-zinc-400 hover:text-red-300"
              >
                Archive
              </button>
            </div>
          </div>
        ))}

        {!loading && effects.length === 0 && (
          <p className="text-zinc-500 text-sm">No sound effects yet. Upload one above.</p>
        )}
      </div>
    </div>
  );
}
