// White bordered card for the light editorial system.
import type { ReactNode } from "react";

interface LightCardProps {
  children: ReactNode;
  className?: string;
}

export function LightCard({ children, className = "" }: LightCardProps) {
  return (
    <div className={`rounded-2xl border border-zinc-200 bg-white shadow-sm ${className}`}>
      {children}
    </div>
  );
}
