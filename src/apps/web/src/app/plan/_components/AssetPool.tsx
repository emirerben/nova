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
 *
 * While any asset is mid-pipeline (uploaded/analyzing) the list re-polls every
 * 5s so the server-side status flips and the subject micro-label land without
 * a page refresh; polling stops once every asset is terminal (ready/failed).
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  deletePoolAsset,
  listPoolAssets,
  registerPoolAsset,
  requestPoolAssetUploadUrls,
  sha256HexOfFile,
  updatePoolAssetContext,
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

// Analysis happens server-side after register (uploaded → analyzing → ready |
// failed) — poll while any asset is mid-pipeline so tiles update in place.
// Unknown future statuses deliberately DON'T poll (no runaway interval).
const ASSET_POLL_MS = 5000;
const NON_TERMINAL_ASSET_STATUSES = new Set(["uploaded", "uploading", "analyzing"]);

/** Merge a fresh poll snapshot over the current tiles, KEEPING each existing
 *  tile's already-signed `display_url`. `_asset_out` re-signs on every read and
 *  a GCS V4 URL embeds a fresh timestamp+signature, so a blind replace hands
 *  every ready `<img>`/`<video>` a new `src` each tick → the browser reloads
 *  every thumbnail every 5s. The prior URL was signed for 60 min, so it's still
 *  valid; reuse it and only new tiles (or ones that hadn't signed yet) take the
 *  fresh URL. Status/subject/brands/etc. always come from the server snapshot. */
function mergePreservingUrls(prev: PoolAsset[], next: PoolAsset[]): PoolAsset[] {
  const prevById = new Map(prev.map((a) => [a.id, a]));
  return next.map((a) => {
    const existing = prevById.get(a.id);
    return existing?.display_url ? { ...a, display_url: existing.display_url } : a;
  });
}

/** Backend flag off → routes 404 with this detail (or a raw 404 wrapper). */
function isUnavailableError(err: unknown): boolean {
  return err instanceof Error && (/not available/i.test(err.message) || err.message.includes("(404)"));
}

/** Local tile for an in-flight upload (before the server row exists). */
interface PendingUpload {
  localId: string;
  filename: string;
}

export default function AssetPool({
  itemId,
  attachedPaths,
  onUseInEdit,
  attachBusy = false,
  onAssetContextUpdated,
}: {
  itemId: string;
  /** gcs_paths already attached as clips — flips a promoted tile to "In edit ✓". */
  attachedPaths?: string[];
  /** "Use in edit" promotion: re-attach the pool object as a clip (video assets
   *  only). Absent → the affordance doesn't render (pool-only surfaces). */
  onUseInEdit?: (asset: PoolAsset) => void | Promise<void>;
  /** True while another attach writer (clip upload) is in flight. attach_clips is
   *  a full-set replace, so concurrent writers silently drop each other's clips —
   *  promotion is disabled until the other write settles. */
  attachBusy?: boolean;
  /** Context edits clear pending AI suggestions; parent clears lifted local rows. */
  onAssetContextUpdated?: (asset: PoolAsset) => void;
}) {
  const enabled = process.env.NEXT_PUBLIC_OVERLAY_AUTOPLACE_ENABLED === "true";

  const [assets, setAssets] = useState<PoolAsset[]>([]);
  const [maxAssets, setMaxAssets] = useState(20);
  const [pending, setPending] = useState<PendingUpload[]>([]);
  const [unavailable, setUnavailable] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [uploadError, setUploadError] = useState<string | null>(null);
  // One promotion at a time: attach_clips is last-writer-wins over the FULL
  // assignment set, so two in-flight promotions built from the same stale
  // snapshot would drop each other's clip. Serializing client-side closes the
  // rapid-double-promote race; attachBusy covers the concurrent-upload writer.
  const [promotingId, setPromotingId] = useState<string | null>(null);
  const noticeTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  // Bumped on every local list mutation (register append, delete). A poll
  // response only applies when the epoch it was dispatched under is still
  // current — a GET racing a register/delete would otherwise resurrect a
  // removed tile or drop a just-registered one until the next tick.
  const listEpoch = useRef(0);
  // One poll in flight at a time. Signing up to 20 URLs server-side can exceed
  // the 5s tick under load; without this a slow server gets a new request every
  // tick (pile-up) and out-of-order responses flicker a ready tile back to
  // "analyzing". The epoch guard can't catch that — both carry the same epoch.
  const pollInFlight = useRef(false);

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

  // Status polling — analysis progresses server-side; refetch every 5s while
  // any asset is non-terminal so "Analyzing…" and the subject micro-label
  // appear without a page refresh. The effect tears down (and the interval
  // stops) as soon as every asset reaches ready/failed.
  const hasNonTerminal = assets.some((a) => NON_TERMINAL_ASSET_STATUSES.has(a.status));
  useEffect(() => {
    if (!enabled || unavailable || !hasNonTerminal) return;
    let cancelled = false;
    const id = setInterval(() => {
      if (pollInFlight.current) return;
      pollInFlight.current = true;
      const epoch = listEpoch.current;
      listPoolAssets(itemId)
        .then((res) => {
          if (cancelled || epoch !== listEpoch.current) return;
          setAssets((prev) => mergePreservingUrls(prev, res.assets));
          setMaxAssets(res.max_assets);
        })
        .catch((err) => {
          if (cancelled) return;
          if (isUnavailableError(err)) setUnavailable(true);
          // Transient poll errors stay silent — the next tick retries.
        })
        .finally(() => {
          pollInFlight.current = false;
        });
    }, ASSET_POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(id);
      // A poll may still be resolving; free the lease so the next mount's
      // interval isn't blocked by a stale in-flight flag.
      pollInFlight.current = false;
    };
  }, [enabled, unavailable, hasNonTerminal, itemId]);

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
            listEpoch.current += 1;
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
        listEpoch.current += 1;
        setAssets((prev) => prev.filter((a) => a.id !== asset.id));
      } catch (err) {
        if (isUnavailableError(err)) setUnavailable(true);
        else setUploadError(err instanceof Error ? err.message : "Couldn't remove that file");
      }
    },
    [itemId],
  );

  // Set lookup: the tile grid re-renders on every job-status poll tick, so keep
  // the per-tile membership check O(1) instead of O(attached) per asset.
  const attached = useMemo(() => new Set(attachedPaths ?? []), [attachedPaths]);

  const handleUseInEdit = useCallback(
    async (asset: PoolAsset) => {
      if (!onUseInEdit) return;
      setPromotingId(asset.id);
      try {
        await onUseInEdit(asset);
      } finally {
        setPromotingId(null);
      }
    },
    [onUseInEdit],
  );

  const handleSaveContext = useCallback(
    async (asset: PoolAsset, userContext: string) => {
      try {
        const updated = await updatePoolAssetContext(itemId, asset.id, userContext || null);
        listEpoch.current += 1;
        setAssets((prev) => prev.map((a) => (a.id === updated.id ? updated : a)));
        onAssetContextUpdated?.(updated);
        showNotice(userContext.trim() ? "Context saved — re-match visuals when ready" : "Context cleared");
      } catch (err) {
        if (isUnavailableError(err)) setUnavailable(true);
        else setUploadError(err instanceof Error ? err.message : "Couldn't save context");
      }
    },
    [itemId, onAssetContextUpdated, showNotice],
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
                Screenshots and screen recordings — Kria will place them on your video for you.
              </p>
              <button
                type="button"
                onClick={() => inputRef.current?.click()}
                className="mt-3 inline-flex min-h-11 items-center gap-1.5 rounded-lg border border-zinc-200 bg-white px-4 py-2 text-sm text-[#3f3f46] transition-colors hover:border-lime-400 hover:text-lime-700 focus-visible:outline focus-visible:outline-2 focus-visible:outline-lime-500 sm:min-h-0"
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
              <ul className="grid grid-cols-2 gap-2 sm:grid-cols-4 md:grid-cols-5">
                {assets.map((asset) => (
                  <AssetTile
                    key={asset.id}
                    asset={asset}
                    onRemove={() => handleRemove(asset)}
                    inEdit={attached.has(asset.gcs_path)}
                    // Version-skew guard: an old API's PoolAssetOut has no gcs_path —
                    // without one there is nothing valid to attach, so the
                    // affordance must not render at all.
                    onUseInEdit={
                      onUseInEdit && asset.gcs_path ? () => handleUseInEdit(asset) : undefined
                    }
                    onSaveContext={(userContext) => handleSaveContext(asset, userContext)}
                    promoting={promotingId === asset.id}
                    promotionDisabled={attachBusy || promotingId !== null}
                  />
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

function AssetTile({
  asset,
  onRemove,
  inEdit = false,
  onUseInEdit,
  onSaveContext,
  promoting = false,
  promotionDisabled = false,
}: {
  asset: PoolAsset;
  onRemove: () => void;
  inEdit?: boolean;
  onUseInEdit?: () => void | Promise<void>;
  onSaveContext: (userContext: string) => void | Promise<void>;
  /** THIS tile's promotion is in flight — shows "Adding…" instead of the button. */
  promoting?: boolean;
  /** ANY attach writer is busy (another promotion or a clip upload) — disables the button. */
  promotionDisabled?: boolean;
}) {
  const label = asset.source_filename ?? "this file";
  const [editingContext, setEditingContext] = useState(false);
  const [draftContext, setDraftContext] = useState(asset.user_context ?? "");
  const [savingContext, setSavingContext] = useState(false);

  useEffect(() => {
    setDraftContext(asset.user_context ?? "");
  }, [asset.user_context]);

  async function saveContext() {
    setSavingContext(true);
    try {
      await onSaveContext(draftContext);
      setEditingContext(false);
    } finally {
      setSavingContext(false);
    }
  }

  if (asset.status === "failed") {
    return (
      <li className="relative flex aspect-square flex-col items-center justify-center rounded-lg border border-dashed border-zinc-200 bg-white p-2 text-center">
        <p className="text-[12px] text-[#71717a]">Couldn&apos;t read this file</p>
        <button
          type="button"
          onClick={onRemove}
          aria-label={`Remove ${label}`}
          className="mt-1 min-h-11 min-w-11 text-[12px] text-[#71717a] underline underline-offset-2 transition-colors hover:text-[#0c0c0e] focus-visible:outline focus-visible:outline-2 focus-visible:outline-lime-500 sm:min-h-[28px] sm:min-w-[28px]"
        >
          Remove
        </button>
      </li>
    );
  }

  const busy = asset.status === "analyzing" || asset.status === "uploading";
  // Detected brand identities (analysis v5) ride the subject line's title
  // attribute — enough to verify detection without new tile chrome.
  const brands = asset.brands ?? [];
  const novaLine = asset.nova_description ?? asset.nova_on_screen_text ?? null;

  return (
    <li className="group relative overflow-hidden rounded-lg border border-zinc-200 bg-white">
      <div className="relative aspect-square overflow-hidden">
        {busy || !asset.display_url ? (
          <div className="absolute inset-0 bg-[linear-gradient(110deg,#f4f4f5,45%,#e4e4e7,55%,#f4f4f5)] bg-[length:200%_100%] motion-safe:animate-shimmer" />
        ) : asset.kind === "video" ? (
          <video src={asset.display_url} muted playsInline preload="metadata" className="h-full w-full object-cover" />
        ) : (
          // eslint-disable-next-line @next/next/no-img-element -- signed GCS thumbnail, not an optimizable static asset
          <img src={asset.display_url} alt={asset.subject ?? label} className="h-full w-full object-cover" />
        )}
        {/* bg-white/95 (not /85): the lime-700 action text must hold the 4.5:1
            contrast floor even over dark video frames (DESIGN.md §8). */}
        <span className="absolute inset-x-0 bottom-0 flex items-center justify-between gap-1 bg-white/95 px-1.5 py-1 text-[12px] text-[#71717a]">
          <span
            className="truncate"
            title={!busy && brands.length > 0 ? `Brands: ${brands.join(", ")}` : undefined}
          >
            {busy ? "Analyzing…" : (asset.subject ?? asset.kind)}
          </span>
          {/* "Use in edit" — video assets only: promotes the pool object to a real
              clip (B-roll / spine candidate). Images stay overlay-only in v1. */}
          {onUseInEdit && asset.kind === "video" && !busy && (
            inEdit ? (
              <span className="shrink-0 text-lime-700">In edit ✓</span>
            ) : promoting ? (
              <span className="shrink-0 text-lime-700">Adding…</span>
            ) : (
              <button
                type="button"
                onClick={onUseInEdit}
                disabled={promotionDisabled}
                aria-label={`Use ${label} in the edit`}
                className="-my-1 flex min-h-11 min-w-11 shrink-0 items-center px-1 text-lime-700 underline underline-offset-2 transition-colors hover:text-lime-800 focus-visible:outline focus-visible:outline-2 focus-visible:outline-lime-500 disabled:cursor-not-allowed disabled:opacity-50 disabled:hover:text-lime-700 sm:min-h-[28px] sm:min-w-[28px]"
              >
                Use in edit
              </button>
            )
          )}
        </span>
      </div>
      <div className="space-y-1.5 px-2 py-2 text-[11px] leading-snug">
        <div>
          <div className="mb-0.5 flex items-center justify-between gap-2">
            <span className="font-semibold text-[#3f3f46]">You</span>
            {!busy && !editingContext && (
              <button
                type="button"
                onClick={() => setEditingContext(true)}
                className="min-h-7 shrink-0 text-lime-700 underline underline-offset-2 hover:text-lime-800 focus-visible:outline focus-visible:outline-2 focus-visible:outline-lime-500"
              >
                {asset.user_context ? "Edit" : "Add context"}
              </button>
            )}
          </div>
          {editingContext ? (
            <div className="space-y-1.5">
              <textarea
                value={draftContext}
                maxLength={500}
                rows={3}
                onChange={(event) => setDraftContext(event.target.value)}
                className="w-full resize-none rounded-md border border-zinc-200 px-2 py-1.5 text-[12px] text-[#0c0c0e] focus-visible:outline focus-visible:outline-2 focus-visible:outline-lime-500"
                placeholder="What should Nova know about this visual?"
              />
              <div className="flex justify-end gap-2">
                <button
                  type="button"
                  onClick={() => {
                    setDraftContext(asset.user_context ?? "");
                    setEditingContext(false);
                  }}
                  className="min-h-8 px-2 text-[11px] text-[#71717a] hover:text-[#0c0c0e]"
                >
                  Cancel
                </button>
                <button
                  type="button"
                  disabled={savingContext}
                  onClick={saveContext}
                  className="min-h-8 rounded-md bg-[#0c0c0e] px-2 text-[11px] font-semibold text-white disabled:opacity-50"
                >
                  {savingContext ? "Saving…" : "Save"}
                </button>
              </div>
            </div>
          ) : (
            <p className="line-clamp-2 text-[#71717a]">
              {asset.user_context || "No context yet"}
            </p>
          )}
        </div>
        <div>
          <span className="font-semibold text-[#3f3f46]">Nova</span>
          <p className="line-clamp-2 text-[#71717a]">{novaLine || "Analysis pending"}</p>
        </div>
      </div>
      <button
        type="button"
        onClick={onRemove}
        aria-label={`Remove ${label}`}
        className="absolute right-1 top-1 flex h-11 w-11 items-center justify-center rounded-full bg-white/90 text-[#3f3f46] opacity-100 transition-opacity focus-visible:opacity-100 focus-visible:outline focus-visible:outline-2 focus-visible:outline-lime-500 sm:h-7 sm:w-7 sm:opacity-0 sm:group-hover:opacity-100"
      >
        ×
      </button>
    </li>
  );
}
