"use client";

import { useState, useRef, useCallback } from "react";
import { uploadGenerativeClip } from "@/lib/generative-api";

const MAX_CLIPS = 10;

interface UploadedClip {
  name: string;
  gcsPath: string;
  status: "uploading" | "done" | "error";
  objectUrl?: string;
}

export function EditUploadStep({
  onSubmit,
  onBack,
}: {
  onSubmit: (clipPaths: string[]) => void;
  onBack: () => void;
}) {
  const [clips, setClips] = useState<UploadedClip[]>([]);
  const inputRef = useRef<HTMLInputElement>(null);

  const handleFiles = useCallback(
    async (files: FileList) => {
      const toAdd = Array.from(files).slice(0, MAX_CLIPS - clips.length);
      for (const file of toAdd) {
        const objectUrl = URL.createObjectURL(file);
        const pending: UploadedClip = {
          name: file.name,
          gcsPath: "",
          status: "uploading",
          objectUrl,
        };
        setClips((prev) => [...prev, pending]);
        try {
          const result = await uploadGenerativeClip(file);
          setClips((prev) =>
            prev.map((c) =>
              c.name === file.name && c.status === "uploading"
                ? { ...c, gcsPath: result.gcs_path, status: "done" }
                : c,
            ),
          );
        } catch {
          setClips((prev) =>
            prev.map((c) =>
              c.name === file.name && c.status === "uploading"
                ? { ...c, status: "error" }
                : c,
            ),
          );
        }
      }
    },
    [clips.length],
  );

  const readyPaths = clips
    .filter((c) => c.status === "done")
    .map((c) => c.gcsPath);
  const atMax = clips.length >= MAX_CLIPS;

  return (
    <div className="flex flex-col gap-6 px-4 py-8 max-w-lg mx-auto animate-fade-up">
      <div className="border-l-4 border-lime-600 pl-4">
        <p className="font-display text-2xl text-[#0c0c0e]">Add your clips</p>
        <p className="text-sm text-[#71717a] mt-1">
          Up to {MAX_CLIPS} videos · from your camera roll
        </p>
      </div>

      {/* Upload affordance */}
      <input
        ref={inputRef}
        type="file"
        multiple
        accept="video/*"
        className="sr-only"
        onChange={(e) => {
          if (e.target.files) void handleFiles(e.target.files);
        }}
      />

      {!atMax && (
        <button
          onClick={() => inputRef.current?.click()}
          className="w-full rounded-2xl border-2 border-dashed border-[#e4e4e7] bg-[#fafaf8] hover:border-lime-600 hover:bg-lime-50 transition py-10 text-center focus:outline-none focus-visible:ring-2 focus-visible:ring-lime-600 min-h-[44px]"
        >
          <p className="text-[#71717a]">+ Add videos</p>
        </button>
      )}

      {atMax && (
        <p className="text-xs text-amber-700 text-center">
          10 clips max — remove one to add more
        </p>
      )}

      {/* Thumbnail grid */}
      {clips.length > 0 && (
        <div className="grid grid-cols-3 gap-2">
          {clips.map((clip, i) => (
            <div
              key={i}
              className="relative aspect-[9/16] rounded-lg bg-[#e4e4e7] overflow-hidden"
            >
              {clip.objectUrl && (
                <video
                  src={clip.objectUrl}
                  className="w-full h-full object-cover"
                  muted
                  playsInline
                />
              )}
              {clip.status === "uploading" && (
                <div className="absolute inset-0 bg-[#0c0c0e]/40 flex items-center justify-center">
                  <div className="w-5 h-5 border-2 border-white border-t-transparent rounded-full animate-spin motion-reduce:animate-none" />
                </div>
              )}
              {clip.status === "error" && (
                <div className="absolute inset-0 bg-red-900/40 flex items-center justify-center">
                  <button
                    onClick={() =>
                      setClips((prev) => prev.filter((_, idx) => idx !== i))
                    }
                    className="text-white text-xs underline"
                  >
                    retry
                  </button>
                </div>
              )}
              <button
                onClick={() =>
                  setClips((prev) => prev.filter((_, idx) => idx !== i))
                }
                className="absolute top-1 right-1 w-5 h-5 rounded-full bg-[#0c0c0e]/60 text-white text-xs flex items-center justify-center hover:bg-[#0c0c0e] focus:outline-none focus-visible:ring-2 focus-visible:ring-lime-600"
                aria-label="Remove clip"
              >
                ×
              </button>
            </div>
          ))}
        </div>
      )}

      <div className="flex gap-3">
        <button
          onClick={onBack}
          className="px-4 text-sm text-[#71717a] hover:text-[#0c0c0e] focus:outline-none focus-visible:ring-2 focus-visible:ring-lime-600 rounded min-h-[44px]"
        >
          ← back
        </button>
        <button
          onClick={() => onSubmit(readyPaths)}
          disabled={readyPaths.length === 0}
          className="flex-1 rounded-xl bg-lime-700 text-white py-3 font-medium hover:bg-lime-800 disabled:opacity-40 disabled:cursor-not-allowed focus:outline-none focus-visible:ring-2 focus-visible:ring-lime-600 min-h-[44px]"
        >
          Make my edit →
        </button>
      </div>
    </div>
  );
}
