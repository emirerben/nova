import { cn } from "@/lib/cn";

/**
 * Editorial wrapper shared by every step of the plan wizard + the item page.
 * Black canvas, centered column, generous vertical rhythm.
 */
export default function PlanShell({
  children,
  className,
  wide = false,
}: {
  children: React.ReactNode;
  className?: string;
  wide?: boolean;
}) {
  return (
    <main className="min-h-[calc(100vh-3.5rem)] bg-black text-white">
      <div className={cn("mx-auto px-4", wide ? "max-w-3xl" : "max-w-2xl", className)}>
        {children}
      </div>
    </main>
  );
}
