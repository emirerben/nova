"use client"

import { Toaster as Sonner } from "sonner"

type ToasterProps = React.ComponentProps<typeof Sonner>

// Stock shadcn/ui Toaster (DESIGN.md §15, owner decision 2026-08-22) — CSS
// custom properties, not a hand-styled `unstyled` override. No `next-themes`:
// every user-facing surface (incl. the editor) is light only, so `theme` is
// pinned to `"light"` rather than reading a theme provider. Position stays
// bottom-center per the pre-existing Kria toast placement.
const Toaster = ({ ...props }: ToasterProps) => {
  return (
    <Sonner
      theme="light"
      position="bottom-center"
      duration={2600}
      closeButton={false}
      className="toaster group"
      style={
        {
          "--normal-bg": "hsl(var(--popover))",
          "--normal-text": "hsl(var(--popover-foreground))",
          "--normal-border": "hsl(var(--border))",
        } as React.CSSProperties
      }
      {...props}
    />
  )
}

export { Toaster }
