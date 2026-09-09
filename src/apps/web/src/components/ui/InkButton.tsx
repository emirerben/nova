// Ink pill CTA — the primary action button for the light editorial system.
// Thin wrapper over the shadcn `Button` primitive (DESIGN.md §15) so every
// existing `<InkButton>` call site keeps working unchanged.
// variant="solid" = Sunlit primary (Butter) fill with warm-ink text ->
// Button variant="ink" (the legacy variant name is kept for call-site
// compatibility). variant="ghost" stays a quiet text link.
import type { ButtonHTMLAttributes, ReactNode } from "react";
import { Button } from "./button";

interface InkButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  children: ReactNode;
  variant?: "solid" | "ghost";
  size?: "default" | "compact";
}

export function InkButton({
  children,
  variant = "solid",
  size = "default",
  className = "",
  ...props
}: InkButtonProps) {
  return (
    <Button
      variant={variant === "solid" ? "ink" : "link"}
      size={size === "compact" ? "sm" : "default"}
      className={className}
      {...props}
    >
      {children}
    </Button>
  );
}
