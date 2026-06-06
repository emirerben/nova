"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { uploadVoiceover } from "@/lib/generative-api";

type Phase = "idle" | "recording" | "review";

function fmtTime(secs: number): string {
  const m = Math.floor(secs / 60);
  const s = Math.floor(secs % 60);
  return `${m.toString().padStart(2, "0")}:${s.toString().padStart(2, "0")}`;
}

// MediaRecorder is unavailable in some browsers (older Safari, locked-down WebViews).
// We resolve this once on mount (client-only) so the record UI is hidden but the
// file-upload fallback always stays usable.
function mediaRecorderSupported(): boolean {
  return (
    typeof window !== "undefined" &&
    typeof navigator !== "undefined" &&
    !!navigator.mediaDevices?.getUserMedia &&
    typeof window.MediaRecorder !== "undefined"
  );
}

/**
 * Add a voiceover to a generative edit: record in-browser (mic + live waveform)
 * OR upload an audio file. On a finished take or chosen file we upload it and
 * call `onVoiceover(gcsPath)`; removing the take calls `onVoiceover(null)`.
 * Presentation + upload only — the page owns the resulting gcs_path.
 */
export function VoiceRecorder({
  onVoiceover,
}: {
  onVoiceover: (gcsPath: string | null) => void;
}) {
  const [recordSupported, setRecordSupported] = useState(false);
  const [phase, setPhase] = useState<Phase>("idle");
  const [elapsed, setElapsed] = useState(0);
  const [audioUrl, setAudioUrl] = useState<string | null>(null);
  const [uploading, setUploading] = useState(false);
  const [uploadedName, setUploadedName] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [micBlocked, setMicBlocked] = useState(false);

  const recorderRef = useRef<MediaRecorder | null>(null);
  const chunksRef = useRef<Blob[]>([]);
  const streamRef = useRef<MediaStream | null>(null);
  const audioCtxRef = useRef<AudioContext | null>(null);
  const analyserRef = useRef<AnalyserNode | null>(null);
  const rafRef = useRef<number | null>(null);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const canvasRef = useRef<HTMLCanvasElement | null>(null);

  useEffect(() => {
    setRecordSupported(mediaRecorderSupported());
  }, []);

  const stopTracksAndAudio = useCallback(() => {
    if (rafRef.current != null) {
      cancelAnimationFrame(rafRef.current);
      rafRef.current = null;
    }
    if (timerRef.current != null) {
      clearInterval(timerRef.current);
      timerRef.current = null;
    }
    streamRef.current?.getTracks().forEach((t) => t.stop());
    streamRef.current = null;
    if (audioCtxRef.current) {
      audioCtxRef.current.close().catch(() => {});
      audioCtxRef.current = null;
    }
    analyserRef.current = null;
  }, []);

  // Clean up everything on unmount, and revoke the object URL.
  useEffect(() => {
    return () => {
      stopTracksAndAudio();
      if (audioUrl) URL.revokeObjectURL(audioUrl);
    };
    // audioUrl intentionally captured at unmount via closure refresh below.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [stopTracksAndAudio]);

  const drawWaveform = useCallback(() => {
    const canvas = canvasRef.current;
    const analyser = analyserRef.current;
    if (!canvas || !analyser) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    const bufferLength = analyser.frequencyBinCount;
    const data = new Uint8Array(bufferLength);

    const render = () => {
      rafRef.current = requestAnimationFrame(render);
      analyser.getByteTimeDomainData(data);
      const w = canvas.width;
      const h = canvas.height;
      ctx.clearRect(0, 0, w, h);
      ctx.fillStyle = "#09090b"; // zinc-950
      ctx.fillRect(0, 0, w, h);
      ctx.lineWidth = 2;
      ctx.strokeStyle = "#a1a1aa"; // zinc-400
      ctx.beginPath();
      const slice = w / bufferLength;
      let x = 0;
      for (let i = 0; i < bufferLength; i++) {
        const v = data[i] / 128.0;
        const y = (v * h) / 2;
        if (i === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
        x += slice;
      }
      ctx.lineTo(w, h / 2);
      ctx.stroke();
    };
    render();
  }, []);

  const doUpload = useCallback(
    async (file: File | Blob, filename: string, displayName: string) => {
      setUploading(true);
      setError(null);
      try {
        const r = await uploadVoiceover(file, filename);
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

  const startRecording = useCallback(async () => {
    setError(null);
    setMicBlocked(false);
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      streamRef.current = stream;

      // Live waveform via AnalyserNode.
      const AudioCtor =
        window.AudioContext ||
        (window as unknown as { webkitAudioContext: typeof AudioContext })
          .webkitAudioContext;
      const audioCtx = new AudioCtor();
      audioCtxRef.current = audioCtx;
      const source = audioCtx.createMediaStreamSource(stream);
      const analyser = audioCtx.createAnalyser();
      analyser.fftSize = 2048;
      source.connect(analyser);
      analyserRef.current = analyser;

      chunksRef.current = [];
      const recorder = new MediaRecorder(stream);
      recorderRef.current = recorder;
      recorder.ondataavailable = (ev) => {
        if (ev.data.size > 0) chunksRef.current.push(ev.data);
      };
      recorder.onstop = () => {
        const blob = new Blob(chunksRef.current, {
          type: chunksRef.current[0]?.type || "audio/webm",
        });
        stopTracksAndAudio();
        const url = URL.createObjectURL(blob);
        setAudioUrl((prev) => {
          if (prev) URL.revokeObjectURL(prev);
          return url;
        });
        setPhase("review");
        void doUpload(blob, "voiceover.webm", "Recorded voiceover");
      };
      recorder.start();

      setElapsed(0);
      timerRef.current = setInterval(() => setElapsed((e) => e + 1), 1000);
      setPhase("recording");
      drawWaveform();
    } catch (e) {
      // Permission denied / no device: keep the file-upload path usable.
      stopTracksAndAudio();
      setMicBlocked(true);
      setPhase("idle");
      setError(null);
      void e;
    }
  }, [doUpload, drawWaveform, stopTracksAndAudio]);

  const stopRecording = useCallback(() => {
    recorderRef.current?.stop();
    recorderRef.current = null;
  }, []);

  const reset = useCallback(() => {
    stopTracksAndAudio();
    setAudioUrl((prev) => {
      if (prev) URL.revokeObjectURL(prev);
      return null;
    });
    setPhase("idle");
    setElapsed(0);
    setUploadedName(null);
    setError(null);
    onVoiceover(null);
  }, [onVoiceover, stopTracksAndAudio]);

  const handleFile = useCallback(
    (files: FileList | null) => {
      const file = files?.[0];
      if (!file) return;
      const url = URL.createObjectURL(file);
      setAudioUrl((prev) => {
        if (prev) URL.revokeObjectURL(prev);
        return url;
      });
      setPhase("review");
      void doUpload(file, file.name, file.name);
    },
    [doUpload],
  );

  const liveStatus =
    phase === "recording"
      ? `Recording, ${fmtTime(elapsed)}`
      : phase === "review"
        ? uploading
          ? "Uploading voiceover"
          : uploadedName
            ? `Voiceover ready: ${uploadedName}`
            : "Voiceover take ready"
        : "No voiceover";

  return (
    <div className="space-y-3">
      {/* Screen-reader-friendly live region for recording/upload state. */}
      <p aria-live="polite" className="sr-only">
        {liveStatus}
      </p>

      {micBlocked && (
        <div className="rounded border border-amber-700/60 bg-amber-950/40 px-3 py-2 text-sm text-amber-200">
          Mic blocked. Upload an audio file instead, or enable mic in your browser
          settings.
        </div>
      )}
      {error && (
        <div className="rounded border border-red-700 bg-red-950/50 px-3 py-2 text-sm text-red-200">
          {error}
        </div>
      )}

      {phase === "idle" && (
        <div className="flex flex-wrap items-center gap-3">
          {recordSupported && (
            <button
              type="button"
              onClick={startRecording}
              aria-label="Record voiceover"
              aria-pressed={false}
              className="inline-flex min-h-[44px] items-center gap-2 rounded border border-zinc-700 bg-zinc-900 px-4 py-2 text-sm text-zinc-200 hover:bg-zinc-800"
            >
              <span aria-hidden className="h-2.5 w-2.5 rounded-full bg-red-500" />
              Record
            </button>
          )}
          <label className="inline-flex min-h-[44px] cursor-pointer items-center rounded border border-zinc-700 bg-zinc-900 px-4 py-2 text-sm text-zinc-200 hover:bg-zinc-800">
            Upload audio
            <input
              type="file"
              accept="audio/*"
              className="sr-only"
              onChange={(e) => handleFile(e.target.files)}
            />
          </label>
        </div>
      )}

      {phase === "recording" && (
        <div className="space-y-3">
          <div className="flex items-center justify-between">
            <span className="inline-flex items-center gap-2 text-sm text-zinc-300">
              <span
                aria-hidden
                className="h-2.5 w-2.5 motion-safe:animate-pulse rounded-full bg-red-500"
              />
              Recording
            </span>
            <span className="text-sm tabular-nums text-zinc-400">{fmtTime(elapsed)}</span>
          </div>
          <canvas
            ref={canvasRef}
            aria-hidden
            width={640}
            height={64}
            className="h-16 w-full rounded border border-zinc-800 bg-zinc-950"
          />
          <button
            type="button"
            onClick={stopRecording}
            aria-label="Stop recording"
            aria-pressed
            className="inline-flex min-h-[44px] items-center gap-2 rounded border border-zinc-700 bg-zinc-900 px-4 py-2 text-sm text-zinc-200 hover:bg-zinc-800"
          >
            <span aria-hidden className="h-2.5 w-2.5 rounded-sm bg-zinc-200" />
            Stop
          </button>
        </div>
      )}

      {phase === "review" && (
        <div className="space-y-3">
          {audioUrl && (
            <audio src={audioUrl} controls className="w-full">
              <track kind="captions" />
            </audio>
          )}
          <div className="flex flex-wrap items-center gap-3">
            {uploading && <span className="text-sm text-zinc-500">Uploading…</span>}
            {!uploading && uploadedName && (
              <span className="text-sm text-zinc-400">{uploadedName}</span>
            )}
            <button
              type="button"
              onClick={reset}
              aria-label="Remove voiceover"
              className="inline-flex min-h-[44px] items-center rounded border border-zinc-700 bg-zinc-900 px-4 py-2 text-sm text-zinc-200 hover:bg-zinc-800"
            >
              {recordSupported ? "Retake / remove" : "Remove"}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
