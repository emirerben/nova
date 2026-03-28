"use client";

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
  | "drive_confirming"
  | "importing"
  | "uploading"
  | "enqueuing"
  | "error";

export default function UploadPage() {
  const router = useRouter();
  const fileInput = useRef<HTMLInputElement>(null);
  const driveTokenRef = useRef<string | null>(null);
  const [selectedPlatforms, setSelectedPlatforms] = useState<string[]>(["instagram", "youtube"]);
  const [state, setState] = useState<UploadState>("idle");
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [dragOver, setDragOver] = useState(false);
  const [driveFile, setDriveFile] = useState<DriveFileSelection | null>(null);

  const driveAvailable = !!GOOGLE_CLIENT_ID && !!GOOGLE_PICKER_API_KEY;

  // Preload GIS + Picker scripts on mount (prevents popup blockers)
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

  const isDriveBusy = state.startsWith("drive_") || state === "importing";
  const isLocalBusy = state === "uploading" || state === "enqueuing";
  const isBusy = isDriveBusy || isLocalBusy;

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

  // ── Google Drive import ───────────────────────────────────────────────────

  async function handleDriveImport() {
    if (selectedPlatforms.length === 0) {
      setErrorMsg("Select at least one platform.");
      return;
    }

    setErrorMsg(null);
    setState("drive_loading");

    try {
      await preloadDriveScripts();

      setState("drive_consent");
      let accessToken: string;
      try {
        accessToken = await requestDriveAccessToken(GOOGLE_CLIENT_ID);
      } catch (err) {
        if (err instanceof Error && err.message === "popup_closed") {
          setState("idle");
          return;
        }
        throw err;
      }

      setState("drive_picking");
      const files = await openDrivePicker(accessToken, GOOGLE_PICKER_API_KEY);
      if (files.length === 0) {
        setState("idle");
        return;
      }

      const file = files[0];
      if (file.sizeBytes > MAX_BYTES) {
        setErrorMsg(`"${file.fileName}" exceeds the 4GB limit. Try a shorter or lower-resolution video.`);
        setState("error");
        return;
      }

      setDriveFile(file);
      driveTokenRef.current = accessToken;
      setState("drive_confirming");
    } catch (err) {
      setState("error");
      if (err instanceof Error && err.message.includes("denied")) {
        setErrorMsg("Google Drive access was denied. Please try again and grant permission.");
      } else {
        setErrorMsg(err instanceof Error ? err.message : "Failed to open Google Drive.");
      }
    }
  }

  async function confirmDriveImport() {
    if (!driveFile || !driveTokenRef.current) {
      setErrorMsg("Authentication expired. Please try again.");
      setState("error");
      return;
    }

    try {
      setState("importing");
      const { job_id } = await importFromDrive({
        drive_file_id: driveFile.fileId,
        filename: driveFile.fileName,
        file_size_bytes: driveFile.sizeBytes,
        mime_type: driveFile.mimeType,
        platforms: selectedPlatforms,
        google_access_token: driveTokenRef.current,
      });

      driveTokenRef.current = null;
      trackRecentJob(job_id, "default");
      router.push(`/jobs/${job_id}`);
    } catch (err) {
      setState("error");
      setErrorMsg(err instanceof Error ? err.message : "Import from Google Drive failed.");
    }
  }

  function cancelDriveConfirm() {
    setDriveFile(null);
    driveTokenRef.current = null;
    setState("idle");
  }

  function resetToIdle() {
    setErrorMsg(null);
    setState("idle");
    setDriveFile(null);
    driveTokenRef.current = null;
  }

  // ── Helpers ───────────────────────────────────────────────────────────────

  function formatBytes(bytes: number): string {
    if (bytes >= 1024 ** 3) return `${(bytes / 1024 ** 3).toFixed(1)} GB`;
    if (bytes >= 1024 ** 2) return `${(bytes / 1024 ** 2).toFixed(0)} MB`;
    return `${(bytes / 1024).toFixed(0)} KB`;
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

      {/* Drop zone */}
      <div
        onDragOver={(e) => { e.preventDefault(); if (!isBusy) setDragOver(true); }}
        onDragLeave={() => setDragOver(false)}
        onDrop={onDrop}
        onClick={() => { if (!isBusy && state !== "error" && state !== "drive_confirming") fileInput.current?.click(); }}
        className={`w-full max-w-lg border-2 border-dashed rounded-2xl flex flex-col items-center justify-center transition-colors ${
          isBusy
            ? "border-zinc-800 cursor-default"
            : state === "error"
              ? "border-zinc-800 cursor-default"
              : dragOver
                ? "border-white bg-zinc-900 cursor-pointer"
                : "border-zinc-700 hover:border-zinc-500 cursor-pointer"
        } ${state === "drive_confirming" ? "py-8" : "h-72"}`}
      >
        {state === "idle" && (
          <>
            <span className="text-5xl mb-3">+</span>
            <p className="text-zinc-300 font-medium">Drop a video or click to browse</p>
            <p className="text-zinc-500 text-sm mt-1">MP4, MOV, AVI · max 4GB · max 30 min</p>

            {/* Drive import button inside drop zone */}
            {driveAvailable && (
              <div className="mt-5 flex flex-col items-center">
                <div className="flex items-center gap-3 mb-3 w-40">
                  <div className="flex-1 h-px bg-zinc-700" />
                  <span className="text-zinc-500 text-xs">or</span>
                  <div className="flex-1 h-px bg-zinc-700" />
                </div>
                <button
                  onClick={(e) => { e.stopPropagation(); handleDriveImport(); }}
                  className="flex items-center gap-2 px-4 py-2 border border-zinc-600 rounded-full text-sm text-zinc-300 hover:border-zinc-400 hover:text-white transition-colors"
                >
                  <DriveIcon />
                  Import from Google Drive
                </button>
              </div>
            )}
          </>
        )}

        {state === "uploading" && (
          <p className="text-zinc-300">Uploading to secure storage...</p>
        )}

        {state === "enqueuing" && (
          <p className="text-zinc-300">Starting pipeline...</p>
        )}

        {state === "drive_loading" && (
          <p className="text-zinc-300 animate-pulse">Connecting to Google Drive...</p>
        )}

        {state === "drive_consent" && (
          <p className="text-zinc-300 animate-pulse">Waiting for Google sign-in...</p>
        )}

        {state === "drive_picking" && (
          <p className="text-zinc-300">Select a video from Google Drive...</p>
        )}

        {state === "drive_confirming" && driveFile && (
          <div className="text-center px-6">
            <p className="text-zinc-400 text-sm mb-2">Import this file?</p>
            <p className="text-white text-lg font-semibold mb-1">{driveFile.fileName}</p>
            <p className="text-zinc-500 text-sm mb-5">{formatBytes(driveFile.sizeBytes)}</p>
            <div className="flex gap-3 justify-center">
              <button
                onClick={(e) => { e.stopPropagation(); confirmDriveImport(); }}
                className="px-5 py-2 bg-white text-black rounded-full text-sm font-semibold hover:bg-zinc-200 transition-colors"
              >
                Import
              </button>
              <button
                onClick={(e) => { e.stopPropagation(); cancelDriveConfirm(); }}
                className="px-5 py-2 border border-zinc-600 text-zinc-300 rounded-full text-sm hover:border-zinc-400 transition-colors"
              >
                Cancel
              </button>
            </div>
          </div>
        )}

        {state === "importing" && (
          <div className="text-center">
            <p className="text-zinc-300 animate-pulse">Importing from Google Drive...</p>
            {driveFile && (
              <p className="text-zinc-500 text-sm mt-1">
                {driveFile.fileName} ({formatBytes(driveFile.sizeBytes)})
              </p>
            )}
          </div>
        )}

        {state === "error" && (
          <div className="text-center px-6">
            <p className="text-red-400 mb-3">{errorMsg}</p>
            <button
              onClick={(e) => { e.stopPropagation(); resetToIdle(); }}
              className="text-xs text-zinc-400 underline hover:text-zinc-200"
            >
              Try again
            </button>
          </div>
        )}
      </div>

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
    </main>
  );
}

function DriveIcon() {
  return (
    <svg className="w-4 h-4" viewBox="0 0 87.3 78" xmlns="http://www.w3.org/2000/svg">
      <path d="m6.6 66.85 3.85 6.65c.8 1.4 1.95 2.5 3.3 3.3l13.75-23.8h-27.5c0 1.55.4 3.1 1.2 4.5z" fill="#0066da"/>
      <path d="m43.65 25-13.75-23.8c-1.35.8-2.5 1.9-3.3 3.3l-20.4 35.3c-.8 1.4-1.2 2.95-1.2 4.5h27.5z" fill="#00ac47"/>
      <path d="m73.55 76.8c1.35-.8 2.5-1.9 3.3-3.3l1.6-2.75 7.65-13.25c.8-1.4 1.2-2.95 1.2-4.5h-27.5l5.85 13.15z" fill="#ea4335"/>
      <path d="m43.65 25 13.75-23.8c-1.35-.8-2.9-1.2-4.5-1.2h-18.5c-1.6 0-3.15.45-4.5 1.2z" fill="#00832d"/>
      <path d="m59.8 53h-32.3l-13.75 23.8c1.35.8 2.9 1.2 4.5 1.2h50.8c1.6 0 3.15-.45 4.5-1.2z" fill="#2684fc"/>
      <path d="m73.4 26.5-10.1-17.5c-.8-1.4-1.95-2.5-3.3-3.3l-13.75 23.8 16.15 23.5h27.45c0-1.55-.4-3.1-1.2-4.5z" fill="#ffba00"/>
    </svg>
  );
}
