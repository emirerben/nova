// Cream canvas wrapper for the /plan flow light editorial surfaces.
// size="narrow" = max-w-[680px] centered (setup screens)
// size="wide" = max-w-[1180px] (workspace — used in PR2)
import type { ReactNode } from "react";

interface LightShellProps {
  children: ReactNode;
  size?: "narrow" | "wide";
  className?: string;
}

export function LightShell({ children, size = "narrow", className = "" }: LightShellProps) {
  const maxW = size === "wide" ? "max-w-[1180px]" : "max-w-[680px]";
  return (
    <div className={`min-h-screen bg-[#fafaf8] ${className}`}>
      <div className={`mx-auto ${maxW} px-6 pb-24 pt-16`}>{children}</div>
    </div>
  );
}
