"use client";

/**
 * FootagePool — "dump the trip, Nova sorts it into your plan" (dogfood #4).
 *
 * A power-up, not the daily loop: lives in the right column below the
 * calendar, CTA is the bordered secondary pill (never a second InkButton on
 * the workspace), and the whole section is suppressed during activation —
 * SeedUploadCard IS the footage pool at that moment.
 *
 * Matched clips appear on item pages as provisional "Matched — keep?" chips;
 * nothing auto-renders. Counts come from real backend state only (§7-D6).
 */

import { useEffect, useRef, useState } from "react";
import {
  attachPoolClips,
  rematchPoolClips,
  requestPoolUploadUrls,
  uploadToGcs,
  type ContentPlan,
} from "@/lib/plan-api";

export function FootagePool({
  plan,
  onRefresh,
  onError,
}: {
  plan: ContentPlan;
  onRefresh: () => void;
  onError: (msg: string) => void;
}) {
  const [uploading, setUploading] = useState(false);
  const [uploadedCount, setUploadedCount] = useState(0);
  const [totalCount, setTotalCount] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);

  const items = plan.items ?? [];
  const pendingItems = items.filter(
    (i) => i.status === "idea" || i.status === "awaiting_clips",
  );
  const poolStatus = plan.pool_status ?? "none";
  const clipCount = plan.pool_clip_count ?? 0;
  const matchedCount = plan.pool_matched_count ?? 0;
  const unmatchedCount = Math.max(0, clipCount - matchedCount);
  const matching = poolStatus === "matching";
  const planFull = pendingItems.length === 0;

  // While the matcher runs, the status only changes server-side — poll so
  // "Sorting…" resolves to matched/failed without a manual reload (dogfood
  // finding: the line sat on "Sorting…" after the task had already failed).
  useEffect(() => {
    if (!matching) return;
    const t = setInterval(onRefresh, 5000);
    return () => clearInterval(t);
  }, [matching, onRefresh]);

  async function handleFiles(files: FileList | null) {
    if (!files || files.length === 0) return;
    const list = Array.from(files);
    setUploading(true);
    setUploadedCount(0);
    setTotalCount(list.length);
    try {
      const urls = await requestPoolUploadUrls(
        plan.id,
        list.map((f) => ({
          filename: f.name,
          content_type: f.type || "video/mp4",
          file_size_bytes: f.size,
        })),
      );
      // Sequential-ish with count progress (real events only — no fake bars).
      let done = 0;
      await Promise.all(
        urls.map(async (u, i) => {
          await uploadToGcs(u.upload_url, list[i]);
          done += 1;
          setUploadedCount(done);
        }),
      );
      await attachPoolClips(
        plan.id,
        urls.map((u) => u.gcs_path),
      );
      onRefresh();
    } catch (err) {
      onError(err instanceof Error ? err.message : "Upload failed");
    } finally {
      setUploading(false);
    }
  }

  async function handleRematch() {
    try {
      await rematchPoolClips(plan.id);
      onRefresh();
    } catch (err) {
      onError(err instanceof Error ? err.message : "Couldn't start matching");
    }
  }

  return (
    <section
      className="rounded-2xl border border-zinc-200 bg-white p-5"
      data-testid="footage-pool"
    >
      <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-lime-700">
        Your footage
      </p>

      {planFull && clipCount === 0 ? (
        // Never accept an upload we can't use.
        <p className="mt-2 text-sm text-[#71717a]">
          Your plan&apos;s filled for now — add footage when new ideas open up.
        </p>
      ) : (
        <>
          <p className="font-display mt-2 text-xl text-[#0c0c0e]">
            Add everything from the trip.
          </p>
          <p className="mt-1 text-sm text-[#71717a]">
            Nova sorts it into your planned posts — you keep or swap each match.
          </p>

          {/* Upload — secondary pill, one primary per screen */}
          <div className="mt-4">
            {uploading ? (
              <p className="text-sm text-[#3f3f46]" role="status" aria-live="polite">
                Uploading {uploadedCount} of {totalCount}…
              </p>
            ) : (
              <label className="inline-flex min-h-11 cursor-pointer items-center rounded-full border border-zinc-200 px-5 py-2 text-sm font-medium text-[#0c0c0e] transition-colors hover:border-zinc-400 focus-within:ring-2 focus-within:ring-lime-600 focus-within:ring-offset-2">
                Add footage
                <input
                  ref={inputRef}
                  type="file"
                  accept="video/mp4,video/quicktime"
                  multiple
                  className="sr-only"
                  disabled={uploading || matching}
                  onChange={(e) => {
                    void handleFiles(e.target.files);
                    e.target.value = "";
                  }}
                />
              </label>
            )}
          </div>

          {/* Matching / matched states — backend state only */}
          {matching && (
            <p className="mt-3 flex items-center gap-2 text-sm text-[#71717a]" role="status">
              <span className="relative flex h-2 w-2">
                <span className="motion-safe:animate-ping absolute inline-flex h-full w-full rounded-full bg-lime-600 opacity-60" />
                <span className="relative inline-flex h-2 w-2 rounded-full bg-lime-600" />
              </span>
              Sorting {clipCount} clip{clipCount === 1 ? "" : "s"} into your plan…
            </p>
          )}
          {!matching && poolStatus === "matched" && matchedCount > 0 && (
            <p className="mt-3 text-sm text-[#3f3f46]">
              <span className="text-lime-700">✓</span> {matchedCount} of {clipCount} clips
              sorted into your plan — look for &ldquo;Matched&rdquo; chips on those days.
            </p>
          )}
          {!matching && (poolStatus === "matched" || poolStatus === "matched_empty") && unmatchedCount > 0 && (
            <div className="mt-2 rounded border border-zinc-200 bg-white px-3 py-2 text-xs text-[#3f3f46]">
              {unmatchedCount} clip{unmatchedCount === 1 ? "" : "s"} didn&apos;t fit this plan yet —
              kept here with your plan.{" "}
              {pendingItems.length > 0 && (
                <button
                  type="button"
                  onClick={handleRematch}
                  className="font-medium text-lime-700 underline-offset-2 hover:underline"
                >
                  Match again
                </button>
              )}
            </div>
          )}
          {!matching && poolStatus === "match_failed" && (
            <div className="mt-3 rounded border border-zinc-200 bg-white px-3 py-2 text-xs text-[#3f3f46]">
              Matching didn&apos;t finish.{" "}
              <button
                type="button"
                onClick={handleRematch}
                className="font-medium text-lime-700 underline-offset-2 hover:underline"
              >
                Try again
              </button>
            </div>
          )}
        </>
      )}
    </section>
  );
}
