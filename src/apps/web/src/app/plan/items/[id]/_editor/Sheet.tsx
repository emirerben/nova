"use client";

/**
 * Pocket-editor bottom sheet primitive (mobile editor Lane A).
 *
 * Two detents:
 *  - "half" (54dvh): non-modal — role=dialog WITHOUT aria-modal, no scrim, no
 *    focus trap; the page behind stays interactive.
 *  - "full" (88dvh): modal — scrim behind the sheet (tap demotes to half),
 *    aria-modal, focus trapped via the repo's useFocusTrap (which also restores
 *    focus to the previously-focused element on close).
 *
 * Motion: entry/exit animate transform/opacity ONLY, driven by the t-modal
 * tokens from globals.css (--modal-open-dur / --modal-close-dur / --modal-ease)
 * through Tailwind arbitrary values. Detent height changes SNAP — the
 * transition property never includes height. Reduced motion is honored with
 * motion-reduce:transition-none (the .t-modal guard in globals.css only covers
 * that class, and globals.css is off-limits to this lane).
 *
 * Gestures resolve through the pure resolveSheetGesture() so the rules stay
 * unit-testable without pointer plumbing.
 */

import {
  useEffect,
  useRef,
  useState,
  type PointerEvent as ReactPointerEvent,
  type ReactNode,
} from "react";
import { createPortal } from "react-dom";
import { useFocusTrap } from "@/components/ui/useFocusTrap";
import { useKeyboardOffset } from "./useKeyboardOffset";
import { Button } from "@/components/ui/button";
import { CloseIcon } from "./editor-icons";

export type SheetDetent = "half" | "full";

export interface SheetProps {
  open: boolean;
  title: string;
  detent: SheetDetent;
  onDetentChange: (d: SheetDetent) => void;
  onClose: () => void;
  children: ReactNode;
  /** Rendered in the title row (between title and close) at FULL detent only. */
  transportSlot?: ReactNode;
  /** Optional portal target (defaults to document.body). */
  container?: Element | null;
  zIndexClassName?: string;
  testId?: string;
}

/** Vertical travel (px) before a drag counts as a detent/dismiss gesture. */
export const SHEET_DRAG_THRESHOLD_PX = 60;

export type SheetGestureAction = "promote" | "demote" | "close" | "none";

/**
 * Pure gesture-decision table for the sheet.
 *  - Horizontal-intent gestures (|dx| >= |dy|) never change detent.
 *  - Handle drags (grabber/title row) always count; body drags only count when
 *    the scroll container sits at scrollTop 0 (otherwise the user is scrolling
 *    content).
 *  - Up past threshold at half → promote. Down past threshold: full → demote,
 *    half → close — except while the keyboard is open, when drag-to-dismiss is
 *    disabled.
 */
export function resolveSheetGesture(input: {
  startedOnHandle: boolean;
  scrollTop: number;
  dx: number;
  dy: number;
  detent: SheetDetent;
  keyboardOpen: boolean;
}): SheetGestureAction {
  const { startedOnHandle, scrollTop, dx, dy, detent, keyboardOpen } = input;
  if (Math.abs(dx) >= Math.abs(dy)) return "none";
  if (Math.abs(dy) < SHEET_DRAG_THRESHOLD_PX) return "none";
  if (!startedOnHandle && scrollTop !== 0) return "none";
  if (dy < 0) {
    // Upward: only meaningful from half.
    return detent === "half" ? "promote" : "none";
  }
  // Downward.
  if (detent === "full") return "demote";
  return keyboardOpen ? "none" : "close";
}

/** Mirrors --modal-close-dur — unmount fallback after the exit transition. */
const CLOSE_UNMOUNT_MS = 150;

type Phase = "closed" | "pre" | "open" | "closing";

export default function Sheet({
  open,
  title,
  detent,
  onDetentChange,
  onClose,
  children,
  transportSlot,
  container,
  zIndexClassName = "z-[95]",
  testId = "pocket-sheet",
}: SheetProps) {
  const sheetRef = useRef<HTMLElement>(null);
  const bodyRef = useRef<HTMLDivElement>(null);
  const dragStartRef = useRef<{
    x: number;
    y: number;
    startedOnHandle: boolean;
    scrollTop: number;
  } | null>(null);
  /** Detent to restore once the keyboard closes after an auto-promote. */
  const autoPromotedFromRef = useRef<SheetDetent | null>(null);

  // ── Mount/animation phase ─────────────────────────────────────────────────
  // "pre" mounts the sheet translated off-screen; the next frame flips to
  // "open" so the CSS transition runs. "closing" plays the exit transition,
  // then a token-matched timeout unmounts. No JS animation — classes only.
  const [phase, setPhase] = useState<Phase>(open ? "pre" : "closed");

  useEffect(() => {
    if (open) setPhase((p) => (p === "open" || p === "pre" ? p : "pre"));
    else setPhase((p) => (p === "closed" ? p : "closing"));
  }, [open]);

  useEffect(() => {
    if (phase !== "pre") return;
    if (typeof requestAnimationFrame !== "function") {
      setPhase("open");
      return;
    }
    const raf = requestAnimationFrame(() => setPhase("open"));
    return () => cancelAnimationFrame(raf);
  }, [phase]);

  useEffect(() => {
    if (phase !== "closing") return;
    const t = window.setTimeout(() => setPhase("closed"), CLOSE_UNMOUNT_MS);
    return () => window.clearTimeout(t);
  }, [phase]);

  const mounted = phase !== "closed";

  // ── Modality ──────────────────────────────────────────────────────────────
  useFocusTrap(sheetRef, open && detent === "full");

  // ── Keyboard-aware detent (visualViewport) ────────────────────────────────
  const keyboardOffset = useKeyboardOffset(open);
  const keyboardOpen = keyboardOffset > 0;

  useEffect(() => {
    if (!open) {
      autoPromotedFromRef.current = null;
      return;
    }
    if (keyboardOpen) {
      if (detent === "half" && autoPromotedFromRef.current === null) {
        autoPromotedFromRef.current = detent;
        onDetentChange("full");
      }
    } else if (autoPromotedFromRef.current !== null) {
      const prior = autoPromotedFromRef.current;
      autoPromotedFromRef.current = null;
      onDetentChange(prior);
    }
  }, [open, keyboardOpen, detent, onDetentChange]);

  // ── Escape closes (half and full) ─────────────────────────────────────────
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  // ── Body scroll lock while mounted ────────────────────────────────────────
  useEffect(() => {
    if (!mounted || typeof document === "undefined") return;
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = prev;
    };
  }, [mounted]);

  // ── Gestures ──────────────────────────────────────────────────────────────
  const handlePointerDown = (e: ReactPointerEvent<HTMLElement>) => {
    const target = e.target as HTMLElement | null;
    dragStartRef.current = {
      x: e.clientX,
      y: e.clientY,
      startedOnHandle: !!target?.closest?.("[data-sheet-handle]"),
      scrollTop: bodyRef.current?.scrollTop ?? 0,
    };
    (e.currentTarget as HTMLElement).setPointerCapture?.(e.pointerId);
  };

  const handlePointerUp = (e: ReactPointerEvent<HTMLElement>) => {
    const drag = dragStartRef.current;
    dragStartRef.current = null;
    if (!drag) return;
    const action = resolveSheetGesture({
      startedOnHandle: drag.startedOnHandle,
      scrollTop: drag.scrollTop,
      dx: e.clientX - drag.x,
      dy: e.clientY - drag.y,
      detent,
      keyboardOpen,
    });
    if (action === "promote") onDetentChange("full");
    else if (action === "demote") onDetentChange("half");
    else if (action === "close") onClose();
  };

  const handlePointerCancel = () => {
    dragStartRef.current = null;
  };

  if (!mounted || typeof document === "undefined") return null;

  const isFull = detent === "full";
  const durationClass =
    phase === "closing"
      ? "duration-[var(--modal-close-dur)]"
      : "duration-[var(--modal-open-dur)]";

  const node = (
    <>
      {isFull && (
        <div
          data-testid={`${testId}-scrim`}
          aria-hidden="true"
          onClick={() => onDetentChange("half")}
          className={[
            "fixed inset-0 bg-[#0c0c0e]/15",
            "transition-opacity ease-[var(--modal-ease)] motion-reduce:transition-none",
            durationClass,
            phase === "open" ? "opacity-100" : "opacity-0",
            zIndexClassName,
          ].join(" ")}
        />
      )}
      <section
        ref={sheetRef}
        role="dialog"
        aria-modal={isFull ? true : undefined}
        aria-label={title}
        data-testid={testId}
        data-detent={detent}
        onPointerDown={handlePointerDown}
        onPointerUp={handlePointerUp}
        onPointerCancel={handlePointerCancel}
        className={[
          "fixed inset-x-0 bottom-0 flex flex-col rounded-t-2xl border-t border-zinc-200 bg-white shadow-[0_-18px_48px_rgba(12,12,14,0.2)]",
          isFull ? "h-[88dvh]" : "h-[54dvh]",
          // transform/opacity only — height changes snap.
          "transition-[transform,opacity] ease-[var(--modal-ease)] will-change-transform motion-reduce:transition-none",
          durationClass,
          phase === "open" ? "translate-y-0 opacity-100" : "translate-y-full opacity-0",
          zIndexClassName,
        ].join(" ")}
      >
        <div data-sheet-handle className="flex flex-none justify-center pt-1 touch-none">
          <Button
            type="button"
            variant="ghost"
            size="icon"
            aria-label={isFull ? "Collapse sheet" : "Expand sheet"}
            onClick={() => onDetentChange(isFull ? "half" : "full")}
            className="w-16"
          >
            <span aria-hidden="true" className="h-1 w-10 rounded-full bg-zinc-300" />
          </Button>
        </div>

        <div
          data-sheet-handle
          className="flex flex-none items-center gap-2 px-5 pb-3 touch-none"
        >
          <h2 className="font-display min-w-0 flex-1 truncate text-lg font-medium text-[#0c0c0e]">
            {title}
          </h2>
          {isFull && transportSlot != null && (
            <div className="flex min-w-0 flex-none items-center">{transportSlot}</div>
          )}
          <Button
            type="button"
            variant="ghost"
            size="icon"
            aria-label="Close"
            onClick={onClose}
            className="flex-none"
          >
            <CloseIcon className="h-4 w-4" />
          </Button>
        </div>

        <div
          ref={bodyRef}
          data-testid={`${testId}-body`}
          className="min-h-0 flex-1 overflow-y-auto overscroll-y-contain px-4 pb-[max(16px,env(safe-area-inset-bottom))]"
        >
          {children}
        </div>
      </section>
    </>
  );

  return createPortal(node, container ?? document.body);
}
