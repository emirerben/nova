"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { enqueueJob, getPresignedUrl, importFromDrive, uploadFileToGcs } from "@/lib/api";
import { trackRecentJob } from "@/hooks/useArchitectureData";
import {
  preloadDriveScripts,
  requestDriveAccessToken,
  openDrivePicker,
  type DriveFileSelection,
} from "@/lib/google-drive-picker";

const GOOGLE_CLIENT_ID = process.env.NEXT_PUBLIC_GOOGLE_CLIENT_ID ?? "";
const GOOGLE_PICKER_API_KEY = process.env.NEXT_PUBLIC_GOOGLE_PICKER_API_KEY ?? "";

const PLATFORMS = [
  { id: "instagram", label: "Instagram Reels" },
  { id: "youtube", label: "YouTube Shorts" },
  { id: "tiktok", label: "TikTok (Phase 3)" },
];

const ALLOWED_MIME = ["video/mp4", "video/quicktime", "video/x-msvideo"];
const MAX_BYTES = 4 * 1024 * 1024 * 1024;

type UploadState =
  | "idle"
  | "drive_loading"
  | "drive_consent"
  | "drive_picking"
  | "drive_queuing"
  | "uploading"
  | "enqueuing"
  | "error";

interface QueuedImport {
  file: DriveFileSelection;
  jobId: string | null;
  status: "pending" | "queued" | "failed";
  error?: string;
}

// Cache the Drive access token for the session (valid ~1 hour)
let cachedDriveToken: { token: string; expiresAt: number } | null = null;

export default function UploadPage() {
  const router = useRouter();
  const fileInput = useRef<HTMLInputElement>(null);
  const [selectedPlatforms, setSelectedPlatforms] = useState<string[]>(["instagram", "youtube"]);
  const [state, setState] = useState<UploadState>("idle");
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [dragOver, setDragOver] = useState(false);
  const [queuedImports, setQueuedImports] = useState<QueuedImport[]>([]);
  const [compress, setCompress] = useState(false);

  const driveAvailable = !!GOOGLE_CLIENT_ID && !!GOOGLE_PICKER_API_KEY;

  useEffect(() => {
    if (driveAvailable) {
      preloadDriveScripts().catch(() => {});
    }
  }, [driveAvailable]);

  function togglePlatform(id: string) {
    setSelectedPlatforms((prev) =>
      prev.includes(id) ? prev.filter((p) => p !== id) : [...prev, id]
    );
  }

  const isBusy = state !== "idle" && state !== "error";

  // ── Local file upload ─────────────────────────────────────────────────────

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
      const { upload_url, job_id, gcs_path } = await getPresignedUrl({
        filename: file.name,
        file_size_bytes: file.size,
        duration_s: 0,
        aspect_ratio: "16:9",
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
    if (isBusy) return;
    const file = e.dataTransfer.files[0];
    if (file) handleFile(file);
  }

  // ── Google Drive import (multi-select) ────────────────────────────────────

  async function handleDriveImport() {
    if (selectedPlatforms.length === 0) {
      setErrorMsg("Select at least one platform.");
      return;
    }
    setErrorMsg(null);
    setState("drive_loading");

    try {
      await preloadDriveScripts();

      // Reuse cached token if still valid (with 5-min buffer)
      let accessToken: string;
      if (cachedDriveToken && Date.now() < cachedDriveToken.expiresAt - 5 * 60 * 1000) {
        accessToken = cachedDriveToken.token;
      } else {
        setState("drive_consent");
        try {
          accessToken = await requestDriveAccessToken(GOOGLE_CLIENT_ID);
        } catch (err) {
          if (err instanceof Error && err.message === "popup_closed") {
            setState("idle");
            return;
          }
          throw err;
        }
        cachedDriveToken = { token: accessToken, expiresAt: Date.now() + 55 * 60 * 1000 };
      }

      setState("drive_picking");
      const files = await openDrivePicker(accessToken, GOOGLE_PICKER_API_KEY, { multiSelect: true });
      if (files.length === 0) {
        setState("idle");
        return;
      }

      // Validate all files (compression shrinks ~10x, so allow larger inputs)
      const sizeLimit = compress ? MAX_BYTES * 10 : MAX_BYTES;
      const limitLabel = compress ? "40GB" : "4GB";
      const oversized = files.find((f) => f.sizeBytes > sizeLimit);
      if (oversized) {
        setErrorMsg(`"${oversized.fileName}" exceeds the ${limitLabel} limit.`);
        setState("error");
        return;
      }

      // Queue all imports
      setState("drive_queuing");
      const imports: QueuedImport[] = files.map((f) => ({
        file: f,
        jobId: null,
        status: "pending" as const,
      }));
      setQueuedImports(imports);

      // Fire all import requests
      // Log what Picker returned for debugging
      console.log("[Nova] Drive Picker returned:", files.map((f) => ({
        id: f.fileId, name: f.fileName, mime: f.mimeType, size: f.sizeBytes,
      })));

      const results = await Promise.allSettled(
        files.map((f) =>
          importFromDrive({
            drive_file_id: f.fileId,
            filename: f.fileName,
            file_size_bytes: f.sizeBytes,
            mime_type: f.mimeType,
            platforms: selectedPlatforms,
            google_access_token: accessToken,
            compress,
          })
        )
      );

      // Log results for debugging
      results.forEach((r, i) => {
        if (r.status === "rejected") {
          console.error(`[Nova] Import failed for ${files[i].fileName}:`, r.reason);
        }
      });

      const updatedImports: QueuedImport[] = files.map((f, i) => {
        const result = results[i];
        if (result.status === "fulfilled") {
          trackRecentJob(result.value.job_id, "default");
          return { file: f, jobId: result.value.job_id, status: "queued" as const };
        }
        return {
          file: f,
          jobId: null,
          status: "failed" as const,
          error: result.reason instanceof Error ? result.reason.message : "Import failed",
        };
      });
      setQueuedImports(updatedImports);

      // Navigate to first successful job after a brief pause to show results
      const firstSuccess = updatedImports.find((i) => i.status === "queued");
      if (firstSuccess?.jobId) {
        setTimeout(() => router.push(`/jobs/${firstSuccess.jobId}`), 1500);
      } else {
        setState("error");
        setErrorMsg("All imports failed. Check your Google Drive permissions and try again.");
      }
    } catch (err) {
      setState("error");
      if (err instanceof Error && err.message.includes("denied")) {
        setErrorMsg("Google Drive access was denied. Please try again and grant permission.");
      } else {
        setErrorMsg(err instanceof Error ? err.message : "Failed to open Google Drive.");
      }
    }
  }

  function resetToIdle() {
    setErrorMsg(null);
    setState("idle");
    setQueuedImports([]);
  }

  function formatBytes(bytes: number): string {
    if (bytes >= 1024 ** 3) return `${(bytes / 1024 ** 3).toFixed(1)} GB`;
    if (bytes >= 1024 ** 2) return `${(bytes / 1024 ** 2).toFixed(0)} MB`;
    return `${(bytes / 1024).toFixed(0)} KB`;
  }

  // ── Render ────────────────────────────────────────────────────────────────

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
            disabled={isBusy}
            className={`px-4 py-2 rounded-full text-sm border transition-colors ${
              selectedPlatforms.includes(p.id)
                ? "bg-white text-black border-white"
                : "border-zinc-600 text-zinc-400 hover:border-zinc-400"
            } ${isBusy ? "opacity-50 cursor-not-allowed" : ""}`}
          >
            {p.label}
          </button>
        ))}
      </div>

      {/* Quick test toggle */}
      <label className="flex items-center gap-2 mb-6 cursor-pointer select-none">
        <input
          type="checkbox"
          checked={compress}
          onChange={(e) => setCompress(e.target.checked)}
          disabled={isBusy}
          className="w-4 h-4 rounded border-zinc-600 bg-zinc-800 text-white accent-white"
        />
        <span className="text-zinc-500 text-xs">Compress to 720p</span>
        <span className="text-zinc-600 text-xs">(~10x faster for testing)</span>
      </label>

      {/* Upload options: two equal cards side by side */}
      {state === "idle" && (
        <div className="w-full max-w-2xl grid grid-cols-1 sm:grid-cols-2 gap-4">
          {/* Local upload card */}
          <div
            onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
            onDragLeave={() => setDragOver(false)}
            onDrop={onDrop}
            onClick={() => fileInput.current?.click()}
            className={`group h-56 border border-zinc-800 rounded-2xl flex flex-col items-center justify-center cursor-pointer transition-all hover:border-zinc-600 hover:bg-zinc-900/50 ${
              dragOver ? "border-white bg-zinc-900" : ""
            }`}
          >
            <div className="w-12 h-12 rounded-full bg-zinc-800 flex items-center justify-center mb-4 group-hover:bg-zinc-700 transition-colors">
              <svg className="w-6 h-6 text-zinc-400" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M3 16.5v2.25A2.25 2.25 0 005.25 21h13.5A2.25 2.25 0 0021 18.75V16.5m-13.5-9L12 3m0 0l4.5 4.5M12 3v13.5" />
              </svg>
            </div>
            <p className="text-zinc-200 font-medium text-sm">Upload from computer</p>
            <p className="text-zinc-500 text-xs mt-1">Drop or click to browse</p>
            <p className="text-zinc-600 text-xs mt-1">MP4, MOV, AVI · max 4GB</p>
          </div>

          {/* Google Drive card */}
          {driveAvailable && (
            <button
              onClick={handleDriveImport}
              className="group h-56 border border-zinc-800 rounded-2xl flex flex-col items-center justify-center cursor-pointer transition-all hover:border-zinc-600 hover:bg-zinc-900/50"
            >
              <div className="w-12 h-12 rounded-full bg-zinc-800 flex items-center justify-center mb-4 group-hover:bg-zinc-700 transition-colors">
                <DriveIcon size={24} />
              </div>
              <p className="text-zinc-200 font-medium text-sm">Import from Google Drive</p>
              <p className="text-zinc-500 text-xs mt-1">Select one or more videos</p>
              <p className="text-zinc-600 text-xs mt-1">No download needed</p>
            </button>
          )}

          {/* Fallback: full-width drop zone when Drive not available */}
          {!driveAvailable && (
            <div className="h-56 border border-zinc-800 rounded-2xl flex flex-col items-center justify-center opacity-30">
              <DriveIcon size={24} />
              <p className="text-zinc-500 text-xs mt-3">Google Drive not configured</p>
            </div>
          )}
        </div>
      )}

      {/* Drive loading states */}
      {(state === "drive_loading" || state === "drive_consent" || state === "drive_picking") && (
        <div className="w-full max-w-lg h-56 border border-zinc-800 rounded-2xl flex flex-col items-center justify-center">
          <div className="w-12 h-12 rounded-full bg-zinc-800 flex items-center justify-center mb-4 animate-pulse">
            <DriveIcon size={24} />
          </div>
          <p className="text-zinc-300 text-sm">
            {state === "drive_loading" && "Connecting to Google Drive..."}
            {state === "drive_consent" && "Waiting for Google sign-in..."}
            {state === "drive_picking" && "Select videos from Google Drive..."}
          </p>
        </div>
      )}

      {/* Drive queuing: show per-file status */}
      {state === "drive_queuing" && queuedImports.length > 0 && (
        <div className="w-full max-w-lg">
          <div className="border border-zinc-800 rounded-2xl p-6">
            <div className="flex items-center gap-3 mb-4">
              <DriveIcon size={20} />
              <p className="text-zinc-200 font-medium text-sm">
                Importing {queuedImports.length} video{queuedImports.length > 1 ? "s" : ""} from Google Drive
              </p>
            </div>
            <div className="space-y-2">
              {queuedImports.map((item, i) => (
                <div key={i} className="flex items-center gap-3 text-sm">
                  <span className="shrink-0 w-5">
                    {item.status === "pending" && <span className="block w-2 h-2 rounded-full bg-zinc-500 animate-pulse" />}
                    {item.status === "queued" && <span className="text-green-400 text-xs">✓</span>}
                    {item.status === "failed" && <span className="text-red-400 text-xs">✕</span>}
                  </span>
                  <span className="text-zinc-300 truncate flex-1">{item.file.fileName}</span>
                  <span className="text-zinc-600 text-xs shrink-0">{formatBytes(item.file.sizeBytes)}</span>
                </div>
              ))}
            </div>
            {queuedImports.some((i) => i.status === "queued") && (
              <p className="text-zinc-500 text-xs mt-4">Redirecting to your first job...</p>
            )}
          </div>
        </div>
      )}

      {/* Upload/enqueue progress */}
      {(state === "uploading" || state === "enqueuing") && (
        <div className="w-full max-w-lg h-56 border border-zinc-800 rounded-2xl flex flex-col items-center justify-center">
          <p className="text-zinc-300 text-sm animate-pulse">
            {state === "uploading" ? "Uploading to secure storage..." : "Starting pipeline..."}
          </p>
        </div>
      )}

      {/* Error state */}
      {state === "error" && (
        <div className="w-full max-w-lg border border-red-900/50 rounded-2xl p-6 text-center">
          <p className="text-red-400 text-sm mb-4">{errorMsg}</p>
          <button
            onClick={resetToIdle}
            className="px-4 py-2 border border-zinc-700 rounded-full text-sm text-zinc-300 hover:border-zinc-500 transition-colors"
          >
            Try again
          </button>
        </div>
      )}

      <input
        ref={fileInput}
        type="file"
        accept="video/mp4,video/quicktime,video/x-msvideo"
        className="hidden"
        disabled={isBusy}
        onChange={(e) => {
          const file = e.target.files?.[0];
          if (file) handleFile(file);
        }}
      />

      {errorMsg && state !== "error" && (
        <p className="text-red-400 mt-4 text-sm">{errorMsg}</p>
      )}

      {/* Template mode link */}
      <p className="mt-8 text-zinc-600 text-xs">
        Or try{" "}
        <Link href="/template" className="text-zinc-400 underline underline-offset-2 hover:text-zinc-300 transition-colors">
          template mode
        </Link>
      </p>
    </main>
  );
}

function DriveIcon({ size = 16 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 87.3 78" xmlns="http://www.w3.org/2000/svg">
      <path d="m6.6 66.85 3.85 6.65c.8 1.4 1.95 2.5 3.3 3.3l13.75-23.8h-27.5c0 1.55.4 3.1 1.2 4.5z" fill="#0066da"/>
      <path d="m43.65 25-13.75-23.8c-1.35.8-2.5 1.9-3.3 3.3l-20.4 35.3c-.8 1.4-1.2 2.95-1.2 4.5h27.5z" fill="#00ac47"/>
      <path d="m73.55 76.8c1.35-.8 2.5-1.9 3.3-3.3l1.6-2.75 7.65-13.25c.8-1.4 1.2-2.95 1.2-4.5h-27.5l5.85 13.15z" fill="#ea4335"/>
      <path d="m43.65 25 13.75-23.8c-1.35-.8-2.9-1.2-4.5-1.2h-18.5c-1.6 0-3.15.45-4.5 1.2z" fill="#00832d"/>
      <path d="m59.8 53h-32.3l-13.75 23.8c1.35.8 2.9 1.2 4.5 1.2h50.8c1.6 0 3.15-.45 4.5-1.2z" fill="#2684fc"/>
      <path d="m73.4 26.5-10.1-17.5c-.8-1.4-1.95-2.5-3.3-3.3l-13.75 23.8 16.15 23.5h27.45c0-1.55-.4-3.1-1.2-4.5z" fill="#ffba00"/>
    </svg>
  );
}
