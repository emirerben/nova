"use client";

import { useSyncExternalStore } from "react";

export interface EditorPlaybackClock {
  getSnapshot: () => number;
  publish: (timeS: number) => void;
  subscribe: (listener: () => void) => () => void;
}

/**
 * Tiny external store for output-timeline frame time.
 *
 * Playback publishes here at decoded-video cadence without putting the whole
 * editor shell on a 60Hz React state loop. Only authored preview layers and
 * playheads subscribe. Scrubs and transport actions continue to use the
 * shell's committed time.
 */
export function createEditorPlaybackClock(initialTimeS = 0): EditorPlaybackClock {
  let timeS = initialTimeS;
  const listeners = new Set<() => void>();

  return {
    getSnapshot: () => timeS,
    publish: (nextTimeS) => {
      const next = Number.isFinite(nextTimeS) ? Math.max(0, nextTimeS) : 0;
      if (Object.is(next, timeS)) return;
      timeS = next;
      listeners.forEach((listener) => listener());
    },
    subscribe: (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
  };
}

export function useEditorPlaybackTime(
  clock: EditorPlaybackClock | null | undefined,
  fallbackTimeS: number,
): number {
  return useSyncExternalStore(
    clock?.subscribe ?? NOOP_SUBSCRIBE,
    clock?.getSnapshot ?? (() => fallbackTimeS),
    () => fallbackTimeS,
  );
}

const NOOP_SUBSCRIBE = () => () => {};
