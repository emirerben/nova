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
import {
  type OverlaySuggestion,
  type PoolAsset,
} from "@/lib/plan-api";
import type { EditorOverlaySuggestionsState } from "./useEditorOverlaySuggestions";

// Mirrors ALLOWED_ASSET_MIME_TYPES in AssetPool.tsx and
// _OVERLAY_ALLOWED_CONTENT_TYPES on the backend.
const POOL_MIME_TYPES = [
  "image/jpeg",
  "image/png",
  "image/webp",
  "image/heic",
  "video/mp4",
  "video/quicktime",
];

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

/** Local tile for an in-flight upload (before the server row exists). */
export interface PendingUpload {
  localId: string;
  filename: string;
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
};

export default function OverlaySuggestions({
  suggestions = EMPTY_SUGGESTIONS,
  assets = [],
  maxAssets = 20,
  pending = [],
  poolUnavailable = false,
  poolError = null,
  onFiles = () => {},
  onRemoveAsset = () => {},
  onAccept,
  onSeek,
}: {
  itemId?: string;
  variantId?: string;
  suggestions?: EditorOverlaySuggestionsState;
  assets?: PoolAsset[];
  maxAssets?: number;
  pending?: PendingUpload[];
  poolUnavailable?: boolean;
  poolError?: string | null;
  onFiles?: (fileList: FileList | File[] | null) => void;
  onRemoveAsset?: (asset: PoolAsset) => void;
  /** Hand the accepted envelope to EditorShell (undo-recorded overlay + sfx). */
  onAccept: (suggestion: OverlaySuggestion) => void;
  /** Seek the editor transport (rows seek to max(0, start_s − 1)). */
  onSeek: (seconds: number) => void;
}) {
  const inputRef = useRef<HTMLInputElement>(null);

  if (poolUnavailable || suggestions.unavailable) {
    return (
      <div className="mt-6 border-t border-zinc-200 pt-4" data-testid="overlay-suggestions">
        <p className="mb-2 text-[12px] font-semibold text-[#3f3f46]">AI suggestions</p>
        <p className="rounded-lg border border-dashed border-zinc-300 px-3 py-3 text-[12px] text-[#71717a]">
          {UNAVAILABLE_COPY}
        </p>
      </div>
    );
  }

  const assetById = new Map(assets.map((a) => [a.id, a]));
  const readyAssetCount = assets.filter((a) => a.status === "ready").length;
  const isEmptyPool = assets.length === 0 && pending.length === 0;
  const atCap = assets.length >= maxAssets;
  const suggestDisabled = readyAssetCount === 0 || suggestions.phase === "matching";
  const { phase, rows, wishlist } = suggestions;

  return (
    <div className="mt-6 border-t border-zinc-200 pt-4" data-testid="overlay-suggestions">
      <p className="mb-1 text-[12px] font-semibold text-[#3f3f46]">AI suggestions</p>
      <p className="mb-3 text-[12px] text-[#71717a]">
        Kria places your screenshots and clips where you talk about them.
      </p>

      <input
        ref={inputRef}
        type="file"
        multiple
        accept={POOL_MIME_TYPES.join(",")}
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
          <button
            type="button"
            onClick={() => inputRef.current?.click()}
            className="mt-2 inline-flex min-h-11 items-center rounded-lg border border-zinc-200 bg-white px-4 text-[12px] text-[#3f3f46] transition-colors hover:border-lime-400 hover:text-lime-700 focus-visible:outline focus-visible:outline-2 focus-visible:outline-lime-500"
          >
            Add visuals
          </button>
        </div>
      ) : (
        <>
          <ul className="flex flex-wrap gap-1.5" data-testid="suggestion-pool-strip">
            {assets.map((asset) => (
              <PoolThumb key={asset.id} asset={asset} onRemove={() => onRemoveAsset(asset)} />
            ))}
            {pending.map((p) => (
              <li
                key={p.localId}
                aria-label={`Uploading ${p.filename}`}
                className="h-12 w-12 overflow-hidden rounded-md border border-zinc-200 bg-[linear-gradient(110deg,#f4f4f5,45%,#e4e4e7,55%,#f4f4f5)] bg-[length:200%_100%] motion-safe:animate-shimmer"
              />
            ))}
            <li>
              <button
                type="button"
                disabled={atCap}
                onClick={() => inputRef.current?.click()}
                aria-label="Add visuals"
                className="flex h-12 w-12 items-center justify-center rounded-md border border-dashed border-zinc-300 bg-white text-[15px] text-[#71717a] transition-colors hover:border-lime-400 hover:text-lime-700 focus-visible:outline focus-visible:outline-2 focus-visible:outline-lime-500 disabled:cursor-not-allowed disabled:opacity-50"
              >
                +
              </button>
            </li>
          </ul>
          {atCap && (
            <p className="mt-1.5 text-[12px] text-[#71717a]">
              Your pool is full — remove a visual to add another.
            </p>
          )}
        </>
      )}
      {poolError && (
        <p className="mt-2 rounded border border-zinc-200 bg-white px-3 py-2 text-[12px] text-[#3f3f46]">
          {poolError}
        </p>
      )}

      {suggestions.staleNotice && (
        <p className="mt-2 rounded border border-zinc-200 bg-white px-3 py-2 text-[12px] text-[#3f3f46]">
          Your script changed — suggestions were cleared. Place visuals again?
        </p>
      )}

      {/* ── Run states ── */}
      {phase === "matching" ? (
        <div className="mt-3 flex items-center gap-2.5 rounded-lg border border-zinc-200 bg-white px-3 py-2.5">
          <span aria-hidden className="relative flex h-2 w-2 shrink-0">
            <span className="absolute inline-flex h-full w-full rounded-full bg-lime-500 opacity-75 motion-safe:animate-ping" />
            <span className="relative inline-flex h-2 w-2 rounded-full bg-lime-500" />
          </span>
          <p className="text-[12px] text-[#0c0c0e]">Matching your visuals to the script…</p>
          {suggestions.stillWorking && (
            <p className="text-[12px] text-[#71717a]">Still working…</p>
          )}
        </div>
      ) : phase === "failed" ? (
        <div className="mt-3 rounded-lg border border-dashed border-zinc-300 bg-white px-3 py-3 text-center">
          <p className="text-[12px] text-[#71717a]">Couldn&apos;t match your visuals this time.</p>
          <button
            type="button"
            onClick={suggestions.start}
            className="mt-2 inline-flex min-h-11 items-center rounded-lg border border-zinc-200 bg-white px-4 text-[12px] text-[#3f3f46] transition-colors hover:border-lime-400 hover:text-lime-700 focus-visible:outline focus-visible:outline-2 focus-visible:outline-lime-500"
          >
            Retry
          </button>
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
          <button
            type="button"
            disabled={suggestDisabled}
            onClick={suggestions.start}
            title={readyAssetCount === 0 ? "Add at least one visual first" : undefined}
            className="inline-flex min-h-11 w-full items-center justify-center gap-1.5 rounded-lg border border-zinc-200 bg-white px-4 text-[12px] text-[#71717a] transition-colors hover:border-lime-400 hover:text-lime-700 focus-visible:outline focus-visible:outline-2 focus-visible:outline-lime-500 disabled:cursor-not-allowed disabled:opacity-50 disabled:hover:border-zinc-200 disabled:hover:text-[#71717a]"
          >
            {phase === "ready" && rows.length > 0 ? "✦ Re-match visuals" : "✦ Place visuals for me"}
          </button>
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

function PoolThumb({ asset, onRemove }: { asset: PoolAsset; onRemove: () => void }) {
  const label = asset.source_filename ?? asset.subject ?? "this file";
  const busy = asset.status === "analyzing" || asset.status === "uploading" || asset.status === "uploaded";
  return (
    <li className="group relative h-12 w-12 overflow-hidden rounded-md border border-zinc-200 bg-white">
      {asset.status === "failed" ? (
        <div
          className="flex h-full w-full items-center justify-center border border-dashed border-zinc-300 text-[10px] text-[#71717a]"
          title="Couldn't read this file"
        >
          !
        </div>
      ) : busy || !asset.display_url ? (
        <div
          className="h-full w-full bg-[linear-gradient(110deg,#f4f4f5,45%,#e4e4e7,55%,#f4f4f5)] bg-[length:200%_100%] motion-safe:animate-shimmer"
          title="Analyzing…"
        />
      ) : asset.kind === "video" ? (
        <video
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
      <button
        type="button"
        onClick={onRemove}
        aria-label={`Remove ${label}`}
        className="absolute right-0.5 top-0.5 flex h-5 w-5 items-center justify-center rounded-full bg-white/90 text-[11px] text-[#3f3f46] opacity-0 transition-opacity focus-visible:opacity-100 focus-visible:outline focus-visible:outline-2 focus-visible:outline-lime-500 group-hover:opacity-100"
      >
        ×
      </button>
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
            <video
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
        <button
          type="button"
          aria-label={`Accept ${label}`}
          onClick={(e) => {
            e.stopPropagation();
            onAccept();
          }}
          className="flex h-11 w-11 items-center justify-center rounded-lg border border-zinc-200 bg-white text-sm text-[#3f3f46] transition-colors hover:border-lime-400 hover:text-lime-700 focus-visible:outline focus-visible:outline-2 focus-visible:outline-lime-500"
        >
          ✓
        </button>
        <button
          type="button"
          aria-label={`Dismiss ${label}`}
          onClick={(e) => {
            e.stopPropagation();
            onReject();
          }}
          className="flex h-11 w-11 items-center justify-center rounded-lg border border-zinc-200 bg-white text-sm text-[#71717a] transition-colors hover:border-zinc-400 hover:text-[#3f3f46] focus-visible:outline focus-visible:outline-2 focus-visible:outline-lime-500"
        >
          ×
        </button>
      </div>
    </li>
  );
}
