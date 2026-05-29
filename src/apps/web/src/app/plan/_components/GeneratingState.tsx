/**
 * Inline "we're working on it" state for async generation (persona + plan).
 * Replaces the old full-page blank takeover — the stepper stays visible above
 * this, so the user keeps their place in the flow. Shimmering skeleton lines
 * preview the shape of what's coming.
 */
export default function GeneratingState({
  title,
  subtitle,
  lines = 4,
}: {
  title: string;
  subtitle: string;
  lines?: number;
}) {
  return (
    <div className="animate-fade-up py-6">
      <div className="mb-6 flex items-center gap-3">
        <span className="relative flex h-3 w-3">
          <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-amber-400/70" />
          <span className="relative inline-flex h-3 w-3 rounded-full bg-amber-400" />
        </span>
        <h1 className="font-display text-2xl text-white">{title}</h1>
      </div>
      <p className="mb-8 text-zinc-400">{subtitle}</p>
      <div className="space-y-3">
        {Array.from({ length: lines }).map((_, i) => (
          <div
            key={i}
            className="h-4 rounded bg-[length:200%_100%] bg-gradient-to-r from-zinc-900 via-zinc-800 to-zinc-900 animate-shimmer"
            style={{ width: `${90 - i * 12}%` }}
          />
        ))}
      </div>
    </div>
  );
}
