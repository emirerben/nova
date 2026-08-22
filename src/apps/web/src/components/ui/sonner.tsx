"use client"

import { Toaster as Sonner } from "sonner"

type ToasterProps = React.ComponentProps<typeof Sonner>

// Kria toast (DESIGN.md §15) — single bottom-center, ink pill, matches the
// pre-existing editor toast look (EditorShell.tsx's old `setToast` styling).
// No `next-themes`: every user-facing surface (incl. the editor) is light
// only, so the toast is unconditionally styled, not theme-switched.
const Toaster = ({ ...props }: ToasterProps) => {
  return (
    <Sonner
      position="bottom-center"
      duration={2600}
      closeButton={false}
      className="toaster group"
      toastOptions={{
        unstyled: true,
        classNames: {
          toast:
            "flex items-center gap-2 rounded-lg bg-[#0c0c0e] px-3 py-1.5 text-[12px] text-white shadow-lg",
          description: "text-[12px] text-zinc-300",
          actionButton: "text-lime-300 underline underline-offset-2",
          cancelButton: "text-zinc-400 underline underline-offset-2",
        },
      }}
      {...props}
    />
  )
}

export { Toaster }
