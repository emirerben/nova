"use client";

// InfoDot — the quiet ⓘ that replaces inline helper paragraphs (DESIGN.md §2).
// Desktop: hovering the dot opens the popover after 150ms and it follows the
// pointer away after 200ms; clicking pins it open until outside-tap/Esc.
// Touch: tap toggles (pointerType "touch" never hover-opens).
// Never used for warnings, errors, disabled reasons, or destructive confirms —
// those stay visible. Max ~2 dots per screen section. Render as a SIBLING of
// the label text, never inside a <label> (wrapped-label click/focus breakage).

import * as Popover from "@radix-ui/react-popover";
import { useEffect, useRef, useState } from "react";
import type { PointerEvent as ReactPointerEvent, ReactNode } from "react";

const HOVER_OPEN_MS = 150;
const HOVER_CLOSE_MS = 200;

interface InfoDotProps {
  /** What the dot explains — becomes the trigger's aria-label ("About {label}"). */
  label: string;
  /** One or two plain sentences. No headings, no CTAs, max ~3 lines. */
  children: ReactNode;
  /** compact = 32px hit area for dense inspector rows; default 44px. */
  size?: "default" | "compact";
  /** Preferred popover side; Radix flips automatically on collision. */
  side?: "top" | "right" | "bottom" | "left";
  className?: string;
}

function InfoGlyph() {
  return (
    <svg
      width="16"
      height="16"
      viewBox="0 0 16 16"
      aria-hidden="true"
      className="text-zinc-400 transition-colors group-hover:text-[#0c0c0e] group-focus-visible:text-[#0c0c0e] group-data-[state=open]:text-lime-700"
    >
      <circle cx="8" cy="8" r="7.25" fill="none" stroke="currentColor" strokeWidth="1.5" />
      <rect x="7.25" y="7" width="1.5" height="4.4" rx="0.75" fill="currentColor" />
      <circle cx="8" cy="4.9" r="0.95" fill="currentColor" />
    </svg>
  );
}

export function InfoDot({
  label,
  children,
  size = "default",
  side = "bottom",
  className = "",
}: InfoDotProps) {
  const hitArea = size === "compact" ? "h-8 w-8" : "h-11 w-11";
  const [open, setOpen] = useState(false);
  // A clicked-open (or clicked-while-hovering) dot is "pinned": pointer-leave
  // no longer closes it — only outside tap, Esc, or clicking the dot again.
  const pinnedRef = useRef(false);
  const timerRef = useRef<number | null>(null);

  const clearTimer = () => {
    if (timerRef.current !== null) {
      window.clearTimeout(timerRef.current);
      timerRef.current = null;
    }
  };
  useEffect(() => clearTimer, []);

  // Touch (and pen) taps fire pointerenter too — hover intent is mouse-only.
  const isHoverPointer = (e: ReactPointerEvent) =>
    e.pointerType !== "touch" && e.pointerType !== "pen";

  const onHoverEnter = (e: ReactPointerEvent) => {
    if (!isHoverPointer(e)) return;
    clearTimer();
    if (open) return;
    timerRef.current = window.setTimeout(() => setOpen(true), HOVER_OPEN_MS);
  };

  const onHoverLeave = (e: ReactPointerEvent) => {
    if (!isHoverPointer(e)) return;
    clearTimer();
    if (!open || pinnedRef.current) return;
    timerRef.current = window.setTimeout(() => setOpen(false), HOVER_CLOSE_MS);
  };

  return (
    <Popover.Root
      open={open}
      onOpenChange={(next) => {
        setOpen(next);
        if (!next) pinnedRef.current = false;
        clearTimer();
      }}
    >
      <Popover.Trigger asChild>
        <button
          type="button"
          aria-label={`About ${label}`}
          onPointerEnter={onHoverEnter}
          onPointerLeave={onHoverLeave}
          onClick={(e) => {
            if (open && !pinnedRef.current) {
              // Hover-opened: the click pins instead of toggling closed.
              // preventDefault stops Radix's own trigger toggle (it composes
              // handlers with a defaultPrevented check).
              pinnedRef.current = true;
              clearTimer();
              e.preventDefault();
              return;
            }
            pinnedRef.current = !open;
          }}
          className={`group -my-2 inline-flex ${hitArea} shrink-0 items-center justify-center rounded-full focus-visible:outline-2 focus-visible:outline-lime-500 ${className}`}
        >
          <span className="flex h-7 w-7 items-center justify-center rounded-full transition-colors group-hover:bg-zinc-100 group-focus-visible:bg-zinc-100 group-data-[state=open]:bg-lime-50">
            <InfoGlyph />
          </span>
        </button>
      </Popover.Trigger>
      <Popover.Portal>
        <Popover.Content
          side={side}
          sideOffset={6}
          collisionPadding={12}
          onPointerEnter={onHoverEnter}
          onPointerLeave={onHoverLeave}
          onOpenAutoFocus={(e) => {
            // Hover-open must not steal focus from what the user is doing;
            // pinned (clicked/keyboard) open keeps Radix's focus handoff.
            if (!pinnedRef.current) e.preventDefault();
          }}
          onEscapeKeyDown={(e) => {
            // Radix listens for Escape in the capture phase; stopping immediate
            // propagation keeps host surfaces with their own document-level
            // Escape handlers (editor Sheet, CopilotDrawer) from also closing.
            // The popover itself still dismisses.
            e.stopImmediatePropagation();
          }}
          className="info-dot-pop z-[130] max-w-[280px] rounded-[12px] border border-zinc-200 bg-white px-3.5 py-3 text-[13px] leading-[19px] text-[#3f3f46] shadow-[0_12px_30px_rgba(0,0,0,0.10)]"
        >
          {children}
          <Popover.Arrow className="fill-white drop-shadow-[0_1px_0_#e4e4e7]" width={12} height={6} />
        </Popover.Content>
      </Popover.Portal>
    </Popover.Root>
  );
}
