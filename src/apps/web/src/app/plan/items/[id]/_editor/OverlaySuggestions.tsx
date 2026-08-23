"use client";

/**
 * OverlaySuggestions — the "AI suggestions" section INSIDE the editor's
 * Overlays drawer pane (overlay auto-placement in the TikTok-parity editor).
 *
 * Top-to-bottom: header → compact pool strip (thumbnails + presigned-primary
 * upload, same flow as AssetPool.tsx) → "✦ Place visuals for me" →
 * matching/zero/failed states or suggestion rows → wishlist footer.
 *
 * Accepting hands the ENVELOPE to EditorShell (`onAccept`): the card joins the
 * working overlay list through the undo history and persists via
 * editor-commit's `accepted_suggestion_ids` — this section never calls the
 * item-page apply endpoint. Row click seeks the editor transport to start−1s.
 *
 * Rendered only when NEXT_PUBLIC_OVERLAY_AUTOPLACE_ENABLED is "true" AND the
 * variant's editor_capabilities.suggestions is true (gated in EditorShell —
 * a song/lyric variant renders no dead chrome).
 */

import { useRef } from "react";
import { StableVideo } from "@/components/StableVideo";
import {
  POOL_ASSET_MIME_TYPES,
  type PendingPoolUpload,
} from "@/app/plan/_hooks/usePoolAssetUploader";
import {
  type OverlaySuggestion,
  type PoolAsset,
} from "@/lib/plan-api";
import type { EditorOverlaySuggestionsState } from "./useEditorOverlaySuggestions";
import { Button } from "@/components/ui/button";

const UNAVAILABLE_COPY = "AI suggestions aren't available right now.";

/** m:ss for row time ranges (same format as SuggestionRail). */
function fmtTime(s: number): string {
  const total = Math.max(0, Math.floor(s));
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, "0")}`;
}

/**
 * Confidence-hedged reason copy (10A, mirroring SuggestionRail's tone):
 * confident rows lead with the transcript anchor ("You say X here —") unless
 * the server reason already quotes it; "likely" rows hedge ("This might fit —").
 */
export function hedgedReason(row: OverlaySuggestion): string {
  if (row.confidence_tier === "likely") return `This might fit — ${row.reason}`;
  const anchor = row.transcript_anchor?.trim();
  if (anchor && !row.reason.includes(anchor)) {
    return `You say “${anchor}” here — ${row.reason}`;
  }
  return row.reason;
}

const EMPTY_SUGGESTIONS: EditorOverlaySuggestionsState = {
  phase: "idle",
  rows: [],
  wishlist: [],
  staleNotice: false,
  stillWorking: false,
  unavailable: false,
  start: () => {},
  removeRow: () => {},
  clearLocal: () => {},
};

export default function OverlaySuggestions({
  suggestions = EMPTY_SUGGESTIONS,
  assets = [],
  maxAssets = 20,
  pending = [],
  reservedSlots = pending.length,
  poolUnavailable = false,
  poolError = null,
  poolMessage = null,
  poolSummary = null,
  onFiles = () => {},
  onRetryPending = () => {},
  onRemovePending = () => {},
  onRemoveAsset = () => {},
  onRetryAsset = () => {},
  onAccept,
  onSeek,
}: {
  itemId?: string;
  variantId?: string;
  suggestions?: EditorOverlaySuggestionsState;
  assets?: PoolAsset[];
  maxAssets?: number;
  pending?: PendingPoolUpload[];
  reservedSlots?: number;
  poolUnavailable?: boolean;
  poolError?: string | null;
  poolMessage?: string | null;
  poolSummary?: string | null;
  onFiles?: (fileList: FileList | File[] | null) => void;
  onRetryPending?: (localId: string) => void;
  onRemovePending?: (localId: string) => void;
  onRemoveAsset?: (asset: PoolAsset) => void;
  onRetryAsset?: (asset: PoolAsset) => void;
  /** Hand the accepted envelope to EditorShell (undo-recorded overlay + sfx). */
  onAccept: (suggestion: OverlaySuggestion) => void;
  /** Seek the editor transport (rows seek to max(0, start_s − 1)). */
  onSeek: (seconds: number) => void;
}) {
  const inputRef = useRef<HTMLInputElement>(null);

  if (poolUnavailable || suggestions.unavailable) {
    return (
      <div className="mt-6 border-t border-zinc-200 pt-4" data-testid="overlay-suggestions">
        <p className="mb-2 text-[12px] font-semibold text-[#3f3f46]">Suggested visuals</p>
        <p className="rounded-lg border border-dashed border-zinc-300 px-3 py-3 text-[12px] text-[#71717a]">
          {UNAVAILABLE_COPY}
        </p>
      </div>
    );
  }

  const assetById = new Map(assets.map((a) => [a.id, a]));
  const readyAssetCount = assets.filter((a) => a.status === "ready").length;
  const isEmptyPool = assets.length === 0 && pending.length === 0;
  const atCap = assets.length + reservedSlots >= maxAssets;
  const releasingSlots = Math.max(0, reservedSlots - pending.length);
  const suggestDisabled = readyAssetCount === 0 || suggestions.phase === "matching";
  const { phase, rows, wishlist } = suggestions;

  return (
    <div className="mt-6 border-t border-zinc-200 pt-4" data-testid="overlay-suggestions">
      <p className="mb-3 text-[12px] font-semibold text-[#3f3f46]">Suggested visuals</p>

      <input
        ref={inputRef}
        type="file"
        multiple
        accept={POOL_ASSET_MIME_TYPES.join(",")}
        className="hidden"
        aria-label="Add visuals to your pool"
        disabled={atCap}
        onChange={(e) => {
          onFiles(e.target.files);
          e.target.value = "";
        }}
      />

      {/* ── Compact pool strip ── */}
      {isEmptyPool ? (
        <div className="rounded-lg border border-dashed border-zinc-300 bg-zinc-50 px-3 py-3 text-center">
          <p className="text-[12px] text-[#3f3f46]">
            Add screenshots or clips of what you talk about
          </p>
          <Button
            type="button"
            variant="outline"
            disabled={atCap}
            onClick={() => inputRef.current?.click()}
            className="mt-2 h-auto min-h-11 border-zinc-200 bg-white px-4 text-[12px] text-[#3f3f46] transition-colors hover:border-lime-400 hover:text-lime-700 focus-visible:outline focus-visible:outline-2 focus-visible:outline-lime-500 disabled:cursor-not-allowed disabled:opacity-50"
          >
            Add visuals
          </Button>
          {atCap && (
            <p className="mt-2 text-[12px] text-[#71717a]">
              {releasingSlots > 0
                ? "Kria is releasing a removed upload slot. You can add another visual when cleanup finishes."
                : "Your pool is full — remove a visual to add another."}
            </p>
          )}
        </div>
      ) : (
        <>
          <ul className="flex flex-wrap gap-1.5" data-testid="suggestion-pool-strip">
            {assets.map((asset) => (
              <PoolThumb
                key={asset.id}
                asset={asset}
                onRemove={() => onRemoveAsset(asset)}
                onRetry={() => onRetryAsset(asset)}
              />
            ))}
            {pending.map((p) => {
              const stageLabel =
                p.stage === "failed"
                  ? "Failed"
                  : p.stage === "preparing"
                    ? "Preparing"
                    : p.stage === "registering"
                      ? "Adding"
                      : "Uploading";
              return (
                <li
                  key={p.localId}
                  aria-label={`${stageLabel} ${p.filename}`}
                  className={
                    p.stage === "failed"
                      ? "flex h-12 w-12 items-center justify-center overflow-hidden rounded-md border border-dashed border-zinc-300 bg-white text-[9px] text-[#71717a]"
                      : "flex h-12 w-12 items-end justify-center overflow-hidden rounded-md border border-zinc-200 bg-[linear-gradient(110deg,#f4f4f5,45%,#e4e4e7,55%,#f4f4f5)] bg-[length:200%_100%] pb-1 text-[9px] text-[#71717a] motion-safe:animate-shimmer"
                  }
                >
                  {stageLabel}
                </li>
              );
            })}
            <li>
              <Button
                type="button"
                variant="outline"
                size="icon"
                disabled={atCap}
                onClick={() => inputRef.current?.click()}
                aria-label="Add visuals"
                className="h-12 w-12 rounded-md border-dashed border-zinc-300 bg-white text-[15px] text-[#71717a] transition-colors hover:border-lime-400 hover:text-lime-700 focus-visible:outline focus-visible:outline-2 focus-visible:outline-lime-500 disabled:cursor-not-allowed disabled:opacity-50"
              >
                +
              </Button>
            </li>
          </ul>
          {atCap && (
            <p className="mt-1.5 text-[12px] text-[#71717a]">
              {releasingSlots > 0
                ? "Kria is releasing a removed upload slot. You can add another visual when cleanup finishes."
                : "Your pool is full — remove a visual to add another."}
            </p>
          )}
          {pending
            .filter((upload) => upload.stage === "failed")
            .map((upload) => (
              <div
                key={`${upload.localId}-error`}
                className="mt-2 rounded border border-dashed border-zinc-300 bg-white px-3 py-2 text-[12px] text-[#71717a]"
              >
                <p className="font-medium text-[#3f3f46]">{upload.filename}</p>
                <p>We couldn&apos;t add this visual. Try again.</p>
                <div className="mt-1 flex gap-3">
                  {upload.retryable && (
                    <Button
                      type="button"
                      variant="link"
                      onClick={() => onRetryPending(upload.localId)}
                      className="h-auto min-h-7 p-0 text-lime-700 underline underline-offset-2 focus-visible:outline focus-visible:outline-2 focus-visible:outline-lime-500"
                    >
                      Retry
                    </Button>
                  )}
                  <Button
                    type="button"
                    variant="link"
                    onClick={() => onRemovePending(upload.localId)}
                    className="h-auto min-h-7 p-0 text-[#71717a] underline underline-offset-2 focus-visible:outline focus-visible:outline-2 focus-visible:outline-lime-500"
                  >
                    Remove
                  </Button>
                </div>
              </div>
            ))}
          {assets
            .filter((asset) => asset.status === "failed")
            .map((asset) => (
              <div
                key={`${asset.id}-analysis-error`}
                className="mt-2 rounded border border-dashed border-zinc-300 bg-white px-3 py-2 text-[12px] text-[#71717a]"
              >
                <p className="font-medium text-[#3f3f46]">
                  {asset.source_filename ?? "This visual"}
                </p>
                <p>
                  This visual couldn&apos;t be read. Try exporting it again, then retry.
                </p>
                <div className="mt-1 flex gap-3">
                  {asset.retryable !== false && (
                    <Button
                      type="button"
                      variant="link"
                      onClick={() => onRetryAsset(asset)}
                      className="h-auto min-h-7 p-0 text-lime-700 underline underline-offset-2 focus-visible:outline focus-visible:outline-2 focus-visible:outline-lime-500"
                    >
                      Retry analysis
                    </Button>
                  )}
                  <Button
                    type="button"
                    variant="link"
                    onClick={() => onRemoveAsset(asset)}
                    className="h-auto min-h-7 p-0 text-[#71717a] underline underline-offset-2 focus-visible:outline focus-visible:outline-2 focus-visible:outline-lime-500"
                  >
                    Remove
                  </Button>
                </div>
              </div>
            ))}
        </>
      )}
      {poolError && (
        <p className="mt-2 rounded border border-zinc-200 bg-white px-3 py-2 text-[12px] text-[#3f3f46]">
          {poolError}
        </p>
      )}
      {poolMessage && (
        <p className="mt-2 rounded border border-zinc-200 bg-white px-3 py-2 text-[12px] text-[#3f3f46]">
          {poolMessage}
        </p>
      )}
      {poolSummary && (
        <p className="mt-2 text-[12px] text-[#71717a]" aria-live="polite">
          {poolSummary}
        </p>
      )}

      {suggestions.staleNotice && (
        <p className="mt-2 rounded border border-zinc-200 bg-white px-3 py-2 text-[12px] text-[#3f3f46]">
          Your script changed, so suggestions were cleared. Match visuals again when you&apos;re ready.
        </p>
      )}

      {/* ── Run states ── */}
      {phase === "matching" ? (
        <div className="mt-3 flex items-center gap-2.5 rounded-lg border border-zinc-200 bg-white px-3 py-2.5">
          <span aria-hidden className="relative flex h-2 w-2 shrink-0">
            <span className="absolute inline-flex h-full w-full rounded-full bg-lime-500 opacity-75 motion-safe:animate-ping" />
            <span className="relative inline-flex h-2 w-2 rounded-full bg-lime-500" />
          </span>
          <p className="text-[12px] text-[#0c0c0e]">Matching visuals to your script…</p>
          {suggestions.stillWorking && (
            <p className="text-[12px] text-[#71717a]">Still working…</p>
          )}
        </div>
      ) : phase === "failed" ? (
        <div className="mt-3 rounded-lg border border-dashed border-zinc-300 bg-white px-3 py-3 text-center">
          <p className="text-[12px] text-[#71717a]">We couldn&apos;t match visuals to your script this time.</p>
          <Button
            type="button"
            variant="outline"
            onClick={suggestions.start}
            className="mt-2 h-auto min-h-11 border-zinc-200 bg-white px-4 text-[12px] text-[#3f3f46] transition-colors hover:border-lime-400 hover:text-lime-700 focus-visible:outline focus-visible:outline-2 focus-visible:outline-lime-500"
          >
            Retry
          </Button>
        </div>
      ) : phase === "zero" ? (
        <div className="mt-3 rounded-lg border border-zinc-200 bg-white px-3 py-2.5">
          <p className="text-[12px] text-[#3f3f46]">
            No confident matches — try adding more specific visuals.
          </p>
          {wishlist.map((line) => (
            <p key={line} className="mt-1 text-[11px] text-[#71717a]">
              {line}
            </p>
          ))}
        </div>
      ) : phase === "ready" && rows.length > 0 ? (
        <ul className="mt-3">
          {rows.map((row) => (
            <SuggestionRow
              key={row.id}
              row={row}
              asset={assetById.get(row.asset_id) ?? null}
              onSeek={() => onSeek(Math.max(0, row.overlay.start_s - 1))}
              onAccept={() => {
                onAccept(row);
                suggestions.removeRow(row.id, { accepted: true });
              }}
              onReject={() => suggestions.removeRow(row.id)}
            />
          ))}
        </ul>
      ) : null}

      {/* ── Entry / re-match button (idle, ready with rows resolved, zero) ── */}
      {phase !== "matching" && phase !== "failed" && (
        <div className="mt-3">
          <Button
            type="button"
            variant="outline"
            disabled={suggestDisabled}
            onClick={suggestions.start}
            title={readyAssetCount === 0 ? "Add at least one visual first" : undefined}
            className="h-auto min-h-11 w-full gap-1.5 border-zinc-200 bg-white px-4 text-[12px] text-[#71717a] transition-colors hover:border-lime-400 hover:text-lime-700 focus-visible:outline focus-visible:outline-2 focus-visible:outline-lime-500 disabled:cursor-not-allowed disabled:opacity-50 disabled:hover:border-zinc-200 disabled:hover:text-[#71717a]"
          >
            {phase === "ready" && rows.length > 0 ? "Match visuals again" : "Place visuals automatically"}
          </Button>
          {readyAssetCount === 0 && (
            <p className="mt-1.5 text-[12px] text-[#71717a]">Add at least one visual first</p>
          )}
        </div>
      )}

      {/* Wishlist footer — unmatched moments, muted, verbatim. */}
      {phase === "ready" && wishlist.length > 0 && (
        <div className="mt-2">
          {wishlist.map((line) => (
            <p key={line} className="text-[11px] text-[#71717a]">
              {line}
            </p>
          ))}
        </div>
      )}
    </div>
  );
}

function PoolThumb({
  asset,
  onRemove,
  onRetry,
}: {
  asset: PoolAsset;
  onRemove: () => void;
  onRetry: () => void;
}) {
  const label = asset.source_filename ?? asset.subject ?? "this file";
  const busy =
    asset.status === "queued" || asset.status === "analyzing" || asset.status === "uploaded";
  return (
    <li className="group relative h-12 w-12 overflow-hidden rounded-md border border-zinc-200 bg-white">
      {asset.status === "failed" ? (
        <div
          className="flex h-full w-full items-center justify-center border border-dashed border-zinc-300 text-[10px] text-[#71717a]"
          title="This visual couldn't be analyzed. Try again."
        >
          !
        </div>
      ) : busy || !asset.display_url ? (
        <div
          className="flex h-full w-full items-center justify-center bg-[linear-gradient(110deg,#f4f4f5,45%,#e4e4e7,55%,#f4f4f5)] bg-[length:200%_100%] text-[9px] text-[#71717a] motion-safe:animate-shimmer"
          title={asset.status === "queued" ? "Queued…" : "Analyzing…"}
        >
          {asset.status === "queued" ? "Queued" : "Analyzing"}
        </div>
      ) : asset.kind === "video" ? (
        <StableVideo
          src={asset.display_url}
          muted
          playsInline
          preload="metadata"
          className="h-full w-full object-cover"
        />
      ) : (
        // eslint-disable-next-line @next/next/no-img-element -- signed GCS thumbnail
        <img src={asset.display_url} alt={label} className="h-full w-full object-cover" />
      )}
      <Button
        type="button"
        variant="ghost"
        size="icon"
        onClick={onRemove}
        aria-label={`Remove ${label}`}
        className="absolute right-0.5 top-0.5 h-5 w-5 rounded-full bg-white/90 text-[11px] text-[#3f3f46] opacity-0 transition-opacity hover:bg-white/90 focus-visible:opacity-100 focus-visible:outline focus-visible:outline-2 focus-visible:outline-lime-500 group-hover:opacity-100"
      >
        ×
      </Button>
      {asset.status === "failed" && asset.retryable !== false && (
        <Button
          type="button"
          variant="ghost"
          size="icon"
          onClick={onRetry}
          aria-label={`Retry analysis for ${label}`}
          className="absolute bottom-0.5 left-0.5 h-5 w-5 rounded-full bg-white/90 text-[11px] text-lime-700 hover:bg-white/90 focus-visible:outline focus-visible:outline-2 focus-visible:outline-lime-500"
        >
          ↻
        </Button>
      )}
    </li>
  );
}

function SuggestionRow({
  row,
  asset,
  onSeek,
  onAccept,
  onReject,
}: {
  row: OverlaySuggestion;
  asset: PoolAsset | null;
  onSeek: () => void;
  onAccept: () => void;
  onReject: () => void;
}) {
  const label = asset?.source_filename ?? asset?.subject ?? row.overlay.kind;
  const thumbUrl = row.overlay.preview_url ?? asset?.display_url ?? null;
  return (
    <li
      role="button"
      tabIndex={0}
      onClick={onSeek}
      onKeyDown={(e) => {
        if (e.target !== e.currentTarget) return;
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onSeek();
        }
      }}
      aria-label={`Preview suggestion: ${label}, ${fmtTime(row.overlay.start_s)} to ${fmtTime(row.overlay.end_s)}`}
      className="flex cursor-pointer items-start gap-2 border-t border-zinc-100 py-2.5 first:border-t-0 focus-visible:outline focus-visible:outline-2 focus-visible:outline-lime-500"
    >
      <div className="h-8 w-11 shrink-0 overflow-hidden rounded-md border border-zinc-200 bg-zinc-900">
        {thumbUrl ? (
          row.overlay.kind === "video" ? (
            <StableVideo
              src={thumbUrl}
              muted
              playsInline
              preload="metadata"
              className="h-full w-full object-cover"
            />
          ) : (
            // eslint-disable-next-line @next/next/no-img-element -- signed GCS thumbnail
            <img src={thumbUrl} alt="" className="h-full w-full object-cover" />
          )
        ) : null}
      </div>

      <div className="min-w-0 flex-1">
        <p className="truncate text-[12px] font-semibold text-[#0c0c0e]">
          <span aria-hidden>✦ </span>
          {label}{" "}
          <span className="font-normal text-[#71717a]">
            {fmtTime(row.overlay.start_s)}–{fmtTime(row.overlay.end_s)}
          </span>
        </p>
        <p className="mt-0.5 text-[11px] leading-snug text-[#71717a]">{hedgedReason(row)}</p>
        {row.sfx && (
          <p className="mt-0.5 text-[11px] text-lime-700">
            + {row.sfx.label ?? "pop"} sound
          </p>
        )}
      </div>

      <div className="flex shrink-0 gap-1">
        <Button
          type="button"
          variant="outline"
          size="icon"
          aria-label={`Use ${label}`}
          onClick={(e) => {
            e.stopPropagation();
            onAccept();
          }}
          className="h-11 w-11 rounded-lg border-zinc-200 bg-white text-sm text-[#3f3f46] transition-colors hover:border-lime-400 hover:text-lime-700 focus-visible:outline focus-visible:outline-2 focus-visible:outline-lime-500"
        >
          ✓
        </Button>
        <Button
          type="button"
          variant="outline"
          size="icon"
          aria-label={`Skip ${label}`}
          onClick={(e) => {
            e.stopPropagation();
            onReject();
          }}
          className="h-11 w-11 rounded-lg border-zinc-200 bg-white text-sm text-[#71717a] transition-colors hover:border-zinc-400 hover:text-[#3f3f46] focus-visible:outline focus-visible:outline-2 focus-visible:outline-lime-500"
        >
          ×
        </Button>
      </div>
    </li>
  );
}
