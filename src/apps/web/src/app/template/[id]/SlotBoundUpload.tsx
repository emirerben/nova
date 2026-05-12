"use client";

import { useRef, useState } from "react";
import {
  type SlotSummary,
  type TemplateListItem,
  createTemplateJob,
  getBatchPresignedUrls,
  normaliseMimeType,
  uploadFileToGcs,
  uploadTemplatePhoto,
} from "@/lib/api";

const VIDEO_MIME = ["video/mp4", "video/quicktime"];
const PHOTO_MIME = [
  "image/jpeg",
  "image/png",
  "image/webp",
  "image/heic",
  "image/heif",
];
const VIDEO_MAX_BYTES = 4 * 1024 * 1024 * 1024;
const PHOTO_MAX_BYTES = 25 * 1024 * 1024;

// Build a "the {other type} goes in slot {N}" hint when the template has
// at least one slot of the opposite media_type. Empty string for templates
// where every slot expects the same media_type — there's no other slot to
// direct the user to.
function oppositeSlotHint(slot: SlotSummary, allSlots: SlotSummary[]): {
  inline: string;
  sentence: string;
} {
  const opposite = allSlots.find((s) => s.media_type !== slot.media_type);
  if (!opposite) return { inline: "", sentence: "" };
  const otherWord = opposite.media_type === "photo" ? "photo" : "video";
  return {
    inline: `the ${otherWord} goes in slot ${opposite.position}`,
    sentence: `${otherWord.charAt(0).toUpperCase()}${otherWord.slice(1)}s go in slot ${opposite.position}.`,
  };
}

export function slotHelperText(slot: SlotSummary, allSlots: SlotSummary[]): string {
  const isPhoto = slot.media_type === "photo";
  const { inline } = oppositeSlotHint(slot, allSlots);
  const base = isPhoto
    ? "Still image — jpg, png, webp, or heic"
    : "Moving clip — mp4 or mov";
  return inline ? `${base} (${inline}).` : `${base}.`;
}

export function mismatchError(slot: SlotSummary, allSlots: SlotSummary[]): string {
  const isPhoto = slot.media_type === "photo";
  const { sentence } = oppositeSlotHint(slot, allSlots);
  const base = isPhoto
    ? `Slot ${slot.position} needs a photo (jpg/png/webp/heic).`
    : `Slot ${slot.position} needs a video (mp4/mov).`;
  return sentence ? `${base} ${sentence}` : base;
}

type SlotState = {
  slot: SlotSummary;
  file: File | null;
  uploading: boolean;
  error: string | null;
  gcsPath: string | null;
};

type Phase = "ready" | "uploading" | "enqueuing" | "error";

interface Props {
  template: TemplateListItem;
  inputs: Record<string, string>;
  onJobCreated: (jobId: string) => void;
}

export default function SlotBoundUpload({ template, inputs, onJobCreated }: Props) {
  const [slots, setSlots] = useState<SlotState[]>(
    () => [...template.slots]
      .sort((a, b) => a.position - b.position)
      .map((s) => ({ slot: s, file: null, uploading: false, error: null, gcsPath: null }))
  );
  const [phase, setPhase] = useState<Phase>("ready");
  const [submitError, setSubmitError] = useState<string | null>(null);
  // One stable ref array for all file inputs — sized to slot count on mount.
  const inputRefs = useRef<Array<HTMLInputElement | null>>([]);

  function patchSlot(idx: number, patch: Partial<SlotState>) {
    setSlots((prev) => prev.map((s, i) => (i === idx ? { ...s, ...patch } : s)));
  }

  function pickFile(idx: number, file: File) {
    const slot = slots[idx].slot;
    const allowed = slot.media_type === "photo" ? PHOTO_MIME : VIDEO_MIME;
    const maxBytes = slot.media_type === "photo" ? PHOTO_MAX_BYTES : VIDEO_MAX_BYTES;

    // HEIC sometimes shows as empty content_type; allow .heic/.heif extensions
    const ext = file.name.split(".").pop()?.toLowerCase() ?? "";
    const heicByExt = slot.media_type === "photo" && (ext === "heic" || ext === "heif");
    const mimeOk = allowed.includes(file.type) || heicByExt;

    if (!mimeOk) {
      patchSlot(idx, {
        file: null,
        gcsPath: null,
        error: mismatchError(slot, template.slots),
      });
      return;
    }
    if (file.size > maxBytes) {
      patchSlot(idx, {
        file: null,
        gcsPath: null,
        error: `File exceeds ${Math.round(maxBytes / 1024 / 1024)}MB limit`,
      });
      return;
    }
    patchSlot(idx, { file, error: null, gcsPath: null });
  }

  async function uploadOne(idx: number, current: SlotState): Promise<string> {
    if (!current.file) throw new Error("No file");
    patchSlot(idx, { uploading: true, error: null });

    try {
      if (current.slot.media_type === "photo") {
        const { gcs_path } = await uploadTemplatePhoto({
          templateId: template.id,
          slotPosition: current.slot.position,
          file: current.file,
        });
        patchSlot(idx, { uploading: false, gcsPath: gcs_path });
        return gcs_path;
      }

      const { urls } = await getBatchPresignedUrls([
        {
          filename: `slot_${current.slot.position}.${current.file.name.split(".").pop() || "mp4"}`,
          content_type: normaliseMimeType(current.file.type),
          file_size_bytes: current.file.size,
        },
      ]);
      await uploadFileToGcs(urls[0].upload_url, current.file);
      patchSlot(idx, { uploading: false, gcsPath: urls[0].gcs_path });
      return urls[0].gcs_path;
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Upload failed";
      patchSlot(idx, { uploading: false, error: msg });
      throw err;
    }
  }

  async function handleSubmit() {
    if (slots.some((s) => !s.file)) {
      setSubmitError("Fill every slot before submitting.");
      return;
    }
    setSubmitError(null);
    setPhase("uploading");

    try {
      // Sequential — keeps server-side image conversion load predictable.
      const gcsPaths: string[] = [];
      for (let i = 0; i < slots.length; i++) {
        const path = await uploadOne(i, slots[i]);
        gcsPaths.push(path);
      }

      setPhase("enqueuing");
      const { job_id } = await createTemplateJob({
        template_id: template.id,
        clip_gcs_paths: gcsPaths,
        selected_platforms: ["tiktok", "instagram", "youtube"],
        inputs,
      });
      onJobCreated(job_id);
    } catch (err) {
      setPhase("error");
      setSubmitError(err instanceof Error ? err.message : "Something went wrong");
    }
  }

  const allReady = slots.every((s) => s.file);
  const submitting = phase === "uploading" || phase === "enqueuing";

  return (
    <div className="space-y-4">
      <p className="text-zinc-400 text-sm">
        This template needs {slots.length} clips in order. Upload one for each slot below.
      </p>

      <ol className="space-y-3">
        {slots.map((s, i) => {
          const isPhoto = s.slot.media_type === "photo";
          const accept = isPhoto
            ? "image/jpeg,image/png,image/webp,image/heic,image/heif,.heic,.heif"
            : "video/mp4,video/quicktime";
          return (
            <li
              key={s.slot.position}
              className="border border-zinc-800 rounded-lg p-4 bg-zinc-900/40"
            >
              <div className="flex items-center justify-between mb-1">
                <span className="text-sm font-medium">
                  Slot {s.slot.position} · {isPhoto ? "Photo" : "Video"}
                </span>
                <span className="text-xs text-zinc-500">
                  ~{s.slot.target_duration_s.toFixed(1)}s
                </span>
              </div>
              <p className="text-xs text-zinc-500 mb-2">
                {slotHelperText(s.slot, template.slots)}
              </p>

              <button
                onClick={() => inputRefs.current[i]?.click()}
                disabled={submitting}
                className="w-full px-3 py-2 rounded-md text-sm border border-zinc-700 hover:border-zinc-500 transition-colors disabled:opacity-50 text-left text-zinc-300"
              >
                {s.file ? s.file.name : isPhoto ? "Choose a photo…" : "Choose a video…"}
              </button>
              <input
                ref={(el) => { inputRefs.current[i] = el; }}
                type="file"
                accept={accept}
                className="hidden"
                onChange={(e) => {
                  const f = e.target.files?.[0];
                  if (f) pickFile(i, f);
                  e.target.value = "";
                }}
              />

              {s.uploading && (
                <p className="mt-2 text-xs text-blue-400">Uploading…</p>
              )}
              {s.gcsPath && !s.uploading && (
                <p className="mt-2 text-xs text-green-400">✓ Uploaded</p>
              )}
              {s.error && (
                <p className="mt-2 text-xs text-red-400">{s.error}</p>
              )}
            </li>
          );
        })}
      </ol>

      {submitError && (
        <div className="bg-red-900/40 border border-red-700 rounded-lg px-4 py-3 text-sm text-red-300">
          {submitError}
        </div>
      )}

      <button
        onClick={handleSubmit}
        disabled={!allReady || submitting}
        className="w-full py-3 rounded-lg bg-white text-black text-sm font-semibold disabled:bg-zinc-700 disabled:text-zinc-400 transition-colors"
      >
        {phase === "uploading"
          ? "Uploading…"
          : phase === "enqueuing"
            ? "Starting render…"
            : "Generate"}
      </button>
    </div>
  );
}
