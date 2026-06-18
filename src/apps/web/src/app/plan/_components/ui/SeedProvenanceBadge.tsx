"use client";

import type { PlanItem } from "@/lib/plan-api";

const MAX_SEED_TEXT = 36;

/**
 * Renders a "From your idea: …" lime soft-pill when a plan item was seeded by
 * the user (source_idea_seed_text or source_idea_seed_id present).
 * Returns null when neither field is set (market-bank or pre-T5 item).
 */
export function SeedProvenanceBadge({ item }: { item: PlanItem }) {
  const text = item.source_idea_seed_text;
  const hasId = Boolean(item.source_idea_seed_id);

  if (!hasId) return null;

  const label = text
    ? `From your idea: ${text.length > MAX_SEED_TEXT ? text.slice(0, MAX_SEED_TEXT) + "…" : text}`
    : "From your idea";

  return (
    <span className="inline-flex shrink-0 items-center gap-1 rounded-full border border-lime-200 bg-lime-50 px-2 py-0.5 text-xs text-lime-800">
      <span aria-hidden="true">✦</span>
      {label}
    </span>
  );
}
