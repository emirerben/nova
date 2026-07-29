/**
 * Pocket-editor icon set (mobile editor Lane A).
 *
 * 24×24 stroke glyphs drawn to one spec: viewBox "0 0 24 24", stroke
 * currentColor at 1.6, round caps/joins, aria-hidden (accessible names come
 * from the wrapping control). PlayIcon is the one filled glyph.
 */

import type { ReactNode } from "react";

export interface IconProps {
  className?: string;
}

function StrokeIcon({ className, children }: IconProps & { children: ReactNode }) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.6}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      className={className}
    >
      {children}
    </svg>
  );
}

/** 4-point sparkle. */
export function NovaIcon({ className }: IconProps) {
  return (
    <StrokeIcon className={className}>
      <path d="M12 3.5L13.9 10.1L20.5 12L13.9 13.9L12 20.5L10.1 13.9L3.5 12L10.1 10.1Z" />
    </StrokeIcon>
  );
}

/** Serif T. */
export function TextIcon({ className }: IconProps) {
  return (
    <StrokeIcon className={className}>
      <path d="M4.5 7V4.5h15V7" />
      <path d="M12 4.5v15" />
      <path d="M9 19.5h6" />
    </StrokeIcon>
  );
}

/** Rounded rect + two caption lines. */
export function CaptionsIcon({ className }: IconProps) {
  return (
    <StrokeIcon className={className}>
      <rect x="3" y="5" width="18" height="14" rx="2.5" />
      <path d="M7 12.5h10" />
      <path d="M7 15.5h6" />
    </StrokeIcon>
  );
}

/** 2×2 grid. */
export function VisualsIcon({ className }: IconProps) {
  return (
    <StrokeIcon className={className}>
      <rect x="4" y="4" width="7" height="7" rx="1.5" />
      <rect x="13" y="4" width="7" height="7" rx="1.5" />
      <rect x="4" y="13" width="7" height="7" rx="1.5" />
      <rect x="13" y="13" width="7" height="7" rx="1.5" />
    </StrokeIcon>
  );
}

/** Beamed note + two circles. */
export function SoundsIcon({ className }: IconProps) {
  return (
    <StrokeIcon className={className}>
      <path d="M9 17.5V6l10-2v11.5" />
      <circle cx="6.5" cy="17.5" r="2.5" />
      <circle cx="16.5" cy="15.5" r="2.5" />
    </StrokeIcon>
  );
}

/** Rect with inset pip rect. */
export function OverlaysIcon({ className }: IconProps) {
  return (
    <StrokeIcon className={className}>
      <rect x="3" y="4.5" width="18" height="15" rx="2.5" />
      <rect x="12" y="12" width="5.5" height="4.5" rx="1" />
    </StrokeIcon>
  );
}

/** Large 4-point star + small star. */
export function StylesIcon({ className }: IconProps) {
  return (
    <StrokeIcon className={className}>
      <path d="M9.5 4.5L11 9L15.5 10.5L11 12L9.5 16.5L8 12L3.5 10.5L8 9Z" />
      <path d="M17.5 13.5l.9 2.6l2.6.9l-2.6.9l-.9 2.6l-.9-2.6l-2.6-.9l2.6-.9Z" />
    </StrokeIcon>
  );
}

/** ‹ chevron. */
export function BackIcon({ className }: IconProps) {
  return (
    <StrokeIcon className={className}>
      <path d="M14.5 5.5L8 12l6.5 6.5" />
    </StrokeIcon>
  );
}

/** Curved arrow, counter-clockwise. */
export function UndoIcon({ className }: IconProps) {
  return (
    <StrokeIcon className={className}>
      <path d="M4.5 9.5h10a5 5 0 0 1 0 10H10" />
      <path d="M8.5 5.5l-4 4l4 4" />
    </StrokeIcon>
  );
}

/** Curved arrow, clockwise. */
export function RedoIcon({ className }: IconProps) {
  return (
    <StrokeIcon className={className}>
      <path d="M19.5 9.5h-10a5 5 0 0 0 0 10H14" />
      <path d="M15.5 5.5l4 4l-4 4" />
    </StrokeIcon>
  );
}

/** ⤢ diagonal expand arrows. */
export function ExpandIcon({ className }: IconProps) {
  return (
    <StrokeIcon className={className}>
      <path d="M14 4.5h5.5V10" />
      <path d="M19.5 4.5L13 11" />
      <path d="M10 19.5H4.5V14" />
      <path d="M4.5 19.5L11 13" />
    </StrokeIcon>
  );
}

/** Filled play triangle. */
export function PlayIcon({ className }: IconProps) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="currentColor"
      stroke="none"
      aria-hidden="true"
      className={className}
    >
      <path d="M8 5.4v13.2a.6.6 0 0 0 .92.5l10.1-6.6a.6.6 0 0 0 0-1L8.92 4.9a.6.6 0 0 0-.92.5Z" />
    </svg>
  );
}

/** Two pause bars. */
export function PauseIcon({ className }: IconProps) {
  return (
    <StrokeIcon className={className}>
      <path d="M8.5 5.5v13" />
      <path d="M15.5 5.5v13" />
    </StrokeIcon>
  );
}

export function PlusIcon({ className }: IconProps) {
  return (
    <StrokeIcon className={className}>
      <path d="M12 5v14" />
      <path d="M5 12h14" />
    </StrokeIcon>
  );
}

/** ✕. */
export function CloseIcon({ className }: IconProps) {
  return (
    <StrokeIcon className={className}>
      <path d="M6 6l12 12" />
      <path d="M18 6L6 18" />
    </StrokeIcon>
  );
}
