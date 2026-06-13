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

import { useEffect } from "react";
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
import { TimelineEditor } from "./TimelineEditor";
import { useTimelineSession } from "./useTimelineSession";
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

  // Clip-timeline editor session — lifted here for the same mounted-across-polls
  // reason as the edit session: the re-render wait must survive the variant
  // flipping to "rendering".
  const timelineSession = useTimelineSession(jobId, variant, refresh);

  // Saving-state poll driver. The page's job poller stops at terminal status,
  // and the single post-commit refresh can race AHEAD of the worker flipping
  // render_status to "rendering" (stale-terminal data → no re-arm). The
  // session itself knows it's awaiting a render — keep refreshing until it
  // settles, so the preview swaps to the fresh output without a tab refocus.
  // Same driver covers timeline re-renders.
  const awaitingTimelineRender = timelineSession.wait.phase === "rendering";
  useEffect(() => {
    if (!session.isSaving && !awaitingTimelineRender) return;
    const t = setInterval(refresh, 2000);
    return () => clearInterval(t);
  }, [session.isSaving, awaitingTimelineRender, refresh]);

  return (
    <div className="flex flex-col gap-4">
      {/* The payoff/progress card duplicates the edit view while a session is
          active (and would flash "Rendering…" during a committed save) — the
          edit card is the single focus until the session settles. The same
          applies to a timeline re-render: the VariantCard well owns that wait
          (ETA band / failed tile), so the generic card hides. */}
      {!session.isActive && !session.justSaved && !timelineSession.isWaiting && (
        <VariantRenderCard
          variant={variant}
          isNewlyReady={isNewlyReady}
          onRetry={onRetry}
          tone="light"
        />
      )}

      {(variant.render_status === "ready" ||
        session.isActive ||
        session.justSaved ||
        timelineSession.isWaiting) && (
        <VariantCard
          variant={variant}
          tracks={tracks}
          styleSets={styleSets}
          tone="light"
          editSession={session}
          timelineSession={timelineSession}
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

      {timelineSession.isEditorOpen && (
        <TimelineEditor
          ownerId={jobId}
          variantId={variant.variant_id}
          onClose={timelineSession.closeEditor}
          onRenderEnqueued={timelineSession.onRenderEnqueued}
        />
      )}
    </div>
  );
}
