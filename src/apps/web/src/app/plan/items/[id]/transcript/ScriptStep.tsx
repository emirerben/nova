"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { InkButton } from "@/components/ui/InkButton";
import { fmtTime } from "@/hooks/useAudioRecorder";
import {
  generateTranscriptScript,
  saveTranscriptScript,
  type ScriptResponse,
} from "@/lib/transcript-api";

/** Optimistic client line split (sentence-ish) — the server refines it on save.
 *  Newline-splitting alone collapses the prose script to a single line. */
function splitLinesClient(text: string): string[] {
  return text
    .split(/(?<=[.!?])\s+|\n+/)
    .map((l) => l.trim())
    .filter(Boolean);
}

export interface ScriptState {
  version: number;
  text: string;
  readTimeS: number;
  lines: string[];
}

/**
 * Step 3 — Script.
 *
 * On enter, POST script (brief, footage_summary, answers, duration_s). Shows the
 * script in a Fraunces card with a lime read-time badge, an inline edit textarea
 * (the ONLY autofocus surface in this takeover), a "Rewrite" ghost, and the
 * "Record this →" primary. If a take was already recorded against an older
 * version, a soft zinc warn appears.
 */
export default function ScriptStep({
  itemId,
  brief,
  footageSummary,
  answers,
  durationS,
  initialScript,
  recordedAgainstVersion,
  onScript,
  onRecord,
}: {
  itemId: string;
  brief: string;
  footageSummary: string | null;
  answers: string[];
  durationS: number;
  /** Reuse an already-generated script when returning from a later step. */
  initialScript: ScriptState | null;
  /** The version a take was recorded against, if any (null = none yet). */
  recordedAgainstVersion: number | null;
  onScript: (script: ScriptState) => void;
  onRecord: () => void;
}) {
  const [script, setScript] = useState<ScriptState | null>(initialScript);
  const [draft, setDraft] = useState(initialScript?.text ?? "");
  const [loading, setLoading] = useState(!initialScript);
  const [rewriting, setRewriting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Guard so we only auto-generate once even under React StrictMode double-mount.
  const generatedRef = useRef(false);

  const adopt = useCallback(
    (res: ScriptResponse) => {
      const next: ScriptState = {
        version: res.version,
        text: res.text,
        readTimeS: res.read_time_s,
        lines: res.lines,
      };
      setScript(next);
      setDraft(res.text);
      onScript(next);
    },
    [onScript],
  );

  useEffect(() => {
    if (initialScript || generatedRef.current) return;
    generatedRef.current = true;
    setLoading(true);
    setError(null);
    generateTranscriptScript(itemId, {
      brief,
      footage_summary: footageSummary,
      answers,
      duration_s: durationS,
    })
      .then(adopt)
      .catch((e: unknown) =>
        setError(e instanceof Error ? e.message : "Couldn't write the script."),
      )
      .finally(() => setLoading(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const rewrite = useCallback(async () => {
    if (rewriting) return;
    setRewriting(true);
    setError(null);
    try {
      const res = await generateTranscriptScript(itemId, {
        brief,
        footage_summary: footageSummary,
        answers,
        duration_s: durationS,
      });
      adopt(res);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Couldn't rewrite the script.");
    } finally {
      setRewriting(false);
    }
  }, [adopt, answers, brief, durationS, footageSummary, itemId, rewriting]);

  // Commit inline edits: update local state optimistically (so the teleprompter
  // and Record flow use the edit immediately), then persist to the DB so the saved
  // script matches what the creator finalized (source="edited") and re-entry keeps
  // it. On success we adopt the server's canonical sentence-split lines + read-time.
  const commitDraft = useCallback(async () => {
    if (!script || draft.trim() === script.text.trim()) return;
    const optimistic: ScriptState = {
      ...script,
      text: draft,
      lines: splitLinesClient(draft),
    };
    setScript(optimistic);
    onScript(optimistic);
    try {
      adopt(await saveTranscriptScript(itemId, draft));
    } catch {
      // Keep the optimistic edit; the next commit (or Record) retries the save.
    }
  }, [adopt, draft, itemId, onScript, script]);

  if (loading) {
    return (
      <div className="flex items-center gap-2 py-10">
        <span className="h-1.5 w-1.5 motion-safe:animate-ping rounded-full bg-lime-600" />
        <span className="text-sm text-[#71717a]">Writing your script…</span>
      </div>
    );
  }

  if (error && !script) {
    return (
      <div className="max-w-xl">
        <div className="rounded-lg border border-zinc-200 bg-white px-4 py-3">
          <p className="text-sm text-[#3f3f46]">{error}</p>
          <button
            type="button"
            onClick={() => void rewrite()}
            className="mt-2 text-xs text-[#71717a] underline underline-offset-4 hover:text-[#0c0c0e]"
          >
            Try again
          </button>
        </div>
      </div>
    );
  }

  if (!script) return null;

  const staleTake =
    recordedAgainstVersion !== null && recordedAgainstVersion !== script.version;

  return (
    <div className="max-w-2xl">
      <div className="flex items-center justify-between gap-4">
        <p className="text-xs font-medium uppercase tracking-wide text-lime-700">
          Your script
        </p>
        {/* Read-time badge — lime soft cell (DESIGN §2 pill / §-teleprompter) */}
        <span className="inline-flex items-center rounded-full border border-lime-200 bg-lime-50 px-3 py-1 text-xs font-medium text-lime-800">
          ≈ {fmtTime(script.readTimeS)} to read
        </span>
      </div>

      <div className="mt-4 rounded-2xl border border-zinc-200 bg-white p-6 shadow-sm">
        <textarea
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onBlur={() => void commitDraft()}
          autoFocus
          aria-label="Edit your script"
          rows={Math.max(6, draft.split("\n").length + 1)}
          className="w-full resize-none bg-transparent font-display text-xl leading-relaxed text-[#0c0c0e] focus:outline-none"
        />
      </div>

      {staleTake && (
        <p className="mt-3 text-sm text-[#71717a]">
          Heads up — your take was for the previous script. Record again to match this
          version.
        </p>
      )}
      {error && <p className="mt-3 text-sm text-red-600">{error}</p>}

      <div className="mt-8 flex flex-wrap items-center gap-4">
        <InkButton
          onClick={() =>
            void (async () => {
              await commitDraft();
              onRecord();
            })()
          }
        >
          Record this →
        </InkButton>
        <button
          type="button"
          onClick={() => void rewrite()}
          disabled={rewriting}
          className="text-sm text-[#71717a] underline underline-offset-4 hover:text-[#0c0c0e] disabled:opacity-50"
        >
          {rewriting ? "Rewriting…" : "Rewrite"}
        </button>
      </div>
    </div>
  );
}
