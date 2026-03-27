"use client";

import { useRouter } from "next/navigation";
import { useRef, useState } from "react";
import { enqueueJob, getPresignedUrl, uploadFileToGcs } from "@/lib/api";
import { trackRecentJob } from "@/hooks/useArchitectureData";

const PLATFORMS = [
  { id: "instagram", label: "Instagram Reels" },
  { id: "youtube", label: "YouTube Shorts" },
  { id: "tiktok", label: "TikTok (Phase 3)" },
];

const ALLOWED_MIME = ["video/mp4", "video/quicktime", "video/x-msvideo"];
const MAX_BYTES = 4 * 1024 * 1024 * 1024;

type UploadState = "idle" | "uploading" | "enqueuing" | "error";

export default function UploadPage() {
  const router = useRouter();
  const fileInput = useRef<HTMLInputElement>(null);
  const [selectedPlatforms, setSelectedPlatforms] = useState<string[]>(["instagram", "youtube"]);
  const [state, setState] = useState<UploadState>("idle");
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [uploadProgress, setUploadProgress] = useState(0);
  const [dragOver, setDragOver] = useState(false);

  function togglePlatform(id: string) {
    setSelectedPlatforms((prev) =>
      prev.includes(id) ? prev.filter((p) => p !== id) : [...prev, id]
    );
  }

  async function handleFile(file: File) {
    setErrorMsg(null);

    if (!ALLOWED_MIME.includes(file.type)) {
      setErrorMsg("Only MP4, MOV, and AVI files are supported.");
      return;
    }
    if (file.size > MAX_BYTES) {
      setErrorMsg("File exceeds the 4GB limit.");
      return;
    }
    if (selectedPlatforms.length === 0) {
      setErrorMsg("Select at least one platform.");
      return;
    }

    try {
      setState("uploading");
      // We don't have video duration client-side without loading the file — use 0 as placeholder;
      // server validates duration after probe
      const { upload_url, job_id, gcs_path } = await getPresignedUrl({
        filename: file.name,
        file_size_bytes: file.size,
        duration_s: 0,
        aspect_ratio: "16:9", // TODO: detect from video metadata
        platforms: selectedPlatforms,
        content_type: file.type,
      });

      await uploadFileToGcs(upload_url, file);
      setState("enqueuing");
      await enqueueJob(job_id, gcs_path, selectedPlatforms);
      trackRecentJob(job_id, "default");

      router.push(`/jobs/${job_id}`);
    } catch (err) {
      setState("error");
      setErrorMsg(err instanceof Error ? err.message : "Upload failed — try again.");
    }
  }

  function onDrop(e: React.DragEvent) {
    e.preventDefault();
    setDragOver(false);
    const file = e.dataTransfer.files[0];
    if (file) handleFile(file);
  }

  return (
    <main className="min-h-screen bg-black text-white flex flex-col items-center justify-center p-6">
      <h1 className="text-4xl font-bold mb-2">Nova</h1>
      <p className="text-zinc-400 mb-10 text-center max-w-sm">
        Upload raw footage. Get 3 clips ready to post — captions, copy, and all.
      </p>

      {/* Platform selector */}
      <div className="flex gap-3 mb-8">
        {PLATFORMS.map((p) => (
          <button
            key={p.id}
            onClick={() => togglePlatform(p.id)}
            className={`px-4 py-2 rounded-full text-sm border transition-colors ${
              selectedPlatforms.includes(p.id)
                ? "bg-white text-black border-white"
                : "border-zinc-600 text-zinc-400 hover:border-zinc-400"
            }`}
          >
            {p.label}
          </button>
        ))}
      </div>

      {/* Drop zone */}
      <div
        onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
        onDragLeave={() => setDragOver(false)}
        onDrop={onDrop}
        onClick={() => fileInput.current?.click()}
        className={`w-full max-w-lg h-64 border-2 border-dashed rounded-2xl flex flex-col items-center justify-center cursor-pointer transition-colors ${
          dragOver ? "border-white bg-zinc-900" : "border-zinc-700 hover:border-zinc-500"
        }`}
      >
        {state === "idle" && (
          <>
            <span className="text-5xl mb-4">+</span>
            <p className="text-zinc-300 font-medium">Drop a video or click to browse</p>
            <p className="text-zinc-500 text-sm mt-1">MP4, MOV, AVI · max 4GB · max 30 min</p>
          </>
        )}
        {state === "uploading" && (
          <p className="text-zinc-300">Uploading to secure storage...</p>
        )}
        {state === "enqueuing" && (
          <p className="text-zinc-300">Starting pipeline...</p>
        )}
        {state === "error" && (
          <p className="text-red-400">{errorMsg}</p>
        )}
      </div>

      <input
        ref={fileInput}
        type="file"
        accept="video/mp4,video/quicktime,video/x-msvideo"
        className="hidden"
        onChange={(e) => {
          const file = e.target.files?.[0];
          if (file) handleFile(file);
        }}
      />

      {errorMsg && state !== "error" && (
        <p className="text-red-400 mt-4 text-sm">{errorMsg}</p>
      )}
    </main>
  );
}
