"use client";

import React, { useEffect, useRef, useState } from "react";
import { stableVideoSourceIdentity } from "./StableVideo";

export interface StablePosterProps
  extends Omit<React.ImgHTMLAttributes<HTMLImageElement>, "src"> {
  /** The latest signed poster URL, which may change on every status poll. */
  src?: string | null;
  /** Stable object/render identity. Signature churn is ignored when this is unchanged. */
  identity?: string;
  /**
   * Optional caller-controlled retry allowance. A changed key lets a failed
   * poster adopt one newly authorized URL while the render identity is stable.
   */
  retryKey?: string;
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
  retryKey,
  fallback = null,
  onError,
  ...rest
}: StablePosterProps) {
  const heldRef = useRef<{ identity: string | null; src: string | null }>({
    identity: null,
    src: null,
  });
  const retriedKeyRef = useRef<string | null>(null);
  const [failed, setFailed] = useState(false);
  const [, setRetryNonce] = useState(0);
  const effectiveIdentity = stableVideoSourceIdentity(src, identity);
  const retryAllowanceKey = retryKey ?? effectiveIdentity;

  // A source can disappear when a new render is selected. Never let the old
  // poster bleed into that new identity while the matching poster is absent.
  if (
    !src &&
    (effectiveIdentity !== heldRef.current.identity || heldRef.current.src !== null)
  ) {
    heldRef.current = { identity: effectiveIdentity, src: null };
  } else if (
    src &&
    (effectiveIdentity !== heldRef.current.identity ||
      (heldRef.current.src === null && !failed))
  ) {
    heldRef.current = { identity: effectiveIdentity, src };
  }

  const previousIdentityRef = useRef<string | null>(null);
  useEffect(() => {
    if (previousIdentityRef.current !== effectiveIdentity) {
      previousIdentityRef.current = effectiveIdentity;
      retriedKeyRef.current = null;
      setFailed(false);
    } else if (
      failed &&
      src &&
      src !== heldRef.current.src &&
      retriedKeyRef.current !== retryAllowanceKey
    ) {
      // A new signed URL gets one retry after an image error. By default that
      // is once per render identity; callers can explicitly authorize bounded
      // retries by changing retryKey.
      retriedKeyRef.current = retryAllowanceKey;
      heldRef.current = { identity: effectiveIdentity, src };
      setFailed(false);
    }
  }, [effectiveIdentity, failed, retryAllowanceKey, src]);

  const heldSrc = heldRef.current.src ?? src ?? null;
  if (!heldSrc || failed) return <>{fallback}</>;

  const handleError: React.ReactEventHandler<HTMLImageElement> = (event) => {
    // A refreshed signature is available in the latest props. Adopt it once
    // before falling back, so a transient expiry does not permanently hide a
    // healthy poster.
    if (
      src &&
      src !== heldRef.current.src &&
      retriedKeyRef.current !== retryAllowanceKey
    ) {
      retriedKeyRef.current = retryAllowanceKey;
      heldRef.current = { ...heldRef.current, src };
      setFailed(false);
      setRetryNonce((value) => value + 1);
    } else {
      setFailed(true);
      onError?.(event);
    }
  };

  return <img {...rest} src={heldSrc} alt={rest.alt ?? ""} onError={handleError} />;
}
