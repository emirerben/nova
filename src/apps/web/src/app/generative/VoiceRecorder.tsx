"use client";

import { useCallback, useState } from "react";
import { uploadVoiceover } from "@/lib/generative-api";
import { fmtTime, useAudioRecorder, type AudioTake } from "@/hooks/useAudioRecorder";

/**
 * Add a voiceover to a generative edit: record in-browser (mic + live waveform)
 * OR upload an audio file. Rendered on the light editorial system.
 *
 * Mic capture / MediaRecorder / waveform / upload-fallback all live in the
 * shared `useAudioRecorder` hook; this component owns the upload + display copy.
 */
export function VoiceRecorder({
  onVoiceover,
}: {
  onVoiceover: (gcsPath: string | null) => void;
}) {
  const [uploading, setUploading] = useState(false);
  const [uploadedName, setUploadedName] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const doUpload = useCallback(
    async (take: AudioTake, displayName: string) => {
      setUploading(true);
      setError(null);
      try {
        const r = await uploadVoiceover(take.blob, take.filename);
        setUploadedName(displayName);
        onVoiceover(r.gcs_path);
      } catch (e) {
        setError(e instanceof Error ? e.message : "Voiceover upload failed");
        onVoiceover(null);
      } finally {
        setUploading(false);
      }
    },
    [onVoiceover],
  );

  const onTake = useCallback(
    (take: AudioTake) => {
      setError(null);
      void doUpload(take, take.fromFile ? take.filename : "Recorded voiceover");
    },
    [doUpload],
  );

  const onClear = useCallback(() => {
    setUploadedName(null);
    setError(null);
    onVoiceover(null);
  }, [onVoiceover]);

  const rec = useAudioRecorder({ onTake, onClear });

  const startRecording = useCallback(() => {
    setError(null);
    void rec.start();
  }, [rec]);

  const liveStatus =
    rec.phase === "recording"
      ? `Recording, ${fmtTime(rec.elapsed)}`
      : rec.phase === "review"
        ? uploading
          ? "Uploading voiceover"
          : uploadedName
            ? `Voiceover ready: ${uploadedName}`
            : "Voiceover take ready"
        : "No voiceover";

  return (
    <div className="space-y-3">
      <p aria-live="polite" className="sr-only">
        {liveStatus}
      </p>

      {rec.micBlocked && (
        <div className="rounded border border-zinc-200 bg-[#fafaf8] px-3 py-2 text-sm text-[#3f3f46]">
          Mic blocked. Upload an audio file instead, or enable mic in your browser
          settings.
        </div>
      )}
      {error && (
        <div className="rounded border border-zinc-200 bg-[#fafaf8] px-3 py-2 text-sm text-red-600">
          {error}
        </div>
      )}

      {rec.phase === "idle" && (
        <div className="flex flex-wrap items-center gap-3">
          {rec.recordSupported && (
            <button
              type="button"
              onClick={startRecording}
              aria-label="Record voiceover"
              aria-pressed={false}
              className="inline-flex min-h-[44px] items-center gap-2 rounded-full border border-zinc-200 bg-white px-4 py-2 text-sm text-[#3f3f46] hover:border-zinc-400"
            >
              <span aria-hidden className="h-2.5 w-2.5 rounded-full bg-red-500" />
              Record
            </button>
          )}
          <label className="inline-flex min-h-[44px] cursor-pointer items-center rounded-full border border-zinc-200 bg-white px-4 py-2 text-sm text-[#3f3f46] hover:border-zinc-400">
            Upload audio
            <input
              type="file"
              accept="audio/*,.mp4,.m4a,.mp3,.wav,.webm,.ogg,.aac"
              className="sr-only"
              onChange={(e) => {
                const file = e.target.files?.[0];
                if (file) rec.useFile(file);
              }}
            />
          </label>
        </div>
      )}

      {rec.phase === "recording" && (
        <div className="space-y-3">
          <div className="flex items-center justify-between">
            <span className="inline-flex items-center gap-2 text-sm text-[#3f3f46]">
              <span
                aria-hidden
                className="h-2.5 w-2.5 motion-safe:animate-pulse rounded-full bg-red-500"
              />
              Recording
            </span>
            <span className="text-sm tabular-nums text-[#71717a]">{fmtTime(rec.elapsed)}</span>
          </div>
          <canvas
            ref={rec.canvasRef}
            aria-hidden
            width={640}
            height={64}
            className="h-16 w-full rounded border border-zinc-200 bg-zinc-100"
          />
          <button
            type="button"
            onClick={rec.stop}
            aria-label="Stop recording"
            aria-pressed
            className="inline-flex min-h-[44px] items-center gap-2 rounded-full border border-zinc-200 bg-white px-4 py-2 text-sm text-[#3f3f46] hover:border-zinc-400"
          >
            <span aria-hidden className="h-2.5 w-2.5 rounded-sm bg-[#3f3f46]" />
            Stop
          </button>
        </div>
      )}

      {rec.phase === "review" && (
        <div className="space-y-3">
          {rec.audioUrl && (
            <audio src={rec.audioUrl} controls className="w-full">
              <track kind="captions" />
            </audio>
          )}
          <div className="flex flex-wrap items-center gap-3">
            {uploading && <span className="text-sm text-[#71717a]">Uploading…</span>}
            {!uploading && uploadedName && (
              <span className="text-sm text-[#71717a]">{uploadedName}</span>
            )}
            <button
              type="button"
              onClick={rec.reset}
              aria-label="Remove voiceover"
              className="inline-flex min-h-[44px] items-center rounded-full border border-zinc-200 bg-white px-4 py-2 text-sm text-[#3f3f46] hover:border-zinc-400"
            >
              {rec.recordSupported ? "Retake / remove" : "Remove"}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
