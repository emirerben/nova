"use client";

import { getTemplatePlaybackUrl } from "@/lib/api";

// ── Active-tile coordination (single playing video at a time) ──────────────
//
// A module-level store of the currently-active templateId. Tiles subscribe via
// useSyncExternalStore (React 18). When setActive(newId) is called, all tiles
// re-read getSnapshot() — the previously active tile sees a different id and
// pauses; the new active tile starts playing.

let activeId: string | null = null;
const listeners = new Set<() => void>();

function emit() {
  listeners.forEach((l) => l());
}

export const activeTileStore = {
  subscribe(listener: () => void): () => void {
    listeners.add(listener);
    return () => listeners.delete(listener);
  },
  getSnapshot(): string | null {
    return activeId;
  },
  setActive(id: string | null): void {
    if (activeId === id) return;
    activeId = id;
    emit();
  },
};

// Pause any active tile when the tab is hidden — saves bandwidth and battery,
// and avoids hitting browser autoplay-quota limits when the page comes back.
if (typeof document !== "undefined") {
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "hidden" && activeId !== null) {
      activeTileStore.setActive(null);
    }
  });
}

// ── Signed playback URL cache ──────────────────────────────────────────────
//
// /templates/{id}/playback-url returns a 1-hour signed URL. Cache in module
// scope so that re-hovering the same tile within the hour doesn't re-hit the
// API. Refresh proactively 60s before expiry, and on <video> errors.

interface CachedUrl {
  url: string;
  expiresAt: number; // ms epoch
}

const urlCache = new Map<string, CachedUrl>();
const inFlight = new Map<string, Promise<string>>();
const REFRESH_MARGIN_MS = 60_000;

export async function getCachedPlaybackUrl(templateId: string): Promise<string> {
  const now = Date.now();
  const cached = urlCache.get(templateId);
  if (cached && now < cached.expiresAt - REFRESH_MARGIN_MS) {
    return cached.url;
  }
  const existing = inFlight.get(templateId);
  if (existing) return existing;

  const fetching = (async () => {
    try {
      const { url, expires_in_s } = await getTemplatePlaybackUrl(templateId);
      urlCache.set(templateId, {
        url,
        expiresAt: Date.now() + expires_in_s * 1000,
      });
      return url;
    } finally {
      inFlight.delete(templateId);
    }
  })();
  inFlight.set(templateId, fetching);
  return fetching;
}

export function invalidatePlaybackUrl(templateId: string): void {
  urlCache.delete(templateId);
}

// ── Capability helpers ─────────────────────────────────────────────────────

export function prefersReducedMotion(): boolean {
  if (typeof window === "undefined") return false;
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

interface NetworkInformation {
  saveData?: boolean;
}

export function saveDataEnabled(): boolean {
  if (typeof navigator === "undefined") return false;
  const conn = (navigator as unknown as { connection?: NetworkInformation }).connection;
  return Boolean(conn?.saveData);
}

export function autoplayDisabled(): boolean {
  return prefersReducedMotion() || saveDataEnabled();
}
