import * as React from "react"

import { cn } from "@/lib/cn"

// Kria editorial textarea (DESIGN.md §15) — same token set as input.tsx.
// `min-h` (not `h`) so a `rows` prop can still grow it past the touch floor;
// the floor itself (44px / 16px text below `sm`) is what mobile-shell's
// iOS-zoom guard cares about.
const Textarea = React.forwardRef<
  HTMLTextAreaElement,
  React.ComponentProps<"textarea">
>(({ className, ...props }, ref) => {
  return (
    <textarea
      className={cn(
        "flex min-h-11 sm:min-h-10 w-full rounded-lg border border-zinc-200 bg-white px-3 py-2 text-base sm:text-sm placeholder:text-zinc-400 focus-visible:border-lime-500/60 focus-visible:outline-none focus-visible:ring-0 disabled:cursor-not-allowed disabled:opacity-50",
        className
      )}
      ref={ref}
      {...props}
    />
  )
})
Textarea.displayName = "Textarea"

export { Textarea }
