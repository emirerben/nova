import * as React from "react"

import { cn } from "@/lib/cn"

// Stock shadcn/ui `new-york` textarea (DESIGN.md §15, owner decision
// 2026-08-22) — same token set as input.tsx. `min-h` (not `h`) so a `rows`
// prop can still grow it; `text-base sm:text-sm` stays the 16px iOS
// zoom-on-focus floor below `sm` (§8) — mobile-shell's guard cares about it.
const Textarea = React.forwardRef<
  HTMLTextAreaElement,
  React.ComponentProps<"textarea">
>(({ className, ...props }, ref) => {
  return (
    <textarea
      className={cn(
        "flex min-h-10 sm:min-h-9 w-full rounded-md border border-input bg-background px-3 py-2 text-base sm:text-sm ring-offset-background placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50",
        className
      )}
      ref={ref}
      {...props}
    />
  )
})
Textarea.displayName = "Textarea"

export { Textarea }
