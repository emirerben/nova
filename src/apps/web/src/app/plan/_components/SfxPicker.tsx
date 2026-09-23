"use client";

/**
 * SfxPicker — searchable, category-grouped sound-effect list.
 *
 * Shared by the editor's Sounds drawer (ToolDrawer, pick = add at playhead)
 * and the legacy timeline's SFX lane popover (SfxLane, pick = select, then
 * "+ Add"). Search + grouping rules live in lib/sfx-browse.ts.
 *
 * onPick receives the ORIGINAL effect object: its preview_audio_url is what
 * the live preview (useSfxPreview) plays, so never hand callers a copy.
 */

import { useId, useMemo, useRef, useState } from "react";
import { Search } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/cn";
import type { SoundEffectSummary } from "@/lib/sfx-api";
import { groupSfxEffects, hasSfxCategories, sfxDurationLabel } from "@/lib/sfx-browse";

export interface SfxPickerProps {
  effects: SoundEffectSummary[];
  onPick: (effect: SoundEffectSummary) => void;
  /** Marks the effect currently chosen by a two-step picker (SfxLane). */
  selectedId?: string | null;
  /** "comfortable" = 44px outlined rows (drawer); "compact" = dense ghost
      rows for popovers, still 44px below `sm` (DESIGN.md §8). */
  density?: "comfortable" | "compact";
  /** Pin the search field while a scrolling parent scrolls the list. */
  stickySearch?: boolean;
  /** Extra classes for the root (e.g. a flex column inside a popover). */
  className?: string;
  /** Extra classes for the results region (e.g. max-h + overflow). */
  listClassName?: string;
}

export default function SfxPicker({
  effects,
  onPick,
  selectedId = null,
  density = "comfortable",
  stickySearch = false,
  className,
  listClassName,
}: SfxPickerProps) {
  const [query, setQuery] = useState("");
  const inputRef = useRef<HTMLInputElement | null>(null);
  const listRef = useRef<HTMLDivElement | null>(null);
  const baseId = useId();
  const listId = `${baseId}-results`;

  const groups = useMemo(() => groupSfxEffects(effects, query), [effects, query]);
  const matchCount = groups.reduce((n, g) => n + g.effects.length, 0);
  const searching = query.trim().length > 0;
  // A library with no categories at all (legacy-only) reads as one flat list.
  // Decided on the whole library, not the results, so a search that only
  // matches legacy effects still shows its "Other" heading.
  const showHeadings = useMemo(() => hasSfxCategories(effects), [effects]);

  if (effects.length === 0) {
    return (
      <div className="rounded-lg border border-dashed border-zinc-300 px-3 py-3 text-[12px] text-ink-2">
        No published sound effects found.
      </div>
    );
  }

  function options(): HTMLButtonElement[] {
    return Array.from(listRef.current?.querySelectorAll<HTMLButtonElement>("[data-sfx-option]") ?? []);
  }

  function handleSearchKeyDown(e: React.KeyboardEvent<HTMLInputElement>) {
    // Escape clears the query without also closing the host sheet/drawer
    // (same contract as CaptionsDrawer's find field).
    if (e.key === "Escape" && query) {
      e.preventDefault();
      e.stopPropagation();
      setQuery("");
      return;
    }
    if (e.key !== "ArrowDown") return;
    const first = options()[0];
    if (!first) return;
    e.preventDefault();
    first.focus();
  }

  function handleListKeyDown(e: React.KeyboardEvent<HTMLDivElement>) {
    if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
    const all = options();
    const index = all.indexOf(e.target as HTMLButtonElement);
    if (index < 0) return;
    e.preventDefault();
    const next = e.key === "ArrowDown" ? all[index + 1] : all[index - 1];
    if (next) next.focus();
    else if (e.key === "ArrowUp") inputRef.current?.focus();
  }

  const compact = density === "compact";

  return (
    <div className={className}>
      <div className={cn("pb-2", stickySearch && "sticky top-0 z-10 bg-white")}>
        <div className="relative">
          <Search
            aria-hidden
            className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-ink-2"
          />
          <Input
            ref={inputRef}
            type="search"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={handleSearchKeyDown}
            placeholder="Search effects"
            aria-label="Search sound effects"
            aria-controls={listId}
            autoComplete="off"
            spellCheck={false}
            className="h-11 rounded-lg border-zinc-200 bg-white pl-9 text-[#0c0c0e] sm:h-9"
          />
        </div>
      </div>
      <p role="status" aria-live="polite" className="sr-only">
        {searching ? (matchCount === 1 ? "1 effect" : `${matchCount} effects`) : ""}
      </p>
      <div id={listId} ref={listRef} onKeyDown={handleListKeyDown} className={listClassName}>
        {groups.length === 0 ? (
          <div className="rounded-lg border border-dashed border-zinc-300 px-3 py-3 text-[12px] text-ink-2">
            <p>No effects match &ldquo;{query.trim()}&rdquo;.</p>
            <Button
              type="button"
              variant="link"
              onClick={() => {
                setQuery("");
                inputRef.current?.focus();
              }}
              className="-mx-1 h-auto min-h-11 px-1 py-0 text-[12px] text-[#0c0c0e] underline underline-offset-2 sm:min-h-7"
            >
              Clear search
            </Button>
          </div>
        ) : (
          groups.map((group) => {
            const headingId = `${baseId}-${group.key}`;
            return (
              <div
                key={group.key}
                role="group"
                aria-labelledby={showHeadings ? headingId : undefined}
                aria-label={showHeadings ? undefined : "Sound effects"}
                className={compact ? "mb-2 last:mb-0" : "mb-4 last:mb-0"}
              >
                {showHeadings && (
                  <h3
                    id={headingId}
                    className={cn(
                      "flex items-baseline gap-1.5 text-[11px] font-semibold uppercase tracking-wide text-ink-2",
                      compact ? "px-2 pb-1 pt-1.5" : "mb-1.5",
                    )}
                  >
                    {group.label}
                    <span className="sr-only">, </span>
                    <span className="font-normal tabular-nums">{group.effects.length}</span>
                  </h3>
                )}
                <ul className={compact ? "space-y-0.5" : "space-y-2"}>
                  {group.effects.map((effect) => {
                    const selected = effect.id === selectedId;
                    return (
                      <li key={effect.id}>
                        <Button
                          type="button"
                          variant={compact ? "ghost" : "outline"}
                          data-sfx-option=""
                          aria-pressed={selectedId != null ? selected : undefined}
                          onClick={() => onPick(effect)}
                          className={cn(
                            "flex h-auto w-full items-center justify-between text-left font-normal text-[#0c0c0e]",
                            compact
                              ? "min-h-11 rounded-md px-2 py-1.5 text-[13px] sm:min-h-9"
                              : "min-h-11 rounded-lg px-3 text-[13px]",
                            // Keep a focused row clear of the pinned search field.
                            stickySearch && "scroll-mt-14",
                            // Sky border on pale Sky: distinct from the pale-Sky hover.
                            selected &&
                              (compact
                                ? "bg-sky-soft font-semibold ring-1 ring-inset ring-sky"
                                : "border-sky bg-sky-soft"),
                          )}
                        >
                          <span className="truncate">{effect.name}</span>
                          <span className="sr-only">, </span>
                          <span className="ml-2 shrink-0 text-[11px] font-normal tabular-nums text-ink-2">
                            {sfxDurationLabel(effect) ?? "SFX"}
                          </span>
                        </Button>
                      </li>
                    );
                  })}
                </ul>
              </div>
            );
          })
        )}
      </div>
    </div>
  );
}
