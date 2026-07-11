"use client";

/**
 * ShotSlotUploader — the filming guide IS the uploader (D1/D3 variant A).
 *
 * Each recommended shot from item.filming_guide becomes a slot with its own
 * upload state machine: idle → uploading → committing → filled | error.
 * Below the shot rows, an extra-footage strip accepts pool clips.
 *
 * Design tokens: §2 light editorial (cream/ink/lime). Cards: bg-white
 * border-zinc-200 rounded-2xl. Lime = shot-slot completion; zinc = pool.
 * See DESIGN.md §2 and the approved mockup in
 * ~/.gstack/projects/emirerben-nova/designs/plan-item-shot-slots-20260607/
 */

import { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import { flushSync } from "react-dom";
import {
  attachClips,
  requestUploadUrls,
  setClipNote,
  updatePlanItemShot,
  uploadToGcs,
  uploadToGcsWithProgress,
  type ClipAssignment,
  type FilmingShot,
  type PlanItem,
} from "@/lib/plan-api";

// Mode-aware header (direction fork, 2026-06-11): an existing-footage creator
// FINDS clips in their gallery; a create-new creator FILMS them.
const HEADER_BY_MODE: Record<string, string> = {
  existing_footage: "What to look for",
  mixed: "Find it or film it",
  create_new: "How to film this",
};

// ── Slot state machine types ──────────────────────────────────────────────────

type SlotPhase = "idle" | "uploading" | "committing" | "filled" | "error";
type ErrorPhase = "upload" | "attach";

interface SlotState {
  phase: SlotPhase;
  // upload phase
  filename?: string;
  progress?: number; // 0–1
  abortController?: AbortController;
  // filled phase
  objectUrl?: string; // only when local file is still available
  mediaKind?: "video" | "image";
  durationLabel?: string; // "0:18"
  gcsPaths?: string; // the committed gcs_path
  /** Creator context note on this clip ("" = none). */
  userNote?: string;
  /** True = footage-pool matcher placed this clip (provisional chip). */
  machineMatched?: boolean;
  // error phase
  errorPhase?: ErrorPhase;
  pendingGcsPath?: string; // attach-phase retry: skip re-upload
}

// ── Helpers ───────────────────────────────────────────────────────────────────

function formatDuration(seconds: number): string {
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  return `${m}:${String(s).padStart(2, "0")}`;
}

function probeVideoDuration(file: File): Promise<string | null> {
  return new Promise((resolve) => {
    const url = URL.createObjectURL(file);
    const video = document.createElement("video");
    video.preload = "metadata";
    video.onloadedmetadata = () => {
      URL.revokeObjectURL(url);
      resolve(isFinite(video.duration) ? formatDuration(video.duration) : null);
    };
    video.onerror = () => {
      URL.revokeObjectURL(url);
      resolve(null);
    };
    video.src = url;
  });
}

const VIDEO_UPLOAD_ACCEPT = "video/mp4,video/quicktime";
const MASONRY_UPLOAD_ACCEPT = `${VIDEO_UPLOAD_ACCEPT},image/jpeg,image/png,image/webp,image/heic,image/heif`;

function uploadContentType(file: File): string {
  if (file.type) return file.type;
  const name = file.name.toLowerCase();
  if (name.endsWith(".jpg") || name.endsWith(".jpeg")) return "image/jpeg";
  if (name.endsWith(".png")) return "image/png";
  if (name.endsWith(".webp")) return "image/webp";
  if (name.endsWith(".heic")) return "image/heic";
  if (name.endsWith(".heif")) return "image/heif";
  if (name.endsWith(".mov")) return "video/quicktime";
  return "video/mp4";
}

function uploadMediaKind(file: File): "video" | "image" {
  return uploadContentType(file).startsWith("image/") ? "image" : "video";
}

// ── Props ─────────────────────────────────────────────────────────────────────

interface ShotSlotUploaderProps {
  item: PlanItem;
  /** Called with the updated PlanItem after every successful attach. */
  onAttached: (updated: PlanItem) => void;
  /**
   * Called whenever the "busy" state changes.
   * busy = any upload or commit in flight → parent should disable Generate.
   */
  onBusyChange: (busy: boolean) => void;
}

// ── Component ─────────────────────────────────────────────────────────────────

export default function ShotSlotUploader({ item, onAttached, onBusyChange }: ShotSlotUploaderProps) {
  const shots = useMemo(() => item.filming_guide ?? [], [item.filming_guide]);
  const isMasonryMontage =
    (item.edit_format ?? "montage") === "montage" && item.montage_preset === "masonry";
  const uploadAccept = isMasonryMontage ? MASONRY_UPLOAD_ACCEPT : VIDEO_UPLOAD_ACCEPT;

  // Per-slot state keyed by shot_id (or "__pool__" for pool uploads in progress).
  // Filled-from-reload state is derived from item.clip_assignments initially.
  const buildInitialSlotState = useCallback((): Record<string, SlotState> => {
    const initial: Record<string, SlotState> = {};
    for (const shot of shots) {
      if (!shot.shot_id) continue;
      const assignment = item.clip_assignments?.find((a) => a.shot_id === shot.shot_id);
      initial[shot.shot_id] = assignment
        ? {
            phase: "filled",
            filename: assignment.gcs_path.split("/").pop() ?? assignment.gcs_path,
            gcsPaths: assignment.gcs_path,
            userNote: assignment.user_note ?? "",
            machineMatched: !!assignment.machine_matched,
          }
        : { phase: "idle" };
    }
    return initial;
  }, [shots, item.clip_assignments]);

  const [slotState, setSlotState] = useState<Record<string, SlotState>>(buildInitialSlotState);

  // Pool clips (already-attached extra footage).
  const [poolPaths, setPoolPaths] = useState<ClipAssignment[]>(
    item.clip_assignments?.filter((a) => a.shot_id === null) ?? [],
  );

  // aria-live announcement text.
  const [announcement, setAnnouncement] = useState("");

  // Serialised attach queue — each attach sends the FULL current assignment map.
  const attachQueue = useRef<Promise<void>>(Promise.resolve());

  // Track busy state for the parent's Generate gate (D6).
  const updateBusy = useCallback(
    (state: Record<string, SlotState>) => {
      const busy = Object.values(state).some(
        (s) => s.phase === "uploading" || s.phase === "committing",
      );
      onBusyChange(busy);
    },
    [onBusyChange],
  );

  // Re-evaluate busy whenever slot state changes.
  useEffect(() => updateBusy(slotState), [slotState, updateBusy]);

  // Derived counts.
  const filledCount = Object.values(slotState).filter((s) => s.phase === "filled").length;
  const anyFilled = filledCount > 0;

  // ── Build assignments from current slot + pool state ──────────────────────

  function buildAssignments(
    currentSlots: Record<string, SlotState>,
    currentPool: ClipAssignment[],
  ): ClipAssignment[] {
    const result: ClipAssignment[] = [];
    for (const shot of shots) {
      const sid = shot.shot_id;
      if (!sid) continue;
      const s = currentSlots[sid];
      // Include both "filled" and "committing" phases: GCS upload succeeded in both.
      if ((s?.phase === "filled" || s?.phase === "committing") && s.gcsPaths) {
        // Carry the note through full re-attaches so it never silently drops.
        result.push({ gcs_path: s.gcsPaths, shot_id: sid, user_note: s.userNote ?? "" });
      }
    }
    for (const p of currentPool) {
      result.push({ gcs_path: p.gcs_path, shot_id: null, user_note: p.user_note ?? "" });
    }
    return result;
  }

  // ── Per-slot upload ───────────────────────────────────────────────────────

  async function handleSlotFile(shot: FilmingShot, file: File) {
    const sid = shot.shot_id;
    if (!sid) return;

    const ac = new AbortController();

    setSlotState((prev) => ({
      ...prev,
      [sid]: { phase: "uploading", filename: file.name, progress: 0, abortController: ac },
    }));
    setAnnouncement(`Shot ${shots.indexOf(shot) + 1} uploading…`);

    let gcsPath: string;
    const contentType = uploadContentType(file);
    const mediaKind = uploadMediaKind(file);
    try {
      const urls = await requestUploadUrls(item.id, [
        { filename: file.name, content_type: contentType, file_size_bytes: file.size },
      ]);
      gcsPath = urls[0].gcs_path;

      await uploadToGcsWithProgress(
        urls[0].upload_url,
        file,
        (frac) => {
          setSlotState((prev) =>
            prev[sid]?.phase === "uploading"
              ? { ...prev, [sid]: { ...prev[sid], progress: frac } }
              : prev,
          );
        },
        ac.signal,
      );
    } catch (err) {
      if ((err as DOMException)?.name === "AbortError") {
        // Cancelled: return to idle, no residue.
        setSlotState((prev) => ({ ...prev, [sid]: { phase: "idle" } }));
        return;
      }
      setSlotState((prev) => ({
        ...prev,
        [sid]: { phase: "error", errorPhase: "upload", filename: file.name },
      }));
      setAnnouncement(`Shot ${shots.indexOf(shot) + 1} upload failed`);
      return;
    }

    // GCS PUT succeeded — probe duration while committing.
    const durationLabel =
      mediaKind === "video" ? await probeVideoDuration(file).catch(() => null) : null;
    const objectUrl = URL.createObjectURL(file);

    // Transition to committing.
    setSlotState((prev) => ({
      ...prev,
      [sid]: { phase: "committing", filename: file.name, progress: 1, gcsPaths: gcsPath },
    }));

    await commitAttach(sid, gcsPath, objectUrl, durationLabel ?? undefined, file.name, mediaKind);
  }

  // ── Pool upload ───────────────────────────────────────────────────────────

  async function handlePoolFiles(files: FileList | null) {
    if (!files || files.length === 0) return;
    const fileList = Array.from(files);

    let urls: { upload_url: string; gcs_path: string }[];
    try {
      urls = await requestUploadUrls(
        item.id,
        fileList.map((f) => ({
          filename: f.name,
          content_type: uploadContentType(f),
          file_size_bytes: f.size,
        })),
      );
    } catch {
      return;
    }

    try {
      await Promise.all(urls.map((u, i) => uploadToGcsWithProgress(u.upload_url, fileList[i], () => {})));
    } catch {
      return;
    }

    const newPoolAssignments = urls.map((u) => ({ gcs_path: u.gcs_path, shot_id: null as null }));

    setPoolPaths((prev) => {
      const updated = [...prev, ...newPoolAssignments];
      // Enqueue the attach.
      void enqueueAttach(slotState, updated);
      return updated;
    });
  }

  function handlePoolRemove(gcsPath: string) {
    setPoolPaths((prev) => {
      const updated = prev.filter((a) => a.gcs_path !== gcsPath);
      void enqueueAttach(slotState, updated);
      return updated;
    });
  }

  // ── Attach serialization ──────────────────────────────────────────────────

  function enqueueAttach(
    currentSlots: Record<string, SlotState>,
    currentPool: ClipAssignment[],
  ): Promise<void> {
    attachQueue.current = attachQueue.current.then(async () => {
      const assignments = buildAssignments(currentSlots, currentPool);
      const gcsPaths = assignments.map((a) => a.gcs_path);
      try {
        const updated = await attachClips(item.id, gcsPaths, assignments);
        onAttached(updated);
      } catch {
        // Pool attach errors are non-fatal — the UI still shows the chips.
      }
    });
    return attachQueue.current;
  }

  async function commitAttach(
    sid: string,
    gcsPath: string,
    objectUrl: string,
    durationLabel: string | undefined,
    filename: string,
    mediaKind: "video" | "image" = "video",
  ) {
    // Capture the latest state synchronously using flushSync.
    // React functional updaters are called lazily during reconciliation, not
    // synchronously when setState is called — the setTimeout(0) trick is
    // unreliable in React 18 concurrent mode (both are macrotasks, ordering
    // is not guaranteed). flushSync forces all pending updates to flush now,
    // so latestSlots/latestPool are populated before we enqueue the attach.
    let latestSlots: Record<string, SlotState> = {};
    let latestPool: ClipAssignment[] = [];

    flushSync(() => {
      setSlotState((prev) => {
        latestSlots = {
          ...prev,
          [sid]: { phase: "committing", filename, progress: 1, gcsPaths: gcsPath, mediaKind },
        };
        return latestSlots;
      });
      setPoolPaths((prev) => {
        latestPool = prev;
        return prev;
      });
    });

    attachQueue.current = attachQueue.current.then(async () => {
      const assignments = buildAssignments(latestSlots, latestPool);
      const gcsPaths = assignments.map((a) => a.gcs_path);
      try {
        const updated = await attachClips(item.id, gcsPaths, assignments);
        setSlotState((prev) => ({
          ...prev,
          [sid]: { phase: "filled", filename, gcsPaths: gcsPath, objectUrl, durationLabel, mediaKind },
        }));
        setAnnouncement(`Shot ${shots.findIndex((s) => s.shot_id === sid) + 1} uploaded`);
        onAttached(updated);
      } catch {
        setSlotState((prev) => ({
          ...prev,
          [sid]: {
            phase: "error",
            errorPhase: "attach",
            filename,
            pendingGcsPath: gcsPath,
            objectUrl,
            durationLabel,
            mediaKind,
          },
        }));
        setAnnouncement(`Shot ${shots.findIndex((s) => s.shot_id === sid) + 1} upload failed`);
      }
    });
  }

  // ── Retry ──────────────────────────────────────────────────────────────────

  function handleRetry(shot: FilmingShot, _retryFile?: File) {
    const sid = shot.shot_id;
    if (!sid) return;
    const s = slotState[sid];
    if (!s) return;

    if (s.errorPhase === "attach" && s.pendingGcsPath) {
      // Re-attach without re-upload.
      const objectUrl = s.objectUrl;
      const durationLabel = s.durationLabel;
      const filename = s.filename ?? "";
      const mediaKind = s.mediaKind ?? "video";
      const gcsPath = s.pendingGcsPath;
      setSlotState((prev) => ({
        ...prev,
        [sid]: { phase: "committing", filename, progress: 1, gcsPaths: gcsPath, mediaKind },
      }));
      void commitAttach(sid, gcsPath, objectUrl ?? "", durationLabel, filename, mediaKind);
    }
    // Upload-phase retry: re-trigger file input (handled by onChange in the slot).
  }

  // ── Cancel ────────────────────────────────────────────────────────────────

  function handleCancel(sid: string) {
    const s = slotState[sid];
    if (s?.abortController) s.abortController.abort();
    // The abort handler in handleSlotFile returns slot to idle.
  }

  // ── Replace ───────────────────────────────────────────────────────────────

  function handleReplace(sid: string) {
    // Revoke any lingering object URL.
    const s = slotState[sid];
    if (s?.objectUrl) URL.revokeObjectURL(s.objectUrl);
    setSlotState((prev) => ({ ...prev, [sid]: { phase: "idle" } }));
  }

  // ── Clip note + provisional-match actions ─────────────────────────────────
  // Both route through PATCH /clips/note: it persists the note, clears
  // machine_matched (the user touched the slot), and re-runs the brief read.

  async function handleSaveNote(sid: string, note: string) {
    const s = slotState[sid];
    if (!s?.gcsPaths) return;
    const updated = await setClipNote(item.id, s.gcsPaths, note);
    setSlotState((prev) => ({
      ...prev,
      [sid]: { ...prev[sid], userNote: note, machineMatched: false },
    }));
    onAttached(updated);
  }

  async function handleKeepMatch(sid: string) {
    const s = slotState[sid];
    if (!s?.gcsPaths) return;
    try {
      const updated = await setClipNote(item.id, s.gcsPaths, s.userNote ?? "");
      setSlotState((prev) => ({
        ...prev,
        [sid]: { ...prev[sid], machineMatched: false },
      }));
      setAnnouncement("Clip kept");
      onAttached(updated);
    } catch {
      // Non-fatal: chip stays provisional; user can retry.
    }
  }

  // ── WS3: Inline shot text editing ────────────────────────────────────────

  const [editingShotId, setEditingShotId] = useState<string | undefined>(undefined);
  const [editWhat, setEditWhat] = useState("");
  const [editHow, setEditHow] = useState("");
  const [shotEditError, setShotEditError] = useState<string | null>(null);

  function startEditShot(shot: FilmingShot) {
    if (!shot.shot_id) return;
    setEditingShotId(shot.shot_id);
    setEditWhat(shot.what);
    setEditHow(shot.how ?? "");
    setShotEditError(null);
  }

  async function handleSaveEditedShot(shot: FilmingShot) {
    if (!shot.shot_id) return;
    try {
      const updated = await updatePlanItemShot(item.id, shot.shot_id, {
        what: editWhat.trim() || shot.what,
        how: editHow.trim(),
      });
      setEditingShotId(undefined);
      setShotEditError(null);
      onAttached(updated);
    } catch {
      setShotEditError("Couldn't save — try again");
    }
  }

  function handleCancelEditShot() {
    setEditingShotId(undefined);
    setShotEditError(null);
  }

  // Determine if any slot is busy uploading (used to gate shot editing).
  const uploaderBusy = Object.values(slotState).some(
    (s) => s.phase === "uploading" || s.phase === "committing",
  );

  // ── WS4: Multi-clip per shot ──────────────────────────────────────────────
  // Per-shot extra-clip upload state: tracks whether a shot's secondary upload
  // is in progress.
  const [multiUploadBusy, setMultiUploadBusy] = useState<Record<string, boolean>>({});

  async function handleExtraClipFiles(shot: FilmingShot, files: FileList | null) {
    const sid = shot.shot_id;
    if (!sid || !files || files.length === 0) return;

    const fileList = Array.from(files);
    setMultiUploadBusy((prev) => ({ ...prev, [sid]: true }));
    try {
      const urls = await requestUploadUrls(
        item.id,
        fileList.map((f) => ({
          filename: f.name,
          content_type: uploadContentType(f),
          file_size_bytes: f.size,
        })),
      );
      await Promise.all(urls.map((u, i) => uploadToGcs(u.upload_url, fileList[i])));

      // Build the full assignment list: existing shot assignments + pool, plus the new ones.
      const newAssignments: ClipAssignment[] = urls.map((u) => ({
        gcs_path: u.gcs_path,
        shot_id: sid,
        user_note: "",
      }));

      // Read current state synchronously via a callback so we see the latest.
      let latestSlots: Record<string, SlotState> = {};
      let latestPool: ClipAssignment[] = [];
      flushSync(() => {
        setSlotState((prev) => {
          latestSlots = prev;
          return prev;
        });
        setPoolPaths((prev) => {
          latestPool = prev;
          return prev;
        });
      });

      const existingAssignments = buildAssignments(latestSlots, latestPool);
      const allAssignments = [...existingAssignments, ...newAssignments];
      const allPaths = allAssignments.map((a) => a.gcs_path);
      const updated = await attachClips(item.id, allPaths, allAssignments);
      onAttached(updated);
    } catch {
      // Non-fatal: show no error — upload inputs reset naturally.
    } finally {
      setMultiUploadBusy((prev) => ({ ...prev, [sid]: false }));
    }
  }

  async function handleRemoveExtraClip(sid: string, gcsPathToRemove: string) {
    let latestSlots: Record<string, SlotState> = {};
    let latestPool: ClipAssignment[] = [];
    flushSync(() => {
      setSlotState((prev) => {
        latestSlots = prev;
        return prev;
      });
      setPoolPaths((prev) => {
        latestPool = prev;
        return prev;
      });
    });

    const existingAssignments = buildAssignments(latestSlots, latestPool);
    // Remove only the specific extra clip (not the primary slot clip for this shot_id).
    // Extra clips are ones where shot_id matches AND gcs_path is NOT the primary slot path.
    const primaryPath = latestSlots[sid]?.gcsPaths;
    const filtered = existingAssignments.filter(
      (a) => !(a.shot_id === sid && a.gcs_path === gcsPathToRemove && a.gcs_path !== primaryPath),
    );
    const paths = filtered.map((a) => a.gcs_path);
    try {
      const updated = await attachClips(item.id, paths, filtered);
      onAttached(updated);
    } catch {
      // Non-fatal.
    }
  }

  // ── Render ────────────────────────────────────────────────────────────────

  return (
    <div className="mb-8 rounded-2xl border border-zinc-200 bg-white p-5">
      {/* aria-live region for screen reader announcements */}
      <div aria-live="polite" className="sr-only">
        {announcement}
      </div>

      {/* Header row (D12: typographic eyebrow, no emoji; mode-aware copy) */}
      <div className="mb-4 flex items-center justify-between">
        <span
          data-plan-shot-list-heading
          tabIndex={-1}
          className="text-xs font-medium uppercase tracking-[0.08em] text-lime-700"
        >
          {HEADER_BY_MODE[item.content_mode ?? "create_new"] ?? HEADER_BY_MODE.create_new}
        </span>
        {/* Progress pill */}
        {anyFilled ? (
          <span className="rounded-full border border-lime-200 bg-lime-50 px-2.5 py-0.5 text-xs text-lime-800">
            {filledCount} of {shots.length} filmed
          </span>
        ) : (
          <span className="text-xs text-[#71717a]">
            0 of {shots.length} filmed
          </span>
        )}
      </div>

      {/* Shot rows */}
      <div className="divide-y divide-zinc-100">
        {shots.map((shot, i) => {
          const sid = shot.shot_id ?? `legacy-${i}`;
          const state = slotState[sid] ?? { phase: "idle" };
          const isEditing = editingShotId === shot.shot_id;
          const canEdit = !!shot.shot_id && !uploaderBusy;
          const clipCount = shot.clip_count ?? 1;
          // Extra clips for this shot: assignments with shot_id === sid that are NOT
          // the primary slot clip.
          const primaryPath = state.gcsPaths;
          const extraClips = (item.clip_assignments ?? []).filter(
            (a) => a.shot_id === sid && a.gcs_path !== primaryPath,
          );
          const totalFilled = (state.phase === "filled" ? 1 : 0) + extraClips.length;
          const remaining = Math.max(0, clipCount - totalFilled);
          return (
            <div
              key={sid}
              data-plan-shot-row={i}
              tabIndex={i === 0 ? -1 : undefined}
              className="py-3 first:pt-0 last:pb-0"
            >
              {/* Shot header — WS3: inline editable */}
              {isEditing ? (
                <div className="mb-2">
                  <div className="flex items-start gap-2">
                    <span className="font-display italic text-[#a1a1aa]">{i + 1}.</span>
                    <div className="flex flex-1 flex-col gap-1.5">
                      <input
                        autoFocus
                        type="text"
                        value={editWhat}
                        onChange={(e) => setEditWhat(e.target.value)}
                        onKeyDown={(e) => {
                          if (e.key === "Enter") { e.preventDefault(); void handleSaveEditedShot(shot); }
                          if (e.key === "Escape") handleCancelEditShot();
                        }}
                        className="rounded border border-zinc-300 bg-white px-2 py-0.5 text-sm text-[#3f3f46] focus:border-lime-600 focus:outline-none focus:ring-1 focus:ring-lime-600"
                        placeholder="What to film"
                      />
                      <input
                        type="text"
                        value={editHow}
                        onChange={(e) => setEditHow(e.target.value)}
                        onKeyDown={(e) => {
                          if (e.key === "Enter") { e.preventDefault(); void handleSaveEditedShot(shot); }
                          if (e.key === "Escape") handleCancelEditShot();
                        }}
                        className="rounded border border-zinc-300 bg-white px-2 py-0.5 text-sm text-[#71717a] focus:border-lime-600 focus:outline-none focus:ring-1 focus:ring-lime-600"
                        placeholder="How (optional)"
                      />
                      <div className="flex items-center gap-2">
                        <button
                          type="button"
                          onClick={() => void handleSaveEditedShot(shot)}
                          className="text-xs font-medium text-lime-700 underline underline-offset-2 hover:text-lime-800 focus-visible:ring-2 focus-visible:ring-lime-600 focus-visible:ring-offset-2"
                        >
                          Save
                        </button>
                        <button
                          type="button"
                          onClick={handleCancelEditShot}
                          className="text-xs text-[#71717a] underline underline-offset-2 hover:text-[#0c0c0e] focus-visible:ring-2 focus-visible:ring-lime-600 focus-visible:ring-offset-2"
                        >
                          Cancel
                        </button>
                        {shotEditError && (
                          <span className="text-xs text-red-600">{shotEditError}</span>
                        )}
                      </div>
                    </div>
                  </div>
                </div>
              ) : (
                <div className="group mb-2 flex items-start gap-2">
                  <span className="font-display italic text-[#a1a1aa]">{i + 1}.</span>
                  {shot.duration_s ? (
                    <span className="rounded bg-zinc-100 px-1.5 py-0.5 text-xs text-[#71717a]">
                      {shot.duration_s}s
                    </span>
                  ) : null}
                  {/* WS4: clip count badge */}
                  {clipCount > 1 && (
                    <span className="rounded bg-zinc-100 px-1.5 py-0.5 text-xs text-[#71717a]">
                      Film {clipCount} clips
                    </span>
                  )}
                  <span className="flex-1 text-sm text-[#3f3f46]">
                    {shot.what}
                    {shot.how ? (
                      <span className="text-[#71717a]"> — {shot.how}</span>
                    ) : null}
                  </span>
                  {/* WS3: pencil edit button — only visible on hover, only when canEdit */}
                  {canEdit && (
                    <button
                      type="button"
                      onClick={() => startEditShot(shot)}
                      aria-label={`Edit shot ${i + 1} text`}
                      className="shrink-0 opacity-0 transition-opacity group-hover:opacity-100 focus-visible:opacity-100 focus-visible:ring-2 focus-visible:ring-lime-600 focus-visible:ring-offset-2"
                    >
                      <svg
                        width="14"
                        height="14"
                        viewBox="0 0 14 14"
                        fill="none"
                        className="text-[#a1a1aa] hover:text-[#71717a]"
                        aria-hidden="true"
                      >
                        <path
                          d="M9.5 2.5L11.5 4.5M2 12H4L10.5 5.5L8.5 3.5L2 10V12Z"
                          stroke="currentColor"
                          strokeWidth="1.2"
                          strokeLinecap="round"
                          strokeLinejoin="round"
                        />
                      </svg>
                    </button>
                  )}
                </div>
              )}

              {/* Slot well (min-h prevents layout shift — D13) */}
              <SlotWell
                shot={shot}
                shotIndex={i}
                state={state}
                anyFilled={anyFilled}
                accept={uploadAccept}
                onFile={(file) => handleSlotFile(shot, file)}
                onCancel={() => handleCancel(sid)}
                onReplace={() => handleReplace(sid)}
                onRetry={() => handleRetry(shot)}
                onKeepMatch={() => handleKeepMatch(sid)}
                onSaveNote={(note) => handleSaveNote(sid, note)}
              />

              {/* WS4: Multi-clip strip — only when clip_count > 1 */}
              {clipCount > 1 && (
                <div className="mt-2">
                  {/* Progress */}
                  <div className="mb-1.5 text-xs text-[#71717a]">
                    {totalFilled} of {clipCount} clips filmed
                  </div>
                  {/* Extra clip chips */}
                  {extraClips.length > 0 && (
                    <div className="mb-2 flex flex-wrap gap-1.5">
                      {extraClips.map((a) => {
                        const name = a.gcs_path.split("/").pop() ?? a.gcs_path;
                        return (
                          <span
                            key={a.gcs_path}
                            className="flex items-center gap-1 rounded border border-lime-200 bg-lime-50 px-2 py-0.5 text-xs text-lime-800"
                          >
                            <span>✓</span>
                            <span className="max-w-[160px] truncate">{name}</span>
                            <button
                              type="button"
                              aria-label={`Remove clip ${name}`}
                              onClick={() => void handleRemoveExtraClip(sid, a.gcs_path)}
                              className="ml-0.5 text-lime-600 hover:text-lime-800 focus-visible:ring-2 focus-visible:ring-lime-600 focus-visible:ring-offset-2"
                            >
                              ✕
                            </button>
                          </span>
                        );
                      })}
                    </div>
                  )}
                  {/* Add more clips input */}
                  {remaining > 0 && (
                    <label className="inline-flex cursor-pointer items-center gap-1.5 rounded-[8px] border border-dashed border-zinc-200 px-3 py-1.5 text-xs text-[#71717a] hover:border-zinc-400 focus-within:ring-2 focus-within:ring-lime-600 focus-within:ring-offset-2">
                      {multiUploadBusy[sid] ? (
                        <span>Uploading…</span>
                      ) : (
                        <>
                          <span>+ Add {remaining > 1 ? `${remaining} more` : "1 more"}</span>
                          <input
                            type="file"
                            accept={uploadAccept}
                            multiple
                            className="sr-only"
                            onChange={(e) => void handleExtraClipFiles(shot, e.target.files)}
                          />
                        </>
                      )}
                    </label>
                  )}
                </div>
              )}
            </div>
          );
        })}
      </div>

      {/* Extra footage strip */}
      <div className="mt-3 border-t border-zinc-100 pt-3">
        <div className="mb-1 flex items-baseline gap-2">
          <span className="text-xs font-medium text-[#3f3f46]">Extra footage</span>
          <span className="text-xs text-[#a1a1aa]">optional</span>
        </div>
        <p className="mb-3 text-xs text-[#71717a]">
          Got more good moments? Add them — the editor will use the best parts.
        </p>
        {/* Pool chips */}
        {poolPaths.length > 0 && (
          <div className="mb-2 flex flex-wrap gap-2">
            {poolPaths.map((a) => {
              const name = a.gcs_path.split("/").pop() ?? a.gcs_path;
              return (
                <span
                  key={a.gcs_path}
                  className="flex items-center gap-1 rounded border border-zinc-200 bg-zinc-50 px-2 py-0.5 text-xs text-[#3f3f46]"
                >
                  <span className="max-w-[200px] truncate">{name}</span>
                  <button
                    type="button"
                    aria-label={`Remove ${name}`}
                    onClick={() => handlePoolRemove(a.gcs_path)}
                    className="ml-0.5 rounded focus-visible:ring-2 focus-visible:ring-lime-600 focus-visible:ring-offset-2"
                  >
                    ✕
                  </button>
                </span>
              );
            })}
          </div>
        )}
        {/* Pool upload input */}
        <label className="inline-flex cursor-pointer items-center gap-1.5 rounded-[8px] border border-dashed border-zinc-200 px-3 py-1.5 text-xs text-[#71717a] hover:border-zinc-400 focus-within:ring-2 focus-within:ring-lime-600 focus-within:ring-offset-2">
          <span>+ Add clips</span>
          <input
            type="file"
            accept={uploadAccept}
            multiple
            className="sr-only"
            onChange={(e) => handlePoolFiles(e.target.files)}
          />
        </label>
      </div>
    </div>
  );
}

// ── SlotWell ─────────────────────────────────────────────────────────────────

interface SlotWellProps {
  shot: FilmingShot;
  shotIndex: number;
  state: SlotState;
  anyFilled: boolean;
  accept: string;
  onFile: (file: File) => void;
  onCancel: () => void;
  onReplace: () => void;
  onRetry: () => void;
  onKeepMatch: () => void;
  onSaveNote: (note: string) => Promise<void>;
}

function SlotWell({ shot, shotIndex, state, anyFilled, accept, onFile, onCancel, onReplace, onRetry, onKeepMatch, onSaveNote }: SlotWellProps) {
  const inputRef = useRef<HTMLInputElement>(null);

  const ariaLabel = `Upload shot ${shotIndex + 1}: ${shot.what}`;

  const { phase } = state;

  if (phase === "idle") {
    const softened = anyFilled;
    return (
      <label
        className={[
          "flex min-h-[56px] cursor-pointer items-center justify-center rounded-[10px] border border-dashed px-4 py-3 text-sm transition-colors",
          softened
            ? "border-zinc-200 text-[#a1a1aa] hover:border-zinc-400 hover:text-[#71717a]"
            : "border-zinc-200 text-[#71717a] hover:border-lime-600 hover:text-lime-700",
          "focus-within:ring-2 focus-within:ring-lime-600 focus-within:ring-offset-2",
        ].join(" ")}
      >
        <span>
          {softened
            ? "Optional — add if you filmed it"
            : "Upload this shot — or drag a file here"}
        </span>
        <input
          ref={inputRef}
          type="file"
          accept={accept}
          className="sr-only"
          aria-label={ariaLabel}
          onChange={(e) => {
            const file = e.target.files?.[0];
            if (file) onFile(file);
            // Reset so the same file can be re-selected after Replace.
            e.target.value = "";
          }}
        />
      </label>
    );
  }

  if (phase === "uploading" || phase === "committing") {
    const prog = state.progress ?? 0;
    const filename = state.filename ?? "";
    return (
      <div className="min-h-[56px] rounded-[10px] border border-zinc-100 bg-[#fafaf8] px-4 py-3">
        <div className="mb-1 flex items-center justify-between">
          <span className="max-w-[200px] truncate text-xs text-[#71717a]">{filename}</span>
          {phase === "uploading" && (
            <button
              type="button"
              onClick={onCancel}
              className="text-xs text-[#71717a] underline underline-offset-2 hover:text-[#0c0c0e] focus-visible:ring-2 focus-visible:ring-lime-600 focus-visible:ring-offset-2"
            >
              Cancel
            </button>
          )}
        </div>
        {/* 4px progress bar (D13) */}
        <div className="h-1 w-full overflow-hidden rounded-full bg-zinc-100">
          <div
            className="h-full rounded-full bg-lime-600 transition-[width]"
            style={{ width: `${Math.round(prog * 100)}%` }}
          />
        </div>
        {phase === "committing" && (
          <p className="mt-1 text-xs text-[#71717a]">Saving…</p>
        )}
      </div>
    );
  }

  if (phase === "filled") {
    const filename = state.filename ?? "";
    const duration = state.durationLabel;
    return (
      <div className="min-h-[56px]">
        <div className="flex items-center gap-3">
          {/* Thumbnail only available when local file is in memory (fresh upload, not reload) */}
          {state.objectUrl && (
            <div className="h-10 w-16 shrink-0 overflow-hidden rounded border border-zinc-200 bg-zinc-100">
              {state.mediaKind === "image" ? (
                // eslint-disable-next-line @next/next/no-img-element
                <img src={state.objectUrl} alt="" className="h-full w-full object-cover" />
              ) : (
                <video
                  src={state.objectUrl}
                  className="h-full w-full object-cover"
                  muted
                  playsInline
                  preload="metadata"
                />
              )}
            </div>
          )}
          {/* Chip — provisional (machine-matched) reads dashed, "keep?" pending;
              a kept/user clip reads solid lime with the ✓. */}
          {state.machineMatched ? (
            <span className="flex min-w-0 items-center gap-1 rounded border border-dashed border-lime-300 bg-white px-2 py-0.5 text-xs text-lime-800">
              <span className="max-w-[160px] truncate">{filename}</span>
              <span className="shrink-0 text-lime-700">· Matched — keep?</span>
            </span>
          ) : (
            <span className="flex min-w-0 items-center gap-1 rounded border border-lime-200 bg-lime-50 px-2 py-0.5 text-xs text-lime-800">
              <span>✓</span>
              <span className="max-w-[200px] truncate">{filename}</span>
              {duration && <span className="shrink-0 text-lime-700"> · {duration}</span>}
            </span>
          )}
          {state.machineMatched ? (
            <>
              <button
                type="button"
                onClick={onKeepMatch}
                className="shrink-0 text-xs font-medium text-lime-700 underline underline-offset-2 hover:text-lime-800 focus-visible:ring-2 focus-visible:ring-lime-600 focus-visible:ring-offset-2"
              >
                Keep
              </button>
              <button
                type="button"
                onClick={onReplace}
                className="shrink-0 text-xs text-[#71717a] underline underline-offset-2 hover:text-[#0c0c0e] focus-visible:ring-2 focus-visible:ring-lime-600 focus-visible:ring-offset-2"
              >
                Swap
              </button>
            </>
          ) : (
            <button
              type="button"
              onClick={onReplace}
              className="shrink-0 text-xs text-[#71717a] underline underline-offset-2 hover:text-[#0c0c0e] focus-visible:ring-2 focus-visible:ring-lime-600 focus-visible:ring-offset-2"
            >
              Replace
            </button>
          )}
        </div>
        <ClipNoteControl note={state.userNote ?? ""} onSave={onSaveNote} />
      </div>
    );
  }

  if (phase === "error") {
    const filename = state.filename ?? "";
    return (
      <div className="flex min-h-[56px] items-center gap-2">
        <span className="flex items-center gap-1 rounded border border-zinc-200 bg-zinc-50 px-2 py-0.5 text-xs text-[#3f3f46]">
          <span className="max-w-[200px] truncate">{filename}</span>
        </span>
        <button
          type="button"
          onClick={onRetry}
          className="text-xs text-[#71717a] underline underline-offset-2 hover:text-[#0c0c0e] focus-visible:ring-2 focus-visible:ring-lime-600 focus-visible:ring-offset-2"
        >
          Upload failed · Retry
        </button>
      </div>
    );
  }

  return null;
}

// ── ClipNoteControl ──────────────────────────────────────────────────────────
// Optional creator context on an attached clip (dogfood feedback #3).
// Link-reveal — one visible input at a time, with a VISIBLE label (never
// placeholder-as-label); saved notes collapse to italic zinc + Edit. Saving a
// note re-runs the brief read server-side.
// Exported: the uninstructed (no-shot-list) clip list on the item page reuses it.

export function ClipNoteControl({
  note,
  onSave,
}: {
  note: string;
  onSave: (note: string) => Promise<void>;
}) {
  // Unique per instance — this control renders once per slot AND once per clip
  // in the uninstructed list, so a hardcoded id produced duplicate DOM ids and
  // broke label association for screen readers (review finding).
  const inputId = useId();
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(note);
  const [saveState, setSaveState] = useState<"idle" | "saving" | "saved" | "error">("idle");

  async function save() {
    const value = draft.trim().slice(0, 200);
    setSaveState("saving");
    try {
      await onSave(value);
      setSaveState("saved");
      setEditing(false);
      setTimeout(() => setSaveState("idle"), 2500);
    } catch {
      setSaveState("error");
    }
  }

  if (editing) {
    return (
      <form
        className="mt-2"
        onSubmit={(e) => {
          e.preventDefault();
          void save();
        }}
      >
        <label className="mb-1 block text-xs font-medium text-[#3f3f46]" htmlFor={inputId}>
          Context <span className="font-normal text-[#a1a1aa]">optional</span>
        </label>
        <div className="flex gap-2">
          <input
            id={inputId}
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            maxLength={200}
            placeholder="e.g. a famous vegan restaurant in Buenos Aires"
            className="w-full rounded border border-zinc-200 bg-white px-2 py-1.5 text-xs text-[#0c0c0e] placeholder:text-[#a1a1aa] focus:border-lime-600 focus:outline-none"
          />
          <button
            type="submit"
            disabled={saveState === "saving"}
            className="shrink-0 text-xs font-medium text-lime-700 underline underline-offset-2 disabled:opacity-50"
          >
            {saveState === "saving" ? "Saving…" : "Save"}
          </button>
          <button
            type="button"
            onClick={() => {
              setDraft(note);
              setEditing(false);
              setSaveState("idle");
            }}
            className="shrink-0 text-xs text-[#71717a] underline underline-offset-2"
          >
            Cancel
          </button>
        </div>
        {saveState === "error" && (
          <p className="mt-1 rounded border border-zinc-200 bg-white px-2 py-1 text-xs text-[#3f3f46]">
            Couldn&apos;t save — try again.
          </p>
        )}
      </form>
    );
  }

  if (note) {
    return (
      <p className="mt-1.5 text-xs italic text-[#71717a]">
        &ldquo;{note}&rdquo;{" "}
        <button
          type="button"
          onClick={() => {
            setDraft(note);
            setEditing(true);
          }}
          className="not-italic text-lime-700 underline-offset-2 hover:underline"
        >
          Edit
        </button>
        {saveState === "saved" && <span className="ml-2 not-italic text-[#a1a1aa]">Saved ✓</span>}
      </p>
    );
  }

  return (
    <button
      type="button"
      onClick={() => setEditing(true)}
      className="mt-1.5 text-xs text-[#71717a] underline-offset-2 hover:text-[#0c0c0e] hover:underline"
    >
      + Add context
    </button>
  );
}
