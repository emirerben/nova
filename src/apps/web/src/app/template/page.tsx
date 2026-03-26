"use client";

import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import {
  type BatchPresignedFile,
  type TemplateListItem,
  createTemplateJob,
  getBatchPresignedUrls,
  listTemplates,
  uploadFileToGcs,
} from "@/lib/api";

// ── Tone → gradient mapping for placeholder thumbnails ──────────────────────
const TONE_GRADIENTS: Record<string, string> = {
  casual: "from-orange-500 to-amber-400",
  energetic: "from-red-500 to-pink-500",
  calm: "from-blue-500 to-teal-400",
  formal: "from-gray-600 to-gray-800",
};

const ALLOWED_MIME = ["video/mp4", "video/quicktime"];
const MAX_BYTES = 4 * 1024 * 1024 * 1024;
const MAX_CLIPS = 20;

type PageState = "gallery" | "upload" | "uploading" | "enqueuing" | "error";

interface ClipFile {
  file: File;
  id: string;
  progress: number; // 0-100
  error: string | null;
}

export default function TemplatePage() {
  const router = useRouter();
  const fileInput = useRef<HTMLInputElement>(null);

  // Gallery state
  const [templates, setTemplates] = useState<TemplateListItem[]>([]);
  const [loadingTemplates, setLoadingTemplates] = useState(true);
  const [selectedTemplate, setSelectedTemplate] = useState<TemplateListItem | null>(null);

  // Upload state
  const [pageState, setPageState] = useState<PageState>("gallery");
  const [clips, setClips] = useState<ClipFile[]>([]);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [dragOver, setDragOver] = useState(false);

  // Fetch templates on mount
  useEffect(() => {
    listTemplates()
      .then(setTemplates)
      .catch((err) => setErrorMsg(err.message))
      .finally(() => setLoadingTemplates(false));
  }, []);

  function selectTemplate(t: TemplateListItem) {
    setSelectedTemplate(t);
    setPageState("upload");
    setClips([]);
    setErrorMsg(null);
  }

  function backToGallery() {
    setPageState("gallery");
    setSelectedTemplate(null);
    setClips([]);
    setErrorMsg(null);
  }

  function addFiles(files: FileList | File[]) {
    const arr = Array.from(files);
    const valid = arr.filter((f) => {
      if (!ALLOWED_MIME.includes(f.type)) return false;
      if (f.size > MAX_BYTES) return false;
      return true;
    });
    setClips((prev) => {
      const combined = [
        ...prev,
        ...valid.map((f) => ({ file: f, id: crypto.randomUUID(), progress: 0, error: null })),
      ];
      return combined.slice(0, MAX_CLIPS);
    });
  }

  function removeClip(id: string) {
    setClips((prev) => prev.filter((c) => c.id !== id));
  }

  async function handleSubmit() {
    if (!selectedTemplate) return;
    setErrorMsg(null);

    const minClips = selectedTemplate.slot_count || 5;
    if (clips.length < minClips) {
      setErrorMsg(`This template needs at least ${minClips} clips. Add ${minClips - clips.length} more.`);
      return;
    }

    try {
      setPageState("uploading");

      // Step 1: Get batch presigned URLs
      const fileMeta: BatchPresignedFile[] = clips.map((c, i) => ({
        filename: `clip_${i}.${c.file.name.split(".").pop() || "mp4"}`,
        content_type: c.file.type || "video/mp4",
        file_size_bytes: c.file.size,
      }));
      const { urls } = await getBatchPresignedUrls(fileMeta);

      // Step 2: Upload all clips in parallel with per-file progress
      const gcsPaths: string[] = new Array(clips.length);
      await Promise.all(
        clips.map(async (clip, i) => {
          try {
            await uploadFileToGcs(urls[i].upload_url, clip.file);
            gcsPaths[i] = urls[i].gcs_path;
            setClips((prev) =>
              prev.map((c) => (c.id === clip.id ? { ...c, progress: 100 } : c))
            );
          } catch (err) {
            setClips((prev) =>
              prev.map((c) =>
                c.id === clip.id
                  ? { ...c, error: err instanceof Error ? err.message : "Upload failed" }
                  : c
              )
            );
            throw err;
          }
        })
      );

      // Step 3: Create template job
      setPageState("enqueuing");
      const { job_id } = await createTemplateJob({
        template_id: selectedTemplate.id,
        clip_gcs_paths: gcsPaths,
        selected_platforms: ["tiktok", "instagram", "youtube"],
      });

      router.push(`/template-jobs/${job_id}`);
    } catch (err) {
      setPageState("error");
      setErrorMsg(err instanceof Error ? err.message : "Something went wrong.");
    }
  }

  // ── Gallery View ────────────────────────────────────────────────────────────
  if (pageState === "gallery") {
    return (
      <main className="min-h-screen bg-black text-white px-4 py-16">
        <div className="max-w-4xl mx-auto">
          <h1 className="text-3xl font-bold mb-2 text-center">Choose a Template</h1>
          <p className="text-zinc-400 text-sm text-center mb-10">
            Pick a viral template — then upload your clips to fill the slots.
          </p>

          {loadingTemplates && (
            <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
              {[1, 2, 3].map((i) => (
                <div key={i} className="h-48 bg-zinc-900 rounded-xl animate-pulse" />
              ))}
            </div>
          )}

          {!loadingTemplates && templates.length === 0 && (
            <div className="text-center py-20">
              <p className="text-zinc-500 text-lg">No templates available yet.</p>
              <p className="text-zinc-600 text-sm mt-2">Templates are being prepared — check back soon.</p>
            </div>
          )}

          {!loadingTemplates && templates.length > 0 && (
            <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
              {templates.map((t) => {
                const gradient = TONE_GRADIENTS[t.copy_tone] || TONE_GRADIENTS.casual;
                return (
                  <button
                    key={t.id}
                    onClick={() => selectTemplate(t)}
                    className="group relative overflow-hidden rounded-xl border border-zinc-800 hover:border-zinc-600 transition-all text-left"
                  >
                    {/* Gradient placeholder (v1: no thumbnails) */}
                    <div className={`h-32 bg-gradient-to-br ${gradient} opacity-80 group-hover:opacity-100 transition-opacity`} />
                    <div className="p-4">
                      <h3 className="font-semibold text-sm mb-1">{t.name}</h3>
                      <div className="flex items-center gap-3 text-xs text-zinc-400">
                        <span>{t.slot_count} slots</span>
                        <span>·</span>
                        <span>{Math.round(t.total_duration_s)}s</span>
                        <span>·</span>
                        <span className="capitalize">{t.copy_tone}</span>
                      </div>
                    </div>
                  </button>
                );
              })}
            </div>
          )}

          {errorMsg && (
            <div className="mt-6 bg-red-900/40 border border-red-700 rounded-lg px-4 py-3 text-sm text-red-300 text-center">
              {errorMsg}
            </div>
          )}

          <p className="mt-8 text-center text-xs text-zinc-600">
            <a href="/" className="underline hover:text-zinc-400">← Back to home</a>
          </p>
        </div>
      </main>
    );
  }

  // ── Upload View (template selected) ─────────────────────────────────────────
  const minClips = selectedTemplate?.slot_count || 5;
  const canSubmit = clips.length >= minClips && pageState === "upload";
  const totalProgress =
    clips.length > 0
      ? Math.round(clips.reduce((sum, c) => sum + c.progress, 0) / clips.length)
      : 0;

  return (
    <main className="min-h-screen bg-black text-white flex flex-col items-center justify-center px-4 py-16">
      <div className="w-full max-w-xl">
        <button
          onClick={backToGallery}
          className="text-zinc-400 text-sm hover:text-white transition-colors mb-6"
        >
          ← Back to templates
        </button>

        <h1 className="text-2xl font-bold mb-1">
          {selectedTemplate?.name || "Upload Clips"}
        </h1>
        <p className="text-zinc-400 text-sm mb-6">
          Upload {minClips}–{MAX_CLIPS} raw clips. AI will assemble them to match this template.
        </p>

        {/* Drop zone */}
        <div
          className={`border-2 border-dashed rounded-xl p-8 text-center cursor-pointer transition-colors ${
            dragOver ? "border-white bg-zinc-800" : "border-zinc-600 hover:border-zinc-400"
          }`}
          onClick={() => fileInput.current?.click()}
          onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
          onDragLeave={() => setDragOver(false)}
          onDrop={(e) => {
            e.preventDefault();
            setDragOver(false);
            addFiles(e.dataTransfer.files);
          }}
        >
          <p className="text-zinc-400 text-sm">
            {clips.length === 0
              ? "Drop clips here or click to browse"
              : `${clips.length} clip${clips.length !== 1 ? "s" : ""} selected`}
          </p>
          <p className="text-zinc-600 text-xs mt-1">MP4, MOV · Up to 4GB each</p>
          <input
            ref={fileInput}
            type="file"
            accept="video/mp4,video/quicktime"
            multiple
            className="hidden"
            onChange={(e) => e.target.files && addFiles(e.target.files)}
          />
        </div>

        {/* Clip list with per-file progress */}
        {clips.length > 0 && (
          <ul className="mt-4 space-y-2">
            {clips.map((c) => (
              <li
                key={c.id}
                className="flex items-center bg-zinc-900 rounded-lg px-4 py-2 text-sm"
              >
                <span className="truncate text-zinc-300 flex-1 mr-3">{c.file.name}</span>
                <span className="text-zinc-500 mr-3 shrink-0">
                  {(c.file.size / 1024 / 1024).toFixed(1)} MB
                </span>
                {c.error ? (
                  <span className="text-red-400 text-xs mr-3">Failed</span>
                ) : c.progress > 0 && c.progress < 100 ? (
                  <span className="text-blue-400 text-xs mr-3">{c.progress}%</span>
                ) : c.progress === 100 ? (
                  <span className="text-green-400 text-xs mr-3">✓</span>
                ) : null}
                {pageState === "upload" && (
                  <button
                    onClick={() => removeClip(c.id)}
                    className="text-zinc-500 hover:text-red-400 transition-colors shrink-0"
                    aria-label="Remove clip"
                  >
                    ✕
                  </button>
                )}
              </li>
            ))}
          </ul>
        )}

        {/* Error message */}
        {errorMsg && (
          <div className="mt-4 bg-red-900/40 border border-red-700 rounded-lg px-4 py-3 text-sm text-red-300">
            {errorMsg}
            {pageState === "error" && (
              <button
                onClick={() => { setPageState("upload"); setErrorMsg(null); }}
                className="ml-3 underline text-red-400 hover:text-red-200"
              >
                Try again
              </button>
            )}
          </div>
        )}

        {/* Upload progress */}
        {pageState === "uploading" && (
          <div className="mt-4">
            <div className="flex justify-between text-xs text-zinc-400 mb-1">
              <span>Uploading clips...</span>
              <span>{totalProgress}%</span>
            </div>
            <div className="h-1.5 bg-zinc-800 rounded-full overflow-hidden">
              <div
                className="h-full bg-white transition-all duration-300"
                style={{ width: `${totalProgress}%` }}
              />
            </div>
          </div>
        )}

        {pageState === "enqueuing" && (
          <p className="mt-4 text-sm text-zinc-400 text-center">Starting AI processing...</p>
        )}

        {/* Submit */}
        <button
          onClick={handleSubmit}
          disabled={!canSubmit}
          className={`mt-6 w-full py-3 rounded-xl text-sm font-semibold transition-colors ${
            canSubmit
              ? "bg-white text-black hover:bg-zinc-200"
              : "bg-zinc-800 text-zinc-500 cursor-not-allowed"
          }`}
        >
          {clips.length < minClips
            ? `Add ${minClips - clips.length} more clip${minClips - clips.length !== 1 ? "s" : ""}`
            : "Create with Template"}
        </button>
      </div>
    </main>
  );
}
