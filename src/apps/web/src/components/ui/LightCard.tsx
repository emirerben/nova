// White bordered card for the light editorial system.
// Thin wrapper over the shadcn `Card` primitive (DESIGN.md §15) so every
// existing `<LightCard>` call site keeps working unchanged.
import type { ReactNode } from "react";
import { Card } from "./card";

interface LightCardProps {
  children: ReactNode;
  className?: string;
}

export function LightCard({ children, className = "" }: LightCardProps) {
  return <Card className={className}>{children}</Card>;
}
