"use client";

import { useState, useRef } from "react";
import Link from "next/link";
import type { ContentPlan } from "@/lib/plan-api";
import { addIdea, deleteIdea, generateIdeasWithAI } from "@/lib/plan-api";

interface IdeasSidebarProps {
  plan: ContentPlan;
  onRefresh: () => void;
}

type MutState = "idle" | "saving" | "error";

export function IdeasSidebar({ plan, onRefresh }: IdeasSidebarProps) {
  const [buffer, setBuffer] = useState("");
  const [mutState, setMutState] = useState<MutState>("idle");
  const [aiGenerating, setAiGenerating] = useState(false);
  const [aiError, setAiError] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  const ideaItems = [...(plan.items ?? [])]
    .filter((i) => i.day_index === null)
    .sort((a, b) => a.position - b.position);
  const plannedItems = [...(plan.items ?? [])]
    .filter((i) => i.day_index !== null)
    .sort((a, b) => (a.day_index ?? 0) - (b.day_index ?? 0));
  const isEmpty = ideaItems.length === 0;
  const serverGenerating = plan.plan_status === "generating";

  async function commitBuffer(raw: string) {
    const text = raw.trim();
    if (!text) return;
    setBuffer("");
    setMutState("saving");
    try {
      await addIdea(plan.id, text);
      setMutState("idle");
      onRefresh();
    } catch {
      setMutState("error");
    }
  }

  async function handleDelete(itemId: string) {
    setMutState("saving");
    try {
      await deleteIdea(itemId);
      setMutState("idle");
      onRefresh();
    } catch {
      setMutState("error");
    }
  }

  async function handleGenerate() {
    setAiError(null);
    setAiGenerating(true);
    try {
      await generateIdeasWithAI(plan.id);
      onRefresh();
    } catch (err) {
      setAiError(err instanceof Error ? err.message : "Couldn't generate ideas");
    } finally {
      setAiGenerating(false);
    }
  }

  return (
    <div className="flex flex-col gap-3">
      {/* Header */}
      <div className="flex items-center justify-between">
        <h2 className="font-display text-[32px] font-medium leading-none text-[#0c0c0e]">
          Ideas
        </h2>
        {mutState === "error" && (
          <span className="text-[11px] text-[#71717a]">Couldn&apos;t save</span>
        )}
        {mutState === "saving" && (
          <span className="text-[11px] text-[#a1a1aa]">Saving…</span>
        )}
      </div>

      {/* Empty state */}
      {isEmpty && !serverGenerating && (
        <div className="rounded-xl border border-dashed border-zinc-200 px-4 py-5">
          <p className="font-display text-[16px] font-medium leading-snug text-[#0c0c0e]">
            What do you want to post about?
          </p>
          <p className="mt-1 text-[12px] text-[#71717a]">
            Your ideas lead — Nova deepens them into filmable shots.
          </p>
        </div>
      )}

      {/* Generating spinner */}
      {serverGenerating && (
        <div className="flex items-center gap-2 py-1">
          <span className="relative flex h-2 w-2">
            <span className="motion-safe:animate-ping absolute inline-flex h-full w-full rounded-full bg-lime-600 opacity-60" />
            <span className="relative inline-flex h-2 w-2 rounded-full bg-lime-600" />
          </span>
          <span className="text-[12px] text-[#71717a]">Generating ideas…</span>
        </div>
      )}

      {/* Items list */}
      {!isEmpty && (
        <section aria-labelledby="ideas-heading">
          <p
            id="ideas-heading"
            className="mb-1 text-[11px] font-semibold uppercase tracking-[.18em] text-lime-700"
          >
            Ideas
          </p>
          <ul className="flex flex-col" aria-label="Ideas">
            {ideaItems.map((item) => (
              <li
                key={item.id}
                className="group flex min-h-[44px] items-start gap-2 border-t border-zinc-100 py-2.5 first:border-t-0"
              >
                <Link
                  href={`/plan/items/${item.id}`}
                  className="flex-1 line-clamp-2 text-[14px] leading-snug text-[#0c0c0e] transition-colors hover:text-lime-700 focus-visible:rounded focus-visible:outline-2 focus-visible:outline-[#0c0c0e]"
                >
                  {item.idea}
                </Link>
                <button
                  type="button"
                  onClick={() => void handleDelete(item.id)}
                  aria-label={`Remove idea: ${item.idea}`}
                  className="flex h-[28px] w-[28px] shrink-0 items-center justify-center rounded text-[#a1a1aa] opacity-0 transition-opacity hover:text-[#0c0c0e] group-hover:opacity-100 focus-visible:opacity-100 focus-visible:outline-2 focus-visible:outline-[#0c0c0e]"
                >
                  ×
                </button>
              </li>
            ))}
          </ul>
        </section>
      )}

      {plannedItems.length > 0 && (
        <section aria-labelledby="planned-ideas-heading" className="pt-2">
          <p
            id="planned-ideas-heading"
            className="mb-1 text-[11px] font-semibold uppercase tracking-[.18em] text-[#a1a1aa]"
          >
            In your plan
          </p>
          <ul className="flex flex-col" aria-label="In your plan">
            {plannedItems.map((item) => (
              <li
                key={item.id}
                className="flex min-h-[40px] items-start gap-2 border-t border-zinc-100 py-2.5 first:border-t-0"
              >
                <Link
                  href={`/plan/items/${item.id}`}
                  className="flex-1 line-clamp-2 text-[13px] leading-snug text-[#71717a] transition-colors hover:text-lime-700 focus-visible:rounded focus-visible:outline-2 focus-visible:outline-[#0c0c0e]"
                >
                  {item.idea}
                </Link>
                <span className="shrink-0 rounded-full border border-lime-200 bg-lime-50 px-2 py-0.5 text-[10px] font-semibold text-lime-800">
                  Day {item.day_index}
                </span>
              </li>
            ))}
          </ul>
        </section>
      )}

      {/* Add-idea input */}
      <div className="flex min-h-[40px] items-center gap-2 rounded-lg border border-dashed border-zinc-300 bg-white px-3 py-2 focus-within:border-lime-500/60">
        <span className="text-[14px] font-bold leading-none text-lime-700" aria-hidden>
          +
        </span>
        <input
          ref={inputRef}
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
          placeholder={isEmpty ? "Add your first idea" : "Add an idea"}
          aria-label="Add a new idea"
          className="flex-1 bg-transparent text-[13px] text-[#0c0c0e] placeholder-zinc-400 focus:outline-none"
        />
      </div>

      {/* Generate with AI */}
      <button
        type="button"
        onClick={() => void handleGenerate()}
        disabled={aiGenerating || serverGenerating}
        className="flex min-h-[44px] items-center justify-center gap-1.5 rounded-lg bg-lime-600 px-4 py-2 text-[13px] font-semibold text-white transition-colors hover:bg-lime-700 focus-visible:outline focus-visible:outline-2 focus-visible:outline-lime-500 disabled:cursor-not-allowed disabled:opacity-50"
      >
        <span aria-hidden>✦</span>
        {aiGenerating ? "Generating…" : "Generate with AI"}
      </button>
      {aiError && (
        <p className="text-[12px] leading-snug text-red-600" role="alert">
          {aiError}
        </p>
      )}
    </div>
  );
}
