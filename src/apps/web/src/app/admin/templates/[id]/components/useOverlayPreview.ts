"use client";

import { useEffect, useRef, useState } from "react";
import { fetchOverlayPreview } from "@/lib/admin-api";
import type { RecipeTextOverlay } from "./recipe-types";
import { resolveOverlayPreview } from "./overlay-constants";

const DEBOUNCE_MS = 400;
// Round time to this granularity when hashing so micro cursor jitter
// (sub-frame redraws) doesn't trigger fresh fetches.
const TIME_HASH_GRANULARITY_S = 0.05;
// Bound the in-memory cache so heavy editing sessions don't pile up Blob URLs.
const CACHE_LIMIT = 32;

interface UseOverlayPreviewArgs {
  slotOverlays: RecipeTextOverlay[];
  slotDurationS: number;
  timeInSlotS: number;
  previewSubject: string;
  /** Disable the hook (e.g. while inline-editing — the input owns the visuals). */
  enabled?: boolean;
}

interface UseOverlayPreviewResult {
  /**
   * The freshly-rendered server PNG for the current state, or null if no PNG
   * yet matches the current input. During video playback this stays null
   * almost always (the cursor keeps changing), so the component should
   * render its own DOM fallback. Once the cursor settles for ~400ms the
   * fetch resolves and pngUrl flips to the matching blob URL.
   */
  pngUrl: string | null;
  loading: boolean;
  error: string | null;
}

/**
 * Manage a debounced server-rendered overlay preview.
 *
 * On every input change we hash the slot state + cursor time (rounded), kick
 * a debounce timer, and on fire either return a cached blob URL or fetch a
 * fresh one. Stale requests are aborted via AbortController so out-of-order
 * responses can't overwrite the visible PNG. Cached blob URLs are revoked
 * when evicted from the LRU.
 */
export function useOverlayPreview(args: UseOverlayPreviewArgs): UseOverlayPreviewResult {
  const {
    slotOverlays,
    slotDurationS,
    timeInSlotS,
    previewSubject,
    enabled = true,
  } = args;

  const [pngUrl, setPngUrl] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // LRU of hash -> blob URL. Held in a ref so it survives renders without
  // forcing re-fetches and lets us revoke URLs on eviction without React state.
  const cacheRef = useRef<Map<string, string>>(new Map());
  const inFlightRef = useRef<AbortController | null>(null);
  const debounceTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const payloadHash = buildHash(slotOverlays, slotDurationS, timeInSlotS, previewSubject);

  useEffect(() => {
    if (!enabled) return;

    // Cache hit short-circuits the network call.
    const cached = cacheRef.current.get(payloadHash);
    if (cached) {
      // Touch for LRU recency.
      cacheRef.current.delete(payloadHash);
      cacheRef.current.set(payloadHash, cached);
      setPngUrl(cached);
      setLoading(false);
      setError(null);
      return;
    }

    // Hash changed and we don't have a cached PNG for it. Drop the previously
    // displayed PNG (which was for an older state) so the component can fall
    // back to its DOM rendering until the new fetch resolves. This keeps the
    // editor responsive during playback / scrubbing instead of showing a
    // stale frozen frame.
    setPngUrl(null);

    if (debounceTimerRef.current) clearTimeout(debounceTimerRef.current);

    debounceTimerRef.current = setTimeout(() => {
      // Abort any prior in-flight request — the response would be stale.
      inFlightRef.current?.abort();
      const ctrl = new AbortController();
      inFlightRef.current = ctrl;

      setLoading(true);
      const overlays = serializeOverlays(slotOverlays, previewSubject);
      fetchOverlayPreview(
        {
          overlays,
          slot_duration_s: slotDurationS,
          time_in_slot_s: timeInSlotS,
          preview_subject: previewSubject || undefined,
        },
        { signal: ctrl.signal },
      )
        .then((blob) => {
          if (ctrl.signal.aborted) return;
          const url = URL.createObjectURL(blob);
          rememberInCache(cacheRef.current, payloadHash, url);
          setPngUrl(url);
          setError(null);
        })
        .catch((err: unknown) => {
          if ((err as { name?: string })?.name === "AbortError") return;
          // Keep the last successful PNG visible; surface the error so the
          // editor can show a "preview stale" hint.
          setError(err instanceof Error ? err.message : String(err));
        })
        .finally(() => {
          if (inFlightRef.current === ctrl) inFlightRef.current = null;
          if (!ctrl.signal.aborted) setLoading(false);
        });
    }, DEBOUNCE_MS);

    return () => {
      if (debounceTimerRef.current) clearTimeout(debounceTimerRef.current);
    };
  }, [payloadHash, enabled, slotOverlays, slotDurationS, timeInSlotS, previewSubject]);

  // Cleanup on unmount: revoke every cached URL and abort any open request.
  useEffect(() => {
    const cache = cacheRef.current;
    return () => {
      inFlightRef.current?.abort();
      const urls = Array.from(cache.values());
      for (const url of urls) URL.revokeObjectURL(url);
      cache.clear();
    };
  }, []);

  return { pngUrl, loading, error };
}

// ── Helpers ──────────────────────────────────────────────────────────────────

function buildHash(
  overlays: RecipeTextOverlay[],
  slotDurationS: number,
  timeInSlotS: number,
  previewSubject: string,
): string {
  // Round time so cursor jitter doesn't invalidate the cache. Round duration
  // for the same reason — both sides only need ~50ms granularity.
  const tBucket = Math.round(timeInSlotS / TIME_HASH_GRANULARITY_S);
  const dBucket = Math.round(slotDurationS / TIME_HASH_GRANULARITY_S);
  // Stringify the rendering-relevant overlay fields. Excluding fields the
  // backend renderer ignores (e.g. role) keeps the cache tighter.
  const stable = overlays.map(serializeOverlayForHash);
  return JSON.stringify([dBucket, tBucket, previewSubject, stable]);
}

function serializeOverlayForHash(overlay: RecipeTextOverlay): unknown {
  return {
    text: overlay.text,
    sample_text: overlay.sample_text,
    position: overlay.position,
    effect: overlay.effect,
    font_style: overlay.font_style,
    font_family: overlay.font_family,
    text_size: overlay.text_size,
    text_color: overlay.text_color,
    start_s: overlay.start_s,
    end_s: overlay.end_s,
    start_s_override: overlay.start_s_override,
    end_s_override: overlay.end_s_override,
    spans: overlay.spans,
    outline_px: overlay.outline_px,
    font_cycle_accel_at_s: overlay.font_cycle_accel_at_s,
    role: overlay.role,
  };
}

/**
 * Build the request payload — pre-resolves preview subject so the rendered
 * `text` matches what the editor's old DOM preview displayed via
 * resolveOverlayPreview. start_s_override / end_s_override are folded into
 * the start_s / end_s the backend reads.
 */
function serializeOverlays(
  overlays: RecipeTextOverlay[],
  previewSubject: string,
): Array<Record<string, unknown>> {
  return overlays.map((overlay) => {
    const resolvedText = resolveOverlayPreview(overlay, previewSubject);
    const start = overlay.start_s_override ?? overlay.start_s;
    const end = overlay.end_s_override ?? overlay.end_s;
    const out: Record<string, unknown> = {
      text: resolvedText,
      position: overlay.position,
      effect: overlay.effect,
      font_style: overlay.font_style,
      text_size: overlay.text_size,
      text_color: overlay.text_color,
      start_s: start,
      end_s: end,
      role: overlay.role,
      sample_text: overlay.sample_text,
    };
    if (overlay.font_family) out.font_family = overlay.font_family;
    if (overlay.spans) out.spans = overlay.spans;
    if (overlay.outline_px != null) out.outline_px = overlay.outline_px;
    if (overlay.font_cycle_accel_at_s != null) {
      out.font_cycle_accel_at_s = overlay.font_cycle_accel_at_s;
    }
    return out;
  });
}

function rememberInCache(cache: Map<string, string>, key: string, url: string): void {
  cache.set(key, url);
  while (cache.size > CACHE_LIMIT) {
    // Oldest insertion order = least-recently-used (cache hits re-set the key).
    const oldest = cache.keys().next().value;
    if (oldest === undefined) break;
    const evicted = cache.get(oldest);
    cache.delete(oldest);
    if (evicted) URL.revokeObjectURL(evicted);
  }
}
