"use client";

import { type ReactNode, useEffect, useState } from "react";
import { FIELD_TILES_FADE_MS } from "./constants";

interface VariantLike {
  variant_id: string;
  render_status: string | null;
}

interface PayoffFieldProps {
  /** Variant list from the job status response. Null/undefined = not yet available. */
  variants: VariantLike[] | null | undefined;
  /** Render function for each variant tile — consumer controls the card style. */
  renderCard: (variant: VariantLike, isNewlyReady: boolean) => ReactNode;
  /** Custom empty-state copy. Defaults to "Your edits will appear here". */
  emptyText?: string;
}

/**
 * Pre/post resolution zone.
 *
 * - When variants is null/undefined/empty: shimmer placeholder "Your edits will appear here" (D7)
 * - When variants have entries: fade-swap to a 9:16 grid of tiles (500ms).
 * - Slot count always from variants.length — never a constant.
 * - isNewlyReady flag passed to renderCard for arrive animation on newly-ready tiles.
 */
export function PayoffField({ variants, renderCard, emptyText }: PayoffFieldProps) {
  const hasVariants = variants != null && variants.length > 0;
  const [wasEmpty, setWasEmpty] = useState(!hasVariants);
  const [opacity, setOpacity] = useState(hasVariants ? 1 : 0);

  // Track newly ready variant ids.
  const [seenReady, setSeenReady] = useState<Set<string>>(new Set());
  const [newlyReady, setNewlyReady] = useState<Set<string>>(new Set());

  useEffect(() => {
    if (!variants) return;
    const currentReady = new Set(
      variants.filter((v) => v.render_status === "ready").map((v) => v.variant_id),
    );
    setNewlyReady((prev) => {
      const freshNew = new Set<string>();
      // Use Array.from for ES2015 compat (avoid --downlevelIteration requirement).
      const currentReadyArray = Array.from(currentReady);
      for (let i = 0; i < currentReadyArray.length; i++) {
        const id = currentReadyArray[i];
        if (!seenReady.has(id) && !prev.has(id)) {
          freshNew.add(id);
        }
      }
      return freshNew;
    });
    setSeenReady(currentReady);
  }, [variants]); // eslint-disable-line react-hooks/exhaustive-deps

  // Transition from empty→populated.
  useEffect(() => {
    if (!hasVariants) {
      setWasEmpty(true);
      setOpacity(0);
      return;
    }
    if (wasEmpty) {
      // Small delay then fade in.
      const t = setTimeout(() => setOpacity(1), 50);
      return () => clearTimeout(t);
    }
    setOpacity(1);
  }, [hasVariants, wasEmpty]);

  if (!hasVariants) {
    return (
      <div className="flex w-full items-center justify-center rounded-xl border border-dashed border-zinc-800 px-6 py-16">
        <div className="flex flex-col items-center gap-4">
          {/* Shimmer skeleton lines */}
          <div className="space-y-2 w-48">
            {[100, 80, 60].map((w, i) => (
              <div
                key={i}
                className="h-3 rounded bg-[length:200%_100%] bg-gradient-to-r from-zinc-900 via-zinc-800 to-zinc-900 motion-safe:animate-shimmer"
                style={{ width: `${w}%` }}
              />
            ))}
          </div>
          <p className="text-sm text-zinc-600">{emptyText ?? "Your edits will appear here"}</p>
        </div>
      </div>
    );
  }

  return (
    <div
      className="transition-opacity"
      style={{
        opacity,
        transitionDuration: `${FIELD_TILES_FADE_MS}ms`,
      }}
    >
      <div
        className={[
          "grid gap-4",
          variants.length === 1
            ? "grid-cols-1 max-w-xs mx-auto"
            : variants.length === 2
              ? "grid-cols-2"
              : "grid-cols-1 sm:grid-cols-2 lg:grid-cols-3",
        ].join(" ")}
      >
        {variants.map((v) =>
          renderCard(v, newlyReady.has(v.variant_id)),
        )}
      </div>
    </div>
  );
}
