"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  FeatureDisabledError,
  PlanApiError,
  registerPoolAsset,
  requestPoolAssetUploadUrls,
  sha256HexOfFile,
  uploadContentTypeForFile,
  uploadToGcs,
  type PoolAsset,
  type PoolReservationCapacity,
} from "@/lib/plan-api";

export const POOL_ASSET_MIME_TYPES = [
  "image/jpeg",
  "image/png",
  "image/webp",
  "image/heic",
  "image/heif",
  "video/mp4",
  "video/quicktime",
] as const;

const TRANSFER_CONCURRENCY = 3;
const MAX_POOL_IMAGE_BYTES = 25 * 1024 * 1024;
const MAX_POOL_VIDEO_BYTES = 512 * 1024 * 1024;
const RESERVATION_CLEANUP_GRACE_MS = 15 * 60 * 1000;
const RESERVATION_TTL_AND_GRACE_MS = 30 * 60 * 1000;

export type PoolUploadStage = "preparing" | "uploading" | "registering" | "failed";
type FailedStage = Exclude<PoolUploadStage, "failed">;

export interface PendingPoolUpload {
  localId: string;
  filename: string;
  stage: PoolUploadStage;
  message: string | null;
  retryable: boolean;
  intent?: string;
  context?: unknown;
}

interface SignedTarget {
  reservation_id: string;
  client_upload_id: string;
  upload_url: string;
  gcs_path: string;
  expires_at: string;
  upload_headers: Record<string, string>;
}

interface InternalUpload extends PendingPoolUpload {
  file: File;
  failedStage: FailedStage | null;
  signed: SignedTarget | null;
  contentHash: string | null | undefined;
  clientUploadId: string;
  correlationId: string;
  removed: boolean;
  reservationMayExist: boolean;
  intent: string;
  intentContext?: unknown;
  abortController: AbortController;
}

function stableId(prefix: string): string {
  try {
    return `${prefix}-${crypto.randomUUID()}`;
  } catch {
    return `${prefix}-${Date.now()}-${Math.random().toString(16).slice(2)}`;
  }
}

interface UsePoolAssetUploaderOptions {
  itemId: string;
  assetCount: number;
  maxAssets: number;
  onRegistered: (asset: PoolAsset, file: File, intent: string, context?: unknown) => void;
  onFailed?: (file: File, intent: string, context?: unknown) => void;
  onUnavailable: () => void;
  onDeduped?: () => void;
  serverReservations?: PoolReservationCapacity[];
  serverOccupiedCount?: number;
  onReservationFinalized?: (reservationId: string, releasedCapacity: boolean) => void;
}

function publicUpload(upload: InternalUpload): PendingPoolUpload {
  return {
    localId: upload.localId,
    filename: upload.filename,
    stage: upload.stage,
    message: upload.message,
    retryable: upload.retryable,
    intent: upload.intent,
    context: upload.intentContext,
  };
}

function stageMessage(stage: FailedStage): string {
  if (stage === "preparing") return "Kria couldn’t start this upload. Retry in a moment.";
  if (stage === "uploading") return "Upload interrupted. Check your connection and retry.";
  return "The file uploaded, but Kria couldn’t add it to your visuals.";
}

function isUnavailable(err: unknown): boolean {
  return (
    err instanceof FeatureDisabledError ||
    (err instanceof PlanApiError && err.status === 404 && /not available/i.test(err.message))
  );
}

function isSupportedPoolFile(file: File): boolean {
  if (file.type) {
    return POOL_ASSET_MIME_TYPES.includes(
      uploadContentTypeForFile(file) as (typeof POOL_ASSET_MIME_TYPES)[number],
    );
  }
  return /\.(jpe?g|png|webp|heic|heif|mp4|mov)$/i.test(file.name);
}

export function usePoolAssetUploader({
  itemId,
  assetCount,
  maxAssets,
  onRegistered,
  onFailed,
  onUnavailable,
  onDeduped,
  serverReservations = [],
  serverOccupiedCount = 0,
  onReservationFinalized,
}: UsePoolAssetUploaderOptions) {
  const internal = useRef(new Map<string, InternalUpload>());
  const registrationTail = useRef<Promise<void>>(Promise.resolve());
  const activeTransfers = useRef(0);
  const transferWaiters = useRef<
    Array<{ resolve: () => void; reject: (reason?: unknown) => void }>
  >([]);
  const [uploads, setUploads] = useState<PendingPoolUpload[]>([]);
  const [reservedSlots, setReservedSlots] = useState(0);
  const [batchMessage, setBatchMessage] = useState<string | null>(null);
  const [batchTotals, setBatchTotals] = useState({ total: 0, completed: 0 });

  const reservedSlotCount = useCallback(() => {
    const now = Date.now();
    const knownServerIds = new Set(
      serverReservations
        .filter((reservation) => {
          const releaseAt = reservation.release_at ? Date.parse(reservation.release_at) : null;
          return releaseAt === null || !Number.isFinite(releaseAt) || releaseAt > now;
        })
        .map((reservation) => reservation.reservation_id),
    );
    const anonymousServerHolds = Math.max(
      0,
      serverOccupiedCount - assetCount - serverReservations.length,
    );
    let count = knownServerIds.size + anonymousServerHolds;
    internal.current.forEach((upload) => {
      if (!upload.signed || !knownServerIds.has(upload.signed.reservation_id)) count += 1;
    });
    return count;
  }, [assetCount, serverOccupiedCount, serverReservations]);

  const publish = useCallback(() => {
    setReservedSlots(reservedSlotCount());
    setUploads(
      Array.from(internal.current.values())
        .filter((upload) => !upload.removed)
        .map(publicUpload),
    );
  }, [reservedSlotCount]);

  useEffect(() => {
    setReservedSlots(reservedSlotCount());
    const releases = serverReservations
      .map((reservation) =>
        reservation.release_at ? Date.parse(reservation.release_at) - Date.now() : Number.NaN,
      )
      .filter((delay) => Number.isFinite(delay) && delay > 0);
    if (releases.length === 0) return;
    const timer = window.setTimeout(
      () => setReservedSlots(reservedSlotCount()),
      Math.min(...releases),
    );
    return () => window.clearTimeout(timer);
  }, [reservedSlotCount, serverReservations]);

  useEffect(
    () => () => {
      // Navigation/unmount must stop active PUTs. The server reservation is
      // intentionally left to its bounded TTL so a late request cannot strand
      // an immortal object or make a future retry race this component.
      internal.current.forEach((upload) => upload.abortController.abort());
      internal.current.clear();
      const cancelled = new DOMException("Upload cancelled", "AbortError");
      transferWaiters.current.splice(0).forEach((waiter) => waiter.reject(cancelled));
    },
    [],
  );

  const isActive = useCallback(
    (upload: InternalUpload) =>
      !upload.removed && internal.current.get(upload.localId) === upload,
    [],
  );

  const acquireTransferSlot = useCallback(async (signal: AbortSignal) => {
    if (signal.aborted) throw new DOMException("Upload cancelled", "AbortError");
    if (activeTransfers.current < TRANSFER_CONCURRENCY) {
      activeTransfers.current += 1;
      return;
    }
    await new Promise<void>((resolve, reject) => {
      let waiter: { resolve: () => void; reject: (reason?: unknown) => void };
      const onAbort = () => {
        const index = transferWaiters.current.indexOf(waiter);
        if (index >= 0) transferWaiters.current.splice(index, 1);
        waiter.reject(new DOMException("Upload cancelled", "AbortError"));
      };
      waiter = {
        resolve: () => {
          signal.removeEventListener("abort", onAbort);
          activeTransfers.current += 1;
          resolve();
        },
        reject: (reason?: unknown) => {
          signal.removeEventListener("abort", onAbort);
          reject(reason);
        },
      };
      signal.addEventListener("abort", onAbort, { once: true });
      transferWaiters.current.push(waiter);
    });
  }, []);

  const releaseTransferSlot = useCallback(() => {
    activeTransfers.current = Math.max(0, activeTransfers.current - 1);
    transferWaiters.current.shift()?.resolve();
  }, []);

  const fail = useCallback(
    (upload: InternalUpload, stage: FailedStage, err: unknown) => {
      if (!isActive(upload)) return;
      if (isUnavailable(err)) onUnavailable();
      upload.stage = "failed";
      upload.failedStage = stage;
      const actionableClientError =
        err instanceof PlanApiError && err.status < 500 && typeof err.message === "string";
      upload.message = actionableClientError ? err.message : stageMessage(stage);
      upload.retryable =
        err instanceof FeatureDisabledError
          ? false
          : err instanceof PlanApiError
            ? err.retryable
            : true;
      upload.reservationMayExist =
        stage === "preparing" &&
        upload.retryable &&
        (!(err instanceof PlanApiError) || err.status >= 500);
      onFailed?.(upload.file, upload.intent, upload.intentContext);
      publish();
    },
    [isActive, onFailed, onUnavailable, publish],
  );

  const finish = useCallback(
    (upload: InternalUpload, asset: PoolAsset) => {
      if (!isActive(upload)) return;
      if (upload.signed) {
        onReservationFinalized?.(upload.signed.reservation_id, asset.deduped);
      }
      internal.current.delete(upload.localId);
      setBatchTotals((prev) => ({ ...prev, completed: prev.completed + 1 }));
      publish();
      if (asset.deduped) onDeduped?.();
      onRegistered(asset, upload.file, upload.intent, upload.intentContext);
    },
    [isActive, onDeduped, onRegistered, onReservationFinalized, publish],
  );

  const enqueueRegistration = useCallback(
    (upload: InternalUpload): Promise<void> => {
      const work = registrationTail.current.then(async () => {
        if (!isActive(upload)) return;
        try {
          upload.stage = "registering";
          upload.failedStage = null;
          upload.message = null;
          publish();
          if (upload.contentHash === undefined) {
            upload.contentHash = await sha256HexOfFile(upload.file);
          }
          if (!isActive(upload)) return;
          if (!upload.signed) throw new Error("Missing upload target");
          const asset = await registerPoolAsset(
            itemId,
            {
              reservation_id: upload.signed.reservation_id,
              gcs_path: upload.signed.gcs_path,
              content_type: uploadContentTypeForFile(upload.file),
              content_hash: upload.contentHash,
              source_filename: upload.file.name,
            },
            upload.correlationId,
          );
          finish(upload, asset);
        } catch (err) {
          // An expired reservation is a transfer-stage failure even though the
          // registration request discovered it. Repeating registration would
          // reuse an object the backend has already removed; restart from a
          // freshly signed target and PUT the retained File again instead.
          const retryStage =
            err instanceof PlanApiError &&
            (err.stage === "transfer" || err.code === "upload_reservation_expired")
              ? "uploading"
              : "registering";
          fail(upload, retryStage, err);
        }
      });
      registrationTail.current = work.then(
        () => undefined,
        () => undefined,
      );
      return work;
    },
    [fail, finish, isActive, itemId, publish],
  );

  const transfer = useCallback(
    async (upload: InternalUpload): Promise<void> => {
      let acquired = false;
      try {
        await acquireTransferSlot(upload.abortController.signal);
        acquired = true;
        if (!isActive(upload)) return;
        if (!upload.signed) throw new Error("Missing upload target");
        upload.stage = "uploading";
        upload.failedStage = null;
        upload.message = null;
        publish();
        await uploadToGcs(
          upload.signed.upload_url,
          upload.file,
          upload.signed.upload_headers,
          upload.correlationId,
          upload.abortController.signal,
        );
        if (!isActive(upload)) return;
        void enqueueRegistration(upload);
      } catch (err) {
        if (err instanceof DOMException && err.name === "AbortError") return;
        fail(upload, "uploading", err);
      } finally {
        if (acquired) releaseTransferSlot();
      }
    },
    [acquireTransferSlot, enqueueRegistration, fail, isActive, publish, releaseTransferSlot],
  );

  const signAndTransfer = useCallback(
    async (batch: InternalUpload[]) => {
      try {
        const targets = await requestPoolAssetUploadUrls(
          itemId,
          batch.map((upload) => ({
            filename: upload.file.name,
            content_type: uploadContentTypeForFile(upload.file),
            file_size_bytes: upload.file.size,
            client_upload_id: upload.clientUploadId,
          })),
          batch[0]?.correlationId ?? stableId("batch"),
        );
        if (targets.length !== batch.length) throw new Error("Upload target count mismatch");
        const targetsByClientId = new Map(
          targets.map((target) => [target.client_upload_id, target] as const),
        );
        if (
          targetsByClientId.size !== targets.length ||
          batch.some((upload) => !targetsByClientId.has(upload.clientUploadId))
        ) {
          throw new Error("Upload target identity mismatch");
        }
        batch.forEach((upload) => {
          if (!isActive(upload)) return;
          upload.signed = targetsByClientId.get(upload.clientUploadId) ?? null;
          upload.reservationMayExist = false;
        });
        publish();
        await Promise.all(batch.map(transfer));
      } catch (err) {
        batch.forEach((upload) => fail(upload, "preparing", err));
      }
    },
    [fail, isActive, itemId, publish, transfer],
  );

  const addFiles = useCallback(
    (
      fileList: FileList | File[] | null,
      options: {
        intent?: string;
        context?: (file: File, index: number) => unknown;
      } = {},
    ): number => {
      if (!fileList) return 0;
      const messages: string[] = [];
      const all = Array.from(fileList);
      const supported = all.filter(isSupportedPoolFile);
      if (supported.length !== all.length) {
        messages.push(
          "Some files weren’t added. Export them as JPG, PNG, WebP, HEIC/HEIF, MP4, or MOV.",
        );
      }
      const validSize = supported.filter((file) => {
        const limit = uploadContentTypeForFile(file).startsWith("video/")
          ? MAX_POOL_VIDEO_BYTES
          : MAX_POOL_IMAGE_BYTES;
        return file.size > 0 && file.size <= limit;
      });
      if (validSize.length !== supported.length) {
        messages.push("Images must be 25 MB or smaller; videos must be 512 MB or smaller.");
      }

      const remaining = Math.max(0, maxAssets - assetCount - reservedSlotCount());
      const accepted = validSize.slice(0, remaining);
      if (validSize.length > remaining) {
        messages.push(
          remaining === 0
            ? "Your visuals pool is full. Remove a visual before adding another."
            : `Your pool has room for ${remaining} more ${remaining === 1 ? "visual" : "visuals"}. Select up to ${remaining}.`,
        );
      }
      setBatchMessage(messages.length > 0 ? messages.join(" ") : null);
      if (accepted.length === 0) return 0;

      const stamp = Date.now();
      const correlationId = stableId("batch");
      const batch = accepted.map<InternalUpload>((file, index) => ({
        localId: `pending-${stamp}-${index}-${file.name}`,
        filename: file.name,
        file,
        stage: "preparing",
        failedStage: null,
        message: null,
        retryable: false,
        signed: null,
        contentHash: undefined,
        clientUploadId: stableId("file"),
        correlationId,
        removed: false,
        reservationMayExist: false,
        intent: options.intent ?? "pool",
        intentContext: options.context?.(file, index),
        abortController: new AbortController(),
      }));
      batch.forEach((upload) => internal.current.set(upload.localId, upload));
      setBatchTotals((prev) => ({ ...prev, total: prev.total + batch.length }));
      publish();
      void signAndTransfer(batch);
      return batch.length;
    },
    [assetCount, maxAssets, publish, reservedSlotCount, signAndTransfer],
  );

  const retry = useCallback(
    (localId: string) => {
      const upload = internal.current.get(localId);
      if (!upload || upload.stage !== "failed" || !upload.failedStage) return;
      const failedStage = upload.failedStage;
      upload.retryable = false;
      if (failedStage === "preparing") {
        upload.stage = "preparing";
        upload.signed = null;
        upload.reservationMayExist = true;
        publish();
        void signAndTransfer([upload]);
      } else if (failedStage === "uploading") {
        // Refresh the signed target before retrying a transfer; a stale/expired
        // GCS signature otherwise makes every retry fail identically.
        upload.stage = "preparing";
        upload.signed = null;
        upload.reservationMayExist = true;
        publish();
        void signAndTransfer([upload]);
      } else {
        upload.stage = "registering";
        upload.failedStage = null;
        upload.message = null;
        publish();
        void enqueueRegistration(upload);
      }
    },
    [enqueueRegistration, publish, signAndTransfer],
  );

  const remove = useCallback(
    (localId: string) => {
      const upload = internal.current.get(localId);
      if (!upload || upload.removed) return;
      upload.removed = true;
      upload.abortController.abort();
      // Keep only a zero-byte placeholder while the reservation hold drains;
      // removed 512 MB Files must not stay reachable for 30 minutes.
      upload.file = new File([], upload.filename, { type: "application/octet-stream" });
      // A signed reservation still counts against the backend pool limit. Keep
      // a hidden local capacity hold until its signed lifetime ends so Remove
      // cannot make the UI promise a slot the server will reject. The active
      // checks also fence any queued digest/registration work from resurrecting
      // the removed upload.
      if (upload.signed || upload.reservationMayExist) {
        const signedRelease = upload.signed
          ? Date.parse(upload.signed.expires_at) + RESERVATION_CLEANUP_GRACE_MS
          : 0;
        const delay = Math.max(
          0,
          Math.max(signedRelease, Date.now() + RESERVATION_TTL_AND_GRACE_MS) - Date.now(),
        );
        window.setTimeout(() => {
          if (internal.current.get(localId) === upload && upload.removed) {
            internal.current.delete(localId);
            publish();
          }
        }, delay);
      } else {
        internal.current.delete(localId);
      }
      setBatchTotals((prev) => ({ ...prev, total: Math.max(prev.completed, prev.total - 1) }));
      publish();
    },
    [publish],
  );

  const failedCount = uploads.filter((upload) => upload.stage === "failed").length;
  const summary = useMemo(() => {
    if (batchTotals.total === 0) return null;
    if (failedCount > 0) {
      return `${batchTotals.completed} of ${batchTotals.total} added; ${failedCount} ${failedCount === 1 ? "needs" : "need"} attention.`;
    }
    if (batchTotals.completed === batchTotals.total) {
      return `${batchTotals.completed} of ${batchTotals.total} added.`;
    }
    return null;
  }, [batchTotals, failedCount]);

  return {
    uploads,
    addFiles,
    retry,
    remove,
    batchMessage,
    summary,
    reservedSlots,
    busy: uploads.some((upload) => upload.stage !== "failed"),
  };
}
