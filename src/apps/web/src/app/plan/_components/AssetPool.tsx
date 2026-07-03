"use client";

/**
 * AssetPool — the per-item "Visuals pool" (overlay auto-placement PR0, plans/005).
 *
 * Creators drop screenshots / screen recordings here; the pool later feeds the
 * AI overlay auto-placement matcher (PR1a+). Flag-gated end to end:
 *   frontend: NEXT_PUBLIC_OVERLAY_AUTOPLACE_ENABLED === "true"  → section renders
 *   backend:  OVERLAY_AUTOPLACE_ENABLED                          → routes 404 when off
 * A backend 404 with the frontend flag on (dual-flag trap) surfaces a quiet
 * dashed-zinc error line — never silent.
 *
 * Interaction states follow the plan-005 decision-2A table + DESIGN.md §2/§9:
 * shimmer + micro-label while uploading/analyzing, dashed zinc "Couldn't read
 * this file" on failure (no red), serif invitation when empty, quiet "N of 20"
 * count, inline reason when the cap disables the add affordance.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import {
  deletePoolAsset,
  listPoolAssets,
  registerPoolAsset,
  requestPoolAssetUploadUrls,
  sha256HexOfFile,
  uploadToGcs,
  type PoolAsset,
} from "@/lib/plan-api";

// Mirrors ALLOWED_OVERLAY_MIME_TYPES in OverlayLane.tsx (not exported there)
// and _OVERLAY_ALLOWED_CONTENT_TYPES on the backend.
const ALLOWED_ASSET_MIME_TYPES = [
  "image/jpeg",
  "image/png",
  "image/webp",
  "image/heic",
  "video/mp4",
  "video/quicktime",
];

const NOTICE_MS = 4000;
const UNAVAILABLE_COPY = "Visuals pool isn't available right now.";

/** Backend flag off → routes 404 with this detail (or a raw 404 wrapper). */
function isUnavailableError(err: unknown): boolean {
  return err instanceof Error && (/not available/i.test(err.message) || err.message.includes("(404)"));
}

/** Local tile for an in-flight upload (before the server row exists). */
interface PendingUpload {
  localId: string;
  filename: string;
}

export default function AssetPool({ itemId }: { itemId: string }) {
  const enabled = process.env.NEXT_PUBLIC_OVERLAY_AUTOPLACE_ENABLED === "true";

  const [assets, setAssets] = useState<PoolAsset[]>([]);
  const [maxAssets, setMaxAssets] = useState(20);
  const [pending, setPending] = useState<PendingUpload[]>([]);
  const [unavailable, setUnavailable] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const noticeTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  const showNotice = useCallback((text: string) => {
    setNotice(text);
    if (noticeTimer.current) clearTimeout(noticeTimer.current);
    noticeTimer.current = setTimeout(() => setNotice(null), NOTICE_MS);
  }, []);

  useEffect(() => () => {
    if (noticeTimer.current) clearTimeout(noticeTimer.current);
  }, []);

  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    listPoolAssets(itemId)
      .then((res) => {
        if (cancelled) return;
        setAssets(res.assets);
        setMaxAssets(res.max_assets);
      })
      .catch((err) => {
        if (cancelled) return;
        if (isUnavailableError(err)) setUnavailable(true);
        else setUploadError(err instanceof Error ? err.message : "Couldn't load your visuals.");
      });
    return () => {
      cancelled = true;
    };
  }, [enabled, itemId]);

  const handleFiles = useCallback(
    async (fileList: FileList | File[] | null) => {
      if (!fileList) return;
      const files = Array.from(fileList).filter((f) =>
        ALLOWED_ASSET_MIME_TYPES.includes(f.type),
      );
      if (files.length === 0) return;
      setUploadError(null);

      const locals: PendingUpload[] = files.map((f, i) => ({
        localId: `pending-${Date.now()}-${i}-${f.name}`,
        filename: f.name,
      }));
      setPending((prev) => [...prev, ...locals]);

      // Presigned direct-PUT is PRIMARY (R1 / review C9+C14). The multipart
      // proxy path buffers the whole body through the Next api-proxy, which on
      // Vercel hits a hard ~4.5MB serverless request-body cap — screen
      // recordings (up to the backend's 100MB cap) can never reach the API.
      // Instead: request a signed URL → PUT the bytes straight to GCS →
      // register the resulting object. uploadToGcs auto-falls-back to the
      // /uploads/relay proxy on a CORS TypeError (any localhost, whose origin
      // the bucket CORS config doesn't list), and pool paths
      // (users/{uid}/plan/{itemId}/pool/) are inside the relay's allowlist —
      // so this one path covers both prod and localhost with no proxy body cap.
      //
      // PROD/PREVIEW PREREQUISITE: the Vercel prod + preview origins must be in
      // the GCS bucket CORS config for the direct PUT to succeed WITHOUT the
      // relay. The relay is the fallback, not the happy path — do not change
      // bucket CORS from here.
      for (let i = 0; i < files.length; i++) {
        const file = files[i];
        const local = locals[i];
        try {
          const [signed] = await requestPoolAssetUploadUrls(itemId, [
            {
              filename: file.name,
              content_type: file.type,
              file_size_bytes: file.size,
            },
          ]);
          await uploadToGcs(signed.upload_url, file);
          // Client-side dedupe hash mirrors the backend multipart path's
          // sha256 so identical bytes register as deduped (never re-analyzed).
          const contentHash = await sha256HexOfFile(file);
          const registered = await registerPoolAsset(itemId, {
            gcs_path: signed.gcs_path,
            content_type: file.type,
            content_hash: contentHash,
            source_filename: file.name,
          });
          setPending((prev) => prev.filter((p) => p.localId !== local.localId));
          if (registered.deduped) {
            showNotice("Already in your pool");
          } else {
            setAssets((prev) => [...prev, registered]);
          }
        } catch (err) {
          setPending((prev) => prev.filter((p) => p.localId !== local.localId));
          if (isUnavailableError(err)) setUnavailable(true);
          else setUploadError(err instanceof Error ? err.message : "Upload failed");
        }
      }
    },
    [itemId, showNotice],
  );

  const handleRemove = useCallback(
    async (asset: PoolAsset) => {
      try {
        await deletePoolAsset(itemId, asset.id);
        setAssets((prev) => prev.filter((a) => a.id !== asset.id));
      } catch (err) {
        if (isUnavailableError(err)) setUnavailable(true);
        else setUploadError(err instanceof Error ? err.message : "Couldn't remove that file");
      }
    },
    [itemId],
  );

  if (!enabled) return null;

  const count = assets.length;
  const atCap = count >= maxAssets;
  const isEmpty = count === 0 && pending.length === 0;
  const inputId = `asset-pool-input-${itemId}`;

  return (
    <div className="my-6">
      <div className="mb-2 flex items-baseline justify-between">
        <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-lime-700">
          Visuals pool
        </p>
        {!isEmpty && !unavailable && (
          <p className="text-[12px] text-[#71717a]">
            {count} of {maxAssets}
          </p>
        )}
      </div>

      {unavailable ? (
        <div className="rounded-xl border border-dashed border-zinc-200 bg-white px-4 py-3 text-sm text-[#71717a]">
          {UNAVAILABLE_COPY}
        </div>
      ) : (
        <>
          <input
            ref={inputRef}
            id={inputId}
            type="file"
            multiple
            accept={ALLOWED_ASSET_MIME_TYPES.join(",")}
            className="sr-only"
            aria-label="Add visuals to your pool"
            disabled={atCap}
            onChange={(e) => {
              handleFiles(e.target.files);
              e.target.value = "";
            }}
          />

          {isEmpty ? (
            /* Empty state — leads with the action (§9), never "Nothing here yet". */
            <div
              className="rounded-xl border border-dashed border-zinc-200 bg-white p-5 text-center"
              onDragOver={(e) => e.preventDefault()}
              onDrop={(e) => {
                e.preventDefault();
                handleFiles(e.dataTransfer.files);
              }}
            >
              <p className="font-display text-[16px] font-medium text-[#0c0c0e]">
                Drop the screenshots you mention in your script
              </p>
              <p className="mt-1 text-[12px] text-[#71717a]">
                Screenshots and screen recordings — Nova will place them on your video for you.
              </p>
              <button
                type="button"
                onClick={() => inputRef.current?.click()}
                className="mt-3 inline-flex items-center gap-1.5 rounded-lg border border-zinc-200 bg-white px-4 py-2 text-sm text-[#3f3f46] transition-colors hover:border-lime-400 hover:text-lime-700 focus-visible:outline focus-visible:outline-2 focus-visible:outline-lime-500"
              >
                Add visuals
              </button>
            </div>
          ) : (
            <div
              onDragOver={(e) => e.preventDefault()}
              onDrop={(e) => {
                e.preventDefault();
                if (!atCap) handleFiles(e.dataTransfer.files);
              }}
            >
              <ul className="grid grid-cols-3 gap-2 sm:grid-cols-4 md:grid-cols-5">
                {assets.map((asset) => (
                  <AssetTile key={asset.id} asset={asset} onRemove={() => handleRemove(asset)} />
                ))}
                {pending.map((p) => (
                  <li
                    key={p.localId}
                    className="relative aspect-square overflow-hidden rounded-lg border border-zinc-200 bg-[linear-gradient(110deg,#f4f4f5,45%,#e4e4e7,55%,#f4f4f5)] bg-[length:200%_100%] motion-safe:animate-shimmer"
                  >
                    <span className="absolute inset-x-0 bottom-0 truncate px-1.5 py-1 text-[12px] text-[#71717a]">
                      Uploading…
                    </span>
                  </li>
                ))}
                {/* Add tile — disabled at cap with an inline reason below (never tooltip-only). */}
                <li>
                  <button
                    type="button"
                    disabled={atCap}
                    onClick={() => inputRef.current?.click()}
                    className="flex aspect-square w-full flex-col items-center justify-center rounded-lg border border-dashed border-zinc-200 bg-white text-[#71717a] transition-colors hover:border-lime-400 hover:text-lime-700 focus-visible:outline focus-visible:outline-2 focus-visible:outline-lime-500 disabled:cursor-not-allowed disabled:opacity-50 disabled:hover:border-zinc-200 disabled:hover:text-[#71717a]"
                  >
                    <span aria-hidden className="text-lg leading-none">+</span>
                    <span className="mt-1 text-[12px]">Add</span>
                  </button>
                </li>
              </ul>
              {atCap && (
                <p className="mt-2 text-[12px] text-[#71717a]">
                  Your pool is full — remove a visual to add another.
                </p>
              )}
            </div>
          )}

          {notice && (
            <p className="mt-2 rounded border border-zinc-200 bg-white px-3 py-2 text-[12px] text-[#3f3f46]">
              {notice}
            </p>
          )}
          {uploadError && (
            <p className="mt-2 rounded border border-zinc-200 bg-white px-3 py-2 text-[12px] text-[#3f3f46]">
              {uploadError}
            </p>
          )}
        </>
      )}
    </div>
  );
}

function AssetTile({ asset, onRemove }: { asset: PoolAsset; onRemove: () => void }) {
  const label = asset.source_filename ?? "this file";

  if (asset.status === "failed") {
    return (
      <li className="relative flex aspect-square flex-col items-center justify-center rounded-lg border border-dashed border-zinc-200 bg-white p-2 text-center">
        <p className="text-[12px] text-[#71717a]">Couldn&apos;t read this file</p>
        <button
          type="button"
          onClick={onRemove}
          aria-label={`Remove ${label}`}
          className="mt-1 min-h-[28px] min-w-[28px] text-[12px] text-[#71717a] underline underline-offset-2 transition-colors hover:text-[#0c0c0e] focus-visible:outline focus-visible:outline-2 focus-visible:outline-lime-500"
        >
          Remove
        </button>
      </li>
    );
  }

  const busy = asset.status === "analyzing" || asset.status === "uploading";

  return (
    <li className="group relative aspect-square overflow-hidden rounded-lg border border-zinc-200 bg-white">
      {busy || !asset.display_url ? (
        <div className="absolute inset-0 bg-[linear-gradient(110deg,#f4f4f5,45%,#e4e4e7,55%,#f4f4f5)] bg-[length:200%_100%] motion-safe:animate-shimmer" />
      ) : asset.kind === "video" ? (
        <video src={asset.display_url} muted playsInline preload="metadata" className="h-full w-full object-cover" />
      ) : (
        // eslint-disable-next-line @next/next/no-img-element -- signed GCS thumbnail, not an optimizable static asset
        <img src={asset.display_url} alt={asset.subject ?? label} className="h-full w-full object-cover" />
      )}
      <span className="absolute inset-x-0 bottom-0 truncate bg-white/85 px-1.5 py-1 text-[12px] text-[#71717a]">
        {busy ? "Analyzing…" : (asset.subject ?? asset.kind)}
      </span>
      <button
        type="button"
        onClick={onRemove}
        aria-label={`Remove ${label}`}
        className="absolute right-1 top-1 flex h-7 w-7 items-center justify-center rounded-full bg-white/90 text-[#3f3f46] opacity-0 transition-opacity focus-visible:opacity-100 focus-visible:outline focus-visible:outline-2 focus-visible:outline-lime-500 group-hover:opacity-100"
      >
        ×
      </button>
    </li>
  );
}
