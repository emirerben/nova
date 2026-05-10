"use client";

import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import type { TemplateListItem } from "@/lib/api";
import {
  activeTileStore,
  autoplayDisabled,
  getCachedPlaybackUrl,
  invalidatePlaybackUrl,
} from "@/lib/template-playback";

const TONE_GRADIENTS: Record<string, string> = {
  casual: "from-orange-500 to-amber-400",
  energetic: "from-red-500 to-pink-500",
  calm: "from-blue-500 to-teal-400",
  formal: "from-gray-600 to-gray-800",
};

const HOVER_DEBOUNCE_MS = 200;
const VIDEO_LOAD_TIMEOUT_MS = 4000;
const VIEWPORT_VISIBLE_THRESHOLD = 0.6;

function clipsLabel(t: TemplateListItem): string {
  const photoSlots = t.slots.filter((s) => s.media_type === "photo").length;
  const videoSlots = t.slots.length - photoSlots;
  if (photoSlots > 0 && videoSlots > 0) {
    return `${videoSlots} video${videoSlots !== 1 ? "s" : ""} + ${photoSlots} photo${photoSlots !== 1 ? "s" : ""}`;
  }
  if (t.required_clips_min === t.required_clips_max) {
    return `${t.required_clips_min} clip${t.required_clips_min !== 1 ? "s" : ""}`;
  }
  return `${t.required_clips_min}–${t.required_clips_max} clips`;
}

interface Props {
  template: TemplateListItem;
  onOpenPreview: (t: TemplateListItem) => void;
}

export default function TemplateTile({ template, onOpenPreview }: Props) {
  const activeId = useSyncExternalStore(
    activeTileStore.subscribe,
    activeTileStore.getSnapshot,
    () => null,
  );
  const isActive = activeId === template.id;

  const [videoUrl, setVideoUrl] = useState<string | null>(null);
  const [videoReady, setVideoReady] = useState(false);
  // Mirror videoReady into a ref so the load-timeout closure reads the
  // latest value, not a stale snapshot from when the timer was scheduled.
  const videoReadyRef = useRef(false);
  videoReadyRef.current = videoReady;

  const videoRef = useRef<HTMLVideoElement | null>(null);
  const tileRef = useRef<HTMLButtonElement | null>(null);
  const hoverTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const loadTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const gradient = TONE_GRADIENTS[template.copy_tone] ?? TONE_GRADIENTS.casual;
  const noAutoplay = autoplayDisabled();

  function activate() {
    activeTileStore.setActive(template.id);
  }

  function deactivate() {
    if (activeTileStore.getSnapshot() === template.id) {
      activeTileStore.setActive(null);
    }
  }

  // Mouse hover (desktop): 200ms debounce so brushing past tiles doesn't
  // trigger 50 URL fetches.
  function onMouseEnter() {
    if (noAutoplay) return;
    if (hoverTimer.current) clearTimeout(hoverTimer.current);
    hoverTimer.current = setTimeout(activate, HOVER_DEBOUNCE_MS);
  }

  function onMouseLeave() {
    if (hoverTimer.current) {
      clearTimeout(hoverTimer.current);
      hoverTimer.current = null;
    }
    deactivate();
  }

  // Mobile: IntersectionObserver picks the most-visible tile as active.
  // Desktop already handles activation via hover; the observer is harmless
  // but slightly redundant. We only register it on touch-capable devices.
  useEffect(() => {
    if (noAutoplay) return;
    if (typeof window === "undefined") return;
    const isTouch = window.matchMedia("(hover: none)").matches;
    if (!isTouch) return;
    const el = tileRef.current;
    if (!el) return;
    const observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (entry.isIntersecting && entry.intersectionRatio >= VIEWPORT_VISIBLE_THRESHOLD) {
            activate();
          } else if (!entry.isIntersecting && activeTileStore.getSnapshot() === template.id) {
            deactivate();
          }
        }
      },
      { threshold: [0, VIEWPORT_VISIBLE_THRESHOLD, 1] },
    );
    observer.observe(el);
    return () => observer.disconnect();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [template.id, noAutoplay]);

  // Fetch the signed URL when this tile becomes active. Reset readiness when
  // the tile deactivates so the next activation starts fresh.
  useEffect(() => {
    if (!isActive) {
      setVideoReady(false);
      if (loadTimer.current) {
        clearTimeout(loadTimer.current);
        loadTimer.current = null;
      }
      return;
    }
    let cancelled = false;
    getCachedPlaybackUrl(template.id)
      .then((url) => {
        if (!cancelled) setVideoUrl(url);
      })
      .catch(() => {
        // Silent fallback: poster stays visible.
      });
    // 4s safety net: if loadeddata never fires, give up and stay on poster.
    loadTimer.current = setTimeout(() => {
      if (!cancelled && !videoReadyRef.current) {
        deactivate();
      }
    }, VIDEO_LOAD_TIMEOUT_MS);
    return () => {
      cancelled = true;
      if (loadTimer.current) {
        clearTimeout(loadTimer.current);
        loadTimer.current = null;
      }
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isActive, template.id]);

  // Cleanup on unmount.
  useEffect(() => {
    return () => {
      if (hoverTimer.current) clearTimeout(hoverTimer.current);
      if (loadTimer.current) clearTimeout(loadTimer.current);
      deactivate();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function onLoadedData() {
    setVideoReady(true);
    if (loadTimer.current) {
      clearTimeout(loadTimer.current);
      loadTimer.current = null;
    }
  }

  function onVideoError() {
    invalidatePlaybackUrl(template.id);
    setVideoUrl(null);
    setVideoReady(false);
    deactivate();
  }

  function onClick() {
    onOpenPreview(template);
  }

  return (
    <button
      ref={tileRef}
      type="button"
      onClick={onClick}
      onMouseEnter={onMouseEnter}
      onMouseLeave={onMouseLeave}
      className="group rounded-xl border border-zinc-900 hover:border-zinc-700 overflow-hidden transition-colors text-left w-full focus:outline-none focus-visible:ring-2 focus-visible:ring-white"
      aria-label={`Preview ${template.name}`}
    >
      <div className="relative aspect-[9/16] w-full bg-black overflow-hidden">
        {/* Base layer: poster image, or gradient fallback for templates
            that haven't been backfilled yet. */}
        {template.thumbnail_url ? (
          /* eslint-disable-next-line @next/next/no-img-element */
          <img
            src={template.thumbnail_url}
            alt=""
            loading="lazy"
            className="absolute inset-0 w-full h-full object-cover"
          />
        ) : (
          <div
            className={`absolute inset-0 bg-gradient-to-br ${gradient} opacity-90 group-hover:opacity-100 transition-opacity`}
          />
        )}

        {/* Video layer: only mounted while this tile is the active one and a
            URL has resolved. Fades in over the poster once loadeddata fires. */}
        {isActive && videoUrl && (
          <video
            ref={videoRef}
            src={videoUrl}
            autoPlay
            muted
            loop
            playsInline
            preload="auto"
            disableRemotePlayback
            onLoadedData={onLoadedData}
            onError={onVideoError}
            className={`absolute inset-0 w-full h-full object-cover transition-opacity duration-150 ${
              videoReady ? "opacity-100" : "opacity-0"
            }`}
          />
        )}

        {/* Mute icon shown when video is actively playing. Communicates
            "this is video, not a static GIF." Decorative, not interactive. */}
        {isActive && videoReady && (
          <div className="absolute bottom-2 right-2 rounded-full bg-black/60 p-1.5 pointer-events-none">
            <MuteIcon />
          </div>
        )}
      </div>

      <div className="p-4">
        <h3 className="font-semibold text-sm mb-1 truncate">{template.name}</h3>
        <p className="text-xs text-zinc-400">
          {Math.round(template.total_duration_s)}s · {clipsLabel(template)}
        </p>
        <p className="text-xs text-zinc-500 mt-0.5 capitalize">{template.copy_tone}</p>
      </div>
    </button>
  );
}

function MuteIcon() {
  return (
    <svg
      width="14"
      height="14"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      className="text-white"
      aria-hidden="true"
    >
      <polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5" />
      <line x1="23" y1="9" x2="17" y2="15" />
      <line x1="17" y1="9" x2="23" y2="15" />
    </svg>
  );
}
