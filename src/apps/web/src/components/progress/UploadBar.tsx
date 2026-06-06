"use client";

/**
 * Determinate upload progress bar sharing the EtaBar visual language.
 * Shows byte-accurate progress. NO ETA — never fabricate a time estimate
 * for uploads (D6: only show figures backed by measured data).
 */
interface UploadBarProps {
  /** Progress fraction 0..1 */
  progress: number;
  /** Optional label shown below the bar, e.g. "3 / 5 MB" or "2 of 4" */
  label?: string;
  /** Accessible label for the progress element. Default: "Upload progress" */
  ariaLabel?: string;
}

export function UploadBar({ progress, label, ariaLabel = "Upload progress" }: UploadBarProps) {
  const pct = Math.round(Math.min(1, Math.max(0, progress)) * 100);
  return (
    <div className="w-full">
      <div
        role="progressbar"
        aria-label={ariaLabel}
        aria-valuenow={pct}
        aria-valuemin={0}
        aria-valuemax={100}
        className="relative h-1 w-full overflow-hidden rounded-full bg-zinc-800"
      >
        <div
          className="relative h-full rounded-full bg-amber-400 motion-safe:transition-[width] motion-safe:duration-300 motion-safe:ease-linear"
          style={{ width: `${pct}%` }}
        />
        {/* Shimmer tip — same as EtaBar */}
        <div
          aria-hidden="true"
          className="pointer-events-none absolute inset-y-0 right-0 w-16 bg-gradient-to-r from-transparent via-amber-200/60 to-transparent motion-safe:animate-shimmer bg-[length:200%_100%]"
        />
      </div>
      {label && (
        <p className="mt-1 text-xs text-zinc-500 tabular-nums">{label}</p>
      )}
    </div>
  );
}
