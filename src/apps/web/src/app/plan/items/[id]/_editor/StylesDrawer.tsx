"use client";

import { useEffect, useState } from "react";
import StyleChip from "@/components/ui/StyleChip";
import {
  getGenerativeStyleSets,
  type GenerativeStyleSet,
} from "@/lib/generative-api";

export default function StylesDrawer({
  sampleText,
  appliedStyleSetId,
  onRestyleAll,
}: {
  sampleText: string | null;
  appliedStyleSetId: string | null;
  onRestyleAll?: (styleSet: GenerativeStyleSet) => void;
}) {
  const [styleSets, setStyleSets] = useState<GenerativeStyleSet[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    setLoading(true);
    getGenerativeStyleSets()
      .then((sets) => {
        if (!active) return;
        setStyleSets(sets);
        setError(null);
      })
      .catch(() => {
        if (!active) return;
        setStyleSets([]);
        setError("Styles could not load.");
      })
      .finally(() => {
        if (active) setLoading(false);
      });

    return () => {
      active = false;
    };
  }, []);

  return (
    <div className="min-h-0 flex-1 overflow-y-auto px-5 pb-5">
      <p className="mb-3 text-[12px] font-semibold text-[#3f3f46]">Styles</p>

      {loading && (
        <div className="grid grid-cols-2 gap-2.5" aria-label="Loading styles">
          {Array.from({ length: 4 }).map((_, index) => (
            <div
              key={index}
              className="h-[84px] rounded-lg border border-zinc-200 bg-zinc-100 motion-safe:animate-pulse"
            />
          ))}
        </div>
      )}

      {!loading && error && (
        <p className="rounded-lg border border-zinc-200 bg-zinc-50 px-3 py-2 text-[12px] text-[#71717a]">
          {error}
        </p>
      )}

      {!loading && !error && styleSets.length === 0 && (
        <p className="rounded-lg border border-zinc-200 bg-zinc-50 px-3 py-2 text-[12px] text-[#71717a]">
          No styles available.
        </p>
      )}

      {!loading && !error && styleSets.length > 0 && (
        <div role="radiogroup" aria-label="Styles" className="grid grid-cols-2 gap-2.5">
          {styleSets.map((styleSet) => (
            <StyleChip
              key={styleSet.id}
              styleSet={styleSet}
              selected={appliedStyleSetId === styleSet.id}
              disabled={!onRestyleAll}
              sampleText={sampleText ?? undefined}
              darkTile
              onSelect={() => onRestyleAll?.(styleSet)}
            />
          ))}
        </div>
      )}
    </div>
  );
}
