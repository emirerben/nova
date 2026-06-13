"use client";

/**
 * IdeasCard — persistent "Your ideas" left-rail card (M1 Bring-Your-Own-Ideas).
 *
 * Design spec: DESIGN.md §2 light-editorial tokens. States table: board.html D2/D4.
 * Empty → §9 invitation (serif line + quiet CTA, no icon-circle).
 * Loaded → editable text rows (not chips), each with ✓/○ status glyph + text.
 * Error → §2 zinc notice line.
 * Keyboard: Enter adds, Backspace-on-empty removes last, row × to remove.
 * a11y: ≥44px rows, aria-labels, glyph + text (never color alone), role="status".
 */

import { useState, useRef, useCallback, useId } from "react";
import { LightCard } from "../ui/LightCard";
import { Eyebrow } from "../ui/Eyebrow";
import type { IdeaSeed, PersonaResponse } from "@/lib/plan-api";
import { patchPersonaIdeas } from "@/lib/plan-api";

interface IdeasCardProps {
  persona: PersonaResponse;
  /** Called with the updated persona after a successful save so the parent
   *  can propagate the new idea_seeds without a full page refresh. */
  onSaved?: (updated: PersonaResponse) => void;
}

type SaveState = "idle" | "saving" | "saved" | "error";

export function IdeasCard({ persona, onSaved }: IdeasCardProps) {
  const seeds: IdeaSeed[] = persona.idea_seeds ?? [];

  // Local shadow of the seeds list — synced to persona prop on save.
  const [localSeeds, setLocalSeeds] = useState<IdeaSeed[]>(seeds);
  const [buffer, setBuffer] = useState("");
  const [saveState, setSaveState] = useState<SaveState>("idle");
  const inputRef = useRef<HTMLInputElement>(null);
  const statusId = useId();

  // Sync when the parent refreshes the persona (e.g. after a plan generation).
  const prevIdRef = useRef(persona.id);
  if (prevIdRef.current !== persona.id) {
    prevIdRef.current = persona.id;
    setLocalSeeds(persona.idea_seeds ?? []);
  }

  const save = useCallback(
    async (next: IdeaSeed[]) => {
      setSaveState("saving");
      try {
        const updated = await patchPersonaIdeas(persona.id, next);
        // Server stamps ids → use server response as authoritative list.
        setLocalSeeds(updated.idea_seeds ?? next);
        setSaveState("saved");
        onSaved?.(updated);
        // Reset "saved" tick after 2s.
        setTimeout(() => setSaveState("idle"), 2000);
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
    <LightCard className="px-5 py-5">
      {/* Header */}
      <div className="flex items-start justify-between">
        <Eyebrow tone="lime">Your ideas</Eyebrow>
        {/* Live-region for save state — announced to screen readers */}
        <span
          id={statusId}
          role="status"
          aria-live="polite"
          className="text-[11px] text-[#71717a]"
        >
          {saveState === "saving" && "Saving…"}
          {saveState === "saved" && (
            <span className="text-lime-700">✓ Saved</span>
          )}
          {saveState === "error" && (
            <span className="text-[#71717a]">Couldn&apos;t save &mdash; try again</span>
          )}
        </span>
      </div>

      {/* Empty state — §9 invitation: serif line + quiet CTA, no icon-circle */}
      {isEmpty && (
        <div className="mt-3">
          <p className="font-serif text-[17px] font-medium leading-snug text-[#0c0c0e]">
            What do you want to post about?
          </p>
          <p className="mt-1 text-[13px] text-[#71717a]">
            Your ideas lead &mdash; Nova deepens them into filmable shots.
          </p>
        </div>
      )}

      {/* Seed rows */}
      {!isEmpty && (
        <ul className="mt-3 flex flex-col gap-2" aria-label="Your ideas">
          {localSeeds.map((seed, idx) => {
            const inPlan = seed.status === "in_plan";
            return (
              <li
                key={seed.id || `seed-${idx}`}
                className="flex min-h-[44px] items-start gap-3 rounded-xl border border-zinc-200 bg-white px-3 py-2.5"
              >
                {/* Status badge: glyph + text (never color alone — §8 a11y) */}
                <span
                  className={[
                    "mt-0.5 shrink-0 rounded-full px-2 py-0.5 text-[10.5px] font-semibold whitespace-nowrap",
                    inPlan
                      ? "bg-lime-50 border border-lime-200 text-lime-800"
                      : "bg-zinc-100 border border-zinc-200 text-[#71717a]",
                  ].join(" ")}
                  aria-label={inPlan ? "In your plan" : "Not yet used"}
                >
                  {inPlan ? "✓ in your plan" : "○ not yet used"}
                </span>

                {/* Idea text */}
                <span className="flex-1 text-[14px] text-[#0c0c0e] leading-snug">
                  {seed.text}
                </span>

                {/* Remove */}
                <button
                  type="button"
                  onClick={() => removeSeed(idx)}
                  aria-label={`Remove idea: ${seed.text}`}
                  className="shrink-0 text-[#a1a1aa] hover:text-[#0c0c0e] transition-colors p-1 -mr-1 min-h-[44px] min-w-[44px] flex items-center justify-center"
                >
                  ×
                </button>
              </li>
            );
          })}
        </ul>
      )}

      {/* Add-idea input — always visible; dashed ghost row style from mockup board */}
      <div className="mt-3">
        <div className="flex min-h-[44px] items-center gap-2 rounded-xl border border-dashed border-zinc-300 bg-white px-3 py-2 focus-within:border-lime-500/60">
          <span className="text-[16px] font-bold text-lime-700 leading-none" aria-hidden>
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
            placeholder="Add an idea — press Enter to save"
            aria-label="Add a new idea"
            className="flex-1 bg-transparent text-[14px] text-[#0c0c0e] placeholder-zinc-400 focus:outline-none"
          />
        </div>

        {/* Error notice — §2 quiet zinc (never red/amber on light) */}
        {saveState === "error" && (
          <p className="mt-2 text-[12px] text-[#71717a]">
            Couldn&apos;t save — check your connection and try again.
          </p>
        )}
      </div>
    </LightCard>
  );
}
