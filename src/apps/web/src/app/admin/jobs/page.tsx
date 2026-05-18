"use client";

/**
 * Admin job listing page. Unified across music + template + auto_music jobs.
 *
 * Companion to /admin/jobs/[id] (debug detail). Powered by
 * `GET /admin/jobs` from src/apps/api/app/routes/admin_jobs.py via the
 * `/api/admin/[...]` Next.js proxy.
 */

import Link from "next/link";
import { useEffect, useState } from "react";

import {
  adminListJobs,
  type AdminJobListItem,
  type JobTypeFilter,
} from "@/lib/admin-jobs-api";

const PAGE_SIZE = 50;

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
};

const JOB_TYPE_LABEL: Record<string, string> = {
  default: "Default",
  template: "Template",
  music: "Music",
  auto_music: "Auto-music",
};

export default function AdminJobsPage() {
  const [items, setItems] = useState<AdminJobListItem[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [jobType, setJobType] = useState<JobTypeFilter>("all");
  const [onlyFailures, setOnlyFailures] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

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
                <th className="text-left px-3 py-2 font-medium">Template / Track</th>
                <th className="text-right px-3 py-2 font-medium">Agents</th>
                <th className="text-right px-3 py-2 font-medium">Failures</th>
                <th className="text-left px-3 py-2 font-medium">Created</th>
              </tr>
            </thead>
            <tbody>
              {loading && items.length === 0 && (
                <tr>
                  <td colSpan={7} className="px-3 py-6 text-zinc-500 text-center">
                    Loading…
                  </td>
                </tr>
              )}
              {!loading && items.length === 0 && (
                <tr>
                  <td colSpan={7} className="px-3 py-6 text-zinc-500 text-center">
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
                    {item.failure_reason && (
                      <span className="ml-2 text-xs text-zinc-500">
                        ({item.failure_reason})
                      </span>
                    )}
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
