import * as React from "react"
import { cva, type VariantProps } from "class-variance-authority"

import { cn } from "@/lib/cn"

// Kria editorial badge (DESIGN.md §15) — pill/eyebrow status chips.
// `ink` = solid dark pill (rare, emphasis only). `lime` = bare uppercase
// eyebrow text, no fill (e.g. "From your idea"-style labels). `lime-soft` =
// the lime-50/200/800 soft pill used for "Ready to post" / positive status.
// `zinc` = neutral status default ("Rendering…", "Needs footage").
const badgeVariants = cva(
  "inline-flex items-center gap-1 rounded-full text-[11px] font-semibold uppercase tracking-[0.18em] transition-colors focus:outline-none",
  {
    variants: {
      variant: {
        ink: "border border-transparent bg-[#0c0c0e] px-2.5 py-1 text-white",
        lime: "border-transparent bg-transparent px-0 py-0 text-lime-700",
        "lime-soft":
          "border border-lime-200 bg-lime-50 px-2.5 py-1 text-lime-800",
        zinc: "border border-zinc-200 bg-white px-2.5 py-1 text-[#71717a]",
      },
    },
    defaultVariants: {
      variant: "zinc",
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
