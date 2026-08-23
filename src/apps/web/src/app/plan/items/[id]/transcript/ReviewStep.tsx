"use client";

import { useEffect, useRef, useState } from "react";
import { InkButton } from "@/components/ui/InkButton";
import { Button } from "@/components/ui/button";
import { fmtTime, type AudioTake } from "@/hooks/useAudioRecorder";

/**
 * Step 5 — Review.
 *
 * Show the recorded take (audio element) + a plain informational line comparing
 * the actual take length to the estimate. There is NO gate and NO coach
 * machinery — this is a receipt, not a grader. "Use this take" closes the
 * takeover; "Retake" returns to Record.
 */
export default function ReviewStep({
  take,
  estimateS,
  onUse,
  onRetake,
}: {
  take: AudioTake;
  estimateS: number;
  onUse: () => void;
  onRetake: () => void;
}) {
  const [takeDurationS, setTakeDurationS] = useState<number | null>(null);
  const audioRef = useRef<HTMLAudioElement | null>(null);

  // Read the actual take length off the audio element once metadata loads.
  useEffect(() => {
    const el = audioRef.current;
    if (!el) return;
    const onMeta = () => {
      if (Number.isFinite(el.duration)) setTakeDurationS(el.duration);
    };
    el.addEventListener("loadedmetadata", onMeta);
    if (Number.isFinite(el.duration) && el.duration > 0) onMeta();
    return () => el.removeEventListener("loadedmetadata", onMeta);
  }, [take.url]);

  return (
    <div className="max-w-xl">
      <p className="mb-3 text-xs font-medium uppercase tracking-wide text-lime-700">
        Your narration
      </p>
      <h1 className="font-display text-3xl leading-snug text-[#0c0c0e]">
        Review your recording
      </h1>

      <div className="mt-6 rounded-2xl border border-zinc-200 bg-white p-5 shadow-sm">
        <audio ref={audioRef} src={take.url} controls className="w-full">
          <track kind="captions" />
        </audio>
      </div>

      <p className="mt-4 text-sm text-[#71717a]">
        Your recording:{" "}
        <span className="tabular-nums text-[#3f3f46]">
          {takeDurationS != null ? fmtTime(takeDurationS) : "—"}
        </span>{" "}
        · target length {fmtTime(estimateS)}
      </p>

      <div className="mt-8 flex flex-wrap items-center gap-4">
        <InkButton onClick={onUse}>Use this recording</InkButton>
        <Button
          type="button"
          variant="link"
          onClick={onRetake}
          className="h-auto p-0 text-sm font-normal text-[#71717a] underline underline-offset-4 hover:text-[#0c0c0e] hover:no-underline"
        >
          Record again
        </Button>
      </div>
    </div>
  );
}
