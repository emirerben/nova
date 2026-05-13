"use client";

import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import {
  type BatchPresignedFile,
  type DriveImportBatchStatusResponse,
  type RequiredInput,
  type TemplateListItem,
  TemplateNotFoundError,
  createTemplateJob,
  getDriveImportBatchStatus,
  getBatchPresignedUrls,
  getTemplate,
  importBatchFromDrive,
  normaliseMimeType,
  prefetchClipAnalyze,
  uploadFileToGcs,
} from "@/lib/api";
import { trackRecentJob } from "@/hooks/useArchitectureData";
import {
  saveBatchToStorage,
  readBatchFromStorage,
  clearBatchStorage,
} from "@/lib/batch-storage";
import {
  preloadDriveScripts,
  requestDriveAccessToken,
  openDrivePicker,
} from "@/lib/google-drive-picker";
import SlotBoundUpload from "./SlotBoundUpload";

const GOOGLE_CLIENT_ID = process.env.NEXT_PUBLIC_GOOGLE_CLIENT_ID ?? "";
const GOOGLE_PICKER_API_KEY = process.env.NEXT_PUBLIC_GOOGLE_PICKER_API_KEY ?? "";

const ALLOWED_MIME = ["video/mp4", "video/quicktime"];
const MAX_BYTES = 4 * 1024 * 1024 * 1024;
const MAX_CLIPS = 20;

type Phase = "ready" | "uploading" | "enqueuing" | "drive_importing" | "error";

interface ClipFile {
  file: File;
  id: string;
  progress: number;
  error: string | null;
  // Probed via HTMLVideoElement on file select. Null until probe completes
  // (or if probing fails — e.g., codec the browser can't decode metadata for).
  // Sent to the backend so the job-create endpoint can reject submissions
  // whose total footage can't fill the template's audio length.
  duration_s: number | null;
}

/** Read a video file's duration via a hidden <video> element. Resolves to
 *  null if the browser can't decode metadata within 5 seconds. */
function probeVideoDuration(file: File): Promise<number | null> {
  return new Promise((resolve) => {
    const url = URL.createObjectURL(file);
    const video = document.createElement("video");
    video.preload = "metadata";
    video.muted = true;
    const cleanup = () => {
      URL.revokeObjectURL(url);
      video.removeAttribute("src");
      video.load();
    };
    const timer = setTimeout(() => {
      cleanup();
      resolve(null);
    }, 5000);
    video.onloadedmetadata = () => {
      clearTimeout(timer);
      const d = Number.isFinite(video.duration) ? video.duration : null;
      cleanup();
      resolve(d);
    };
    video.onerror = () => {
      clearTimeout(timer);
      cleanup();
      resolve(null);
    };
    video.src = url;
  });
}

let cachedDriveToken: { token: string; expiresAt: number } | null = null;

export default function TemplateDetailPage() {
  const router = useRouter();
  const params = useParams<{ id: string }>();
  const templateId = params?.id ?? "";

  const fileInput = useRef<HTMLInputElement>(null);
  const drivePollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const [template, setTemplate] = useState<TemplateListItem | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);

  const [phase, setPhase] = useState<Phase>("ready");
  const [clips, setClips] = useState<ClipFile[]>([]);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [dragOver, setDragOver] = useState(false);
  const [compress, setCompress] = useState(false);
  const [isRecovery, setIsRecovery] = useState(false);
  const [driveImportStatus, setDriveImportStatus] =
    useState<DriveImportBatchStatusResponse | null>(null);

  // Per-template inputs (e.g. {location: "Tokyo"}). Keys come from required_inputs.
  const [inputs, setInputs] = useState<Record<string, string>>({});

  const driveAvailable = !!GOOGLE_CLIENT_ID && !!GOOGLE_PICKER_API_KEY;

  useEffect(() => {
    if (driveAvailable) preloadDriveScripts().catch(() => {});
  }, [driveAvailable]);

  useEffect(() => {
    return () => {
      if (drivePollRef.current) {
        clearTimeout(
          drivePollRef.current as unknown as ReturnType<typeof setTimeout>,
        );
      }
    };
  }, []);

  useEffect(() => {
    if (!templateId) return;
    let cancelled = false;
    getTemplate(templateId)
      .then((t) => {
        if (!cancelled) setTemplate(t);
      })
      .catch((e: unknown) => {
        if (cancelled) return;
        if (e instanceof TemplateNotFoundError) {
          setLoadError("Template not found.");
        } else {
          setLoadError(e instanceof Error ? e.message : "Failed to load template");
        }
      })
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [templateId]);

  function startBatchPolling(
    batchId: string,
    tplId: string,
    requiredClipsMin: number,
  ) {
    if (drivePollRef.current) {
      clearTimeout(drivePollRef.current as unknown as ReturnType<typeof setTimeout>);
    }
    setPhase("drive_importing");

    const minClips = requiredClipsMin;
    const maxRetries = 3;
    let consecutiveErrors = 0;
    // Track which GCS paths we've already prefetched. The Drive batch task
    // appends to `status.gcs_paths` as each file finishes uploading; we
    // fire prefetch for new entries every poll so analysis starts before
    // the LAST file even arrives. Saves the most time on slow batches.
    const prefetched = new Set<string>();

    async function pollOnce() {
      try {
        const status = await getDriveImportBatchStatus(batchId);
        consecutiveErrors = 0;
        setDriveImportStatus(status);

        for (const p of status.gcs_paths) {
          if (!prefetched.has(p)) {
            prefetched.add(p);
            prefetchClipAnalyze(p, tplId);
          }
        }

        if (status.status === "complete") {
          clearBatchStorage();
          setPhase("enqueuing");
          const { job_id } = await createTemplateJob({
            template_id: tplId,
            clip_gcs_paths: status.gcs_paths,
            selected_platforms: ["tiktok", "instagram", "youtube"],
            inputs,
          });
          trackRecentJob(job_id, "template");
          router.push(`/template-jobs/${job_id}`);
        } else if (status.status === "partial_failure") {
          if (status.completed >= minClips) {
            clearBatchStorage();
            setPhase("enqueuing");
            const { job_id } = await createTemplateJob({
              template_id: tplId,
              clip_gcs_paths: status.gcs_paths,
              selected_platforms: ["tiktok", "instagram", "youtube"],
              inputs,
            });
            trackRecentJob(job_id, "template");
            router.push(`/template-jobs/${job_id}`);
          } else {
            clearBatchStorage();
            setPhase("error");
            setErrorMsg(
              `Only ${status.completed}/${status.total} clips imported (need at least ${minClips}). ` +
                `Failed: ${status.errors.join("; ")}`,
            );
          }
        } else if (status.status === "failed") {
          clearBatchStorage();
          setPhase("error");
          setErrorMsg(`All imports failed: ${status.errors.join("; ")}`);
        } else {
          drivePollRef.current = setTimeout(pollOnce, 3000) as unknown as ReturnType<typeof setInterval>;
        }
      } catch {
        consecutiveErrors++;
        if (consecutiveErrors >= maxRetries) {
          clearBatchStorage();
          setPhase("error");
          setErrorMsg("Failed to check import status.");
        } else {
          drivePollRef.current = setTimeout(
            pollOnce,
            3000 * consecutiveErrors,
          ) as unknown as ReturnType<typeof setInterval>;
        }
      }
    }

    drivePollRef.current = setTimeout(pollOnce, 1000) as unknown as ReturnType<typeof setInterval>;
  }

  // Resume in-progress Drive batch when user returns to this template.
  const recoveryAttempted = useRef(false);
  useEffect(() => {
    if (recoveryAttempted.current) return;
    if (!template) return;
    recoveryAttempted.current = true;

    const saved = readBatchFromStorage();
    if (!saved || saved.template_id !== template.id) return;

    getDriveImportBatchStatus(saved.batch_id)
      .then((status) => {
        if (status.status === "importing") {
          setIsRecovery(true);
          startBatchPolling(saved.batch_id, template.id, template.required_clips_min);
        } else {
          clearBatchStorage();
        }
      })
      .catch(() => clearBatchStorage());
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [template]);

  function addFiles(files: FileList | File[]) {
    const arr = Array.from(files);
    const valid = arr.filter(
      (f) => ALLOWED_MIME.includes(f.type) && f.size <= MAX_BYTES,
    );
    const fresh: ClipFile[] = valid.map((f) => ({
      file: f,
      id: crypto.randomUUID(),
      progress: 0,
      error: null,
      duration_s: null,
    }));
    setClips((prev) => [...prev, ...fresh].slice(0, MAX_CLIPS));
    // Probe durations in the background. Each probe updates its own clip
    // when finished; users can submit immediately and the duration check
    // re-evaluates as soon as the probes complete.
    for (const c of fresh) {
      probeVideoDuration(c.file).then((d) => {
        setClips((prev) =>
          prev.map((p) => (p.id === c.id ? { ...p, duration_s: d } : p)),
        );
      });
    }
  }

  function removeClip(id: string) {
    setClips((prev) => prev.filter((c) => c.id !== id));
  }

  function inputsValid(reqInputs: RequiredInput[]): string | null {
    for (const r of reqInputs) {
      const v = (inputs[r.key] ?? "").trim();
      if (r.required && !v) return `${r.label} is required`;
      if (v.length > r.max_length)
        return `${r.label} exceeds ${r.max_length} chars`;
    }
    return null;
  }

  async function handleSubmit() {
    if (!template) return;
    setErrorMsg(null);

    const inputsErr = inputsValid(template.required_inputs);
    if (inputsErr) {
      setErrorMsg(inputsErr);
      return;
    }

    const minClips = template.required_clips_min;
    const maxClipsForTemplate = Math.min(template.required_clips_max, MAX_CLIPS);
    if (clips.length < minClips) {
      setErrorMsg(`Add ${minClips - clips.length} more clip${minClips - clips.length !== 1 ? "s" : ""}.`);
      return;
    }
    if (clips.length > maxClipsForTemplate) {
      setErrorMsg(`Remove ${clips.length - maxClipsForTemplate} clip${clips.length - maxClipsForTemplate !== 1 ? "s" : ""}.`);
      return;
    }

    // Reject when the user's clips can't fill the template's audio length.
    // Skip if any clip's duration is still being probed (rare — probes run
    // when files are added; this only matters if submit fires within ~50ms).
    const allProbed = clips.every((c) => c.duration_s != null);
    if (allProbed && template.total_duration_s > 0) {
      const totalSeconds = clips.reduce((sum, c) => sum + (c.duration_s ?? 0), 0);
      const required = template.total_duration_s;
      if (totalSeconds + 0.25 < required) {
        const shortBy = required - totalSeconds;
        setErrorMsg(
          `Your clips total ${totalSeconds.toFixed(1)}s but this template needs ` +
          `${required.toFixed(1)}s of footage. Add ${shortBy.toFixed(1)}s more.`,
        );
        return;
      }
    }

    try {
      setPhase("uploading");
      const fileMeta: BatchPresignedFile[] = clips.map((c, i) => ({
        filename: `clip_${i}.${c.file.name.split(".").pop() || "mp4"}`,
        content_type: normaliseMimeType(c.file.type),
        file_size_bytes: c.file.size,
      }));
      const { urls } = await getBatchPresignedUrls(fileMeta);

      const gcsPaths: string[] = new Array(clips.length);
      await Promise.all(
        clips.map(async (clip, i) => {
          try {
            await uploadFileToGcs(urls[i].upload_url, clip.file);
            gcsPaths[i] = urls[i].gcs_path;
            setClips((prev) =>
              prev.map((c) => (c.id === clip.id ? { ...c, progress: 100 } : c)),
            );
            // Kick off pre-emptive Gemini analysis for THIS clip while the
            // other clips are still uploading. By the time the user-visible
            // POST /template-jobs fires below, the orchestrator's Redis
            // cache lookup hits and skips the Gemini round-trip entirely.
            // Fire-and-forget — errors are swallowed (see prefetchClipAnalyze).
            prefetchClipAnalyze(urls[i].gcs_path, template.id);
          } catch (err) {
            const m = err instanceof Error ? err.message : "Upload failed";
            setClips((prev) =>
              prev.map((c) => (c.id === clip.id ? { ...c, error: m } : c)),
            );
            throw err;
          }
        }),
      );

      setPhase("enqueuing");
      // Send probed durations alongside paths so the backend can enforce
      // the same total-duration check (defense in depth — a malicious or
      // legacy client might bypass the FE check above).
      const clip_durations = clips
        .map((c) => c.duration_s)
        .filter((d): d is number => d != null);
      const { job_id } = await createTemplateJob({
        template_id: template.id,
        clip_gcs_paths: gcsPaths,
        clip_durations: clip_durations.length === clips.length ? clip_durations : undefined,
        selected_platforms: ["tiktok", "instagram", "youtube"],
        inputs,
      });
      trackRecentJob(job_id, "template");
      router.push(`/template-jobs/${job_id}`);
    } catch (err) {
      setPhase("error");
      setErrorMsg(err instanceof Error ? err.message : "Something went wrong.");
    }
  }

  async function handleDriveImport() {
    if (!template) return;
    setErrorMsg(null);
    setIsRecovery(false);

    const inputsErr = inputsValid(template.required_inputs);
    if (inputsErr) {
      setErrorMsg(inputsErr);
      return;
    }

    try {
      await preloadDriveScripts();

      let accessToken: string;
      if (cachedDriveToken && Date.now() < cachedDriveToken.expiresAt - 5 * 60 * 1000) {
        accessToken = cachedDriveToken.token;
      } else {
        try {
          accessToken = await requestDriveAccessToken(GOOGLE_CLIENT_ID);
        } catch (err) {
          if (err instanceof Error && err.message === "popup_closed") return;
          throw err;
        }
        cachedDriveToken = { token: accessToken, expiresAt: Date.now() + 55 * 60 * 1000 };
      }

      const files = await openDrivePicker(accessToken, GOOGLE_PICKER_API_KEY, { multiSelect: true });
      if (files.length === 0) return;

      const minClips = template.required_clips_min;
      const maxClipsForTemplate = Math.min(template.required_clips_max, MAX_CLIPS);
      if (files.length < minClips) {
        setErrorMsg(`Need at least ${minClips} clips. You selected ${files.length}.`);
        return;
      }
      if (files.length > maxClipsForTemplate) {
        setErrorMsg(`At most ${maxClipsForTemplate} clips. You selected ${files.length}.`);
        return;
      }
      const sizeLimit = compress ? MAX_BYTES * 10 : MAX_BYTES;
      const limitLabel = compress ? "40GB" : "4GB";
      for (const f of files) {
        if (f.sizeBytes > sizeLimit) {
          setErrorMsg(`"${f.fileName}" exceeds the ${limitLabel} limit.`);
          return;
        }
      }

      setPhase("drive_importing");
      const { batch_id } = await importBatchFromDrive({
        files: files.map((f) => ({
          drive_file_id: f.fileId,
          filename: f.fileName,
          file_size_bytes: f.sizeBytes,
          mime_type: f.mimeType,
        })),
        google_access_token: accessToken,
        compress,
      });
      saveBatchToStorage(batch_id, template.id);
      startBatchPolling(batch_id, template.id, template.required_clips_min);
    } catch (err) {
      setPhase("error");
      setErrorMsg(
        err instanceof Error && err.message.includes("denied")
          ? "Google Drive access was denied. Please try again."
          : err instanceof Error
            ? err.message
            : "Drive import failed.",
      );
    }
  }

  // ── Render ────────────────────────────────────────────────────────────────

  if (loading) {
    return (
      <main className="min-h-[calc(100vh-3.5rem)] bg-black text-white px-4 py-12">
        <div className="max-w-xl mx-auto">
          <div className="h-6 w-40 bg-zinc-900 rounded animate-pulse mb-6" />
          <div className="h-8 w-72 bg-zinc-900 rounded animate-pulse mb-3" />
          <div className="h-4 w-56 bg-zinc-900 rounded animate-pulse" />
        </div>
      </main>
    );
  }

  if (!template) {
    return (
      <main className="min-h-[calc(100vh-3.5rem)] bg-black text-white px-4 py-12">
        <div className="max-w-xl mx-auto text-center">
          <p className="text-zinc-400 mb-4">{loadError ?? "Template not found."}</p>
          <Link href="/" className="text-sm underline text-zinc-300 hover:text-white">
            ← Back to templates
          </Link>
        </div>
      </main>
    );
  }

  const isSlotBound = !!template.slots?.some((s) => s.media_type === "photo");
  const minClips = template.required_clips_min;
  const maxClips = Math.min(template.required_clips_max, MAX_CLIPS);
  const totalProgress =
    clips.length > 0
      ? Math.round(clips.reduce((sum, c) => sum + c.progress, 0) / clips.length)
      : 0;
  // Block submit when probed durations sum < template length. Probes are
  // async; if any clip hasn't been measured yet, allow submit and let the
  // backend make the call (it has the same rule).
  const allDurationsKnown = clips.every((c) => c.duration_s != null);
  const totalDurationS = clips.reduce((s, c) => s + (c.duration_s ?? 0), 0);
  const durationOk =
    !allDurationsKnown ||
    template.total_duration_s <= 0 ||
    totalDurationS + 0.25 >= template.total_duration_s;
  const canSubmit =
    clips.length >= minClips && phase === "ready" && durationOk;

  return (
    <main className="min-h-[calc(100vh-3.5rem)] bg-black text-white px-4 py-10">
      <div className="max-w-xl mx-auto">
        <Link
          href="/"
          aria-label="Back to templates"
          className="text-zinc-400 text-sm hover:text-white transition-colors mb-6 inline-block"
        >
          ← Templates
        </Link>

        <h1 className="text-3xl font-semibold mb-1">{template.name}</h1>
        <p className="text-zinc-400 text-sm mb-6">
          {Math.round(template.total_duration_s)}s ·{" "}
          {minClips === maxClips ? `${minClips} clips` : `${minClips}–${maxClips} clips`}
          {" · "}
          <span className="capitalize">{template.copy_tone}</span>
        </p>

        {/* Required inputs (only renders if template declares any) */}
        {template.required_inputs.length > 0 && (
          <div className="mb-5 space-y-3">
            {template.required_inputs.map((r) => (
              <div key={r.key}>
                <label className="block text-zinc-400 text-xs mb-1.5">
                  {r.label}
                  {r.required && <span className="text-red-400 ml-1">*</span>}
                </label>
                <input
                  type="text"
                  value={inputs[r.key] ?? ""}
                  onChange={(e) =>
                    setInputs((prev) => ({ ...prev, [r.key]: e.target.value }))
                  }
                  placeholder={r.placeholder}
                  maxLength={r.max_length}
                  disabled={phase !== "ready"}
                  className="w-full px-3 py-2 bg-zinc-900 border border-zinc-700 rounded-lg text-sm text-white placeholder-zinc-500 focus:outline-none focus:border-zinc-500 disabled:opacity-50"
                />
              </div>
            ))}
          </div>
        )}

        {isSlotBound ? (
          <SlotBoundUpload
            template={template}
            inputs={inputs}
            onJobCreated={(jobId) => {
              trackRecentJob(jobId, "template");
              router.push(`/template-jobs/${jobId}`);
            }}
          />
        ) : (
          <>
            <label className="flex items-center gap-2 mb-4 cursor-pointer select-none">
              <input
                type="checkbox"
                checked={compress}
                onChange={(e) => setCompress(e.target.checked)}
                disabled={phase !== "ready"}
                className="w-4 h-4 rounded border-zinc-600 bg-zinc-800 text-white accent-white"
              />
              <span className="text-zinc-500 text-xs">Compress to 720p</span>
              <span className="text-zinc-600 text-xs">(~10x faster)</span>
            </label>

            <div
              className={`border-2 border-dashed rounded-xl p-8 text-center cursor-pointer transition-colors ${
                dragOver ? "border-white bg-zinc-800" : "border-zinc-600 hover:border-zinc-400"
              }`}
              onClick={() => fileInput.current?.click()}
              onDragOver={(e) => {
                e.preventDefault();
                setDragOver(true);
              }}
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

              {driveAvailable && phase === "ready" && (
                <div className="mt-4 flex flex-col items-center">
                  <div className="flex items-center gap-3 mb-3 w-32">
                    <div className="flex-1 h-px bg-zinc-700" />
                    <span className="text-zinc-500 text-xs">or</span>
                    <div className="flex-1 h-px bg-zinc-700" />
                  </div>
                  <button
                    onClick={(e) => {
                      e.stopPropagation();
                      handleDriveImport();
                    }}
                    className="flex items-center gap-2 px-4 py-2 border border-zinc-600 rounded-full text-xs text-zinc-300 hover:border-zinc-400 hover:text-white transition-colors"
                  >
                    <DriveIcon size={14} />
                    Import clips from Google Drive
                  </button>
                </div>
              )}
            </div>

            {phase === "drive_importing" && driveImportStatus && (
              <div className="mt-4">
                <div className="flex justify-between text-xs text-zinc-400 mb-1">
                  <span>
                    Importing from Google Drive ({driveImportStatus.completed}/{driveImportStatus.total})
                  </span>
                  {driveImportStatus.current_file && (
                    <span className="text-zinc-500 truncate ml-2">
                      {driveImportStatus.current_file}
                    </span>
                  )}
                </div>
                <div className="h-1.5 bg-zinc-800 rounded-full overflow-hidden">
                  <div
                    className="h-full bg-white transition-all duration-500"
                    style={{
                      width: `${
                        driveImportStatus.total > 0
                          ? (driveImportStatus.completed / driveImportStatus.total) * 100
                          : 0
                      }%`,
                    }}
                  />
                </div>
              </div>
            )}

            {phase === "drive_importing" && !driveImportStatus && (
              <p className="mt-4 text-sm text-zinc-400 text-center animate-pulse">
                {isRecovery ? "Resuming Drive import…" : "Starting Drive import…"}
              </p>
            )}

            {clips.length > 0 && (
              <ul className="mt-4 space-y-2">
                {clips.map((c) => (
                  <li
                    key={c.id}
                    className="flex items-center bg-zinc-900 rounded-lg px-4 py-2 text-sm"
                  >
                    <span className="truncate text-zinc-300 flex-1 mr-3">
                      {c.file.name}
                    </span>
                    {c.duration_s != null && (
                      <span className="text-zinc-500 mr-3 shrink-0 text-xs">
                        {c.duration_s.toFixed(1)}s
                      </span>
                    )}
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
                    {phase === "ready" && (
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

            {/* Live total-vs-required readout. Only renders when every clip's
                duration probe has finished and the template has a known
                length, so a half-probed state doesn't flash a false warning. */}
            {clips.length > 0 &&
              clips.every((c) => c.duration_s != null) &&
              template.total_duration_s > 0 && (() => {
                const total = clips.reduce((s, c) => s + (c.duration_s ?? 0), 0);
                const need = template.total_duration_s;
                const tooShort = total + 0.25 < need;
                return (
                  <div
                    className={`mt-3 px-3 py-2 rounded-lg text-xs flex justify-between items-center ${
                      tooShort
                        ? "bg-amber-900/30 border border-amber-700/60 text-amber-200"
                        : "bg-zinc-900 border border-zinc-800 text-zinc-400"
                    }`}
                  >
                    <span>
                      Footage total: <strong className="text-white">{total.toFixed(1)}s</strong>
                      {" "}/{" "}{need.toFixed(1)}s required
                    </span>
                    {tooShort && (
                      <span className="text-amber-300">
                        Add {(need - total).toFixed(1)}s more
                      </span>
                    )}
                  </div>
                );
              })()}

            {errorMsg && (
              <div className="mt-4 bg-red-900/40 border border-red-700 rounded-lg px-4 py-3 text-sm text-red-300">
                {errorMsg}
                {phase === "error" && (
                  <button
                    onClick={() => {
                      setPhase("ready");
                      setErrorMsg(null);
                    }}
                    className="ml-3 underline text-red-400 hover:text-red-200"
                  >
                    Try again
                  </button>
                )}
              </div>
            )}

            {phase === "uploading" && (
              <div className="mt-4">
                <div className="flex justify-between text-xs text-zinc-400 mb-1">
                  <span>Uploading clips…</span>
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

            {phase === "enqueuing" && (
              <p className="mt-4 text-sm text-zinc-400 text-center">
                Starting AI processing…
              </p>
            )}

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
                : "Generate video"}
            </button>
          </>
        )}
      </div>
    </main>
  );
}

function DriveIcon({ size = 16 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 87.3 78" xmlns="http://www.w3.org/2000/svg">
      <path d="m6.6 66.85 3.85 6.65c.8 1.4 1.95 2.5 3.3 3.3l13.75-23.8h-27.5c0 1.55.4 3.1 1.2 4.5z" fill="#0066da" />
      <path d="m43.65 25-13.75-23.8c-1.35.8-2.5 1.9-3.3 3.3l-20.4 35.3c-.8 1.4-1.2 2.95-1.2 4.5h27.5z" fill="#00ac47" />
      <path d="m73.55 76.8c1.35-.8 2.5-1.9 3.3-3.3l1.6-2.75 7.65-13.25c.8-1.4 1.2-2.95 1.2-4.5h-27.5l5.85 13.15z" fill="#ea4335" />
      <path d="m43.65 25 13.75-23.8c-1.35-.8-2.9-1.2-4.5-1.2h-18.5c-1.6 0-3.15.45-4.5 1.2z" fill="#00832d" />
      <path d="m59.8 53h-32.3l-13.75 23.8c1.35.8 2.9 1.2 4.5 1.2h50.8c1.6 0 3.15-.45 4.5-1.2z" fill="#2684fc" />
      <path d="m73.4 26.5-10.1-17.5c-.8-1.4-1.95-2.5-3.3-3.3l-13.75 23.8 16.15 23.5h27.45c0-1.55-.4-3.1-1.2-4.5z" fill="#ffba00" />
    </svg>
  );
}
