"use client";

/**
 * AssetPool — the per-item "Visuals pool" (overlay auto-placement PR0, plans/005).
 *
 * Creators drop screenshots / screen recordings here; the pool later feeds the
 * AI overlay auto-placement matcher (PR1a+). The pool is also the durable
 * staging lane for manual media overlays, so it is enabled by the union of
 * auto-placement, guided-edit, and manual-overlay flags. A backend 404 still
 * surfaces a quiet dashed-zinc error line — never silent.
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
  reanalyzePoolAsset,
  updatePoolAssetContext,
  type PoolAsset,
  type PoolReservationCapacity,
} from "@/lib/plan-api";
import { poolAssetAnalysisLine } from "@/lib/pool-asset-display";
import { mergePoolAssetsPreservingDisplayUrls } from "@/lib/pool-assets";
import { StableVideo } from "@/components/StableVideo";
import { Dropzone } from "@/components/ui/dropzone";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import {
  POOL_ASSET_MIME_TYPES,
  usePoolAssetUploader,
  type PendingPoolUpload,
} from "@/app/plan/_hooks/usePoolAssetUploader";

const NOTICE_MS = 4000;
const UNAVAILABLE_COPY = "Visuals pool isn't available right now.";

// Analysis happens server-side after register (queued → analyzing → ready |
// failed) — poll while any asset is mid-pipeline so tiles update in place.
// Unknown future statuses deliberately DON'T poll (no runaway interval).
const ASSET_POLL_MS = 5000;
const NON_TERMINAL_ASSET_STATUSES = new Set(["uploaded", "queued", "analyzing"]);

/** Backend flag off → routes 404 with this detail (or a raw 404 wrapper). */
function isUnavailableError(err: unknown): boolean {
  return err instanceof Error && (/not available/i.test(err.message) || err.message.includes("(404)"));
}

export default function AssetPool({
  itemId,
  attachedPaths,
  onUseInEdit,
  attachBusy = false,
  onAssetContextUpdated,
  onMutated,
  onAssetsChanged,
  embedded = false,
  concise = false,
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
  /** Any successful pool mutation can stale an approved guided-edit proposal. */
  onMutated?: () => void;
  /** Fires whenever the live pool list changes (initial fetch, poll tick,
   *  register, delete, promote) — lets the parent page track e.g. whether a
   *  ready pool asset exists for the auto-design Generate gate, without a
   *  second fetch of the same list (P2-5, 2026-08-18 adversarial review). */
  onAssetsChanged?: (assets: PoolAsset[]) => void;
  /** Mounted inside the item-setup Card's "Visuals" tab (Lane G): drops the
   *  "Visuals pool" eyebrow + outer bordered container (the Card already
   *  supplies both) and swaps the empty state for the shared Dropzone. */
  embedded?: boolean;
  /** Parent already supplies the Visuals title and description. */
  concise?: boolean;
}) {
  const guidedEditEnabled = process.env.NEXT_PUBLIC_GUIDED_EDIT_ENABLED === "true";
  const enabled =
    process.env.NEXT_PUBLIC_OVERLAY_AUTOPLACE_ENABLED === "true" || guidedEditEnabled;

  const [assets, setAssets] = useState<PoolAsset[]>([]);
  const [serverReservations, setServerReservations] = useState<PoolReservationCapacity[]>([]);
  const [serverOccupiedCount, setServerOccupiedCount] = useState(0);
  const [maxAssets, setMaxAssets] = useState(20);
  const [unavailable, setUnavailable] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [uploadError, setUploadError] = useState<string | null>(null);
  // One promotion at a time: attach_clips is last-writer-wins over the FULL
  // assignment set, so two in-flight promotions built from the same stale
  // snapshot would drop each other's clip. Serializing client-side closes the
  // rapid-double-promote race; attachBusy covers the concurrent-upload writer.
  const [promotingId, setPromotingId] = useState<string | null>(null);
  const [reanalyzingId, setReanalyzingId] = useState<string | null>(null);
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

  const uploader = usePoolAssetUploader({
    itemId,
    assetCount: assets.length,
    maxAssets,
    onRegistered: (asset) => {
      listEpoch.current += 1;
      setAssets((prev) => [...prev.filter((row) => row.id !== asset.id), asset]);
      onMutated?.();
    },
    onUnavailable: () => setUnavailable(true),
    onDeduped: () => showNotice("Already in your pool"),
    serverReservations,
    serverOccupiedCount,
    onReservationFinalized: (reservationId, releasedCapacity) => {
      setServerReservations((current) =>
        current.filter((reservation) => reservation.reservation_id !== reservationId),
      );
      if (releasedCapacity) {
        setServerOccupiedCount((current) => Math.max(0, current - 1));
      }
    },
  });
  const pending = uploader.uploads;
  const addPoolFiles = uploader.addFiles;

  useEffect(() => () => {
    if (noticeTimer.current) clearTimeout(noticeTimer.current);
  }, []);

  useEffect(() => {
    onAssetsChanged?.(assets);
    // onAssetsChanged intentionally excluded: an inline arrow prop would
    // otherwise re-fire this effect every parent render even when `assets`
    // itself hasn't changed.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [assets]);

  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    const startedAtEpoch = listEpoch.current;
    listPoolAssets(itemId)
      .then((res) => {
        if (cancelled || startedAtEpoch !== listEpoch.current) return;
        setAssets((current) => mergePoolAssetsPreservingDisplayUrls(current, res.assets));
        setMaxAssets(res.max_assets);
        setServerReservations(res.active_reservations ?? []);
        setServerOccupiedCount(res.occupied_assets ?? res.assets.length);
      })
      .catch((err) => {
        if (cancelled) return;
        if (isUnavailableError(err)) setUnavailable(true);
        else setUploadError("We couldn’t load your visuals. Check your connection and try again.");
      });
    return () => {
      cancelled = true;
    };
  }, [enabled, itemId]);

  // Status polling — analysis progresses server-side; refetch every 5s while
  // any asset is non-terminal so "Analyzing…" and the subject micro-label
  // appear without a page refresh. The effect tears down (and the interval
  // stops) as soon as every asset reaches ready/failed.
  const hasNonTerminal =
    assets.some(
      (a) =>
        NON_TERMINAL_ASSET_STATUSES.has(a.status) ||
        a.media_status === "pending" ||
        a.preview_status === "pending",
    ) ||
    serverReservations.some((reservation) => reservation.release_at === null);
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
          setAssets((prev) => mergePoolAssetsPreservingDisplayUrls(prev, res.assets));
          setMaxAssets(res.max_assets);
          setServerReservations(res.active_reservations ?? []);
          setServerOccupiedCount(res.occupied_assets ?? res.assets.length);
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
    (files: FileList | File[] | null) => {
      setUploadError(null);
      addPoolFiles(files);
    },
    [addPoolFiles],
  );

  const handleRemove = useCallback(
    async (asset: PoolAsset) => {
      try {
        await deletePoolAsset(itemId, asset.id);
        listEpoch.current += 1;
        setAssets((prev) => prev.filter((a) => a.id !== asset.id));
        setServerOccupiedCount((current) => Math.max(0, current - 1));
        onMutated?.();
      } catch (err) {
        if (isUnavailableError(err)) setUnavailable(true);
        else setUploadError("We couldn’t remove that visual. Try again.");
      }
    },
    [itemId, onMutated],
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
        onMutated?.();
        showNotice(userContext.trim() ? "Context saved — re-match visuals when ready" : "Context cleared");
      } catch (err) {
        if (isUnavailableError(err)) setUnavailable(true);
        else setUploadError("We couldn’t save that context. Try again.");
      }
    },
    [itemId, onAssetContextUpdated, onMutated, showNotice],
  );

  const handleRetryAnalysis = useCallback(
    async (asset: PoolAsset) => {
      setReanalyzingId(asset.id);
      setUploadError(null);
      try {
        const updated = await reanalyzePoolAsset(itemId, asset.id);
        listEpoch.current += 1;
        setAssets((prev) => prev.map((row) => (row.id === updated.id ? updated : row)));
      } catch (err) {
        setUploadError("Kria couldn’t retry that analysis. Try again.");
      } finally {
        setReanalyzingId(null);
      }
    },
    [itemId],
  );

  if (!enabled) return null;

  const count = assets.length;
  const imageCount = assets.filter((asset) => asset.kind === "image").length;
  const videoCount = count - imageCount;
  const atCap = count + uploader.reservedSlots >= maxAssets;
  const releasingSlots = Math.max(0, uploader.reservedSlots - pending.length);
  const isEmpty = count === 0 && pending.length === 0;
  const inputId = `asset-pool-input-${itemId}`;

  const body = (
    <>
      {!embedded && (
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
      )}

      {embedded && !concise && !unavailable && (
        <div className="mb-4 flex flex-col gap-1 sm:flex-row sm:items-start sm:justify-between sm:gap-4">
          <div>
            <p className="text-sm font-medium text-[#0c0c0e]">Photos and supporting videos</p>
            <p className="text-xs text-muted-foreground">
              Add photos here, not in Clips. Kria can use saved visuals when it builds your edit.
            </p>
          </div>
          <p
            className="shrink-0 text-xs font-medium text-lime-700"
            role="status"
            aria-live="polite"
            data-testid="visuals-saved-receipt"
          >
            {count === 0
              ? "No visuals saved"
              : `${count} ${count === 1 ? "visual" : "visuals"} saved${
                  imageCount > 0 && videoCount > 0
                    ? ` (${imageCount} ${imageCount === 1 ? "photo" : "photos"}, ${videoCount} ${videoCount === 1 ? "video" : "videos"})`
                    : ""
                }`}
          </p>
        </div>
      )}

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
            accept={POOL_ASSET_MIME_TYPES.join(",")}
            className="sr-only"
            aria-label="Add visuals to your pool"
            disabled={atCap}
            onChange={(e) => {
              handleFiles(e.target.files);
              e.target.value = "";
            }}
          />

          {isEmpty ? (
            embedded ? (
              /* Embedded (item-setup Card "Visuals" tab, Lane G) — the shared
                 Dropzone primitive instead of the standalone dashed box; the
                 Card already supplies the surrounding chrome. */
              <div>
                <Dropzone
                  onFiles={(files) => {
                    if (!atCap) handleFiles(files);
                  }}
                  accept={POOL_ASSET_MIME_TYPES.join(",")}
                  multiple
                  disabled={atCap}
                  title={concise ? "Choose files or drop them here" : "Drop photos or supporting videos"}
                  subline={concise ? undefined : "They are saved to Visuals when they appear below"}
                  ariaLabel="Add visuals"
                  inputAriaLabel="Add visuals to your pool (embedded)"
                />
                {releasingSlots > 0 && (
                  <p className="mt-2 text-[12px] text-[#71717a]">
                    Kria is releasing a removed upload slot. You can add another visual when cleanup finishes.
                  </p>
                )}
              </div>
            ) : (
            /* Empty state — leads with the action (§9), never "Nothing here yet". */
            <div
              className="rounded-xl border border-dashed border-zinc-200 bg-white p-5 text-center"
              onDragOver={(e) => e.preventDefault()}
              onDrop={(e) => {
                e.preventDefault();
                if (!atCap) handleFiles(e.dataTransfer.files);
              }}
            >
              <p className="font-display text-[16px] font-medium text-[#0c0c0e]">
                {guidedEditEnabled
                  ? "Add the photos and videos that belong in this story"
                  : "Drop the screenshots you mention in your script"}
              </p>
              <p className="mt-1 text-[12px] text-[#71717a]">
                {guidedEditEnabled
                  ? "Kria will review these alongside your main clips before proposing the edit."
                  : "Screenshots and screen recordings — Kria will place them on your video for you."}
              </p>
              <Button
                type="button"
                variant="outline"
                disabled={atCap}
                onClick={() => inputRef.current?.click()}
                className="mt-3 min-h-11 sm:min-h-0"
              >
                Add visuals
              </Button>
              {releasingSlots > 0 && (
                <p className="mt-2 text-[12px] text-[#71717a]">
                  Kria is releasing a removed upload slot. You can add another visual when cleanup finishes.
                </p>
              )}
            </div>
            )
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
                    onRetryAnalysis={() => handleRetryAnalysis(asset)}
                    retryingAnalysis={reanalyzingId === asset.id}
                    promoting={promotingId === asset.id}
                    promotionDisabled={attachBusy || promotingId !== null}
                  />
                ))}
                {pending.map((upload) => (
                  <PendingUploadTile
                    key={upload.localId}
                    upload={upload}
                    onRetry={() => uploader.retry(upload.localId)}
                    onRemove={() => uploader.remove(upload.localId)}
                  />
                ))}
                {/* Add tile — disabled at cap with an inline reason below (never tooltip-only). */}
                <li>
                  <Button
                    type="button"
                    variant="outline"
                    disabled={atCap}
                    onClick={() => inputRef.current?.click()}
                    className="aspect-square h-auto w-full flex-col gap-0 rounded-lg border-dashed p-0 text-[#71717a] hover:text-[#71717a]"
                  >
                    <span aria-hidden className="text-lg leading-none">+</span>
                    <span className="mt-1 text-[12px]">Add</span>
                  </Button>
                </li>
              </ul>
              {atCap && (
                <p className="mt-2 text-[12px] text-[#71717a]">
                  {releasingSlots > 0
                    ? "Kria is releasing a removed upload slot. You can add another visual when cleanup finishes."
                    : "Your pool is full — remove a visual to add another."}
                </p>
              )}
            </div>
          )}

          {notice && (
            <p className="mt-2 rounded border border-zinc-200 bg-white px-3 py-2 text-[12px] text-[#3f3f46]">
              {notice}
            </p>
          )}
          {uploader.batchMessage && (
            <p className="mt-2 rounded border border-zinc-200 bg-white px-3 py-2 text-[12px] text-[#3f3f46]">
              {uploader.batchMessage}
            </p>
          )}
          {uploader.summary && (
            <p className="mt-2 text-[12px] text-[#71717a]" aria-live="polite">
              {uploader.summary}
            </p>
          )}
          {uploadError && (
            <p className="mt-2 rounded border border-zinc-200 bg-white px-3 py-2 text-[12px] text-[#3f3f46]">
              {uploadError}
            </p>
          )}
        </>
      )}
    </>
  );

  return embedded ? body : <div className="my-6">{body}</div>;
}

function PendingUploadTile({
  upload,
  onRetry,
  onRemove,
}: {
  upload: PendingPoolUpload;
  onRetry: () => void;
  onRemove: () => void;
}) {
  if (upload.stage === "failed") {
    return (
      <li className="relative flex aspect-square flex-col items-center justify-center rounded-lg border border-dashed border-zinc-200 bg-white p-2 text-center">
        <p className="mb-1 max-w-full truncate text-[11px] font-medium text-[#3f3f46]">
          {upload.filename}
        </p>
        <p className="line-clamp-3 text-[12px] text-[#71717a]">{upload.message}</p>
        <div className="mt-1 flex gap-2 text-[12px]">
          {upload.retryable && (
            <Button
              type="button"
              variant="link"
              onClick={onRetry}
              className="h-auto min-h-7 p-0"
            >
              Retry
            </Button>
          )}
          <Button
            type="button"
            variant="link"
            onClick={onRemove}
            className="h-auto min-h-7 p-0 text-[#71717a]"
          >
            Remove
          </Button>
        </div>
      </li>
    );
  }
  const label =
    upload.stage === "preparing"
      ? "Preparing…"
      : upload.stage === "registering"
        ? "Adding…"
        : "Uploading…";
  return (
    <li
      aria-label={`${label} ${upload.filename}`}
      className="relative aspect-square overflow-hidden rounded-lg border border-zinc-200 bg-[linear-gradient(110deg,#f4f4f5,45%,#e4e4e7,55%,#f4f4f5)] bg-[length:200%_100%] motion-safe:animate-shimmer"
    >
      <span className="absolute inset-x-0 bottom-0 truncate px-1.5 py-1 text-[12px] text-[#71717a]">
        {label}
      </span>
    </li>
  );
}

function AssetTile({
  asset,
  onRemove,
  inEdit = false,
  onUseInEdit,
  onSaveContext,
  onRetryAnalysis,
  retryingAnalysis = false,
  promoting = false,
  promotionDisabled = false,
}: {
  asset: PoolAsset;
  onRemove: () => void;
  inEdit?: boolean;
  onUseInEdit?: () => void | Promise<void>;
  onSaveContext: (userContext: string) => void | Promise<void>;
  onRetryAnalysis: () => void | Promise<void>;
  retryingAnalysis?: boolean;
  /** THIS tile's promotion is in flight — shows "Adding…" instead of the button. */
  promoting?: boolean;
  /** ANY attach writer is busy (another promotion or a clip upload) — disables the button. */
  promotionDisabled?: boolean;
}) {
  const label = asset.source_filename ?? "this file";
  const [editingContext, setEditingContext] = useState(false);
  const [draftContext, setDraftContext] = useState(asset.user_context ?? "");
  const [savingContext, setSavingContext] = useState(false);
  const [contextError, setContextError] = useState<string | null>(null);
  // Some HEIC/HEVC uploads still fail to decode client-side even with a
  // preview (or predate the preview backfill) — fall back to the kind-label
  // placeholder instead of a permanently broken tile.
  const [mediaError, setMediaError] = useState(false);

  useEffect(() => {
    setDraftContext(asset.user_context ?? "");
  }, [asset.user_context]);

  useEffect(() => {
    setMediaError(false);
  }, [asset.display_url]);

  async function saveContext() {
    setSavingContext(true);
    setContextError(null);
    try {
      await onSaveContext(draftContext);
      setEditingContext(false);
    } catch (err) {
      setContextError("We couldn’t save that context. Try again.");
    } finally {
      setSavingContext(false);
    }
  }

  const mediaReady = asset.media_status === "ready";
  if (
    (!mediaReady && asset.status === "failed") ||
    asset.media_status === "failed" ||
    asset.media_status === "unreadable" ||
    asset.preview_status === "failed"
  ) {
    return (
      <li className="relative flex aspect-square flex-col items-center justify-center rounded-lg border border-dashed border-zinc-200 bg-white p-2 text-center">
        <p className="text-[12px] text-[#71717a]">
          We couldn&apos;t read this file or prepare it for the editor. Check the file type and try again.
        </p>
        {asset.retryable !== false && (
          <Button
            type="button"
            variant="link"
            onClick={onRetryAnalysis}
            disabled={retryingAnalysis}
            className="mt-1 h-auto min-h-11 min-w-11 p-0 text-[12px] sm:min-h-[28px] sm:min-w-[28px]"
          >
            {retryingAnalysis ? "Retrying…" : "Retry analysis"}
          </Button>
        )}
        <Button
          type="button"
          variant="link"
          onClick={onRemove}
          aria-label={`Remove ${label}`}
          className="mt-1 h-auto min-h-11 min-w-11 p-0 text-[12px] text-[#71717a] hover:text-[#0c0c0e] sm:min-h-[28px] sm:min-w-[28px]"
        >
          Remove
        </Button>
      </li>
    );
  }

  const busy =
    asset.status === "queued" ||
    asset.status === "analyzing" ||
    asset.status === "uploaded" ||
    asset.media_status === "pending" ||
    asset.preview_status === "pending";
  // Detected brand identities (analysis v5) ride the subject line's title
  // attribute — enough to verify detection without new tile chrome.
  const brands = asset.brands ?? [];
  const novaStatusLine = poolAssetAnalysisLine(asset);

  return (
    <li className="group relative overflow-hidden rounded-lg border border-zinc-200 bg-white">
      <div className="relative aspect-square overflow-hidden">
        {busy || !asset.display_url || mediaError ? (
          <div className="absolute inset-0 flex items-center justify-center bg-[linear-gradient(110deg,#f4f4f5,45%,#e4e4e7,55%,#f4f4f5)] bg-[length:200%_100%] motion-safe:animate-shimmer">
            {!busy && mediaError && (
              <span className="text-[11px] font-medium capitalize text-[#71717a]">{asset.kind}</span>
            )}
          </div>
        ) : asset.kind === "video" ? (
          <StableVideo
            src={asset.display_url}
            poster={asset.preview_url ?? undefined}
            muted
            playsInline
            preload="metadata"
            onError={() => setMediaError(true)}
            className="h-full w-full object-cover"
          />
        ) : (
          // eslint-disable-next-line @next/next/no-img-element -- signed GCS thumbnail, not an optimizable static asset
          <img
            src={asset.display_url}
            alt={asset.subject ?? label}
            onError={() => setMediaError(true)}
            className="h-full w-full object-cover"
          />
        )}
        {/* bg-white/95 (not /85): the lime-700 action text must hold the 4.5:1
            contrast floor even over dark video frames (DESIGN.md §8). */}
        <span className="absolute inset-x-0 bottom-0 flex items-center justify-between gap-1 bg-white/95 px-1.5 py-1 text-[12px] text-[#71717a]">
          <span
            className="truncate"
            title={!busy && brands.length > 0 ? `Brands: ${brands.join(", ")}` : undefined}
          >
            {busy
              ? asset.status === "queued"
                ? "Queued…"
                : "Analyzing…"
              : asset.status === "failed"
                ? "Manual visual ready"
                : (asset.subject ?? asset.kind)}
          </span>
          {/* "Use in edit" — video assets only: promotes the pool object to a real
              clip (B-roll / spine candidate). Images stay overlay-only in v1. */}
          {onUseInEdit && asset.kind === "video" && !busy && (
            inEdit ? (
              <span className="shrink-0 text-lime-700">In edit ✓</span>
            ) : promoting ? (
              <span className="shrink-0 text-lime-700">Adding…</span>
            ) : (
              <Button
                type="button"
                variant="link"
                onClick={onUseInEdit}
                disabled={promotionDisabled}
                aria-label={`Use ${label} in the edit`}
                className="-my-1 h-auto min-h-11 min-w-11 shrink-0 px-1 sm:min-h-[28px] sm:min-w-[28px]"
              >
                Use in edit
              </Button>
            )
          )}
        </span>
      </div>
      <div className="space-y-1.5 px-2 py-2 text-[11px] leading-snug">
        <div>
          <div className="mb-0.5 flex items-center justify-between gap-2">
            <span className="font-semibold text-[#3f3f46]">You</span>
            {!busy && !editingContext && (
              <Button
                type="button"
                variant="link"
                onClick={() => setEditingContext(true)}
                className="h-auto min-h-7 shrink-0 p-0"
              >
                {asset.user_context ? "Edit" : "Add context"}
              </Button>
            )}
          </div>
          {editingContext ? (
            <div className="space-y-1.5">
              <Textarea
                value={draftContext}
                maxLength={500}
                rows={3}
                onChange={(event) => setDraftContext(event.target.value.slice(0, 500))}
                className="resize-none"
                placeholder="What should Kria know about this visual?"
              />
              {contextError && <p className="text-[11px] text-[#3f3f46]">{contextError}</p>}
              <div className="flex justify-end gap-2">
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  onClick={() => {
                    setDraftContext(asset.user_context ?? "");
                    setEditingContext(false);
                  }}
                  className="h-auto min-h-8 px-2 text-[11px] text-[#71717a] hover:text-[#0c0c0e]"
                >
                  Cancel
                </Button>
                <Button
                  type="button"
                  size="sm"
                  disabled={savingContext}
                  onClick={saveContext}
                  className="h-auto min-h-8 px-2 text-[11px]"
                >
                  {savingContext ? "Saving…" : "Save"}
                </Button>
              </div>
            </div>
          ) : (
            <p className="line-clamp-2 text-[#71717a]">
              {asset.user_context || "No context yet"}
            </p>
          )}
        </div>
        <div>
          <span className="font-semibold text-[#3f3f46]">Kria</span>
          <p className="line-clamp-2 text-[#71717a]">{novaStatusLine}</p>
        </div>
      </div>
      <Button
        type="button"
        variant="ghost"
        size="icon"
        onClick={onRemove}
        aria-label={`Remove ${label}`}
        className="absolute right-1 top-1 h-11 w-11 rounded-full bg-white/90 text-[#3f3f46] opacity-100 hover:bg-white sm:h-7 sm:w-7 sm:opacity-0 sm:group-hover:opacity-100"
      >
        ×
      </Button>
    </li>
  );
}
