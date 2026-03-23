"use client";

import { useRouter } from "next/navigation";
import { useRef, useState } from "react";
import { createTemplateJob, getPresignedUrl, uploadFileToGcs } from "@/lib/api";

const PLATFORMS = [
  { id: "instagram", label: "Instagram Reels" },
  { id: "youtube", label: "YouTube Shorts" },
  { id: "tiktok", label: "TikTok" },
];

const ALLOWED_MIME = ["video/mp4", "video/quicktime", "video/x-msvideo"];
const MAX_BYTES = 4 * 1024 * 1024 * 1024;
const MIN_CLIPS = 5;
const MAX_CLIPS = 20;

// In v1 the template ID is configured server-side; we read it from env or use a well-known default.
// Operators set NEXT_PUBLIC_DEFAULT_TEMPLATE_ID in .env.local
const DEFAULT_TEMPLATE_ID = process.env.NEXT_PUBLIC_DEFAULT_TEMPLATE_ID ?? "default";

type UploadState = "idle" | "uploading" | "enqueuing" | "error";

interface ClipFile {
  file: File;
  id: string; // local key for React
}

export default function TemplatePage() {
  const router = useRouter();
  const fileInput = useRef<HTMLInputElement>(null);
  const [clips, setClips] = useState<ClipFile[]>([]);
  const [selectedPlatforms, setSelectedPlatforms] = useState<string[]>(["instagram", "youtube"]);
  const [state, setState] = useState<UploadState>("idle");
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [uploadProgress, setUploadProgress] = useState(0); // 0-100
  const [dragOver, setDragOver] = useState(false);

  function togglePlatform(id: string) {
    setSelectedPlatforms((prev) =>
      prev.includes(id) ? prev.filter((p) => p !== id) : [...prev, id]
    );
  }

  function addFiles(files: FileList | File[]) {
    const arr = Array.from(files);
    const valid = arr.filter((f) => {
      if (!ALLOWED_MIME.includes(f.type)) return false;
      if (f.size > MAX_BYTES) return false;
      return true;
    });
    setClips((prev) => {
      const combined = [...prev, ...valid.map((f) => ({ file: f, id: crypto.randomUUID() }))];
      return combined.slice(0, MAX_CLIPS);
    });
  }

  function removeClip(id: string) {
    setClips((prev) => prev.filter((c) => c.id !== id));
  }

  async function handleSubmit() {
    setErrorMsg(null);

    if (clips.length < MIN_CLIPS) {
      setErrorMsg(`Upload at least ${MIN_CLIPS} clips to use template mode.`);
      return;
    }
    if (selectedPlatforms.length === 0) {
      setErrorMsg("Select at least one platform.");
      return;
    }

    try {
      setState("uploading");

      // Upload each clip via presigned URL
      const gcsPaths: string[] = [];
      for (let i = 0; i < clips.length; i++) {
        const { file } = clips[i];
        const { upload_url, gcs_path } = await getPresignedUrl({
          filename: `clip_${i}.mp4`,
          file_size_bytes: file.size,
          duration_s: 0,
          aspect_ratio: "16:9",
          platforms: selectedPlatforms,
          content_type: file.type,
        });
        await uploadFileToGcs(upload_url, file);
        gcsPaths.push(gcs_path);
        setUploadProgress(Math.round(((i + 1) / clips.length) * 100));
      }

      setState("enqueuing");
      const { job_id } = await createTemplateJob({
        template_id: DEFAULT_TEMPLATE_ID,
        clip_gcs_paths: gcsPaths,
        selected_platforms: selectedPlatforms,
      });

      router.push(`/template-jobs/${job_id}`);
    } catch (err) {
      setState("error");
      setErrorMsg(err instanceof Error ? err.message : "Something went wrong.");
    }
  }

  const canSubmit = clips.length >= MIN_CLIPS && state === "idle";

  return (
    <main className="min-h-screen bg-black text-white flex flex-col items-center justify-center px-4 py-16">
      <div className="w-full max-w-xl">
        <h1 className="text-3xl font-bold mb-2 text-center">Template Mode</h1>
        <p className="text-zinc-400 text-sm text-center mb-8">
          Upload {MIN_CLIPS}–{MAX_CLIPS} raw clips. AI will assemble them to match a curated TikTok template.
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
              : `${clips.length} clip${clips.length !== 1 ? "s" : ""} selected (${MIN_CLIPS}–${MAX_CLIPS} required)`}
          </p>
          <p className="text-zinc-600 text-xs mt-1">MP4, MOV, AVI · Up to 4GB each</p>
          <input
            ref={fileInput}
            type="file"
            accept="video/mp4,video/quicktime,video/x-msvideo"
            multiple
            className="hidden"
            onChange={(e) => e.target.files && addFiles(e.target.files)}
          />
        </div>

        {/* Clip list */}
        {clips.length > 0 && (
          <ul className="mt-4 space-y-2">
            {clips.map((c) => (
              <li
                key={c.id}
                className="flex items-center justify-between bg-zinc-900 rounded-lg px-4 py-2 text-sm"
              >
                <span className="truncate text-zinc-300 flex-1 mr-4">{c.file.name}</span>
                <span className="text-zinc-500 mr-4 shrink-0">
                  {(c.file.size / 1024 / 1024).toFixed(1)} MB
                </span>
                <button
                  onClick={() => removeClip(c.id)}
                  className="text-zinc-500 hover:text-red-400 transition-colors shrink-0"
                  aria-label="Remove clip"
                >
                  ✕
                </button>
              </li>
            ))}
          </ul>
        )}

        {/* Platform selector */}
        <div className="mt-6">
          <p className="text-sm text-zinc-400 mb-2">Platforms</p>
          <div className="flex gap-3">
            {PLATFORMS.map((p) => (
              <button
                key={p.id}
                onClick={() => togglePlatform(p.id)}
                className={`px-4 py-2 rounded-lg text-sm font-medium transition-colors ${
                  selectedPlatforms.includes(p.id)
                    ? "bg-white text-black"
                    : "bg-zinc-800 text-zinc-400 hover:bg-zinc-700"
                }`}
              >
                {p.label}
              </button>
            ))}
          </div>
        </div>

        {/* Error message */}
        {errorMsg && (
          <div className="mt-4 bg-red-900/40 border border-red-700 rounded-lg px-4 py-3 text-sm text-red-300">
            {errorMsg}
            {state === "error" && (
              <button
                onClick={() => { setState("idle"); setErrorMsg(null); }}
                className="ml-3 underline text-red-400 hover:text-red-200"
              >
                Try again
              </button>
            )}
          </div>
        )}

        {/* Progress bar */}
        {state === "uploading" && (
          <div className="mt-4">
            <div className="flex justify-between text-xs text-zinc-400 mb-1">
              <span>Uploading clips...</span>
              <span>{uploadProgress}%</span>
            </div>
            <div className="h-1.5 bg-zinc-800 rounded-full overflow-hidden">
              <div
                className="h-full bg-white transition-all duration-300"
                style={{ width: `${uploadProgress}%` }}
              />
            </div>
          </div>
        )}

        {state === "enqueuing" && (
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
          {clips.length < MIN_CLIPS
            ? `Add ${MIN_CLIPS - clips.length} more clip${MIN_CLIPS - clips.length !== 1 ? "s" : ""}`
            : "Create with Template"}
        </button>

        <p className="mt-4 text-center text-xs text-zinc-600">
          <a href="/" className="underline hover:text-zinc-400">← Back to standard upload</a>
        </p>
      </div>
    </main>
  );
}
