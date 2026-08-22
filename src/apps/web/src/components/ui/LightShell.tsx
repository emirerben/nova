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
    // Page body is bg-black text-white (src/app/layout.tsx); light pages
    // like this one only override the background, so a stock shadcn
    // primitive that inherits color (Label, etc.) rendered white-on-white
    // until text-foreground was added here.
    <div className={`min-h-screen bg-background text-foreground ${className}`}>
      <div className={`mx-auto ${maxW} px-6 pb-24 pt-16`}>{children}</div>
    </div>
  );
}
