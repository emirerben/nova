"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import { InkButton } from "@/components/ui/InkButton";
import {
  getTranscriptAnalyze,
  startTranscriptAnalyze,
  type AnalyzeResult,
} from "@/lib/transcript-api";

const POLL_MS = 2000;
const MAX_POLL_MS = 2 * 60 * 1000;

export interface BriefResult {
  brief: string;
  durationS: number;
  footageSummary: string | null;
}

/**
 * Step 1 — Brief.
 *
 * "What's this video about?" → on Continue, POST analyze then poll the footage
 * read (PULSE-style loading). Stores duration_s + footage_summary. A failed read
 * degrades quietly ("we'll write from your brief") and proceeds with a null
 * summary. When the item has no clips, we lead with the action (add clips) rather
 * than a broken analyze call.
 */
export default function BriefStep({
  itemId,
  hasClips,
  fallbackDurationS,
  onDone,
}: {
  itemId: string;
  hasClips: boolean;
  /** Used when analyze fails / returns no duration (e.g. estimate from the guide). */
  fallbackDurationS: number;
  onDone: (result: BriefResult) => void;
}) {
  const [brief, setBrief] = useState("");
  const [analyzing, setAnalyzing] = useState(false);
  const [softNote, setSoftNote] = useState<string | null>(null);
  const cancelledRef = useRef(false);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Stop any in-flight poll if the step unmounts (e.g. rail navigation).
  useEffect(() => {
    return () => {
      cancelledRef.current = true;
      if (timerRef.current) clearTimeout(timerRef.current);
    };
  }, []);

  const poll = useCallback(
    (analyzeId: string): Promise<AnalyzeResult> =>
      new Promise((resolve, reject) => {
        const started = Date.now();
        const tick = async () => {
          if (cancelledRef.current) return;
          try {
            const res = await getTranscriptAnalyze(itemId, analyzeId);
            if (res.status === "ready" || res.status === "failed") {
              resolve(res);
              return;
            }
          } catch (e) {
            reject(e);
            return;
          }
          if (Date.now() - started > MAX_POLL_MS) {
            resolve({ status: "failed" });
            return;
          }
          timerRef.current = setTimeout(() => void tick(), POLL_MS);
        };
        void tick();
      }),
    [itemId],
  );

  const handleContinue = useCallback(async () => {
    const trimmed = brief.trim();
    if (!trimmed || analyzing) return;
    setAnalyzing(true);
    setSoftNote(null);
    cancelledRef.current = false;
    try {
      const { analyze_id } = await startTranscriptAnalyze(itemId);
      const res = await poll(analyze_id);
      if (res.status === "failed") {
        onDone({
          brief: trimmed,
          durationS: fallbackDurationS,
          footageSummary: null,
        });
        return;
      }
      onDone({
        brief: trimmed,
        durationS: res.duration_s ?? fallbackDurationS,
        footageSummary: res.footage_summary ?? null,
      });
    } catch (e) {
      // Networked failure of the analyze kickoff itself — still let them write
      // from the brief, but surface a quiet note.
      setSoftNote("We couldn't read your footage — we'll write from your brief.");
      onDone({ brief: trimmed, durationS: fallbackDurationS, footageSummary: null });
      void e;
    } finally {
      setAnalyzing(false);
    }
  }, [analyzing, brief, fallbackDurationS, itemId, onDone, poll]);

  // No clips → lead with the action, not the absence (DESIGN §9).
  if (!hasClips) {
    return (
      <div className="max-w-xl">
        <p className="mb-3 text-xs font-medium uppercase tracking-wide text-lime-700">
          Get a transcript
        </p>
        <h1 className="font-display text-3xl leading-snug text-[#0c0c0e]">
          Add your clips first, and Nova will write what to say over them.
        </h1>
        <p className="mt-4 text-[#71717a]">
          The transcript is tuned to your footage — its length, its moments. Once a
          clip is attached, come back here.
        </p>
        <div className="mt-8">
          <Link
            href={`/plan/items/${itemId}`}
            className="inline-flex min-h-[44px] items-center rounded-full bg-[#0c0c0e] px-9 py-[15px] text-[15px] font-semibold text-white transition-opacity hover:opacity-80"
          >
            Add clips →
          </Link>
        </div>
      </div>
    );
  }

  if (analyzing) {
    // PULSE tier (DESIGN §7): lime ping dot + serif line + no bar / no ETA.
    return (
      <div className="max-w-xl">
        <div className="flex items-center gap-2 py-2" role="status" aria-live="polite">
          <span className="h-1.5 w-1.5 motion-safe:animate-ping rounded-full bg-lime-600" />
          <span className="text-sm text-[#71717a]">Reading your footage…</span>
        </div>
        <h1 className="mt-4 font-display text-3xl leading-snug text-[#0c0c0e]">
          Watching your clips so the words land on the right moments.
        </h1>
        <div className="mt-8 h-24 w-full motion-safe:animate-pulse rounded-2xl border border-zinc-200 bg-zinc-50" />
      </div>
    );
  }

  return (
    <div className="max-w-xl">
      <p className="mb-3 text-xs font-medium uppercase tracking-wide text-lime-700">
        Get a transcript
      </p>
      <h1 className="font-display text-3xl leading-snug text-[#0c0c0e] sm:text-4xl">
        What&apos;s this video about?
      </h1>
      <p className="mt-4 text-[#71717a]">
        A sentence or two is plenty — Nova reads your footage and writes what to say
        over it.
      </p>

      <textarea
        value={brief}
        onChange={(e) => setBrief(e.target.value)}
        rows={4}
        autoFocus
        placeholder="e.g. My morning routine as a new dad — the chaos and the quiet moments."
        aria-label="What's this video about"
        onKeyDown={(e) => {
          if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
            e.preventDefault();
            void handleContinue();
          }
        }}
        className="mt-8 w-full resize-none rounded-2xl border border-zinc-200 bg-white px-4 py-3 text-[15px] text-[#0c0c0e] placeholder-zinc-400 focus:border-lime-500/60 focus:outline-none"
      />

      {softNote && <p className="mt-3 text-sm text-[#71717a]">{softNote}</p>}

      <div className="mt-8">
        <InkButton onClick={() => void handleContinue()} disabled={!brief.trim()}>
          Continue →
        </InkButton>
      </div>
    </div>
  );
}
