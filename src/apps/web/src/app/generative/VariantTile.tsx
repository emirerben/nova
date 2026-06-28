"use client";

/**
 * One variant's tile on the public generative page: the render/payoff card +
 * the interactive VariantCard, with the instant-edit session lifted up here.
 *
 * The session hook must live ABOVE the `render_status === "ready"` gate:
 * committing an edit flips the variant to "rendering" on the next poll, and if
 * the session state lived inside that gate it would unmount — losing the draft
 * and dropping the user back to a "Rendering…" placeholder. This tile stays
 * mounted for the variant's whole lifetime (keyed by variant_id in the page),
 * so the editing preview survives the commit round-trip.
 */

import { useEffect, useState } from "react";
import {
  changeVariantStyle,
  editVariant,
  retextVariant,
  setVariantIntroSize,
  setVariantMix,
  swapVariantSong,
  type GenerativeStyleSet,
  type GenerativeVariant,
} from "@/lib/generative-api";
import type { MusicTrackSummary } from "@/lib/music-api";
import { VariantRenderCard } from "@/components/progress";
import { VariantCard } from "./VariantCard";
import { InlineClipsEditor } from "@/app/plan/_components/InlineClipsEditor";
import { useVariantEditSession } from "@/lib/variant-editor/useVariantEditSession";

export function VariantTile({
  variant,
  jobId,
  tracks,
  styleSets,
  isNewlyReady,
  onRetry,
  refresh,
}: {
  variant: GenerativeVariant;
  jobId: string;
  tracks: MusicTrackSummary[];
  styleSets: GenerativeStyleSet[];
  isNewlyReady: boolean;
  onRetry: () => void;
  refresh: () => void;
}) {
  const session = useVariantEditSession(variant, async (payload) => {
    await editVariant(jobId, variant.variant_id, payload);
    refresh();
  });

  const [clipsOpen, setClipsOpen] = useState(false);

  // Saving-state poll driver: keep refreshing while a commit is in-flight so
  // the preview swaps to the fresh output without a tab refocus.
  useEffect(() => {
    if (!session.isSaving) return;
    const t = setInterval(refresh, 2000);
    return () => clearInterval(t);
  }, [session.isSaving, refresh]);

  return (
    <div className="flex flex-col gap-4">
      {!session.isActive && !session.justSaved && (
        <VariantRenderCard
          variant={variant}
          isNewlyReady={isNewlyReady}
          onRetry={onRetry}
          tone="light"
        />
      )}

      {(variant.render_status === "ready" || session.isActive || session.justSaved) && (
        <VariantCard
          variant={variant}
          tracks={tracks}
          styleSets={styleSets}
          tone="light"
          editSession={session}
          clipsOpen={clipsOpen}
          onToggleClips={() => setClipsOpen((o) => !o)}
          onSwap={async (trackId) => {
            await swapVariantSong(jobId, variant.variant_id, trackId);
            refresh();
          }}
          onRetext={async (text) => {
            await retextVariant(jobId, variant.variant_id, { text });
            refresh();
          }}
          onRemoveText={async () => {
            await retextVariant(jobId, variant.variant_id, { remove: true });
            refresh();
          }}
          onChangeStyle={async (styleSetId) => {
            await changeVariantStyle(jobId, variant.variant_id, styleSetId);
            refresh();
          }}
          onResize={async (px) => {
            await setVariantIntroSize(jobId, variant.variant_id, px);
            refresh();
          }}
          onSetMix={async (mix) => {
            await setVariantMix(jobId, variant.variant_id, mix);
            refresh();
          }}
          onChangeLayout={async (layout) => {
            await editVariant(jobId, variant.variant_id, { intro_layout: layout });
            refresh();
          }}
        />
      )}

      {clipsOpen && (
        <div className="rounded-xl border border-zinc-200 bg-[#fafaf8] p-3">
          <InlineClipsEditor
            ownerId={jobId}
            variantId={variant.variant_id}
            base="generative"
            onRenderEnqueued={() => { setClipsOpen(false); refresh(); }}
          />
        </div>
      )}
    </div>
  );
}
