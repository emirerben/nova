import * as React from "react"
import { cva, type VariantProps } from "class-variance-authority"

import { cn } from "@/lib/cn"

// Stock shadcn/ui `new-york` badge (DESIGN.md §15, owner decision
// 2026-08-22 — replaces the hand-edited Kria pill/eyebrow variant). `ink`,
// `lime`, `lime-soft`, and `zinc` are aliases kept byte-identical to their
// stock base variant so existing call sites keep working unchanged.
const badgeVariants = cva(
  "inline-flex items-center rounded-md border px-2.5 py-0.5 text-xs font-semibold transition-colors focus:outline-none focus:ring-2 focus:ring-ring focus:ring-offset-2",
  {
    variants: {
      variant: {
        default:
          "border-transparent bg-primary text-primary-foreground hover:bg-primary/80",
        secondary:
          "border-transparent bg-secondary text-secondary-foreground hover:bg-secondary/80",
        destructive:
          "border-transparent bg-destructive text-destructive-foreground hover:bg-destructive/80",
        outline: "text-foreground",
        // Aliases (§15 variant table) — same look as their stock base.
        ink: "border-transparent bg-primary text-primary-foreground hover:bg-primary/80",
        lime: "border-transparent bg-secondary text-secondary-foreground hover:bg-secondary/80",
        "lime-soft":
          "border-transparent bg-secondary text-secondary-foreground hover:bg-secondary/80",
        zinc: "text-foreground",
      },
    },
    defaultVariants: {
      variant: "default",
    },
  }
)

export interface BadgeProps
  extends React.HTMLAttributes<HTMLDivElement>,
    VariantProps<typeof badgeVariants> {}

function Badge({ className, variant, ...props }: BadgeProps) {
  return (
    <div className={cn(badgeVariants({ variant }), className)} {...props} />
  )
}

export { Badge, badgeVariants }
