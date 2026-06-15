"use client";

/**
 * IdeasSidebar — slim left-rail ideas list for the redesigned plan home.
 *
 * Shows the user's BYO idea seeds with:
 * - A lime "Day N" badge when the seed is linked to a plan item
 *   (source_idea_seed_id match via plan.items)
 * - Subtle "→" on hover that navigates to that item
 * - Empty state with a small text input to submit a new idea
 * - Inline add-idea input always visible below the list
 *
 * Uses plan.idea_seeds (from GET /content-plans, populated since this PR)
 * so no separate persona API call is needed.
 */

import { useState, useRef, useCallback } from "react";
import Link from "next/link";
import type { ContentPlan, IdeaSeed, PersonaResponse } from "@/lib/plan-api";
import { patchPersonaIdeas } from "@/lib/plan-api";

interface IdeasSidebarProps {
  /** The current plan, used to cross-reference source_idea_seed_id */
  plan: ContentPlan;
  /** The current persona (holds the canonical idea_seeds list for mutations) */
  persona: PersonaResponse;
  /** Called with the updated persona after a successful save */
  onSaved: (updated: PersonaResponse) => void;
}

type SaveState = "idle" | "saving" | "error";

export function IdeasSidebar({ plan, persona, onSaved }: IdeasSidebarProps) {
  // Prefer seeds from the plan API (reflects latest in_plan status from the
  // last poll); fall back to persona seeds for optimistic local mutations.
  const seeds: IdeaSeed[] = (plan.idea_seeds ?? persona.idea_seeds ?? []) as IdeaSeed[];

  const [localSeeds, setLocalSeeds] = useState<IdeaSeed[]>(seeds);
  const [buffer, setBuffer] = useState("");
  const [saveState, setSaveState] = useState<SaveState>("idle");
  const inputRef = useRef<HTMLInputElement>(null);

  // Sync when parent plan/persona refresh (e.g. after an add-ideas generation)
  const prevPlanIdRef = useRef(plan.id);
  if (prevPlanIdRef.current !== plan.id) {
    prevPlanIdRef.current = plan.id;
    setLocalSeeds((plan.idea_seeds ?? persona.idea_seeds ?? []) as IdeaSeed[]);
  }

  // Build lookup: seed_id → linked plan item (for badge + nav)
  const seedToItem = new Map<string, { id: string; day_index: number }>();
  for (const item of plan.items ?? []) {
    if (item.source_idea_seed_id) {
      seedToItem.set(item.source_idea_seed_id, {
        id: item.id,
        day_index: item.day_index,
      });
    }
  }

  const save = useCallback(
    async (next: IdeaSeed[]) => {
      setSaveState("saving");
      try {
        const updated = await patchPersonaIdeas(persona.id, next);
        setLocalSeeds((updated.idea_seeds ?? next) as IdeaSeed[]);
        setSaveState("idle");
        onSaved(updated);
      } catch {
        setSaveState("error");
      }
    },
    [persona.id, onSaved],
  );

  function commitBuffer(raw: string) {
    const text = raw.trim();
    if (!text) return;
    const next: IdeaSeed[] = [
      ...localSeeds,
      { id: "", text, pillar: null, status: "pending" },
    ];
    setLocalSeeds(next);
    setBuffer("");
    void save(next);
  }

  function removeSeed(idx: number) {
    const next = localSeeds.filter((_, i) => i !== idx);
    setLocalSeeds(next);
    void save(next);
  }

  const isEmpty = localSeeds.length === 0;

  return (
    <div className="flex flex-col gap-3">
      {/* Header */}
      <div className="flex items-center justify-between">
        <span className="text-[11px] font-semibold uppercase tracking-[.18em] text-lime-700">
          Your ideas
        </span>
        {saveState === "error" && (
          <span className="text-[11px] text-[#71717a]">Couldn&apos;t save</span>
        )}
        {saveState === "saving" && (
          <span className="text-[11px] text-[#a1a1aa]">Saving…</span>
        )}
      </div>

      {/* Empty state */}
      {isEmpty && (
        <div className="rounded-xl border border-dashed border-zinc-200 px-4 py-5">
          <p className="font-display text-[16px] font-medium leading-snug text-[#0c0c0e]">
            What do you want to post about?
          </p>
          <p className="mt-1 text-[12px] text-[#71717a]">
            Your ideas lead — Nova deepens them into filmable shots.
          </p>
        </div>
      )}

      {/* Seed list */}
      {!isEmpty && (
        <ul className="flex flex-col" aria-label="Your ideas">
          {localSeeds.map((seed, idx) => {
            const inPlan = seed.status === "in_plan";
            const linked = seed.id ? seedToItem.get(seed.id) : undefined;

            const row = (
              <li
                key={seed.id || `seed-${idx}`}
                className="group flex min-h-[44px] items-start gap-2 border-t border-zinc-100 py-2.5 first:border-t-0"
              >
                {/* Idea text — truncated to 1 line */}
                <span className="flex-1 line-clamp-1 text-[14px] leading-snug text-[#0c0c0e]">
                  {seed.text}
                </span>

                <div className="flex shrink-0 items-center gap-1.5">
                  {/* "Day N" badge when linked to a plan item */}
                  {inPlan && linked && (
                    <span className="rounded-full border border-lime-200 bg-lime-50 px-2 py-0.5 text-[10.5px] font-semibold text-lime-800">
                      Day {linked.day_index}
                    </span>
                  )}
                  {/* "→" nav hint on hover — only when there's a linked item */}
                  {linked && (
                    <span
                      className="text-[13px] text-lime-600 opacity-0 transition-opacity group-hover:opacity-100"
                      aria-hidden
                    >
                      →
                    </span>
                  )}
                  {/* Remove button — only visible on hover */}
                  <button
                    type="button"
                    onClick={() => removeSeed(idx)}
                    aria-label={`Remove idea: ${seed.text}`}
                    className="flex h-[28px] w-[28px] items-center justify-center rounded text-[#a1a1aa] opacity-0 transition-opacity hover:text-[#0c0c0e] group-hover:opacity-100"
                  >
                    ×
                  </button>
                </div>
              </li>
            );

            // Wrap in Link if there's a linked plan item
            return linked ? (
              <Link
                key={seed.id || `seed-${idx}`}
                href={`/plan/items/${linked.id}`}
                className="block focus-visible:outline-2 focus-visible:outline-[#0c0c0e] focus-visible:rounded"
              >
                {row}
              </Link>
            ) : (
              row
            );
          })}
        </ul>
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
              commitBuffer(buffer);
            } else if (e.key === "Backspace" && buffer === "" && localSeeds.length > 0) {
              removeSeed(localSeeds.length - 1);
            }
          }}
          onBlur={() => commitBuffer(buffer)}
          placeholder={isEmpty ? "Add your first idea" : "Add an idea"}
          aria-label="Add a new idea"
          className="flex-1 bg-transparent text-[13px] text-[#0c0c0e] placeholder-zinc-400 focus:outline-none"
        />
      </div>
    </div>
  );
}
