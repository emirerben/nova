import { cn } from "@/lib/cn";

const WIDTHS = {
  default: "max-w-2xl", // readable editorial column (wizard steps, idea header)
  wide: "max-w-3xl",
  results: "max-w-6xl", // focused-player results: hero + filmstrip + editor need room
} as const;

/**
 * Editorial wrapper shared by every step of the plan wizard + the item page.
 * Black canvas, centered column, generous vertical rhythm.
 *
 * `size` controls the column width; `wide` is kept for back-compat (= "wide").
 * The generated-video results view uses `size="results"` so three 9:16 videos
 * aren't crushed into a 672px column.
 */
export default function PlanShell({
  children,
  className,
  wide = false,
  size,
}: {
  children: React.ReactNode;
  className?: string;
  wide?: boolean;
  size?: keyof typeof WIDTHS;
}) {
  const widthClass = WIDTHS[size ?? (wide ? "wide" : "default")];
  return (
    <main className="min-h-[calc(100vh-3.5rem)] bg-black text-white">
      <div className={cn("mx-auto px-4", widthClass, className)}>{children}</div>
    </main>
  );
}
