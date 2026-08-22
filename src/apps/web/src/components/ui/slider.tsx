"use client"

import * as React from "react"
import * as SliderPrimitive from "@radix-ui/react-slider"

import { cn } from "@/lib/cn"

// Stock shadcn/ui `new-york` slider (DESIGN.md §15, owner decision
// 2026-08-22): primary/20 track, primary range, 20px thumb with an
// invisible ::before expanding the hit area to the 44px touch floor (§8)
// without growing the visual thumb.
interface SliderProps
  extends React.ComponentPropsWithoutRef<typeof SliderPrimitive.Root> {
  /**
   * Radix puts the accessible `role="slider"` on the Thumb, not the Root —
   * an `aria-label`/`aria-labelledby` given to this component describes the
   * (single) thumb, so it's forwarded there instead of spread onto Root
   * where it would be inert for accessible-name computation.
   */
  "aria-label"?: string
  "aria-labelledby"?: string
}

const Slider = React.forwardRef<
  React.ElementRef<typeof SliderPrimitive.Root>,
  SliderProps
>(({ className, "aria-label": ariaLabel, "aria-labelledby": ariaLabelledBy, ...props }, ref) => (
  <SliderPrimitive.Root
    ref={ref}
    className={cn(
      "relative flex w-full touch-none select-none items-center",
      className
    )}
    {...props}
  >
    <SliderPrimitive.Track className="relative h-1.5 w-full grow overflow-hidden rounded-full bg-primary/20">
      <SliderPrimitive.Range className="absolute h-full bg-primary" />
    </SliderPrimitive.Track>
    <SliderPrimitive.Thumb
      aria-label={ariaLabel}
      aria-labelledby={ariaLabelledBy}
      className={cn(
        "relative block h-5 w-5 rounded-full border border-primary/50 bg-background shadow transition-colors",
        "before:absolute before:inset-[-12px] before:content-['']",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:pointer-events-none disabled:opacity-50"
      )}
    />
  </SliderPrimitive.Root>
))
Slider.displayName = SliderPrimitive.Root.displayName

export { Slider }
