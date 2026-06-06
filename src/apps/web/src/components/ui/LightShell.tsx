// Cream canvas wrapper for the light editorial system.
// size="narrow" = max-w-[680px] centered (setup screens)
// size="wide" = max-w-[1180px] (workspace, library, generative)
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
