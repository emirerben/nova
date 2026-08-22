import * as React from "react"
import { Slot } from "@radix-ui/react-slot"
import { cva, type VariantProps } from "class-variance-authority"

import { cn } from "@/lib/cn"

// Kria editorial button (DESIGN.md §15). Pills, not the shadcn default
// rounded-md — every surface gets exactly one solid `ink`/`default` CTA;
// everything else is `outline`/`ghost`/`link`. `destructive` is zinc, never
// red (D10 "no red walls"). Sizes are pinned by
// src/__tests__/ui/button.test.tsx (default/sm token strings) — do not
// change px-9/py-[15px]/text-[15px] (default) or h-9/px-5/text-[13px] (sm)
// without updating that guard.
const buttonVariants = cva(
  "inline-flex items-center justify-center gap-2 whitespace-nowrap rounded-full font-semibold transition-opacity focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#0c0c0e] disabled:pointer-events-none disabled:opacity-40 [&_svg]:pointer-events-none [&_svg]:size-4 [&_svg]:shrink-0",
  {
    variants: {
      variant: {
        // "default" is the cva fallback (no variant prop supplied); "ink" is
        // the same look under an explicit name so callers can be intentional
        // (the `<InkButton>` wrapper always passes `variant="ink"`).
        default: "bg-[#0c0c0e] text-white hover:opacity-80",
        ink: "bg-[#0c0c0e] text-white hover:opacity-80",
        outline:
          "border border-zinc-200 bg-white text-[#3f3f46] hover:border-zinc-400",
        secondary: "bg-zinc-100 text-[#0c0c0e] hover:bg-zinc-200",
        ghost: "text-[#0c0c0e] hover:bg-zinc-100",
        link: "text-[#71717a] underline-offset-4 hover:underline",
        // Soft lime pill — eyebrow-style secondary actions, never a
        // destructive or primary CTA.
        lime: "border border-lime-200 bg-lime-50 text-lime-800 hover:bg-lime-100",
        // Deliberately NOT red — D10. Zinc fill signals "serious" without
        // the alarm-red anti-pattern.
        destructive: "bg-[#3f3f46] text-white hover:opacity-80",
      },
      size: {
        default: "px-9 py-[15px] text-[15px]",
        sm: "h-9 px-5 text-[13px]",
        lg: "min-h-12 px-9 text-[15px]",
        icon: "h-11 w-11",
        "icon-sm": "h-8 w-8",
        pill: "min-h-11 px-3 py-1.5 text-xs",
      },
    },
    defaultVariants: {
      variant: "default",
      size: "default",
    },
  }
)

export interface ButtonProps
  extends React.ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof buttonVariants> {
  asChild?: boolean
}

const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant, size, asChild = false, ...props }, ref) => {
    const Comp = asChild ? Slot : "button"
    return (
      <Comp
        className={cn(buttonVariants({ variant, size, className }))}
        ref={ref}
        {...props}
      />
    )
  }
)
Button.displayName = "Button"

export { Button, buttonVariants }
