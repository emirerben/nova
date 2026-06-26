"use client";

import React, { forwardRef, useRef, useState } from "react";

/**
 * Safely extract the URL pathname (without query string / signature).
 * Falls back to the raw string on parse error so malformed URLs never throw.
 */
function safePathname(url: string | null | undefined): string | null {
  if (!url) return null;
  try {
    return new URL(url).pathname;
  } catch {
    return url;
  }
}

export interface StableVideoProps extends Omit<React.VideoHTMLAttributes<HTMLVideoElement>, "src"> {
  /**
   * The latest signed URL. This changes every ~2 s as the backend re-signs GCS
   * URLs on each status poll — StableVideo ignores those changes when `identity`
   * hasn't changed, preventing playback restarts.
   */
  src?: string | null;
  /**
   * A stable, per-render identity key.  Provide `render_finished_at` for output
   * videos or `base_video_path` for base (text-free) previews.
   *
   * When the identity changes, StableVideo adopts the new `src` (new bytes →
   * swap the video). When only the signed URL churns (same identity), the held
   * src is kept — no reload, no glitch.
   *
   * Defaults to the URL pathname (stable across re-signs) when omitted.
   */
  identity?: string;
}

/**
 * A <video> wrapper that prevents playback restarts caused by re-signed GCS URLs.
 *
 * Problem: every status poll delivers the same video object with a fresh
 * `?X-Goog-Signature=...` query string. Any `<video src={output_url}>` bound
 * directly sees a "new" src every 2 s → React reloads the element → playback
 * restarts (visible glitch), and when a re-render completes the old video stays
 * pinned until a page refresh.
 *
 * Solution: hold the src in a ref, only adopting a new value when `identity`
 * changes (a genuinely new render = new bytes). On signed-URL expiry, fall
 * forward via the onError handler.
 *
 * Usage — output videos:
 *   <StableVideo
 *     src={variant.output_url}
 *     identity={variant.render_finished_at ?? undefined}
 *     controls
 *     className="h-full w-full object-contain"
 *   />
 *
 * Usage — base (text-free) previews:
 *   <StableVideo
 *     src={variant.base_video_url}
 *     identity={variant.base_video_path ?? undefined}
 *     autoPlay loop muted playsInline
 *     className="h-full w-full object-contain"
 *   />
 *
 * Usage — Hero (keep old video playing during same-variant re-render):
 *   <StableVideo
 *     src={variant.output_url}
 *     identity={`${variant.variant_id}:${variant.render_finished_at ?? ""}`}
 *     controls
 *     className="h-full w-full object-contain"
 *   />
 */
export const StableVideo = forwardRef<HTMLVideoElement, StableVideoProps>(
  function StableVideo({ src, identity, onError, ...rest }, ref) {
    // Holds the last-adopted { identity, src } pair.
    const heldRef = useRef<{ identity: string | null; src: string | null }>({
      identity: null,
      src: null,
    });
    // Used only to force a re-render after an onError fall-forward (expired sig).
    const [, setErrorNonce] = useState(0);

    // Effective identity: explicit prop wins; otherwise strip the query string
    // so the same GCS object produces the same identity regardless of re-signing.
    const effectiveIdentity: string | null =
      identity !== undefined ? (identity ?? null) : safePathname(src);

    // Adopt a new src when:
    //   • we've never held anything yet (first load with a real src), OR
    //   • the identity genuinely changed (new render = new bytes).
    // When only the signature churns (src changed, identity same), keep held src.
    if (src && (heldRef.current.src === null || effectiveIdentity !== heldRef.current.identity)) {
      heldRef.current = { identity: effectiveIdentity, src };
    }

    const videoSrc = heldRef.current.src ?? src ?? undefined;

    const handleError: React.ReactEventHandler<HTMLVideoElement> = (e) => {
      // Expired signature mid-session — fall forward to the latest signed URL
      // delivered by the most recent poll.
      if (src && src !== heldRef.current.src) {
        heldRef.current = { ...heldRef.current, src };
        setErrorNonce((n) => n + 1);
      }
      onError?.(e);
    };

    // Pass src as undefined (not null) to avoid React's "received null" warning.
    return (
      <video
        ref={ref}
        src={videoSrc ?? undefined}
        onError={handleError}
        {...rest}
      />
    );
  },
);
