"use client";

/**
 * ShowcaseMarquee — landing-page video marquee.
 *
 * Layout
 * ------
 * Mobile:  fixed-height cards in a horizontal scroll lane (touch-pan-x).
 * md+:     all six cards share the row width (md:flex-1), portrait aspect ratio
 *          (3:4 = slightly taller than square) — no vertical bleed, no horizontal
 *          scrollbar at any viewport width. pb-8 clears translate-y-3 (12px
 *          stagger) + shadow bleed (~24px).
 *
 * Playback
 * --------
 * Desktop (≥768 px): every card with a `src` that is ≥50% visible plays
 * simultaneously; each pauses independently when it leaves the viewport.
 * Mobile (<768 px): only the single most-visible card plays at a time
 * (battery / bandwidth on the horizontal scroll lane).
 *
 * Guardrails (mirroring TemplateTile.tsx):
 *   - typeof IntersectionObserver === "undefined" → skip IO entirely (SSR /
 *     jsdom). Cards stay in their gradient/poster state.
 *   - window.matchMedia("prefers-reduced-motion: reduce") → never autoplay.
 *   - el.play()?.catch(() => {}) — never an unguarded play() call (iOS Low
 *     Power Mode / autoplay-blocked browsers throw NotAllowedError).
 *   - onError → hide <video>, show gradient fallback.
 */

import { useEffect, useRef, useState } from "react";

export interface ShowcaseClip {
  title: string;
  from: string;
  to: string;
  src?: string;
}

interface Props {
  clips: ShowcaseClip[];
}

const VISIBLE_THRESHOLD = 0.5;

export default function ShowcaseMarquee({ clips }: Props) {
  // Set of card indices currently playing.
  const [playingSet, setPlayingSet] = useState<Set<number>>(() => new Set());
  const videoRefs = useRef<(HTMLVideoElement | null)[]>([]);
  const cardRefs = useRef<(HTMLDivElement | null)[]>([]);

  // Play/pause videos when the playing set changes.
  useEffect(() => {
    videoRefs.current.forEach((el, i) => {
      if (!el) return;
      if (playingSet.has(i)) {
        el.play()?.catch(() => {
          // NotAllowedError on iOS Low Power Mode / blocked autoplay — silent.
        });
      } else {
        el.pause();
      }
    });
  }, [playingSet]);

  // IntersectionObserver: manage which cards are playing.
  useEffect(() => {
    if (typeof IntersectionObserver === "undefined") return;
    if (typeof window === "undefined") return;
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;

    const observers: IntersectionObserver[] = [];

    clips.forEach((clip, i) => {
      if (!clip.src) return; // No video for this card — skip.
      const el = cardRefs.current[i];
      if (!el) return;

      const obs = new IntersectionObserver(
        ([entry]) => {
          const isDesktop = window.matchMedia("(min-width: 768px)").matches;

          if (isDesktop) {
            // Desktop: each card manages itself independently — all visible cards play.
            if (
              entry.isIntersecting &&
              entry.intersectionRatio >= VISIBLE_THRESHOLD
            ) {
              setPlayingSet((prev) => {
                if (prev.has(i)) return prev;
                const next = new Set(prev);
                next.add(i);
                return next;
              });
            } else if (!entry.isIntersecting) {
              setPlayingSet((prev) => {
                if (!prev.has(i)) return prev;
                const next = new Set(prev);
                next.delete(i);
                return next;
              });
            }
          } else {
            // Mobile: only the single most-visible card plays.
            if (
              entry.isIntersecting &&
              entry.intersectionRatio >= VISIBLE_THRESHOLD
            ) {
              setPlayingSet(new Set([i]));
            } else if (!entry.isIntersecting) {
              setPlayingSet((prev) => {
                if (!prev.has(i)) return prev;
                const next = new Set(prev);
                next.delete(i);
                return next;
              });
            }
          }
        },
        { threshold: [0, VISIBLE_THRESHOLD, 1] },
      );
      obs.observe(el);
      observers.push(obs);
    });

    return () => observers.forEach((obs) => obs.disconnect());
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [clips.map((c) => c.src).join(",")]);

  return (
    <>
      {/* Mobile: fixed-height cards in a horizontal scroll lane.
          md+: flex-1 cards share the row width; pb-8 prevents shadow bleed. */}
      <section
        className="mt-[72px] flex items-end gap-[18px] overflow-x-auto md:overflow-visible px-9 pb-8 touch-pan-x"
        aria-label="Videos created by Nova"
      >
        {clips.map((clip, i) => (
          <div
            key={clip.title}
            ref={(el) => {
              cardRefs.current[i] = el;
            }}
            className={`relative h-[240px] w-[135px] shrink-0 md:h-auto md:aspect-[2/3] md:w-auto md:flex-1 overflow-hidden rounded-[18px] border border-zinc-200 shadow-[0_4px_20px_rgba(0,0,0,0.08)] ${
              i % 2 === 0 ? "translate-y-3" : ""
            }`}
            style={{
              background: `linear-gradient(165deg, ${clip.from}, ${clip.to})`,
            }}
          >
            {/* Video layer — only rendered when a src is available */}
            {clip.src && (
              <video
                ref={(el) => {
                  videoRefs.current[i] = el;
                }}
                src={clip.src}
                muted
                loop
                playsInline
                preload="metadata"
                aria-label={`${clip.title} — created with Nova`}
                onError={() => {
                  // Hide the video element on error; the gradient fallback shows.
                  const el = videoRefs.current[i];
                  if (el) el.style.display = "none";
                  setPlayingSet((prev) => {
                    if (!prev.has(i)) return prev;
                    const next = new Set(prev);
                    next.delete(i);
                    return next;
                  });
                }}
                className="absolute inset-0 h-full w-full object-cover"
              />
            )}

            {/* Credit overlay */}
            <span className="absolute bottom-[14px] left-[15px] text-[9px] uppercase tracking-[0.14em] text-white/50">
              created with nova
            </span>
          </div>
        ))}
      </section>
      <p className="mt-[52px] text-center text-[11.5px] uppercase tracking-[0.2em] text-[#a1a1aa]">
        Created by Nova — real videos, edited by the agent
      </p>
    </>
  );
}
