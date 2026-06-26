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
 * - ready:     9:16 video player + Download; arrive animation on isNewlyReady;
 *              t-skel cross-blur reveals content from skeleton on isNewlyReady
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

  // t-skel reveal state. Starts true if status is already "ready" on mount
  // (no animation needed — show content immediately). Otherwise false until
  // isNewlyReady fires, which plays the cross-blur skeleton→content transition.
  const [revealed, setRevealed] = useState(render_status === "ready");

  useEffect(() => {
    if (isNewlyReady && !arrivedRef.current) {
      arrivedRef.current = true;
      setArrivedOnce(true);
      // One rAF so the skeleton layer is fully painted before we flip to content.
      const raf = requestAnimationFrame(() => setRevealed(true));
      return () => cancelAnimationFrame(raf);
    }
  }, [isNewlyReady]);

  // Fallback: handle render_status becoming "ready" without isNewlyReady
  // (e.g. polling catches a done state without a transition event).
  // setRevealed(true) is idempotent — safe to call even if already revealed.
  useEffect(() => {
    if (render_status === "ready") setRevealed(true);
  }, [render_status]);

  const labelClass = tone === "light" ? "text-[#3f3f46]" : "text-zinc-300";
  const bodyClass = tone === "light" ? "bg-zinc-100" : "bg-zinc-900";
  const ringClass = tone === "light" ? "ring-lime-600/60" : "ring-amber-400/60";

  // Failed state: no t-skel needed, render directly.
  if (render_status === "failed") {
    return (
      <div role="group" aria-label={`${displayName} edit`} className="flex flex-col gap-2">
        <p className={`text-sm font-medium ${labelClass}`}>{displayName}</p>
        <FailedCard errorClass={error_class ?? null} onRetry={onRetry} tone={tone} />
      </div>
    );
  }

  return (
    <div
      role="group"
      aria-label={`${displayName} edit`}
      className="flex flex-col gap-2"
    >
      {/* Variant label */}
      <p className={`text-sm font-medium ${labelClass}`}>{displayName}</p>

      {/* t-skel: stacks skeleton + content layers; .is-revealed cross-blurs between them.
          The arrive ring (D12) is also applied here so it frames the whole card. */}
      <div
        className={[
          `t-skel aspect-[9/16] w-full overflow-hidden rounded-lg ${bodyClass}`,
          revealed ? "is-revealed" : "",
          arrivedOnce ? `ring-2 ${ringClass}` : "",
        ]
          .filter(Boolean)
          .join(" ")}
      >
        {/* Skeleton layer: looping shimmer + status text (pending or rendering).
            ShimmerSweep's animate-shimmer already loops — we don't use is-pulsing
            (one-shot) since renders can take 60s+. */}
        <div className="t-skel-skeleton">
          <ShimmerSweep tone={tone} />
          <div className="absolute inset-0 flex flex-col items-center justify-center gap-3">
            {render_status === "rendering" ? (
              <RenderingStatus
                startedAt={render_started_at ?? null}
                tone={tone}
                onRetry={onRetry}
              />
            ) : (
              <PendingStatus tone={tone} />
            )}
          </div>
        </div>

        {/* Content layer: ready state with video.
            Invisible (opacity 0, blurred) until .is-revealed flips it visible.
            Uses StableVideo so re-signed URLs (new ?X-Goog-Signature every 2s poll)
            don't restart playback. */}
        <div className="t-skel-content">
          <ReadyCardInner
            outputUrl={output_url ?? null}
            renderFinishedAt={render_finished_at ?? null}
            displayName={displayName}
            tone={tone}
          />
        </div>
      </div>

      {/* Download / Share actions shown below the card after reveal */}
      {revealed && output_url && (
        <ReadyCardActions outputUrl={output_url} displayName={displayName} tone={tone} />
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Skeleton layer sub-components (status text inside shimmer)
// ---------------------------------------------------------------------------

function PendingStatus({ tone }: { tone: "dark" | "light" }) {
  const textClass = tone === "light" ? "text-[#71717a]" : "text-zinc-500";
  return <p className={`text-sm ${textClass}`}>Getting ready…</p>;
}

function RenderingStatus({
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
  const textClass = tone === "light" ? "text-[#71717a]" : "text-zinc-400";
  const btnClass = tone === "light"
    ? "rounded border border-zinc-200 px-3 py-1 text-xs text-[#3f3f46] hover:bg-zinc-100"
    : "rounded border border-zinc-600 px-3 py-1 text-xs text-zinc-300 hover:bg-zinc-800";

  if (stalled) {
    return (
      <>
        <p className={`text-sm ${textClass}`}>Taking longer than usual…</p>
        {onRetry && (
          <button onClick={onRetry} className={btnClass}>
            Try again
          </button>
        )}
      </>
    );
  }

  return (
    <p className={`text-sm ${textClass}`}>
      Rendering · {formatElapsed(elapsed)}
    </p>
  );
}

// ---------------------------------------------------------------------------
// Content layer (video + empty state, inside t-skel-content)
// ---------------------------------------------------------------------------

function ReadyCardInner({
  outputUrl,
  renderFinishedAt,
  displayName,
  tone,
}: {
  outputUrl: string | null;
  renderFinishedAt: string | null;
  displayName: string;
  tone: "dark" | "light";
}) {
  const emptyTextClass = tone === "light" ? "text-[#71717a]" : "text-zinc-500";
  const stableVideoSrc = outputUrl ?? undefined;

  return (
    <div className="h-full w-full">
      {stableVideoSrc ? (
        <StableVideo
          src={stableVideoSrc}
          identity={renderFinishedAt ?? undefined}
          controls
          playsInline
          loop
          className="h-full w-full object-cover"
          aria-label={`${displayName} edit preview`}
        />
      ) : (
        <div className={`flex h-full items-center justify-center text-sm ${emptyTextClass}`}>
          Video ready
        </div>
      )}
    </div>
  );
}

// Download / Share actions rendered below the card after reveal.
function ReadyCardActions({
  outputUrl,
  displayName,
  tone,
}: {
  outputUrl: string;
  displayName: string;
  tone: "dark" | "light";
}) {
  const btnClass = tone === "light"
    ? "border-zinc-200 text-[#3f3f46] hover:bg-zinc-100"
    : "border-zinc-700 text-zinc-300 hover:bg-zinc-800";
  return (
    <div className="flex gap-2">
      <a
        href={outputUrl}
        download
        className={`flex-1 rounded border py-1.5 text-center text-xs ${btnClass}`}
      >
        Download
      </a>
      <ShareButton url={outputUrl} label={displayName} tone={tone} />
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
