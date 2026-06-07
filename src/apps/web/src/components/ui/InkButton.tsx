// Ink pill CTA — the primary action button for the light editorial system.
// variant="solid" = #0c0c0e fill, white text.
// variant="ghost" = transparent, #71717a text, underline on hover.
import type { ButtonHTMLAttributes, ReactNode } from "react";

interface InkButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  children: ReactNode;
  variant?: "solid" | "ghost";
}

export function InkButton({ children, variant = "solid", className = "", ...props }: InkButtonProps) {
  const base = "inline-flex items-center justify-center rounded-full text-[15px] font-semibold transition-opacity hover:opacity-80 focus-visible:outline-2 focus-visible:outline-[#0c0c0e] disabled:opacity-40";
  const styles = variant === "solid"
    ? "bg-[#0c0c0e] px-9 py-[15px] text-white"
    : "px-4 py-2 text-[#71717a] hover:underline underline-offset-4";
  return (
    <button className={`${base} ${styles} ${className}`} {...props}>
      {children}
    </button>
  );
}
