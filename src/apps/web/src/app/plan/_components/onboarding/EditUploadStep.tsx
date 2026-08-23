"use client";

import { useState, useRef, useCallback } from "react";
import { Button } from "@/components/ui/button";
import { uploadGenerativeClip } from "@/lib/generative-api";

const MAX_CLIPS = 10;

interface UploadedClip {
  name: string;
  file: File;
  gcsPath: string;
  status: "uploading" | "done" | "error";
  objectUrl?: string;
}

export function EditUploadStep({
  onSubmit,
  onBack,
}: {
  onSubmit: (clips: { gcsPath: string; objectUrl: string }[]) => void;
  onBack?: () => void;
}) {
  const [clips, setClips] = useState<UploadedClip[]>([]);
  const inputRef = useRef<HTMLInputElement>(null);

  const uploadClip = useCallback(async (file: File, objectUrl: string) => {
    setClips((prev) =>
      prev.map((clip) =>
        clip.objectUrl === objectUrl ? { ...clip, status: "uploading" } : clip,
      ),
    );
    try {
      const result = await uploadGenerativeClip(file);
      setClips((prev) =>
        prev.map((clip) =>
          clip.objectUrl === objectUrl
            ? { ...clip, gcsPath: result.gcs_path, status: "done" }
            : clip,
        ),
      );
    } catch {
      setClips((prev) =>
        prev.map((clip) =>
          clip.objectUrl === objectUrl ? { ...clip, status: "error" } : clip,
        ),
      );
    }
  }, []);

  const handleFiles = useCallback(
    async (files: FileList) => {
      const toAdd = Array.from(files).slice(0, MAX_CLIPS - clips.length);
      for (const file of toAdd) {
        const objectUrl = URL.createObjectURL(file);
        const pending: UploadedClip = {
          name: file.name,
          file,
          gcsPath: "",
          status: "uploading",
          objectUrl,
        };
        setClips((prev) => [...prev, pending]);
        await uploadClip(file, objectUrl);
      }
    },
    [clips.length, uploadClip],
  );

  const readyClips = clips
    .filter((c) => c.status === "done")
    .map((c) => ({ gcsPath: c.gcsPath, objectUrl: c.objectUrl ?? "" }));
  const readyPaths = readyClips.map((c) => c.gcsPath);
  const atMax = clips.length >= MAX_CLIPS;

  return (
    <div className="flex flex-col gap-6 px-4 py-8 max-w-lg mx-auto animate-fade-up">
      <div className="border-l-4 border-lime-600 pl-4">
        <p className="font-display text-2xl text-[#0c0c0e]">Add your clips</p>
        <p className="text-sm text-[#71717a] mt-1">
          You can add up to {MAX_CLIPS} clips from your camera roll.
        </p>
      </div>

      {/* Upload affordance */}
      <input
        ref={inputRef}
        type="file"
        multiple
        accept="video/*"
        aria-label="Add video clips"
        className="sr-only"
        onChange={(e) => {
          if (e.target.files) void handleFiles(e.target.files);
        }}
      />

      {!atMax && (
        <Button
          type="button"
          variant="outline"
          onClick={() => inputRef.current?.click()}
          className="h-auto min-h-[44px] w-full rounded-2xl border-2 border-dashed border-[#e4e4e7] bg-[#ffffff] py-10 text-center hover:border-lime-600 hover:bg-lime-50 focus-visible:ring-lime-600"
        >
          <p className="text-[#71717a]">Add videos</p>
        </Button>
      )}

      {atMax && (
        <p className="text-xs text-amber-700 text-center">
          You can add up to 10 clips. Remove one to add another.
        </p>
      )}

      {/* Thumbnail grid */}
      {clips.length > 0 && (
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
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
                <div className="absolute inset-0 flex flex-col items-center justify-center gap-2 bg-red-950/70 p-2 text-center" role="alert">
                  <p className="text-xs text-white">
                    {clip.name} couldn&apos;t upload. Use an MP4 or MOV video.
                  </p>
                  <Button
                    type="button"
                    variant="ghost"
                    onClick={() => void uploadClip(clip.file, clip.objectUrl ?? "")}
                    className="h-auto min-h-11 min-w-11 p-0 text-xs text-white underline hover:bg-transparent hover:text-white sm:min-h-0 sm:min-w-0"
                  >
                    Retry upload
                  </Button>
                </div>
              )}
              <Button
                type="button"
                variant="ghost"
                size="icon"
                onClick={() =>
                  setClips((prev) => prev.filter((_, idx) => idx !== i))
                }
                className="absolute top-1 right-1 flex h-11 w-11 items-center justify-center rounded-full bg-[#0c0c0e]/60 p-0 text-xs text-white hover:bg-[#0c0c0e] focus-visible:ring-lime-600 sm:h-5 sm:w-5"
                aria-label="Remove clip"
              >
                ×
              </Button>
            </div>
          ))}
        </div>
      )}

      <div className="flex gap-3">
        {onBack && (
          <Button
            type="button"
            variant="ghost"
            onClick={onBack}
            className="h-auto min-h-[44px] rounded px-4 text-sm text-[#71717a] hover:bg-transparent hover:text-[#0c0c0e] focus-visible:ring-lime-600"
          >
            Back
          </Button>
        )}
        <Button
          type="button"
          onClick={() => onSubmit(readyClips)}
          disabled={readyPaths.length === 0}
          className="h-auto min-h-[44px] flex-1 rounded-xl bg-lime-700 py-3 font-medium text-white hover:bg-lime-800 disabled:cursor-not-allowed disabled:opacity-40 focus-visible:ring-lime-600"
        >
          Create video
        </Button>
      </div>
    </div>
  );
}
