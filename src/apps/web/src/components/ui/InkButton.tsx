// Ink pill CTA — the primary action button for the light editorial system.
// Thin wrapper over the shadcn `Button` primitive (DESIGN.md §15) so every
// existing `<InkButton>` call site keeps working unchanged.
// variant="solid" = #0c0c0e fill, white text -> Button variant="ink".
// variant="ghost" = transparent, #71717a text, underline on hover -> Button variant="link".
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
