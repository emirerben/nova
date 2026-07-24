import { useId } from "react";

/**
 * Kria logo mark: three 9:16 frames fanned from a bottom pivot.
 * Gaps between frames are true negative space (SVG masks), so the mark works
 * on any background. Fill follows `currentColor` — set text color on the
 * className (e.g. text-lime-600 on light surfaces, text-white on dark).
 * Canonical asset: ~/.gstack/projects/emirerben-nova/designs/kria-logo-20260721/final/
 */
export default function KriaMark({ className }: { className?: string }) {
  const id = useId();
  const behindMidAndFront = `${id}-behind-mid-front`;
  const behindFront = `${id}-behind-front`;
  return (
    <svg
      viewBox="-53 -92 110 100"
      className={className}
      aria-hidden="true"
      focusable="false"
    >
      <defs>
        <mask id={behindMidAndFront}>
          <rect x="-70" y="-110" width="150" height="140" fill="#fff" />
          <rect x="-22" y="-79" width="48" height="78" rx="13" fill="#000" />
          <rect
            x="-22"
            y="-79"
            width="48"
            height="78"
            rx="13"
            fill="#000"
            transform="rotate(24)"
          />
        </mask>
        <mask id={behindFront}>
          <rect x="-70" y="-110" width="150" height="140" fill="#fff" />
          <rect
            x="-22"
            y="-79"
            width="48"
            height="78"
            rx="13"
            fill="#000"
            transform="rotate(24)"
          />
        </mask>
      </defs>
      <g fill="currentColor">
        <g mask={`url(#${behindMidAndFront})`}>
          <rect
            x="-19"
            y="-76"
            width="42"
            height="72"
            rx="10"
            transform="rotate(-24)"
          />
        </g>
        <g mask={`url(#${behindFront})`}>
          <rect x="-19" y="-76" width="42" height="72" rx="10" />
        </g>
        <rect
          x="-19"
          y="-76"
          width="42"
          height="72"
          rx="10"
          transform="rotate(24)"
        />
      </g>
    </svg>
  );
}
