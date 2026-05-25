"use client";

/**
 * Admin job listing page. Unified across music + template + auto_music jobs.
 *
 * Companion to /admin/jobs/[id] (debug detail). Powered by
 * `GET /admin/jobs` from src/apps/api/app/routes/admin_jobs.py via the
 * `/api/admin/[...]` Next.js proxy.
 *
 * The Running-for column and queue-position badge are how we answer
 * "why is this job stuck and what's piling up behind it?" without
 * SSH-ing to a Fly machine.
 */

import Link from "next/link";
import { useEffect, useState } from "react";

import {
  adminGetQueueState,
  adminListJobs,
  type AdminJobListItem,
  type JobTypeFilter,
  type QueueSnapshotResponse,
} from "@/lib/admin-jobs-api";

const PAGE_SIZE = 50;
// Queue snapshot polling cadence. 10s feels live without hammering the broker —
// each fetch costs one inspect() + N LLEN calls.
const QUEUE_POLL_MS = 10_000;

// Time-in-processing thresholds for color coding (seconds). Typical
// template/music jobs land 2-5 min (CLAUDE.md). 5min → amber means
// "worth glancing at"; 15min → red means "almost certainly stuck".
const PROCESSING_AMBER_S = 5 * 60;
const PROCESSING_RED_S = 15 * 60;

const STATUS_COLOR: Record<string, string> = {
  queued: "text-yellow-400",
  processing: "text-blue-400",
  template_ready: "text-green-400",
  music_ready: "text-green-400",
  variants_ready: "text-green-400",
  processing_failed: "text-red-400",
  matching_failed: "text-red-400",
  variants_failed: "text-red-400",
  no_labeled_tracks: "text-red-400",
  cancelled: "text-zinc-400",
};

const JOB_TYPE_LABEL: Record<string, string> = {
  default: "Default",
  template: "Template",
  music: "Music",
  auto_music: "Auto-music",
  generative: "Generative",
};

export default function AdminJobsPage() {
  const [items, setItems] = useState<AdminJobListItem[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [jobType, setJobType] = useState<JobTypeFilter>("all");
  const [onlyFailures, setOnlyFailures] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [queue, setQueue] = useState<QueueSnapshotResponse | null>(null);
  const [queueError, setQueueError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    adminListJobs({ jobType, onlyFailures, limit: PAGE_SIZE, offset })
      .then((data) => {
        if (cancelled) return;
        setItems(data.items);
        setTotal(data.total);
      })
      .catch((err: Error) => {
        if (!cancelled) setError(err.message);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [jobType, onlyFailures, offset]);

  // Queue snapshot: poll every QUEUE_POLL_MS so the summary panel stays
  // live without a manual refresh. A failed fetch records the error but
  // keeps the last good snapshot rendered (don't blank the UI on a transient
  // proxy hiccup).
  useEffect(() => {
    let cancelled = false;
    const fetchQueue = () => {
      adminGetQueueState()
        .then((snap) => {
          if (cancelled) return;
          setQueue(snap);
          setQueueError(null);
        })
        .catch((err: Error) => {
          if (!cancelled) setQueueError(err.message);
        });
    };
    fetchQueue();
    const t = setInterval(fetchQueue, QUEUE_POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(t);
    };
  }, []);

  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const currentPage = Math.floor(offset / PAGE_SIZE) + 1;

  return (
    <main className="min-h-screen bg-black text-white px-4 py-12">
      <div className="max-w-6xl mx-auto">
        <header className="mb-8">
          <div className="flex items-center justify-between">
            <div>
              <h1 className="text-2xl font-bold">Jobs</h1>
              <p className="text-zinc-400 text-sm mt-1">
                {total} job{total !== 1 ? "s" : ""} · click a row to inspect
                agents, parameters, and assembly trace
              </p>
            </div>
            <Link
              href="/admin"
              className="px-4 py-2 bg-zinc-800 text-zinc-300 rounded-lg text-sm hover:bg-zinc-700"
            >
              ← Admin
            </Link>
          </div>

          <QueueSummary snapshot={queue} error={queueError} />

          <div className="mt-6 flex flex-wrap items-center gap-3">
            <label className="flex items-center gap-2 text-sm text-zinc-400">
              Type
              <select
                value={jobType}
                onChange={(e) => {
                  setOffset(0);
                  setJobType(e.target.value as JobTypeFilter);
                }}
                className="bg-zinc-900 border border-zinc-800 rounded px-2 py-1 text-sm text-white"
              >
                <option value="all">All</option>
                <option value="music">Music</option>
                <option value="template">Template</option>
                <option value="auto_music">Auto-music</option>
                <option value="generative">Generative</option>
                <option value="default">Default</option>
              </select>
            </label>

            <label className="flex items-center gap-2 text-sm text-zinc-400">
              <input
                type="checkbox"
                checked={onlyFailures}
                onChange={(e) => {
                  setOffset(0);
                  setOnlyFailures(e.target.checked);
                }}
                className="accent-red-500"
              />
              Failures only
            </label>
          </div>
        </header>

        {error && (
          <div className="mb-6 rounded border border-red-800 bg-red-950/40 px-4 py-3 text-sm text-red-300">
            {error}
          </div>
        )}

        <div className="overflow-x-auto rounded-lg border border-zinc-800">
          <table className="w-full text-sm">
            <thead className="bg-zinc-900 text-zinc-400">
              <tr>
                <th className="text-left px-3 py-2 font-medium">Job ID</th>
                <th className="text-left px-3 py-2 font-medium">Type</th>
                <th className="text-left px-3 py-2 font-medium">Status</th>
                <th className="text-left px-3 py-2 font-medium">Running for</th>
                <th className="text-left px-3 py-2 font-medium">Template / Track</th>
                <th className="text-right px-3 py-2 font-medium">Agents</th>
                <th className="text-right px-3 py-2 font-medium">Failures</th>
                <th className="text-left px-3 py-2 font-medium">Created</th>
              </tr>
            </thead>
            <tbody>
              {loading && items.length === 0 && (
                <tr>
                  <td colSpan={8} className="px-3 py-6 text-zinc-500 text-center">
                    Loading…
                  </td>
                </tr>
              )}
              {!loading && items.length === 0 && (
                <tr>
                  <td colSpan={8} className="px-3 py-6 text-zinc-500 text-center">
                    No jobs match these filters.
                  </td>
                </tr>
              )}
              {items.map((item) => (
                <tr
                  key={item.job_id}
                  className="border-t border-zinc-800 hover:bg-zinc-900/60"
                >
                  <td className="px-3 py-2 font-mono text-xs">
                    <Link
                      href={`/admin/jobs/${item.job_id}`}
                      className="text-blue-400 hover:underline"
                    >
                      {item.job_id.slice(0, 8)}
                    </Link>
                  </td>
                  <td className="px-3 py-2">
                    {JOB_TYPE_LABEL[item.job_type] ?? item.job_type}
                  </td>
                  <td className="px-3 py-2">
                    <span className={STATUS_COLOR[item.status] ?? "text-zinc-300"}>
                      {item.status}
                    </span>
                    {item.status === "queued" && (
                      <QueuePositionBadge jobId={item.job_id} snapshot={queue} />
                    )}
                    {item.failure_reason && (
                      <span className="ml-2 text-xs text-zinc-500">
                        ({item.failure_reason})
                      </span>
                    )}
                  </td>
                  <td className="px-3 py-2">
                    <RunningFor seconds={item.time_in_processing_s} />
                  </td>
                  <td className="px-3 py-2 text-zinc-400 text-xs">
                    {item.template_id ?? item.music_track_id ?? "—"}
                  </td>
                  <td className="px-3 py-2 text-right text-zinc-400">
                    {item.agent_run_count}
                  </td>
                  <td className="px-3 py-2 text-right">
                    <span
                      className={
                        item.failure_count > 0 ? "text-red-400" : "text-zinc-600"
                      }
                    >
                      {item.failure_count}
                    </span>
                  </td>
                  <td className="px-3 py-2 text-zinc-500 text-xs">
                    {new Date(item.created_at).toLocaleString()}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        {totalPages > 1 && (
          <div className="mt-6 flex items-center justify-center gap-2">
            <button
              type="button"
              disabled={offset === 0}
              onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
              className="px-3 py-1 rounded bg-zinc-800 text-zinc-300 text-sm disabled:opacity-40"
            >
              ← Prev
            </button>
            <span className="text-zinc-500 text-sm">
              Page {currentPage} / {totalPages}
            </span>
            <button
              type="button"
              disabled={offset + PAGE_SIZE >= total}
              onClick={() => setOffset(offset + PAGE_SIZE)}
              className="px-3 py-1 rounded bg-zinc-800 text-zinc-300 text-sm disabled:opacity-40"
            >
              Next →
            </button>
          </div>
        )}
      </div>
    </main>
  );
}

// ── Queue summary panel ─────────────────────────────────────────────────────

function QueueSummary({
  snapshot,
  error,
}: {
  snapshot: QueueSnapshotResponse | null;
  error: string | null;
}): JSX.Element {
  if (!snapshot && !error) {
    return (
      <div className="mt-4 rounded border border-zinc-800 bg-zinc-950 px-4 py-3 text-xs text-zinc-500">
        Loading queue state…
      </div>
    );
  }
  if (snapshot && !snapshot.ok) {
    return (
      <div className="mt-4 rounded border border-red-900 bg-red-950/40 px-4 py-3 text-xs text-red-300">
        Broker unreachable — queue depth and worker assignment are unknown.
        Workers may still be processing jobs, but the admin UI can&apos;t see
        them right now.
      </div>
    );
  }
  if (!snapshot) {
    return (
      <div className="mt-4 rounded border border-red-900 bg-red-950/40 px-4 py-3 text-xs text-red-300">
        Could not fetch queue state: {error}
      </div>
    );
  }

  const totalDepth = snapshot.queues.reduce((acc, q) => acc + q.depth, 0);
  return (
    <div className="mt-4 rounded border border-zinc-800 bg-zinc-950 px-4 py-3 flex flex-wrap items-baseline gap-x-6 gap-y-2 text-xs">
      <span>
        <span className="text-zinc-500">Active workers:</span>{" "}
        <span className="text-zinc-200 font-medium">
          {snapshot.active_workers.length}
        </span>
      </span>
      <span>
        <span className="text-zinc-500">Queued:</span>{" "}
        <span
          className={
            totalDepth > 0 ? "text-yellow-400 font-medium" : "text-zinc-200"
          }
        >
          {totalDepth}
        </span>
      </span>
      {snapshot.queues.map((q) => (
        <span key={q.name} className="text-zinc-500">
          {q.name}: <span className="text-zinc-300">{q.depth}</span>
          {q.oldest_pending_job_id && (
            <Link
              href={`/admin/jobs/${q.oldest_pending_job_id}`}
              className="ml-2 text-blue-400 font-mono hover:underline"
              title="Oldest queued job"
            >
              oldest: {q.oldest_pending_job_id.slice(0, 8)}
            </Link>
          )}
        </span>
      ))}
    </div>
  );
}

// ── Per-row cells ───────────────────────────────────────────────────────────

function QueuePositionBadge({
  jobId,
  snapshot,
}: {
  jobId: string;
  snapshot: QueueSnapshotResponse | null;
}): JSX.Element | null {
  if (!snapshot?.ok) return null;
  // Walk all queues for the matching oldest_pending_job_id. We only get
  // queue *position 0* this way; for the rest the snapshot doesn't carry
  // the full list. That's intentional — the queue summary tells you
  // "N queued, oldest is X", which is enough for the list view. Per-job
  // position is on the detail page (live inspect).
  for (const q of snapshot.queues) {
    if (q.oldest_pending_job_id === jobId) {
      return (
        <span className="ml-2 px-1.5 py-0.5 rounded bg-yellow-950 text-yellow-300 text-[10px] font-mono">
          next up
        </span>
      );
    }
  }
  return null;
}

function RunningFor({ seconds }: { seconds: number | null }): JSX.Element {
  if (seconds === null || seconds === undefined) {
    return <span className="text-zinc-600">—</span>;
  }
  const color =
    seconds >= PROCESSING_RED_S
      ? "text-red-400 font-medium"
      : seconds >= PROCESSING_AMBER_S
        ? "text-yellow-400"
        : "text-zinc-300";
  return <span className={color}>{formatDuration(seconds)}</span>;
}

function formatDuration(seconds: number): string {
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  if (m === 0) return `${s}s`;
  if (m < 60) return `${m}m ${s}s`;
  const h = Math.floor(m / 60);
  const remM = m % 60;
  return `${h}h ${remM}m`;
}
