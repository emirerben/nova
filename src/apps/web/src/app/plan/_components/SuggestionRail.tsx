"use client";

/**
 * SuggestionRail — AI overlay auto-placement review (plans/005 PR2).
 *
 * Checklist-rail flow (approved wireframe variant C, 2026-07-02): the creator
 * clicks "✦ Place visuals for me", the matcher runs (Pulse state, polled every
 * 2.5s), and suggestions arrive as a light editorial card — one row per
 * placement with a transcript-grounded reason. Rows are an INDEX into the
 * video: clicking one seeks the page's variant preview to start−1s and plays
 * (reveal, decision 1A; `prefers-reduced-motion` seeks without playing).
 * Per-row ✓ stages / × rejects; one footer CTA "Apply N to video" dispatches
 * once (decision 4A) with ONLY the kept suggestions, carrying any user edits
 * (e.g. the "+ pop sound ×" control strips just that row's sfx).
 *
 * Suggestion preview approach: a 9:16 mini-preview INSIDE the rail card
 * (muted video of the selected variant with the kept suggestions stacked as
 * dashed-lime ✦ CSS cards — same x_frac/y_frac/scale math as the Hero CSS
 * preview). Chosen over overlaying the page's hero video because the hero
 * lives in FocusedResults with its own preview layers; reaching into it from
 * here would be invasive. The reveal still drives the page video via a DOM
 * query scoped to `[data-variant-preview="<variantId>"]`.
 *
 * 006 T3 (005-4A lane rendering): the working rows + kept ids can be LIFTED to
 * the page via the optional `rows`/`onRowsChange`/`keptIds`/`onKeptIdsChange`
 * props (see useOverlaySuggestionState) so the timeline lanes render the same
 * envelopes as editable provenance cards. Lane edits patch the envelopes and
 * implicitly stage the row; Apply POSTs the CURRENT (edited) envelopes. The
 * mini-preview is strictly read-only — video cards seek to clip_trim_start_s
 * for their poster frame so the creator previews the ACTUAL segment.
 *
 * Flag-gated end to end, same as AssetPool:
 *   frontend: NEXT_PUBLIC_OVERLAY_AUTOPLACE_ENABLED === "true" → renders
 *   backend:  OVERLAY_AUTOPLACE_ENABLED                        → routes 404 when off
 */

import {
  type Dispatch,
  type SetStateAction,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";
import {
  applyOverlaySuggestions,
  dismissOverlaySuggestions,
  getOverlaySuggestions,
  listPoolAssets,
  suggestVariantOverlays,
  type MediaOverlay,
  type OverlayApplyReceipt,
  type OverlaySuggestion,
  type OverlaySuggestionsResponse,
  type PoolAsset,
} from "@/lib/plan-api";
import { mediaClassFor } from "./cardMedia";
import { demotePatch } from "./OverlayCardPopover";
import { overlayCardStyle } from "./overlayCardStyle";

const POLL_INTERVAL_MS = 2500;
/** After 60s of matching, add "Still working…" (keep polling — D19, no tier upgrade). */
const STILL_WORKING_TICKS = Math.ceil(60_000 / POLL_INTERVAL_MS);
const STALE_NOTICE_MS = 8000;

type Phase = "idle" | "matching" | "ready" | "zero" | "failed" | "applied";

/** Backend flag off → routes 404 (dual-flag trap, same detection as AssetPool). */
function isUnavailableError(err: unknown): boolean {
  return (
    err instanceof Error && (/not available/i.test(err.message) || err.message.includes("(404)"))
  );
}

function prefersReducedMotion(): boolean {
  return (
    typeof window !== "undefined" &&
    typeof window.matchMedia === "function" &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches
  );
}

/** m:ss for row time ranges. */
function fmtTime(s: number): string {
  const total = Math.max(0, Math.floor(s));
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, "0")}`;
}

/** Seconds rounded to one decimal, trailing .0 dropped ("2.8", "5.5", "3"). */
function fmtDur(s: number): string {
  return `${Math.round(s * 10) / 10}`;
}

/** True when a suggestion's embedded overlay is a full-screen cutaway. */
function isFullscreenRow(row: OverlaySuggestion): boolean {
  return (row.overlay.display_mode ?? "pip") === "fullscreen";
}

/**
 * Plan 009 ARCH-4 ("never silent"): quiet zinc receipt lines for the variant's
 * `overlay_apply_receipt`. Demotions with reason "hook"/"intro" name the why;
 * both a demoted and a dropped line can appear; null/empty receipt → no lines.
 * Exported for unit tests.
 */
export function receiptLines(receipt: OverlayApplyReceipt | null | undefined): string[] {
  if (!receipt) return [];
  const lines: string[] = [];
  const demoted = receipt.demoted ?? 0;
  const dropped = receipt.dropped ?? 0;
  if (demoted > 0) {
    const noun = demoted === 1 ? "visual" : "visuals";
    lines.push(
      receipt.reason === "hook" || receipt.reason === "intro"
        ? `${demoted} ${noun} shown smaller to protect your intro`
        : `${demoted} ${noun} shown smaller`,
    );
  }
  if (dropped > 0) {
    lines.push(
      `${dropped} visual${dropped === 1 ? "" : "s"} couldn't fit and ${
        dropped === 1 ? "was" : "were"
      } skipped`,
    );
  }
  return lines;
}

function safePlay(video: HTMLVideoElement) {
  try {
    const p = video.play();
    if (p && typeof p.catch === "function") p.catch(() => {});
  } catch {
    // jsdom / autoplay-blocked — seeking already happened, that's enough.
  }
}

export default function SuggestionRail({
  itemId,
  variantId,
  previewUrl,
  onApplied,
  rows: rowsProp,
  onRowsChange,
  keptIds: keptIdsProp,
  onKeptIdsChange,
  onSuggestionEdit,
  applyReceipt,
}: {
  itemId: string;
  /** Currently focused variant — null before any variant renders. */
  variantId: string | null;
  /** Playback URL of the selected variant, for the in-card mini-preview. */
  previewUrl?: string | null;
  /** Called after a successful apply so the page can mark the variant rendering + refetch. */
  onApplied?: () => void;
  /**
   * 006 T3 controlled mode: the page lifts the working rows + kept ids
   * (useOverlaySuggestionState) so the timeline lanes can render/edit the same
   * envelopes. Omit all four to keep the rail self-contained (legacy behavior).
   */
  rows?: OverlaySuggestion[];
  onRowsChange?: Dispatch<SetStateAction<OverlaySuggestion[]>>;
  keptIds?: Set<string>;
  onKeptIdsChange?: Dispatch<SetStateAction<Set<string>>>;
  /**
   * 009 T5: the page's lifted suggestion-edit path (useOverlaySuggestionState)
   * — the rail's one-tap "Show as small card instead" demote routes through it
   * so the patch + implicit staging behave exactly like a lane edit. Falls
   * back to the internal rows state in self-contained mode.
   */
  onSuggestionEdit?: (suggestionId: string, patch: Partial<MediaOverlay>) => void;
  /**
   * 009 T5 (ARCH-4, never silent): the focused variant's apply-time guardrail
   * receipt — rendered as quiet zinc line(s); disappears when null.
   */
  applyReceipt?: OverlayApplyReceipt | null;
}) {
  const enabled = process.env.NEXT_PUBLIC_OVERLAY_AUTOPLACE_ENABLED === "true";

  const [phase, setPhase] = useState<Phase>("idle");
  const phaseRef = useRef<Phase>("idle");
  phaseRef.current = phase;

  /** Working copy of the pending suggestions — × removes, sfx-strip edits in place.
   *  Controlled by the page when `rows`/`onRowsChange` are provided (006 T3). */
  const [internalRows, setInternalRows] = useState<OverlaySuggestion[]>([]);
  const rows = rowsProp ?? internalRows;
  const setRows = onRowsChange ?? setInternalRows;
  /** Rows the user explicitly ✓-staged (solid styling; all remaining rows apply). */
  const [internalKeptIds, setInternalKeptIds] = useState<Set<string>>(new Set());
  const confirmedIds = keptIdsProp ?? internalKeptIds;
  const setConfirmedIds = onKeptIdsChange ?? setInternalKeptIds;
  const [wishlist, setWishlist] = useState<string[]>([]);
  const [assets, setAssets] = useState<PoolAsset[]>([]);
  const [staleNotice, setStaleNotice] = useState(false);
  const [announcement, setAnnouncement] = useState("");
  const [actionError, setActionError] = useState<string | null>(null);
  const [appliedCount, setAppliedCount] = useState(0);
  const [applying, setApplying] = useState(false);
  const [pollTicks, setPollTicks] = useState(0);
  const [unavailable, setUnavailable] = useState(false);
  const [miniTime, setMiniTime] = useState(0);

  const miniVideoRef = useRef<HTMLVideoElement>(null);
  const staleTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const showStaleNotice = useCallback(() => {
    setStaleNotice(true);
    if (staleTimer.current) clearTimeout(staleTimer.current);
    staleTimer.current = setTimeout(() => setStaleNotice(false), STALE_NOTICE_MS);
  }, []);

  useEffect(
    () => () => {
      if (staleTimer.current) clearTimeout(staleTimer.current);
    },
    [],
  );

  /** Seek the page's variant preview (DOM query, pragmatic per plans/005 PR2)
   *  and the in-card mini-preview to start−1s; play unless reduced-motion. */
  const reveal = useCallback(
    (suggestion: OverlaySuggestion) => {
      const t = Math.max(0, suggestion.overlay.start_s - 1);
      const reduced = prefersReducedMotion();
      const pageVideo = variantId
        ? document.querySelector<HTMLVideoElement>(`[data-variant-preview="${variantId}"] video`)
        : null;
      if (pageVideo) {
        pageVideo.currentTime = t;
        if (!reduced) safePlay(pageVideo);
      }
      const mini = miniVideoRef.current;
      if (mini) {
        mini.currentTime = t;
        if (!reduced) safePlay(mini);
      }
      // Make the row's card visible in the CSS stack immediately (timeupdate
      // takes over once the mini video actually plays).
      setMiniTime(suggestion.overlay.start_s);
    },
    [variantId],
  );

  /** Fold a GET response into local state. Only (re)seeds rows when entering
   *  ready from a non-ready phase so user edits are never clobbered. */
  const applyResponse = useCallback(
    (res: OverlaySuggestionsResponse) => {
      if (res.stale_cleared) showStaleNotice();
      const prev = phaseRef.current;
      switch (res.status) {
        case "matching":
          setPhase("matching");
          break;
        case "ready":
          if (prev !== "ready") {
            setRows(res.suggestions);
            setConfirmedIds(new Set());
            setWishlist(res.wishlist);
            setPhase("ready");
            const first = res.suggestions[0];
            if (first) setMiniTime(first.overlay.start_s);
            if (prev === "matching") {
              // Suggestions just arrived — announce + auto-reveal (1A). Skip the
              // reveal if the user is already interacting with the video.
              setAnnouncement(
                `${res.suggestions.length} suggestion${res.suggestions.length === 1 ? "" : "s"} ready`,
              );
              if (first) {
                const pageVideo = variantId
                  ? document.querySelector<HTMLVideoElement>(
                      `[data-variant-preview="${variantId}"] video`,
                    )
                  : null;
                if (!pageVideo || pageVideo.paused) reveal(first);
              }
            }
          }
          break;
        case "zero":
          setWishlist(res.wishlist);
          setPhase("zero");
          break;
        case "failed":
          setPhase("failed");
          break;
        default:
          // status null — nothing pending. Keep the applied receipt if showing.
          if (prev !== "applied") setPhase("idle");
      }
    },
    [reveal, showStaleNotice, variantId, setRows, setConfirmedIds],
  );

  const refresh = useCallback(async () => {
    if (!variantId) return;
    try {
      applyResponse(await getOverlaySuggestions(itemId, variantId));
    } catch (err) {
      if (isUnavailableError(err)) setUnavailable(true);
      // Transient poll errors: keep the current phase; the next tick retries.
    }
  }, [itemId, variantId, applyResponse]);

  // Initial load + reset on variant switch.
  useEffect(() => {
    if (!enabled || !variantId) return;
    setPhase("idle");
    setRows([]);
    setConfirmedIds(new Set());
    setWishlist([]);
    setActionError(null);
    setPollTicks(0);
    void refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled, itemId, variantId]);

  // Pool assets — thumbnails per asset_id + the "≥1 ready asset" button gate.
  useEffect(() => {
    if (!enabled || !variantId) return;
    let cancelled = false;
    listPoolAssets(itemId)
      .then((res) => {
        if (!cancelled) setAssets(res.assets);
      })
      .catch((err) => {
        if (!cancelled && isUnavailableError(err)) setUnavailable(true);
      });
    return () => {
      cancelled = true;
    };
  }, [enabled, itemId, variantId, phase === "matching"]); // eslint-disable-line react-hooks/exhaustive-deps

  // Poll while matching; "Still working…" past 60s. R4 (C12): NO client-side
  // give-up. The client never fabricates a "failed" phase from a timeout — a
  // match that completes server-side at minute 6 would otherwise stay invisible
  // until the user reloaded or hit Retry (wasting a fresh LLM run). The failed
  // tile shows ONLY when GET /suggestions actually returns status "failed" (or
  // "zero"); until then we keep polling and let the "Still working…" leave-note
  // carry the long wait (D19).
  useEffect(() => {
    if (phase !== "matching") return;
    const id = setInterval(() => {
      setPollTicks((t) => t + 1);
      void refresh();
    }, POLL_INTERVAL_MS);
    return () => clearInterval(id);
  }, [phase, refresh]);

  const handleSuggest = useCallback(async () => {
    if (!variantId) return;
    setActionError(null);
    setStaleNotice(false);
    setAnnouncement("");
    setPollTicks(0);
    setPhase("matching");
    try {
      await suggestVariantOverlays(itemId, variantId);
    } catch (err) {
      if (isUnavailableError(err)) setUnavailable(true);
      setPhase("failed");
    }
  }, [itemId, variantId]);

  const handleApply = useCallback(async () => {
    if (!variantId || rows.length === 0) return;
    setActionError(null);
    setApplying(true);
    try {
      await applyOverlaySuggestions(itemId, variantId, rows);
      setAppliedCount(rows.length);
      setRows([]);
      setConfirmedIds(new Set());
      setPhase("applied");
      onApplied?.();
    } catch (err) {
      setActionError(
        err instanceof Error ? err.message : "Couldn't apply your visuals. Try again.",
      );
    } finally {
      setApplying(false);
    }
  }, [itemId, variantId, rows, onApplied, setRows, setConfirmedIds]);

  const handleDismiss = useCallback(async () => {
    if (!variantId) return;
    setRows([]);
    setConfirmedIds(new Set());
    setPhase("idle");
    try {
      await dismissOverlaySuggestions(itemId, variantId);
    } catch {
      // Dismiss is best-effort — the local rail already cleared.
    }
  }, [itemId, variantId, setRows, setConfirmedIds]);

  const keepRow = useCallback(
    (id: string) => {
      setConfirmedIds((prev) => new Set(prev).add(id));
    },
    [setConfirmedIds],
  );

  const rejectRow = useCallback(
    (id: string) => {
      setRows((prev) => prev.filter((r) => r.id !== id));
      setConfirmedIds((prev) => {
        const next = new Set(prev);
        next.delete(id);
        return next;
      });
    },
    [setRows, setConfirmedIds],
  );

  /** The sfx × strips ONLY the sound from that suggestion (overlay stays). */
  const stripSfx = useCallback(
    (id: string) => {
      setRows((prev) => prev.map((r) => (r.id === id ? { ...r, sfx: null } : r)));
    },
    [setRows],
  );

  /**
   * 009 T5 one-tap demote: convert a fullscreen suggestion to pip, reusing the
   * popover's demotePatch so both surfaces demote identically (fracs kept when
   * present; born-fullscreen falls back to the center preset). Routes through
   * the page's onSuggestionEdit when lifted; otherwise mirrors its semantics
   * locally (patch + implicit stage).
   */
  const demoteRow = useCallback(
    (row: OverlaySuggestion) => {
      const patch = demotePatch(row.overlay);
      if (onSuggestionEdit) {
        onSuggestionEdit(row.id, patch);
        return;
      }
      setRows((prev) =>
        prev.map((r) => (r.id === row.id ? { ...r, overlay: { ...r.overlay, ...patch } } : r)),
      );
      setConfirmedIds((prev) => {
        if (prev.has(row.id)) return prev;
        const next = new Set(prev);
        next.add(row.id);
        return next;
      });
    },
    [onSuggestionEdit, setRows, setConfirmedIds],
  );

  if (!enabled || !variantId || unavailable) return null;

  const assetById = new Map(assets.map((a) => [a.id, a]));
  const readyAssetCount = assets.filter((a) => a.status === "ready").length;
  const suggestDisabled = readyAssetCount === 0;
  const keptCount = rows.length;
  const stillWorking = pollTicks >= STILL_WORKING_TICKS;
  // 009 T5: fullscreen set-level summary + honest per-row copy.
  const fullscreenRows = rows.filter(isFullscreenRow);
  const fullscreenTotalS = fullscreenRows.reduce(
    (acc, r) => acc + Math.max(0, r.overlay.end_s - r.overlay.start_s),
    0,
  );
  const applyReceiptLines = receiptLines(applyReceipt);

  return (
    <div className="my-6">
      {/* Live region — suggestions arriving are announced politely (8A). */}
      <p role="status" aria-live="polite" className="sr-only">
        {announcement}
      </p>

      {/* 009 T5 ARCH-4 receipt — quiet zinc, never silent, gone when null. */}
      {applyReceiptLines.length > 0 && (
        <div className="mb-2" data-testid="overlay-apply-receipt">
          {applyReceiptLines.map((line) => (
            <p key={line} className="text-[12px] text-[#71717a]">
              {line}
            </p>
          ))}
        </div>
      )}

      {staleNotice && (
        <p className="mb-2 rounded border border-zinc-200 bg-white px-3 py-2 text-[12px] text-[#3f3f46]">
          Your script changed — suggestions were cleared. Place visuals again?
        </p>
      )}

      {phase === "matching" ? (
        /* §7 Pulse — lime ping dot + serif line, no fake progress bar. */
        <div className="flex items-center gap-2.5 rounded-2xl border border-zinc-200 bg-white p-4">
          <span aria-hidden className="relative flex h-2 w-2 shrink-0">
            <span className="absolute inline-flex h-full w-full rounded-full bg-lime-500 opacity-75 motion-safe:animate-ping" />
            <span className="relative inline-flex h-2 w-2 rounded-full bg-lime-500" />
          </span>
          <p className="font-display text-[15px] font-medium text-[#0c0c0e]">
            Matching your visuals to the script…
          </p>
          {stillWorking && <p className="text-[12px] text-[#71717a]">Still working…</p>}
        </div>
      ) : phase === "failed" ? (
        /* D10 failure tone — dashed zinc, single Retry, never red. */
        <div className="rounded-xl border border-dashed border-zinc-200 bg-white px-4 py-4 text-center">
          <p className="text-sm text-[#71717a]">Couldn&apos;t match your visuals this time.</p>
          <button
            type="button"
            onClick={handleSuggest}
            className="mt-2 inline-flex min-h-[44px] items-center rounded-lg border border-zinc-200 bg-white px-4 py-2 text-sm text-[#3f3f46] transition-colors hover:border-lime-400 hover:text-lime-700 focus-visible:outline focus-visible:outline-2 focus-visible:outline-lime-500"
          >
            Retry
          </button>
        </div>
      ) : phase === "zero" ? (
        /* Zero match — §2 zinc notice + wishlist lines verbatim. */
        <div className="rounded-xl border border-zinc-200 bg-white px-4 py-3">
          <p className="text-sm text-[#3f3f46]">No matching visuals yet</p>
          {wishlist.map((line) => (
            <p key={line} className="mt-1 text-[12px] text-[#71717a]">
              {line}
            </p>
          ))}
          <SuggestButton
            onClick={handleSuggest}
            disabled={suggestDisabled}
            label="✦ Place visuals for me"
            className="mt-3"
          />
          {suggestDisabled && <InlineReason />}
        </div>
      ) : phase === "ready" && rows.length > 0 ? (
        /* The rail card — checklist of suggestions (wireframe variant C). */
        <div className="rounded-2xl border border-zinc-200 bg-white p-4">
          <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-lime-700">
            Suggested edit
          </p>
          <h3 className="font-display mt-1 text-[17px] font-medium text-[#0c0c0e]">
            {rows.length} visual{rows.length === 1 ? "" : "s"}, matched to your script
          </h3>

          {/* 9:16 mini-preview of the selected variant with the kept-suggestion
              stack as dashed-lime ✦ cards (pragmatic in-card preview). */}
          {previewUrl && (
            <div className="relative mx-auto my-3 aspect-[9/16] w-32 overflow-hidden rounded-lg border border-zinc-200 bg-zinc-100">
              <video
                ref={miniVideoRef}
                src={previewUrl}
                muted
                playsInline
                preload="metadata"
                className="h-full w-full object-contain"
                onTimeUpdate={(e) => setMiniTime(e.currentTarget.currentTime)}
              />
              {rows
                .filter((r) => miniTime >= r.overlay.start_s && miniTime <= r.overlay.end_s)
                .map((r) => {
                  const asset = assetById.get(r.asset_id);
                  return (
                    <div
                      key={r.id}
                      className="pointer-events-none rounded border-[1.5px] border-dashed border-lime-600 bg-lime-50/40"
                      style={overlayCardStyle(r.overlay)}
                    >
                      <span
                        aria-hidden
                        className="absolute -right-1 -top-1 z-10 flex h-3.5 w-3.5 items-center justify-center rounded-full bg-lime-600 text-[8px] text-white"
                      >
                        ✦
                      </span>
                      {asset?.display_url ? (
                        asset.kind === "video" ? (
                          <video
                            src={asset.display_url}
                            muted
                            playsInline
                            preload="metadata"
                            className={mediaClassFor(r.overlay.display_mode)}
                            data-testid={`mini-preview-video-${r.id}`}
                            // Read-only poster: seek to the trimmed-in point so
                            // the creator previews the ACTUAL segment (006 T3).
                            onLoadedMetadata={(e) => {
                              e.currentTarget.currentTime =
                                r.overlay.clip_trim_start_s ?? 0;
                            }}
                          />
                        ) : (
                          // eslint-disable-next-line @next/next/no-img-element -- signed GCS thumbnail
                          <img
                            src={asset.display_url}
                            alt=""
                            className={mediaClassFor(r.overlay.display_mode)}
                          />
                        )
                      ) : (
                        <div className="aspect-video w-full rounded bg-zinc-800" />
                      )}
                    </div>
                  );
                })}
            </div>
          )}

          {/* 009 T5 set-level summary — honest about what a takeover does. */}
          {fullscreenRows.length > 0 && (
            <p
              data-testid="fullscreen-set-summary"
              className="mb-1 text-[12px] text-[#3f3f46]"
            >
              {fullscreenRows.length} full-screen moment
              {fullscreenRows.length === 1 ? "" : "s"} · {fmtDur(fullscreenTotalS)}s total —
              they cover you while you keep talking.
            </p>
          )}

          <ul>
            {rows.map((row) => (
              <SuggestionRow
                key={row.id}
                row={row}
                asset={assetById.get(row.asset_id) ?? null}
                confirmed={confirmedIds.has(row.id)}
                onReveal={() => reveal(row)}
                onKeep={() => keepRow(row.id)}
                onReject={() => rejectRow(row.id)}
                onStripSfx={() => stripSfx(row.id)}
                onDemote={() => demoteRow(row)}
              />
            ))}
          </ul>

          {/* Partial match: unmatched moments listed as wishlist lines under the rows. */}
          {wishlist.length > 0 && (
            <div className="mt-2 rounded-xl border border-dashed border-zinc-200 px-3 py-2">
              {wishlist.map((line) => (
                <p key={line} className="text-[11px] text-[#71717a]">
                  {line}
                </p>
              ))}
            </div>
          )}

          <div className="mt-3 flex gap-2">
            <button
              type="button"
              disabled={keptCount === 0 || applying}
              onClick={handleApply}
              className="min-h-[44px] flex-1 rounded-lg bg-lime-600 px-4 py-2 text-sm font-semibold text-white transition-colors hover:bg-lime-700 focus-visible:outline focus-visible:outline-2 focus-visible:outline-lime-500 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {applying ? "Applying…" : `Apply ${keptCount} to video`}
            </button>
            <button
              type="button"
              onClick={handleDismiss}
              className="min-h-[44px] rounded-lg border border-zinc-200 bg-white px-4 py-2 text-sm text-[#71717a] transition-colors hover:border-zinc-400 focus-visible:outline focus-visible:outline-2 focus-visible:outline-lime-500"
            >
              Dismiss
            </button>
          </div>

          {actionError && (
            <p className="mt-2 rounded border border-zinc-200 bg-white px-3 py-2 text-[12px] text-[#3f3f46]">
              {actionError}
            </p>
          )}

          <div className="mt-2">
            <SuggestButton
              onClick={handleSuggest}
              disabled={suggestDisabled}
              label="✦ Re-match visuals"
            />
          </div>
        </div>
      ) : (
        /* Idle (or post-apply receipt) — the §12 Generate-with-AI entry button. */
        <div>
          {phase === "applied" && (
            <p className="mb-2 text-[12px] text-[#3f3f46]">
              Baking your {appliedCount} visual{appliedCount === 1 ? "" : "s"} in — the preview
              above is exactly what renders.
            </p>
          )}
          <SuggestButton
            onClick={handleSuggest}
            disabled={suggestDisabled}
            label="✦ Place visuals for me"
          />
          {suggestDisabled && <InlineReason />}
        </div>
      )}
    </div>
  );
}

/** §12 Generate-with-AI token: zinc border, ✦ prefix, lime hover. */
function SuggestButton({
  onClick,
  disabled,
  label,
  className = "",
}: {
  onClick: () => void;
  disabled: boolean;
  label: string;
  className?: string;
}) {
  return (
    <button
      type="button"
      disabled={disabled}
      onClick={onClick}
      className={`inline-flex min-h-[44px] items-center gap-1.5 rounded-lg border border-zinc-200 bg-white px-4 py-2 text-[12px] text-[#71717a] transition-colors hover:border-lime-400 hover:text-lime-700 focus-visible:outline focus-visible:outline-2 focus-visible:outline-lime-500 disabled:cursor-not-allowed disabled:opacity-50 disabled:hover:border-zinc-200 disabled:hover:text-[#71717a] ${className}`}
    >
      {label}
    </button>
  );
}

/** Inline disabled reason — text below the button, never tooltip-only. */
function InlineReason() {
  return <p className="mt-1.5 text-[12px] text-[#71717a]">Add at least one visual first</p>;
}

function SuggestionRow({
  row,
  asset,
  confirmed,
  onReveal,
  onKeep,
  onReject,
  onStripSfx,
  onDemote,
}: {
  row: OverlaySuggestion;
  asset: PoolAsset | null;
  confirmed: boolean;
  onReveal: () => void;
  onKeep: () => void;
  onReject: () => void;
  onStripSfx: () => void;
  /** 009 T5 one-tap demote (fullscreen rows only): convert to a small pip card. */
  onDemote: () => void;
}) {
  const label = asset?.source_filename ?? asset?.subject ?? row.overlay.kind;
  // 009 T5: the thumbnail IS the mode signal — fullscreen rows render a 9:16
  // cover-cropped mini-takeover tile (portrait, full-bleed media, zero rounded
  // chrome inside); pip rows keep the landscape rounded tile unchanged.
  const isFullscreen = isFullscreenRow(row);
  const durationS = Math.max(0, row.overlay.end_s - row.overlay.start_s);
  const mediaClass = isFullscreen ? mediaClassFor("fullscreen") : "h-full w-full object-cover";
  return (
    <li
      // R4 (C11 / WCAG 2.1.1): the row IS its primary action (reveal = seek the
      // preview + play). role="button" announces it as interactive to screen
      // readers, and Enter/Space fire that same reveal so a keyboard user can
      // PREVIEW a suggestion before deciding. Keep/reject stay on the explicit
      // 44px ✓/× buttons (their own keyboard path) — the row no longer hijacks
      // Enter to stage, which had left reveal unreachable by keyboard.
      role="button"
      tabIndex={0}
      onClick={onReveal}
      onKeyDown={(e) => {
        if (e.target !== e.currentTarget) return;
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onReveal();
        } else if (e.key === "Delete" || e.key === "Backspace") {
          e.preventDefault();
          onReject();
        }
      }}
      aria-label={`Preview suggestion: ${label}, ${fmtTime(row.overlay.start_s)} to ${fmtTime(row.overlay.end_s)}`}
      className={`flex cursor-pointer items-start gap-2.5 border-t border-zinc-100 py-2.5 transition-colors first:border-t-0 focus-visible:outline focus-visible:outline-2 focus-visible:outline-lime-500 ${
        confirmed ? "bg-lime-50/60" : ""
      }`}
    >
      {/* Thumbnail from the pool asset; dark tile fallback. Fullscreen rows:
          taller 9:16 portrait tile, cover-cropped media, zero rounded chrome
          inside — a mini takeover. Pip rows unchanged. */}
      <div
        data-testid={`suggestion-thumb-${row.id}`}
        data-thumb-mode={isFullscreen ? "fullscreen" : "pip"}
        className={
          isFullscreen
            ? "aspect-[9/16] w-8 shrink-0 overflow-hidden border border-zinc-200 bg-zinc-900"
            : "h-8 w-11 shrink-0 overflow-hidden rounded-md border border-zinc-200 bg-zinc-900"
        }
      >
        {asset?.display_url ? (
          asset.kind === "video" ? (
            <video
              src={asset.display_url}
              muted
              playsInline
              preload="metadata"
              className={mediaClass}
            />
          ) : (
            // eslint-disable-next-line @next/next/no-img-element -- signed GCS thumbnail
            <img src={asset.display_url} alt="" className={mediaClass} />
          )
        ) : null}
      </div>

      <div className="min-w-0 flex-1">
        <p className="truncate text-[12px] font-semibold text-[#0c0c0e]">
          <span aria-hidden>✦ </span>
          {/* 009 T5: "Full screen" soft pill (§2 soft cell) BEFORE the filename. */}
          {isFullscreen && (
            <span className="mr-1 inline-block rounded-full border border-lime-200 bg-lime-50 px-1.5 py-px align-middle text-[10px] font-semibold text-lime-800">
              Full screen
            </span>
          )}
          {label}{" "}
          <span className="font-normal text-[#71717a]">
            {fmtTime(row.overlay.start_s)}–{fmtTime(row.overlay.end_s)}
          </span>
        </p>
        {/* "likely" tier rows keep the server's hedged copy verbatim (10A).
            Fullscreen rows lead with the honest takeover line first. */}
        <p className="mt-0.5 text-[11px] leading-snug text-[#71717a]">
          {isFullscreen && (
            <span className="text-[#3f3f46]">
              Full-screen cutaway · {fmtDur(durationS)}s — covers you while you keep
              talking.{" "}
            </span>
          )}
          {row.reason}
        </p>
        {isFullscreen && (
          <button
            type="button"
            aria-label={`Show ${label} as small card instead`}
            onClick={(e) => {
              e.stopPropagation();
              onDemote();
            }}
            className="mt-1 text-[11px] text-[#71717a] underline underline-offset-2 transition-colors hover:text-[#3f3f46] focus-visible:outline focus-visible:outline-2 focus-visible:outline-lime-500"
          >
            Show as small card instead
          </button>
        )}
        {row.sfx && (
          <p className="mt-0.5 text-[11px] text-lime-700">
            + {row.sfx.label ?? "pop"} sound{" "}
            <button
              type="button"
              aria-label={`Remove sound from ${label}`}
              onClick={(e) => {
                e.stopPropagation();
                onStripSfx();
              }}
              className="inline-flex min-h-11 min-w-11 items-center justify-center rounded border border-zinc-200 px-1 text-[#71717a] transition-colors hover:border-zinc-400 hover:text-[#3f3f46] focus-visible:outline focus-visible:outline-2 focus-visible:outline-lime-500 sm:min-h-0 sm:min-w-0"
            >
              ×
            </button>
          </p>
        )}
      </div>

      <div className="flex shrink-0 flex-col gap-1">
        <button
          type="button"
          aria-label={`Keep ${label}`}
          onClick={(e) => {
            e.stopPropagation();
            onKeep();
          }}
          className={`flex h-11 w-11 items-center justify-center rounded-lg border text-sm transition-colors focus-visible:outline focus-visible:outline-2 focus-visible:outline-lime-500 ${
            confirmed
              ? "border-lime-600 bg-lime-600 text-white"
              : "border-zinc-200 bg-white text-[#3f3f46] hover:border-lime-400 hover:text-lime-700"
          }`}
        >
          ✓
        </button>
        <button
          type="button"
          aria-label={`Reject ${label}`}
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
