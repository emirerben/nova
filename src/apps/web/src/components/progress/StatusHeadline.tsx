"use client";

import { useEffect, useRef, useState } from "react";
import { HEADLINE_CROSSFADE_MS, HEADLINE_MIN_DWELL_MS } from "./constants";

interface StatusHeadlineProps {
  text: string;
}

/**
 * Crossfading Playfair Display headline.
 *
 * D14:
 * - 450ms crossfade between texts.
 * - 600ms minimum dwell — rapid changes are queued, not dropped.
 * - Reduced motion: instant swap (no crossfade).
 * - role="status" aria-live="polite" — each new text announced once to screen readers.
 */
export function StatusHeadline({ text }: StatusHeadlineProps) {
  const [displayed, setDisplayed] = useState(text);
  const [incoming, setIncoming] = useState<string | null>(null);
  const [phase, setPhase] = useState<"idle" | "fading-out" | "fading-in">("idle");
  const queueRef = useRef<string[]>([]);
  const dwellTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const crossfadeTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const lastSwapRef = useRef<number>(0);
  const reducedMotionRef = useRef(false);

  useEffect(() => {
    reducedMotionRef.current = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  }, []);

  const processQueue = () => {
    if (queueRef.current.length === 0) {
      setPhase("idle");
      setIncoming(null);
      return;
    }
    const next = queueRef.current.shift()!;
    const now = Date.now();
    const sinceLast = now - lastSwapRef.current;
    const delay = Math.max(0, HEADLINE_MIN_DWELL_MS - sinceLast);

    dwellTimerRef.current = setTimeout(() => {
      if (reducedMotionRef.current) {
        setDisplayed(next);
        lastSwapRef.current = Date.now();
        processQueue();
        return;
      }
      // Fade-out phase
      setIncoming(next);
      setPhase("fading-out");
      crossfadeTimerRef.current = setTimeout(() => {
        setDisplayed(next);
        setIncoming(null);
        setPhase("fading-in");
        lastSwapRef.current = Date.now();
        crossfadeTimerRef.current = setTimeout(() => {
          setPhase("idle");
          processQueue();
        }, HEADLINE_CROSSFADE_MS);
      }, HEADLINE_CROSSFADE_MS);
    }, delay);
  };

  useEffect(() => {
    if (text === displayed && queueRef.current.length === 0) return;
    if (text === displayed) return;

    // If already transitioning, queue the new text.
    if (phase !== "idle") {
      // Replace last queued item if it hasn't been consumed yet (no stale values).
      queueRef.current = [text];
      return;
    }

    queueRef.current.push(text);
    processQueue();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [text]);

  // Cleanup on unmount.
  useEffect(() => {
    return () => {
      if (dwellTimerRef.current) clearTimeout(dwellTimerRef.current);
      if (crossfadeTimerRef.current) clearTimeout(crossfadeTimerRef.current);
    };
  }, []);

  const crossfadeDuration = `${HEADLINE_CROSSFADE_MS}ms`;

  return (
    <div role="status" aria-live="polite" className="relative min-h-[2em] overflow-hidden">
      {/* Displayed (current) headline */}
      <h2
        className="font-display text-xl text-white transition-opacity"
        style={{
          transitionDuration: crossfadeDuration,
          opacity: phase === "fading-out" ? 0 : 1,
        }}
        aria-hidden={phase === "fading-out" ? "true" : undefined}
      >
        {displayed}
      </h2>
      {/* Incoming headline (fades in while current fades out) */}
      {incoming && (
        <h2
          className="font-display absolute inset-0 text-xl text-white transition-opacity"
          style={{
            transitionDuration: crossfadeDuration,
            opacity: phase === "fading-out" ? 1 : 0,
          }}
          aria-hidden="true"
        >
          {incoming}
        </h2>
      )}
    </div>
  );
}
