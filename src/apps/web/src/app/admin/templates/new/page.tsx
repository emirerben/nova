"use client";

import { useRouter } from "next/navigation";
import { useCallback, useState } from "react";
import {
  adminCreateTemplate,
  adminGetPresignedUpload,
} from "@/lib/admin-api";
import { useFileUpload } from "@/hooks/useFileUpload";

/**
 * Upload a new template video + set parameters.
 * Flow: upload video → fill form → submit → redirect to detail page.
 */
export default function NewTemplatePage() {
  const router = useRouter();

  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [sourceUrl, setSourceUrl] = useState("");
  const [clipsMin, setClipsMin] = useState(5);
  const [clipsMax, setClipsMax] = useState(10);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const upload = useFileUpload({
    getPresignedUrl: async (file) => {
      return adminGetPresignedUpload(file.name, file.type || "video/mp4");
    },
  });

  const handleFileSelect = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => {
      if (!e.target.files) return;
      // Only allow one template video
      upload.clearFiles();
      const entries = upload.addFiles([e.target.files[0]]);
      upload.startUpload(entries);
    },
    [upload],
  );

  const handleSubmit = useCallback(
    async (e: React.FormEvent) => {
      e.preventDefault();
      if (upload.successfulPaths.length === 0) {
        setError("Upload a video first");
        return;
      }
      if (!name.trim()) {
        setError("Name is required");
        return;
      }

      setSubmitting(true);
      setError(null);
      try {
        const template = await adminCreateTemplate({
          name: name.trim(),
          gcs_path: upload.successfulPaths[0],
          required_clips_min: clipsMin,
          required_clips_max: clipsMax,
          description: description.trim() || undefined,
          source_url: sourceUrl.trim() || undefined,
        });
        router.push(`/admin/templates/${template.id}?tab=recipe`);
      } catch (err) {
        setError(err instanceof Error ? err.message : "Creation failed");
        setSubmitting(false);
      }
    },
    [upload.successfulPaths, name, description, sourceUrl, clipsMin, clipsMax, router],
  );

  const uploadedFile = upload.files[0];

  return (
    <div className="p-6 max-w-xl">
      <h1 className="text-lg font-semibold mb-6">New Template</h1>

      <form onSubmit={handleSubmit} className="space-y-5">
        {/* Video upload */}
        <div>
          <label className="block text-sm text-zinc-400 mb-1.5">Template Video</label>
          {uploadedFile ? (
            <div className="border border-zinc-800 rounded p-3 flex items-center gap-3">
              <span className="text-sm text-zinc-300 truncate flex-1">{uploadedFile.file.name}</span>
              {uploadedFile.error ? (
                <span className="text-red-400 text-xs">{uploadedFile.error}</span>
              ) : uploadedFile.progress === 100 ? (
                <span className="text-green-400 text-xs">Uploaded</span>
              ) : (
                <div className="w-24 bg-zinc-800 rounded-full h-1.5">
                  <div
                    className="bg-blue-500 h-full rounded-full transition-all"
                    style={{ width: `${uploadedFile.progress}%` }}
                  />
                </div>
              )}
              <button
                type="button"
                onClick={() => upload.clearFiles()}
                className="text-zinc-600 hover:text-zinc-400 text-xs"
              >
                Remove
              </button>
            </div>
          ) : (
            <div className="border-2 border-dashed border-zinc-700 rounded-lg p-8 text-center hover:border-zinc-500 transition-colors">
              <input
                type="file"
                accept="video/mp4,video/quicktime"
                onChange={handleFileSelect}
                className="hidden"
                id="template-video-input"
              />
              <label htmlFor="template-video-input" className="cursor-pointer">
                <p className="text-sm text-zinc-400">Click to select a template video</p>
                <p className="text-xs text-zinc-600 mt-1">MP4 or MOV, 9:16 aspect ratio recommended</p>
              </label>
            </div>
          )}
        </div>

        {/* Name */}
        <div>
          <label className="block text-sm text-zinc-400 mb-1.5">Name *</label>
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="e.g. Travel Montage V2"
            required
            className="w-full bg-zinc-900 border border-zinc-700 rounded px-3 py-2 text-white text-sm focus:outline-none focus:border-zinc-500"
          />
        </div>

        {/* Description */}
        <div>
          <label className="block text-sm text-zinc-400 mb-1.5">Description</label>
          <textarea
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder="Admin notes about this template..."
            rows={2}
            className="w-full bg-zinc-900 border border-zinc-700 rounded px-3 py-2 text-white text-sm focus:outline-none focus:border-zinc-500 resize-none"
          />
        </div>

        {/* Source URL */}
        <div>
          <label className="block text-sm text-zinc-400 mb-1.5">Source URL</label>
          <input
            value={sourceUrl}
            onChange={(e) => setSourceUrl(e.target.value)}
            placeholder="https://www.tiktok.com/..."
            className="w-full bg-zinc-900 border border-zinc-700 rounded px-3 py-2 text-white text-sm focus:outline-none focus:border-zinc-500"
          />
        </div>

        {/* Clip requirements */}
        <div className="grid grid-cols-2 gap-4">
          <div>
            <label className="block text-sm text-zinc-400 mb-1.5">Min Clips</label>
            <input
              type="number"
              value={clipsMin}
              onChange={(e) => setClipsMin(Number(e.target.value))}
              min={1}
              max={30}
              className="w-full bg-zinc-900 border border-zinc-700 rounded px-3 py-2 text-white text-sm focus:outline-none focus:border-zinc-500"
            />
          </div>
          <div>
            <label className="block text-sm text-zinc-400 mb-1.5">Max Clips</label>
            <input
              type="number"
              value={clipsMax}
              onChange={(e) => setClipsMax(Number(e.target.value))}
              min={1}
              max={30}
              className="w-full bg-zinc-900 border border-zinc-700 rounded px-3 py-2 text-white text-sm focus:outline-none focus:border-zinc-500"
            />
          </div>
        </div>

        {error && <p className="text-red-400 text-sm">{error}</p>}

        <button
          type="submit"
          disabled={submitting || upload.uploading || upload.successfulPaths.length === 0}
          className="px-6 py-2.5 text-sm bg-white text-black rounded font-medium hover:bg-zinc-200 disabled:opacity-50"
        >
          {submitting ? "Creating..." : "Create Template"}
        </button>
      </form>
    </div>
  );
}
