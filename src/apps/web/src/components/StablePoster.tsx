"use client";

import React, { useEffect, useRef, useState } from "react";
import { stableVideoSourceIdentity } from "./StableVideo";

export interface StablePosterProps
  extends Omit<React.ImgHTMLAttributes<HTMLImageElement>, "src"> {
  /** The latest signed poster URL, which may change on every status poll. */
  src?: string | null;
  /** Stable object/render identity. Signature churn is ignored when this is unchanged. */
  identity?: string;
  /** Optional placeholder shown after the poster has failed to load. */
  fallback?: React.ReactNode;
}

/**
 * Renders a signed JPEG poster without letting signature refreshes reload it.
 *
 * A poster is a progressive enhancement: if extraction/backfill is unavailable
 * or the image expires before the next poll, the caller's fallback remains in
 * place and the video can still be used normally.
 */
export function StablePoster({
  src,
  identity,
  fallback = null,
  onError,
  ...rest
}: StablePosterProps) {
  const heldRef = useRef<{ identity: string | null; src: string | null }>({
    identity: null,
    src: null,
  });
  const retriedIdentityRef = useRef<string | null>(null);
  const [failed, setFailed] = useState(false);
  const [, setRetryNonce] = useState(0);
  const effectiveIdentity = stableVideoSourceIdentity(src, identity);

  // A source can disappear when a new render is selected. Never let the old
  // poster bleed into that new identity while the matching poster is absent.
  if (
    !src &&
    (effectiveIdentity !== heldRef.current.identity || heldRef.current.src !== null)
  ) {
    heldRef.current = { identity: effectiveIdentity, src: null };
  } else if (
    src &&
    (heldRef.current.src === null ||
      effectiveIdentity !== heldRef.current.identity)
  ) {
    heldRef.current = { identity: effectiveIdentity, src };
  }

  const previousIdentityRef = useRef<string | null>(null);
  useEffect(() => {
    if (previousIdentityRef.current !== effectiveIdentity) {
      previousIdentityRef.current = effectiveIdentity;
      retriedIdentityRef.current = null;
      setFailed(false);
    } else if (
      failed &&
      src &&
      src !== heldRef.current.src &&
      retriedIdentityRef.current !== effectiveIdentity
    ) {
      // A new signed URL gets one retry after an image error, but subsequent
      // signature churn does not keep re-requesting a permanently missing key.
      retriedIdentityRef.current = effectiveIdentity;
      heldRef.current = { identity: effectiveIdentity, src };
      setFailed(false);
    }
  }, [effectiveIdentity, failed, src]);

  const heldSrc = heldRef.current.src ?? src ?? null;
  if (!heldSrc || failed) return <>{fallback}</>;

  const handleError: React.ReactEventHandler<HTMLImageElement> = (event) => {
    // A refreshed signature is available in the latest props. Adopt it once
    // before falling back, so a transient expiry does not permanently hide a
    // healthy poster.
    if (
      src &&
      src !== heldRef.current.src &&
      retriedIdentityRef.current !== effectiveIdentity
    ) {
      retriedIdentityRef.current = effectiveIdentity;
      heldRef.current = { ...heldRef.current, src };
      setFailed(false);
      setRetryNonce((value) => value + 1);
    } else {
      setFailed(true);
    }
    onError?.(event);
  };

  return <img {...rest} src={heldSrc} alt={rest.alt ?? ""} onError={handleError} />;
}
