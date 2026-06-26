"use client";

import { useEffect, useRef, useState } from "react";
import { StableVideo } from "../StableVideo";
import { errorCopy, variantDisplayName } from "./constants";
import { formatElapsed } from "./logic";

/** How long a variant can stay "rendering" before we surface a stall hint. */
const STALL_HINT_MS = 300_000; // 5 min

export interface VariantRenderCardVariant {
  variant_id: string;
  render_status: string | null;
  render_started_at?: string | null;
  render_finished_at?: string | null;
  output_url?: string | null;
  error?: string | null;
  error_class?: string | null;
}

interface VariantRenderCardProps {
  variant: VariantRenderCardVariant;
  /** True when this variant just became ready (triggers arrive animation). */
  isNewlyReady: boolean;
  /** Called when the user taps "Try again" on a failed variant. */
  onRetry?: () => void;
  /** "light" renders on cream canvas; "dark" (default) renders dark theatre palette. */
  tone?: "dark" | "light";
}

/**
 * Per-variant tile.
 *
 * States:
 * - pending:   shimmer sweep, "Getting ready…"
 * - rendering: shimmer sweep + live elapsed clock from render_started_at;
 *              after 5 min surfaces a stall hint with "Try again"
 * - ready:     9:16 video player + Download; arrive animation on isNewlyReady
 * - failed:    dashed border, human error copy + "Try again"
 *
 * D20: tone="light" swaps to cream-canvas palette.
 * No red (brand guideline). No raw error text shown.
 */
export function VariantRenderCard({ variant, isNewlyReady, onRetry, tone = "dark" }: VariantRenderCardProps) {
  const { variant_id, render_status, render_started_at, render_finished_at, output_url, error_class } = variant;
  const displayName = variantDisplayName(variant_id);

  const [arrivedOnce, setArrivedOnce] = useState(false);
  const arrivedRef = useRef(false);

  useEffect(() => {
    if (isNewlyReady && !arrivedRef.current) {
      arrivedRef.current = true;
      setArrivedOnce(true);
    }
  }, [isNewlyReady]);

  const labelClass = tone === "light" ? "text-[#3f3f46]" : "text-zinc-300";

  return (
    <div
      role="group"
      aria-label={`${displayName} edit`}
      className="flex flex-col gap-2"
    >
      {/* Variant label */}
      <p className={`text-sm font-medium ${labelClass}`}>{displayName}</p>

      {/* Card body */}
      {render_status === "ready" ? (
        <ReadyCard
          outputUrl={output_url ?? null}
          renderFinishedAt={render_finished_at ?? null}
          displayName={displayName}
          isNew={arrivedOnce}
          tone={tone}
        />
      ) : render_status === "failed" ? (
        <FailedCard errorClass={error_class ?? null} onRetry={onRetry} tone={tone} />
      ) : render_status === "rendering" ? (
        <RenderingCard startedAt={render_started_at ?? null} tone={tone} onRetry={onRetry} />
      ) : (
        <PendingCard tone={tone} />
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Sub-cards
// ---------------------------------------------------------------------------

function PendingCard({ tone }: { tone: "dark" | "light" }) {
  const bodyClass = tone === "light" ? "bg-zinc-100" : "bg-zinc-900";
  const textClass = tone === "light" ? "text-[#71717a]" : "text-zinc-500";
  return (
    <div className={`relative aspect-[9/16] w-full overflow-hidden rounded-lg ${bodyClass}`}>
      <ShimmerSweep tone={tone} />
      <div className="absolute inset-0 flex items-center justify-center">
        <p className={`text-sm ${textClass}`}>Getting ready…</p>
      </div>
    </div>
  );
}

function RenderingCard({
  startedAt,
  tone,
  onRetry,
}: {
  startedAt: string | null;
  tone: "dark" | "light";
  onRetry?: () => void;
}) {
  const [elapsed, setElapsed] = useState(() => {
    if (!startedAt) return 0;
    return Date.now() - new Date(startedAt).getTime();
  });

  useEffect(() => {
    const id = setInterval(() => {
      if (!startedAt) return;
      setElapsed(Date.now() - new Date(startedAt).getTime());
    }, 1000);
    return () => clearInterval(id);
  }, [startedAt]);

  const stalled = elapsed >= STALL_HINT_MS;
  const bodyClass = tone === "light" ? "bg-zinc-100" : "bg-zinc-900";
  const textClass = tone === "light" ? "text-[#71717a]" : "text-zinc-400";
  const btnClass = tone === "light"
    ? "rounded border border-zinc-200 px-3 py-1 text-xs text-[#3f3f46] hover:bg-zinc-100"
    : "rounded border border-zinc-600 px-3 py-1 text-xs text-zinc-300 hover:bg-zinc-800";
  return (
    <div className={`relative aspect-[9/16] w-full overflow-hidden rounded-lg ${bodyClass}`}>
      <ShimmerSweep tone={tone} />
      <div className="absolute inset-0 flex flex-col items-center justify-center gap-3">
        {stalled ? (
          <>
            <p className={`text-sm ${textClass}`}>Taking longer than usual…</p>
            {onRetry && (
              <button onClick={onRetry} className={btnClass}>
                Try again
              </button>
            )}
          </>
        ) : (
          <p className={`text-sm ${textClass}`}>
            Rendering · {formatElapsed(elapsed)}
          </p>
        )}
      </div>
    </div>
  );
}

function ReadyCard({
  outputUrl,
  renderFinishedAt,
  displayName,
  isNew,
  tone,
}: {
  outputUrl: string | null;
  renderFinishedAt: string | null;
  displayName: string;
  isNew: boolean;
  tone: "dark" | "light";
}) {
  const bodyClass = tone === "light" ? "bg-zinc-100" : "bg-zinc-900";
  const ringClass = tone === "light" ? "ring-lime-600/60" : "ring-amber-400/60";
  const emptyTextClass = tone === "light" ? "text-[#71717a]" : "text-zinc-500";
  const btnClass = tone === "light"
    ? "border-zinc-200 text-[#3f3f46] hover:bg-zinc-100"
    : "border-zinc-700 text-zinc-300 hover:bg-zinc-800";

  // Use StableVideo so re-signed URLs (new ?X-Goog-Signature every 2s poll) don't
  // restart playback. ReadyCard unmounts while a re-render is in progress (parent
  // shows RenderingCard), so StableVideo remounts fresh and adopts the new URL
  // naturally on re-render completion without a page refresh.
  const stableVideoSrc = outputUrl ?? undefined;

  return (
    <div
      className={[
        `aspect-[9/16] w-full overflow-hidden rounded-lg ${bodyClass}`,
        "motion-safe:transition-[transform,box-shadow]",
        isNew
          ? `motion-safe:animate-fade-up ring-2 ${ringClass}`
          : "",
      ]
        .filter(Boolean)
        .join(" ")}
    >
      {stableVideoSrc ? (
        <div className="flex h-full flex-col">
          <StableVideo
            src={stableVideoSrc}
            identity={renderFinishedAt ?? undefined}
            controls
            playsInline
            loop
            className="h-full w-full object-cover"
            aria-label={`${displayName} edit preview`}
          />
        </div>
      ) : (
        <div className={`flex h-full items-center justify-center text-sm ${emptyTextClass}`}>
          Video ready
        </div>
      )}
      {/* Download / Share actions */}
      {stableVideoSrc && (
        <div className="flex gap-2 pt-2">
          <a
            href={stableVideoSrc}
            download
            className={`flex-1 rounded border py-1.5 text-center text-xs ${btnClass}`}
          >
            Download
          </a>
          <ShareButton url={stableVideoSrc} label={displayName} tone={tone} />
        </div>
      )}
    </div>
  );
}

function FailedCard({
  errorClass,
  onRetry,
  tone,
}: {
  errorClass: string | null;
  onRetry?: () => void;
  tone: "dark" | "light";
}) {
  const copy = errorCopy(errorClass);
  const cardClass = tone === "light"
    ? "border-zinc-300 bg-zinc-50"
    : "border-zinc-700 bg-zinc-900/40";
  const textClass = tone === "light" ? "text-[#71717a]" : "text-zinc-400";
  const btnClass = tone === "light"
    ? "border-zinc-200 text-[#3f3f46] hover:bg-zinc-100"
    : "border-zinc-600 text-zinc-300 hover:bg-zinc-800";
  return (
    <div className={`aspect-[9/16] w-full rounded-lg border border-dashed p-4 ${cardClass}`}>
      <div className="flex h-full flex-col items-center justify-center gap-4 text-center">
        <p className={`text-sm ${textClass}`}>Couldn&apos;t finish this one — {copy}</p>
        {onRetry && (
          <button
            onClick={onRetry}
            className={`rounded border px-3 py-1.5 text-xs ${btnClass}`}
          >
            Try again
          </button>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Shimmer utility — exported so Hero and other overlays can reuse it
// ---------------------------------------------------------------------------

export function ShimmerSweep({ tone }: { tone: "dark" | "light" }) {
  const gradClass = tone === "light"
    ? "from-zinc-100 via-zinc-200 to-zinc-100"
    : "from-zinc-900 via-zinc-800 to-zinc-900";
  return (
    <div
      className={`absolute inset-0 bg-[length:200%_100%] bg-gradient-to-r ${gradClass} motion-safe:animate-shimmer`}
      aria-hidden="true"
    />
  );
}

// ---------------------------------------------------------------------------
// Share button (Web Share API, gracefully degrades to copy)
// ---------------------------------------------------------------------------

function ShareButton({ url, label, tone }: { url: string; label: string; tone: "dark" | "light" }) {
  const [copied, setCopied] = useState(false);
  const btnClass = tone === "light"
    ? "border-zinc-200 text-[#3f3f46] hover:bg-zinc-100"
    : "border-zinc-700 text-zinc-300 hover:bg-zinc-800";

  const handleShare = async () => {
    if (navigator.share) {
      try {
        await navigator.share({ title: `${label} edit`, url });
      } catch {
        // User dismissed share sheet — ignore.
      }
    } else {
      await navigator.clipboard.writeText(url);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    }
  };

  return (
    <button
      onClick={handleShare}
      className={`flex-1 rounded border py-1.5 text-center text-xs ${btnClass}`}
    >
      {copied ? "Copied!" : "Share"}
    </button>
  );
}
