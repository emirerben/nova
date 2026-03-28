"use client";

import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import {
  type BatchPresignedFile,
  type DriveImportBatchStatusResponse,
  type TemplateListItem,
  createTemplateJob,
  getDriveImportBatchStatus,
  getBatchPresignedUrls,
  importBatchFromDrive,
  listTemplates,
  uploadFileToGcs,
} from "@/lib/api";
import { trackRecentJob } from "@/hooks/useArchitectureData";
import {
  preloadDriveScripts,
  requestDriveAccessToken,
  openDrivePicker,
} from "@/lib/google-drive-picker";

const GOOGLE_CLIENT_ID = process.env.NEXT_PUBLIC_GOOGLE_CLIENT_ID ?? "";
const GOOGLE_PICKER_API_KEY = process.env.NEXT_PUBLIC_GOOGLE_PICKER_API_KEY ?? "";

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

type PageState = "gallery" | "upload" | "uploading" | "enqueuing" | "drive_importing" | "error";

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

  // Drive import state
  const [driveImportStatus, setDriveImportStatus] = useState<DriveImportBatchStatusResponse | null>(null);
  const driveAvailable = !!GOOGLE_CLIENT_ID && !!GOOGLE_PICKER_API_KEY;
  const drivePollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // Clean up Drive polling on unmount
  useEffect(() => {
    return () => {
      if (drivePollRef.current) clearTimeout(drivePollRef.current as unknown as ReturnType<typeof setTimeout>);
    };
  }, []);

  // Preload Drive scripts
  useEffect(() => {
    if (driveAvailable) preloadDriveScripts().catch(() => {});
  }, [driveAvailable]);

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

      trackRecentJob(job_id, "template");
      router.push(`/template-jobs/${job_id}`);
    } catch (err) {
      setPageState("error");
      setErrorMsg(err instanceof Error ? err.message : "Something went wrong.");
    }
  }

  // ── Drive import for template clips ──────────────────────────────────────

  async function handleDriveImportClips() {
    if (!selectedTemplate) return;
    setErrorMsg(null);

    try {
      await preloadDriveScripts();

      let accessToken: string;
      try {
        accessToken = await requestDriveAccessToken(GOOGLE_CLIENT_ID);
      } catch (err) {
        if (err instanceof Error && err.message === "popup_closed") return;
        throw err;
      }

      const files = await openDrivePicker(accessToken, GOOGLE_PICKER_API_KEY, { multiSelect: true });
      if (files.length === 0) return;

      // Validate count
      const minClips = selectedTemplate.slot_count || 5;
      if (files.length < minClips) {
        setErrorMsg(`This template needs at least ${minClips} clips. You selected ${files.length}.`);
        return;
      }
      if (files.length > MAX_CLIPS) {
        setErrorMsg(`Maximum ${MAX_CLIPS} clips per batch.`);
        return;
      }

      // Check individual file sizes
      for (const f of files) {
        if (f.sizeBytes > MAX_BYTES) {
          setErrorMsg(`"${f.fileName}" exceeds the 4GB limit.`);
          return;
        }
      }

      setPageState("drive_importing");

      const { batch_id, gcs_paths } = await importBatchFromDrive({
        files: files.map((f) => ({
          drive_file_id: f.fileId,
          filename: f.fileName,
          file_size_bytes: f.sizeBytes,
          mime_type: f.mimeType,
        })),
        google_access_token: accessToken,
      });

      // Poll with setTimeout recursion (prevents overlapping callbacks)
      async function pollOnce() {
        try {
          const status = await getDriveImportBatchStatus(batch_id);
          setDriveImportStatus(status);

          if (status.status === "complete") {
            setPageState("enqueuing");
            const { job_id } = await createTemplateJob({
              template_id: selectedTemplate.id,
              clip_gcs_paths: status.gcs_paths,
              selected_platforms: ["tiktok", "instagram", "youtube"],
            });
            trackRecentJob(job_id, "template");
            router.push(`/template-jobs/${job_id}`);
          } else if (status.status === "partial_failure") {
            if (status.completed >= minClips) {
              setPageState("enqueuing");
              const { job_id } = await createTemplateJob({
                template_id: selectedTemplate.id,
                clip_gcs_paths: status.gcs_paths,
                selected_platforms: ["tiktok", "instagram", "youtube"],
              });
              trackRecentJob(job_id, "template");
              router.push(`/template-jobs/${job_id}`);
            } else {
              setPageState("error");
              setErrorMsg(
                `Only ${status.completed}/${status.total} clips imported (need at least ${minClips}). ` +
                `Failed: ${status.errors.join("; ")}`
              );
            }
          } else if (status.status === "failed") {
            setPageState("error");
            setErrorMsg(`All imports failed: ${status.errors.join("; ")}`);
          } else {
            // Still importing, schedule next poll (setTimeout prevents overlap)
            drivePollRef.current = setTimeout(pollOnce, 3000) as unknown as ReturnType<typeof setInterval>;
          }
        } catch {
          setPageState("error");
          setErrorMsg("Failed to check import status.");
        }
      }
      drivePollRef.current = setTimeout(pollOnce, 1000) as unknown as ReturnType<typeof setInterval>;
    } catch (err) {
      setPageState("error");
      if (err instanceof Error && err.message.includes("denied")) {
        setErrorMsg("Google Drive access was denied. Please try again and grant permission.");
      } else {
        setErrorMsg(err instanceof Error ? err.message : "Drive import failed.");
      }
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

          {/* Drive import button */}
          {driveAvailable && pageState === "upload" && (
            <div className="mt-4 flex flex-col items-center">
              <div className="flex items-center gap-3 mb-3 w-32">
                <div className="flex-1 h-px bg-zinc-700" />
                <span className="text-zinc-500 text-xs">or</span>
                <div className="flex-1 h-px bg-zinc-700" />
              </div>
              <button
                onClick={(e) => { e.stopPropagation(); handleDriveImportClips(); }}
                className="flex items-center gap-2 px-4 py-2 border border-zinc-600 rounded-full text-xs text-zinc-300 hover:border-zinc-400 hover:text-white transition-colors"
              >
                <svg className="w-3.5 h-3.5" viewBox="0 0 87.3 78" xmlns="http://www.w3.org/2000/svg">
                  <path d="m6.6 66.85 3.85 6.65c.8 1.4 1.95 2.5 3.3 3.3l13.75-23.8h-27.5c0 1.55.4 3.1 1.2 4.5z" fill="#0066da"/>
                  <path d="m43.65 25-13.75-23.8c-1.35.8-2.5 1.9-3.3 3.3l-20.4 35.3c-.8 1.4-1.2 2.95-1.2 4.5h27.5z" fill="#00ac47"/>
                  <path d="m73.55 76.8c1.35-.8 2.5-1.9 3.3-3.3l1.6-2.75 7.65-13.25c.8-1.4 1.2-2.95 1.2-4.5h-27.5l5.85 13.15z" fill="#ea4335"/>
                  <path d="m43.65 25 13.75-23.8c-1.35-.8-2.9-1.2-4.5-1.2h-18.5c-1.6 0-3.15.45-4.5 1.2z" fill="#00832d"/>
                  <path d="m59.8 53h-32.3l-13.75 23.8c1.35.8 2.9 1.2 4.5 1.2h50.8c1.6 0 3.15-.45 4.5-1.2z" fill="#2684fc"/>
                  <path d="m73.4 26.5-10.1-17.5c-.8-1.4-1.95-2.5-3.3-3.3l-13.75 23.8 16.15 23.5h27.45c0-1.55-.4-3.1-1.2-4.5z" fill="#ffba00"/>
                </svg>
                Import clips from Google Drive
              </button>
            </div>
          )}
        </div>

        {/* Drive import progress */}
        {pageState === "drive_importing" && driveImportStatus && (
          <div className="mt-4">
            <div className="flex justify-between text-xs text-zinc-400 mb-1">
              <span>
                Importing from Google Drive ({driveImportStatus.completed}/{driveImportStatus.total})
              </span>
              {driveImportStatus.current_file && (
                <span className="text-zinc-500 truncate ml-2">{driveImportStatus.current_file}</span>
              )}
            </div>
            <div className="h-1.5 bg-zinc-800 rounded-full overflow-hidden">
              <div
                className="h-full bg-white transition-all duration-500"
                style={{ width: `${driveImportStatus.total > 0 ? (driveImportStatus.completed / driveImportStatus.total) * 100 : 0}%` }}
              />
            </div>
          </div>
        )}

        {pageState === "drive_importing" && !driveImportStatus && (
          <p className="mt-4 text-sm text-zinc-400 text-center animate-pulse">Starting Drive import...</p>
        )}

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
