// Eyebrow label: 11px semibold uppercase tracked.
// tone="lime" = lime-700 text; tone="muted" = zinc-400 text.
interface EyebrowProps {
  children: React.ReactNode;
  tone?: "lime" | "muted";
  className?: string;
}

export function Eyebrow({ children, tone = "muted", className = "" }: EyebrowProps) {
  const color = tone === "lime" ? "text-lime-700" : "text-[#a1a1aa]";
  return (
    <p className={`text-[11px] font-semibold uppercase tracking-[0.18em] ${color} ${className}`}>
      {children}
    </p>
  );
}
