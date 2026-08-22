import * as React from "react"

import { cn } from "@/lib/cn"

// Kria editorial input (DESIGN.md §15). h-11/text-base below `sm` keeps
// every input at/above the 16px iOS zoom-on-focus floor (§8); h-10/text-sm
// above `sm` matches the dense desktop density. Lime focus border, no ring —
// mobile-shell.test.tsx also scans `<Input` for sub-16px classes.
const Input = React.forwardRef<HTMLInputElement, React.ComponentProps<"input">>(
  ({ className, type, ...props }, ref) => {
    return (
      <input
        type={type}
        className={cn(
          "flex h-11 sm:h-10 w-full rounded-lg border border-zinc-200 bg-white px-3 text-base sm:text-sm transition-colors file:border-0 file:bg-transparent file:text-sm file:font-medium file:text-[#0c0c0e] placeholder:text-zinc-400 focus-visible:border-lime-500/60 focus-visible:outline-none focus-visible:ring-0 disabled:cursor-not-allowed disabled:opacity-50",
          className
        )}
        ref={ref}
        {...props}
      />
    )
  }
)
Input.displayName = "Input"

export { Input }
