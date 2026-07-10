"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import type { ContentPlan, PlanItem, PlanItemStatus } from "@/lib/plan-api";
import { addIdea, deleteIdea, generateIdeasWithAI } from "@/lib/plan-api";

interface IdeasHomeProps {
  plan: ContentPlan;
  onRefresh: () => void | Promise<unknown>;
}

type MutState = "idle" | "saving" | "error";

const CONFIRM_DELETE_STATUSES = new Set<PlanItemStatus>(["ready", "generating", "rerolling"]);

export function IdeasHome({ plan, onRefresh }: IdeasHomeProps) {
  const [buffer, setBuffer] = useState("");
  const [mutState, setMutState] = useState<MutState>("idle");
  const [aiGenerating, setAiGenerating] = useState(false);
  const [aiError, setAiError] = useState<string | null>(null);
  const [confirmingId, setConfirmingId] = useState<string | null>(null);

  const items = [...(plan.items ?? [])].sort((a, b) => b.position - a.position);
  const readyCount = items.filter((item) => item.status === "ready").length;
  const renderingCount = items.filter(
    (item) => item.status === "generating" || item.status === "rerolling",
  ).length;
  const hasStats = readyCount > 0 || renderingCount > 0;
  const serverGenerating = plan.plan_status === "generating";
  const showGeneratingRow = aiGenerating || serverGenerating;
  const generateDisabled = showGeneratingRow;

  async function commitBuffer(raw: string) {
    const text = raw.trim();
    if (!text) return;
    setBuffer("");
    setMutState("saving");
    try {
      await addIdea(plan.id, text);
      setMutState("idle");
      setAiError(null);
      await onRefresh();
    } catch {
      setMutState("error");
    }
  }

  async function handleDelete(itemId: string) {
    setMutState("saving");
    try {
      await deleteIdea(itemId);
      setConfirmingId(null);
      setMutState("idle");
      await onRefresh();
    } catch {
      setMutState("error");
    }
  }

  async function handleGenerate() {
    if (generateDisabled) return;
    setAiError(null);
    setAiGenerating(true);
    try {
      await generateIdeasWithAI(plan.id);
      await onRefresh();
    } catch (err) {
      setAiError(err instanceof Error ? err.message : "Couldn't generate ideas");
    } finally {
      setAiGenerating(false);
    }
  }

  function requestDelete(item: PlanItem) {
    if (CONFIRM_DELETE_STATUSES.has(item.status)) {
      setConfirmingId(item.id);
      return;
    }
    void handleDelete(item.id);
  }

  return (
    <section aria-labelledby="ideas-heading" className="flex flex-col gap-7">
      <header>
        <div className="flex items-baseline justify-between gap-6">
          <h1
            id="ideas-heading"
            className="font-display text-[44px] font-medium leading-none text-[#0c0c0e]"
          >
            Ideas
          </h1>
          {hasStats && (
            <p className="text-right text-[13px] leading-snug text-[#71717a]">
              {readyCount > 0 && (
                <>
                  <b className="font-semibold text-lime-700">{readyCount} ready</b>
                  {" · "}
                </>
              )}
              {renderingCount > 0 && (
                <>
                  <span>{renderingCount} rendering</span>
                  {" · "}
                </>
              )}
              <Link
                href="/library"
                aria-label="View ready videos in your library"
                className="underline-offset-4 hover:underline focus-visible:outline-2 focus-visible:outline-[#0c0c0e]"
              >
                View ready videos →
              </Link>
            </p>
          )}
        </div>
        <p className="mt-3 text-[14px] text-[#71717a]">
          Every idea here becomes a video.
        </p>
      </header>

      <div className="flex flex-col gap-3">
        <div className="flex flex-col gap-3 sm:flex-row">
          <div className="flex min-h-[44px] flex-1 items-center gap-2 rounded-lg border border-dashed border-zinc-300 bg-white px-3 py-2 focus-within:border-lime-500/60">
            <span className="text-[14px] font-bold leading-none text-lime-700" aria-hidden>
              +
            </span>
            <input
              type="text"
              value={buffer}
              onChange={(e) => setBuffer(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  e.preventDefault();
                  void commitBuffer(buffer);
                }
              }}
              onBlur={() => void commitBuffer(buffer)}
              placeholder="A video idea, rough is fine…"
              aria-label="Add a new idea"
              className="min-w-0 flex-1 bg-transparent text-[13px] text-[#0c0c0e] placeholder-zinc-400 focus:outline-none"
            />
          </div>
          <button
            type="button"
            onClick={() => void handleGenerate()}
            disabled={generateDisabled}
            className="flex min-h-[44px] items-center justify-center gap-1.5 rounded-lg border border-zinc-200 bg-white px-4 py-2 text-[12px] font-medium text-[#71717a] transition-colors hover:border-lime-400 hover:text-lime-700 focus-visible:outline-2 focus-visible:outline-[#0c0c0e] disabled:cursor-not-allowed disabled:opacity-50 sm:w-auto"
          >
            <span aria-hidden>✦</span>
            Generate with AI
          </button>
        </div>
        {mutState === "error" && (
          <p className="text-[12px] leading-snug text-[#71717a]" role="alert">
            Couldn&apos;t save.
          </p>
        )}
        {aiError && (
          <p className="text-[12px] leading-snug text-[#71717a]" role="alert">
            {aiError}
          </p>
        )}
        {plan.plan_status === "failed" && (
          <div className="rounded-lg border border-dashed border-zinc-200 px-4 py-3 text-[13px] text-[#71717a]">
            That idea didn&apos;t come through.{" "}
            <button
              type="button"
              onClick={() => void handleGenerate()}
              disabled={generateDisabled}
              className="font-medium text-[#3f3f46] underline underline-offset-4 hover:text-lime-700 focus-visible:outline-2 focus-visible:outline-[#0c0c0e] disabled:cursor-not-allowed disabled:opacity-50"
            >
              Try again
            </button>
          </div>
        )}
      </div>

      <ol aria-label="Ideas ledger" className="flex flex-col">
        {showGeneratingRow && <GeneratingLedgerRow />}
        {items.length === 0 && !showGeneratingRow && (
          <li className="border-t border-zinc-100 py-6">
            <p className="font-display text-[16px] font-medium text-[#0c0c0e]">
              Pitch your first idea.
            </p>
          </li>
        )}
        {items.map((item, index) => (
          <IdeaLedgerRow
            key={item.id}
            item={item}
            ordinal={index + 1}
            confirming={confirmingId === item.id}
            onCancelConfirm={() => setConfirmingId(null)}
            onDelete={() => requestDelete(item)}
            onConfirmDelete={() => void handleDelete(item.id)}
          />
        ))}
      </ol>
    </section>
  );
}

function GeneratingLedgerRow() {
  return (
    <li
      role="status"
      aria-live="polite"
      className="grid min-h-[48px] grid-cols-[1fr_auto] items-start gap-3 border-t border-zinc-100 py-2.5 min-[380px]:grid-cols-[2rem_1fr_auto]"
    >
      <span
        aria-hidden
        className="relative mt-1 hidden h-2 w-2 min-[380px]:flex"
      >
        <span className="absolute inline-flex h-full w-full rounded-full bg-lime-600 opacity-60 motion-safe:animate-ping" />
        <span className="relative inline-flex h-2 w-2 rounded-full bg-lime-600" />
      </span>
      <div className="flex min-w-0 flex-col gap-2">
        <div className="h-4 w-2/3 rounded bg-[linear-gradient(110deg,#f4f4f5,45%,#e4e4e7,55%,#f4f4f5)] bg-[length:200%_100%] motion-safe:animate-shimmer" />
        <span className="text-[12px] text-[#71717a]">Nova is writing an idea…</span>
      </div>
      <span className="sr-only">Generating</span>
    </li>
  );
}

interface IdeaLedgerRowProps {
  item: PlanItem;
  ordinal: number;
  confirming: boolean;
  onCancelConfirm: () => void;
  onDelete: () => void;
  onConfirmDelete: () => void;
}

function IdeaLedgerRow({
  item,
  ordinal,
  confirming,
  onCancelConfirm,
  onDelete,
  onConfirmDelete,
}: IdeaLedgerRowProps) {
  const previousStatusRef = useRef(item.status);
  const [pulseReady, setPulseReady] = useState(false);

  useEffect(() => {
    const previousStatus = previousStatusRef.current;
    if (previousStatus !== "ready" && item.status === "ready") {
      setPulseReady(true);
      const timeout = window.setTimeout(() => setPulseReady(false), 1000);
      previousStatusRef.current = item.status;
      return () => window.clearTimeout(timeout);
    }
    previousStatusRef.current = item.status;
    return undefined;
  }, [item.status]);

  return (
    <li className="group grid min-h-[48px] grid-cols-[1fr_auto] items-start gap-3 border-t border-zinc-100 py-2.5 min-[380px]:grid-cols-[2rem_1fr_auto_auto]">
      <span
        aria-hidden
        className="hidden w-8 shrink-0 font-display text-[20px] italic leading-none text-zinc-300 min-[380px]:block"
      >
        {ordinal}
      </span>
      <Link
        href={`/plan/items/${item.id}`}
        className="line-clamp-2 min-w-0 text-[15px] leading-snug text-[#0c0c0e] transition-colors hover:text-lime-700 focus-visible:rounded focus-visible:outline-2 focus-visible:outline-[#0c0c0e]"
      >
        {item.idea}
      </Link>
      <div className="flex min-h-[28px] items-start justify-end">
        {confirming ? (
          <DeleteConfirm onKeep={onCancelConfirm} onDelete={onConfirmDelete} />
        ) : (
          <StatusSlot status={item.status} pulseReady={pulseReady} />
        )}
      </div>
      <button
        type="button"
        onClick={onDelete}
        aria-label={`Remove idea: ${item.idea}`}
        className="flex h-[28px] w-[28px] shrink-0 items-center justify-center rounded text-[#a1a1aa] opacity-100 transition-opacity hover:text-[#0c0c0e] focus-visible:opacity-100 focus-visible:outline-2 focus-visible:outline-[#0c0c0e] [@media(hover:hover)]:opacity-0 [@media(hover:hover)]:group-hover:opacity-100 [@media(hover:hover)]:focus-visible:opacity-100"
      >
        ×
      </button>
    </li>
  );
}

function StatusSlot({
  status,
  pulseReady,
}: {
  status: PlanItemStatus;
  pulseReady: boolean;
}) {
  if (status === "ready") {
    return (
      <span
        className={`whitespace-nowrap rounded-full border border-lime-200 bg-lime-50 px-2 py-0.5 text-[11px] font-medium text-lime-800 ${pulseReady ? "motion-safe:animate-pulse" : ""}`}
      >
        Ready to post
      </span>
    );
  }
  if (status === "generating" || status === "rerolling") {
    return (
      <span className="flex items-center gap-2 whitespace-nowrap text-[12px] text-[#71717a]">
        Rendering…
        <span className="relative flex h-2 w-2" aria-hidden>
          <span className="absolute inline-flex h-full w-full rounded-full bg-lime-600 opacity-60 motion-safe:animate-ping" />
          <span className="relative inline-flex h-2 w-2 rounded-full bg-lime-600" />
        </span>
      </span>
    );
  }
  if (status === "failed") {
    return (
      <span className="text-right text-[12px] leading-snug text-[#71717a]">
        Didn&apos;t render — open to retry
      </span>
    );
  }
  if (status === "awaiting_clips") {
    return <span className="whitespace-nowrap text-[12px] text-[#71717a]">Needs footage</span>;
  }
  return <span className="whitespace-nowrap text-[12px] text-[#a1a1aa]">Plan this →</span>;
}

function DeleteConfirm({
  onKeep,
  onDelete,
}: {
  onKeep: () => void;
  onDelete: () => void;
}) {
  return (
    <div className="text-right text-[12px] leading-snug text-[#71717a]">
      <span>Delete idea? It has a video — </span>
      <button
        type="button"
        onClick={onKeep}
        className="underline underline-offset-4 hover:text-[#0c0c0e] focus-visible:outline-2 focus-visible:outline-[#0c0c0e]"
      >
        Keep
      </button>
      <span> / </span>
      <button
        type="button"
        onClick={onDelete}
        className="underline underline-offset-4 hover:text-[#0c0c0e] focus-visible:outline-2 focus-visible:outline-[#0c0c0e]"
      >
        Delete
      </button>
    </div>
  );
}
