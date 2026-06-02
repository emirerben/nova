"use client";

/**
 * /admin/review — the dev-loop video-triage surface (plan M1 / T6).
 *
 * The PHONE is the right device for vertical-video QA, so this is a single
 * column of tappable cards, each one a grader `escalate` verdict: the rendered
 * clip (thumbnail/playback), per-dimension scores, the one-line rationale, and
 * the risk tag. A tap writes a calibration label (👍 → auto_pass, 👎 →
 * auto_reject) that re-trains the grader's thresholds.
 *
 * Read-only on the video pipeline: labeling NEVER mutates a job or ships
 * anything — it only appends a calibration row. Mirrors the poll+render shape
 * of /admin/jobs/page.tsx. Powered by GET/POST /admin/review (admin_review.py)
 * through the /api/admin/[...] proxy.
 */

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import {
  adminLabelReview,
  adminListReview,
  type ReviewItem,
  type ReviewVerdict,
} from "@/lib/admin-review-api";

// Poll cadence — the grader writes new escalations as the builder loop renders.
// 15s keeps the queue fresh on the phone without hammering the proxy.
const REVIEW_POLL_MS = 15_000;

export default function AdminReviewPage() {
  const [items, setItems] = useState<ReviewItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const fetchReview = useCallback(() => {
    return adminListReview(50)
      .then((data) => {
        setItems(data.items);
        setError(null);
      })
      .catch((err: Error) => setError(err.message))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    let cancelled = false;
    const run = () => {
      if (cancelled) return;
      void fetchReview();
    };
    run();
    const t = setInterval(run, REVIEW_POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(t);
    };
  }, [fetchReview]);

  return (
    <main className="min-h-screen bg-black text-white px-4 py-10">
      <div className="max-w-md mx-auto">
        <header className="mb-6 flex items-center justify-between">
          <div>
            <h1 className="text-2xl font-bold">Review</h1>
            <p className="text-zinc-400 text-sm mt-1">
              {items.length} video{items.length !== 1 ? "s" : ""} the grader
              escalated · tap 👍/👎 to calibrate
            </p>
          </div>
          <Link
            href="/admin"
            className="px-3 py-2 bg-zinc-800 text-zinc-300 rounded-lg text-sm hover:bg-zinc-700"
          >
            ← Admin
          </Link>
        </header>

        {error && (
          <div className="mb-6 rounded border border-red-800 bg-red-950/40 px-4 py-3 text-sm text-red-300">
            {error}
          </div>
        )}

        {loading && items.length === 0 && (
          <p className="text-zinc-500 text-sm text-center py-12">Loading escalations…</p>
        )}

        {!loading && items.length === 0 && !error && (
          <div className="text-center py-16 text-zinc-500">
            <p className="text-3xl mb-2">✅</p>
            <p className="text-sm">Nothing to review — the grader is keeping up.</p>
          </div>
        )}

        <div className="space-y-5">
          {items.map((item) => (
            <ReviewCard
              key={item.run_id}
              item={item}
              onLabeled={() => void fetchReview()}
            />
          ))}
        </div>
      </div>
    </main>
  );
}

// ── Card ─────────────────────────────────────────────────────────────────────

function ReviewCard({
  item,
  onLabeled,
}: {
  item: ReviewItem;
  onLabeled: () => void;
}): JSX.Element {
  const [submitting, setSubmitting] = useState<ReviewVerdict | null>(null);
  const [labeled, setLabeled] = useState(item.labeled);
  const [labelError, setLabelError] = useState<string | null>(null);

  const submit = useCallback(
    async (verdict: ReviewVerdict) => {
      setSubmitting(verdict);
      setLabelError(null);
      try {
        await adminLabelReview(item.run_id, verdict);
        setLabeled(true);
        onLabeled();
      } catch (err) {
        setLabelError((err as Error).message);
      } finally {
        setSubmitting(null);
      }
    },
    [item.run_id, onLabeled],
  );

  return (
    <div className="rounded-xl border border-zinc-800 bg-zinc-950 overflow-hidden">
      {/* Media: prefer playable video, fall back to the still thumbnail. */}
      <div className="bg-black aspect-[9/16] max-h-[60vh] flex items-center justify-center">
        {item.video_url ? (
          <video
            src={item.video_url}
            poster={item.thumbnail_url ?? undefined}
            controls
            playsInline
            className="h-full w-full object-contain"
          />
        ) : item.thumbnail_url ? (
          // eslint-disable-next-line @next/next/no-img-element -- signed GCS URL, not a static asset
          <img
            src={item.thumbnail_url}
            alt="rendered clip"
            className="h-full w-full object-contain"
          />
        ) : (
          <span className="text-zinc-600 text-sm">No preview available</span>
        )}
      </div>

      <div className="p-4 space-y-3">
        <div className="flex items-center justify-between gap-2">
          <RiskTag tag={item.risk_tag} />
          <span className="text-xs text-zinc-500 font-mono">
            avg {item.avg.toFixed(2)} · conf {item.confidence.toFixed(2)}
          </span>
        </div>

        <p className="text-sm text-zinc-200">
          {item.reasoning || "No rationale provided."}
        </p>

        <ScoreBars scores={item.scores} />

        {item.job_id && (
          <Link
            href={`/admin/jobs/${item.job_id}`}
            className="inline-block text-xs text-blue-400 hover:underline font-mono"
          >
            job {item.job_id.slice(0, 8)} →
          </Link>
        )}

        {labelError && (
          <p className="text-xs text-red-400">{labelError}</p>
        )}

        {labeled ? (
          <div className="rounded-lg bg-zinc-900 text-zinc-400 text-center py-2.5 text-sm">
            Calibration recorded ✓
          </div>
        ) : (
          <div className="grid grid-cols-2 gap-3 pt-1">
            <button
              type="button"
              disabled={submitting !== null}
              onClick={() => submit("auto_pass")}
              className="py-3 rounded-lg bg-green-900/40 border border-green-800 text-green-300 text-sm font-medium hover:bg-green-900/60 disabled:opacity-50"
            >
              {submitting === "auto_pass" ? "…" : "👍 Looks good"}
            </button>
            <button
              type="button"
              disabled={submitting !== null}
              onClick={() => submit("auto_reject")}
              className="py-3 rounded-lg bg-red-900/40 border border-red-800 text-red-300 text-sm font-medium hover:bg-red-900/60 disabled:opacity-50"
            >
              {submitting === "auto_reject" ? "…" : "👎 Reject"}
            </button>
          </div>
        )}
      </div>
    </div>
  );
}

function RiskTag({ tag }: { tag: string }): JSX.Element {
  const color =
    tag === "reject"
      ? "bg-red-950 text-red-300"
      : tag === "low_confidence"
        ? "bg-amber-950 text-amber-300"
        : tag === "borderline"
          ? "bg-yellow-950 text-yellow-300"
          : "bg-zinc-800 text-zinc-300";
  return (
    <span className={`px-2 py-0.5 rounded text-[11px] font-mono ${color}`}>
      {tag || "—"}
    </span>
  );
}

function ScoreBars({ scores }: { scores: Record<string, number> }): JSX.Element | null {
  const entries = Object.entries(scores);
  if (entries.length === 0) return null;
  return (
    <div className="space-y-1.5">
      {entries
        .sort(([a], [b]) => a.localeCompare(b))
        .map(([dim, val]) => (
          <div key={dim} className="flex items-center gap-2">
            <span className="text-[11px] text-zinc-500 w-32 truncate">{dim}</span>
            <div className="flex-1 h-1.5 rounded bg-zinc-800 overflow-hidden">
              <div
                className="h-full bg-zinc-500"
                style={{ width: `${Math.max(0, Math.min(100, (val / 5) * 100))}%` }}
              />
            </div>
            <span className="text-[11px] text-zinc-400 w-7 text-right">
              {val.toFixed(1)}
            </span>
          </div>
        ))}
    </div>
  );
}
