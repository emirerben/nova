"use client";

import { useEffect, useRef, useState } from "react";
import { errorCopy, variantDisplayName } from "./constants";
import { formatElapsed } from "./logic";

export interface VariantRenderCardVariant {
  variant_id: string;
  render_status: string | null;
  render_started_at?: string | null;
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
}

/**
 * Per-variant tile.
 *
 * States:
 * - pending:   shimmer sweep, "Getting ready…"
 * - rendering: shimmer sweep + live elapsed clock from render_started_at
 * - ready:     9:16 video player + Download; arrive animation on isNewlyReady
 * - failed:    dashed zinc border, human error copy + "Try again"
 *
 * No red (brand guideline). No raw error text shown.
 */
export function VariantRenderCard({ variant, isNewlyReady, onRetry }: VariantRenderCardProps) {
  const { variant_id, render_status, render_started_at, output_url, error_class } = variant;
  const displayName = variantDisplayName(variant_id);

  const [arrivedOnce, setArrivedOnce] = useState(false);
  const arrivedRef = useRef(false);

  useEffect(() => {
    if (isNewlyReady && !arrivedRef.current) {
      arrivedRef.current = true;
      setArrivedOnce(true);
    }
  }, [isNewlyReady]);

  return (
    <div
      role="group"
      aria-label={`${displayName} edit`}
      className="flex flex-col gap-2"
    >
      {/* Variant label */}
      <p className="text-sm font-medium text-zinc-300">{displayName}</p>

      {/* Card body */}
      {render_status === "ready" ? (
        <ReadyCard
          outputUrl={output_url ?? null}
          displayName={displayName}
          isNew={arrivedOnce}
        />
      ) : render_status === "failed" ? (
        <FailedCard errorClass={error_class ?? null} onRetry={onRetry} />
      ) : render_status === "rendering" ? (
        <RenderingCard startedAt={render_started_at ?? null} />
      ) : (
        <PendingCard />
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Sub-cards
// ---------------------------------------------------------------------------

function PendingCard() {
  return (
    <div className="relative aspect-[9/16] w-full overflow-hidden rounded-lg bg-zinc-900">
      <ShimmerSweep />
      <div className="absolute inset-0 flex items-center justify-center">
        <p className="text-sm text-zinc-500">Getting ready…</p>
      </div>
    </div>
  );
}

function RenderingCard({ startedAt }: { startedAt: string | null }) {
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

  return (
    <div className="relative aspect-[9/16] w-full overflow-hidden rounded-lg bg-zinc-900">
      <ShimmerSweep />
      <div className="absolute inset-0 flex items-center justify-center">
        <p className="text-sm text-zinc-400">
          Rendering · {formatElapsed(elapsed)}
        </p>
      </div>
    </div>
  );
}

function ReadyCard({
  outputUrl,
  displayName,
  isNew,
}: {
  outputUrl: string | null;
  displayName: string;
  isNew: boolean;
}) {
  return (
    <div
      className={[
        "aspect-[9/16] w-full overflow-hidden rounded-lg bg-zinc-900",
        "motion-safe:transition-[transform,box-shadow]",
        isNew
          ? "motion-safe:animate-fade-up ring-2 ring-amber-400/60"
          : "",
      ]
        .filter(Boolean)
        .join(" ")}
    >
      {outputUrl ? (
        <div className="flex h-full flex-col">
          <video
            src={outputUrl}
            controls
            playsInline
            loop
            className="h-full w-full object-cover"
            aria-label={`${displayName} edit preview`}
          />
        </div>
      ) : (
        <div className="flex h-full items-center justify-center text-sm text-zinc-500">
          Video ready
        </div>
      )}
      {/* Download / Share actions */}
      {outputUrl && (
        <div className="flex gap-2 pt-2">
          <a
            href={outputUrl}
            download
            className="flex-1 rounded border border-zinc-700 py-1.5 text-center text-xs text-zinc-300 hover:bg-zinc-800"
          >
            Download
          </a>
          <ShareButton url={outputUrl} label={displayName} />
        </div>
      )}
    </div>
  );
}

function FailedCard({
  errorClass,
  onRetry,
}: {
  errorClass: string | null;
  onRetry?: () => void;
}) {
  const copy = errorCopy(errorClass);
  return (
    <div className="aspect-[9/16] w-full rounded-lg border border-dashed border-zinc-700 bg-zinc-900/40 p-4">
      <div className="flex h-full flex-col items-center justify-center gap-4 text-center">
        <p className="text-sm text-zinc-400">Couldn&apos;t finish this one — {copy}</p>
        {onRetry && (
          <button
            onClick={onRetry}
            className="rounded border border-zinc-600 px-3 py-1.5 text-xs text-zinc-300 hover:bg-zinc-800"
          >
            Try again
          </button>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Shimmer utility
// ---------------------------------------------------------------------------

function ShimmerSweep() {
  return (
    <div
      className="absolute inset-0 bg-[length:200%_100%] bg-gradient-to-r from-zinc-900 via-zinc-800 to-zinc-900 motion-safe:animate-shimmer"
      aria-hidden="true"
    />
  );
}

// ---------------------------------------------------------------------------
// Share button (Web Share API, gracefully degrades to copy)
// ---------------------------------------------------------------------------

function ShareButton({ url, label }: { url: string; label: string }) {
  const [copied, setCopied] = useState(false);

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
      className="flex-1 rounded border border-zinc-700 py-1.5 text-center text-xs text-zinc-300 hover:bg-zinc-800"
    >
      {copied ? "Copied!" : "Share"}
    </button>
  );
}
