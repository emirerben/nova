"use client";

import type { MutableRefObject } from "react";
import { useCallback, useEffect, useRef, useState } from "react";

export type AudioRecorderPhase = "idle" | "recording" | "review";

/** Whether the browser can record audio in-page (mic + MediaRecorder). */
export function mediaRecorderSupported(): boolean {
  return (
    typeof window !== "undefined" &&
    typeof navigator !== "undefined" &&
    !!navigator.mediaDevices?.getUserMedia &&
    typeof window.MediaRecorder !== "undefined"
  );
}

/** A finished take: the raw Blob plus a ready-made object URL for playback. */
export interface AudioTake {
  blob: Blob;
  url: string;
  /** Suggested upload filename ("voiceover.webm") — Blobs carry no name. */
  filename: string;
  /** True when the take came from a chosen file (already has a real name). */
  fromFile: boolean;
}

export interface UseAudioRecorderOptions {
  /** Fired once a take is ready (recorded OR uploaded). Receives the finished take. */
  onTake?: (take: AudioTake) => void;
  /** Fired when the take is cleared (reset / remove). */
  onClear?: () => void;
}

export interface UseAudioRecorder {
  /** Whether in-browser recording is available (mic + MediaRecorder). */
  recordSupported: boolean;
  phase: AudioRecorderPhase;
  /** Elapsed recording seconds (0 outside "recording"). */
  elapsed: number;
  /** Object URL of the current take, or null. */
  audioUrl: string | null;
  /** True while the current take has no source name (recorded, not uploaded). */
  micBlocked: boolean;
  /** The most recent take, or null. */
  take: AudioTake | null;
  /** Attach to a <canvas> to render the live waveform during recording. */
  canvasRef: MutableRefObject<HTMLCanvasElement | null>;
  /** Request the mic and begin recording. Sets micBlocked on permission failure. */
  start: () => Promise<void>;
  /** Stop the in-progress recording; a take fires via onTake afterwards. */
  stop: () => void;
  /** Accept a chosen audio file as the take (upload fallback). */
  useFile: (file: File) => void;
  /** Clear the current take and return to idle. */
  reset: () => void;
}

/**
 * Reusable mic-capture + MediaRecorder + live-waveform + upload-fallback hook.
 *
 * Extracted from generative/VoiceRecorder.tsx (behaviour preserved byte-for-byte):
 * - `start()` requests the mic, wires an AnalyserNode for the waveform, and begins
 *   a MediaRecorder. On permission denial it sets `micBlocked` and returns to idle.
 * - `stop()` finalises a Blob and surfaces it via `onTake`.
 * - `useFile(file)` is the upload fallback (also fires `onTake`).
 * - The consumer owns the actual upload — the hook only produces the take + URL.
 * - Waveform draws to the `canvasRef` canvas; identical zinc-100/zinc-600 look.
 *
 * The hook does NOT perform the upload itself so it can back both the generative
 * VoiceRecorder bar and the teleprompter recorder, which upload/attach differently.
 */
export function useAudioRecorder(opts: UseAudioRecorderOptions = {}): UseAudioRecorder {
  const { onTake, onClear } = opts;

  const [recordSupported, setRecordSupported] = useState(false);
  const [phase, setPhase] = useState<AudioRecorderPhase>("idle");
  const [elapsed, setElapsed] = useState(0);
  const [audioUrl, setAudioUrl] = useState<string | null>(null);
  const [micBlocked, setMicBlocked] = useState(false);
  const [take, setTake] = useState<AudioTake | null>(null);

  const recorderRef = useRef<MediaRecorder | null>(null);
  const chunksRef = useRef<Blob[]>([]);
  const streamRef = useRef<MediaStream | null>(null);
  const audioCtxRef = useRef<AudioContext | null>(null);
  const analyserRef = useRef<AnalyserNode | null>(null);
  const rafRef = useRef<number | null>(null);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const canvasRef = useRef<HTMLCanvasElement | null>(null);

  // Keep the latest callbacks in refs so recorder.onstop (a stable closure)
  // always fires the current onTake without re-subscribing.
  const onTakeRef = useRef(onTake);
  onTakeRef.current = onTake;
  const onClearRef = useRef(onClear);
  onClearRef.current = onClear;

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

  useEffect(() => {
    return () => {
      stopTracksAndAudio();
      if (audioUrl) URL.revokeObjectURL(audioUrl);
    };
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
      ctx.fillStyle = "#f4f4f5"; // zinc-100 — light canvas
      ctx.fillRect(0, 0, w, h);
      ctx.lineWidth = 2;
      ctx.strokeStyle = "#52525b"; // zinc-600
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

  const setTakeFrom = useCallback((next: AudioTake) => {
    setAudioUrl((prev) => {
      if (prev && prev !== next.url) URL.revokeObjectURL(prev);
      return next.url;
    });
    setTake(next);
    setPhase("review");
    onTakeRef.current?.(next);
  }, []);

  const start = useCallback(async () => {
    setMicBlocked(false);
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      streamRef.current = stream;

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
        setTakeFrom({ blob, url, filename: "voiceover.webm", fromFile: false });
      };
      recorder.start();

      setElapsed(0);
      timerRef.current = setInterval(() => setElapsed((e) => e + 1), 1000);
      setPhase("recording");
      drawWaveform();
    } catch (e) {
      stopTracksAndAudio();
      setMicBlocked(true);
      setPhase("idle");
      void e;
    }
  }, [drawWaveform, setTakeFrom, stopTracksAndAudio]);

  const stop = useCallback(() => {
    recorderRef.current?.stop();
    recorderRef.current = null;
  }, []);

  const useFile = useCallback(
    (file: File) => {
      const url = URL.createObjectURL(file);
      setTakeFrom({ blob: file, url, filename: file.name, fromFile: true });
    },
    [setTakeFrom],
  );

  const reset = useCallback(() => {
    stopTracksAndAudio();
    setAudioUrl((prev) => {
      if (prev) URL.revokeObjectURL(prev);
      return null;
    });
    setTake(null);
    setPhase("idle");
    setElapsed(0);
    setMicBlocked(false);
    onClearRef.current?.();
  }, [stopTracksAndAudio]);

  return {
    recordSupported,
    phase,
    elapsed,
    audioUrl,
    micBlocked,
    take,
    canvasRef,
    start,
    stop,
    useFile,
    reset,
  };
}

/** mm:ss formatter shared by recorder surfaces. */
export function fmtTime(secs: number): string {
  const m = Math.floor(secs / 60);
  const s = Math.floor(secs % 60);
  return `${m.toString().padStart(2, "0")}:${s.toString().padStart(2, "0")}`;
}
