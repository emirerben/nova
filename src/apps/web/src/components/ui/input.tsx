import * as React from "react"

import { cn } from "@/lib/cn"

// Stock shadcn/ui `new-york` input (DESIGN.md §15, owner decision
// 2026-08-22). `h-10 sm:h-9`/`text-base sm:text-sm` is kept below `sm` so
// every input stays at/above the 16px iOS zoom-on-focus floor (§8) —
// mobile-shell.test.tsx scans `<Input` for sub-16px classes.
const Input = React.forwardRef<HTMLInputElement, React.ComponentProps<"input">>(
  ({ className, type, ...props }, ref) => {
    return (
      <input
        type={type}
        className={cn(
          "flex h-10 sm:h-9 w-full rounded-md border border-input bg-background px-3 py-2 text-base sm:text-sm ring-offset-background file:border-0 file:bg-transparent file:text-sm file:font-medium file:text-foreground placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50",
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
