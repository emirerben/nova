"use client";

import { useEffect, useState } from "react";
import { listTemplateJobs, type TemplateJobListItem } from "@/lib/api";

const STATUS_COLORS: Record<string, string> = {
  queued: "text-yellow-400",
  processing: "text-blue-400",
  template_ready: "text-green-400",
  processing_failed: "text-red-400",
};

const PAGE_SIZE = 20;

export default function QADashboardPage() {
  const [jobs, setJobs] = useState<TemplateJobListItem[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setLoading(true);
    listTemplateJobs(PAGE_SIZE, offset)
      .then((data) => {
        setJobs(data.jobs);
        setTotal(data.total);
      })
      .catch((err) => setError(err.message))
      .finally(() => setLoading(false));
  }, [offset]);

  const totalPages = Math.ceil(total / PAGE_SIZE);
  const currentPage = Math.floor(offset / PAGE_SIZE) + 1;

  return (
    <main className="min-h-screen bg-black text-white px-4 py-16">
      <div className="max-w-4xl mx-auto">
        <div className="flex items-center justify-between mb-8">
          <div>
            <h1 className="text-2xl font-bold">QA Dashboard</h1>
            <p className="text-zinc-400 text-sm mt-1">
              Internal tool — {total} template job{total !== 1 ? "s" : ""} total
            </p>
          </div>
          <a
            href="/template"
            className="px-4 py-2 bg-zinc-800 text-zinc-300 rounded-lg text-sm hover:bg-zinc-700 transition-colors"
          >
            + New job
          </a>
        </div>

        {error && (
          <div className="mb-4 bg-red-900/40 border border-red-700 rounded-lg px-4 py-3 text-sm text-red-300">
            {error}
          </div>
        )}

        {/* Table */}
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-zinc-800 text-zinc-400 text-left">
                <th className="pb-3 pr-4 font-medium">Job ID</th>
                <th className="pb-3 pr-4 font-medium">Status</th>
                <th className="pb-3 pr-4 font-medium">Template</th>
                <th className="pb-3 pr-4 font-medium">Created</th>
                <th className="pb-3 font-medium">Action</th>
              </tr>
            </thead>
            <tbody>
              {loading ? (
                Array.from({ length: 5 }).map((_, i) => (
                  <tr key={i} className="border-b border-zinc-900">
                    <td colSpan={5} className="py-3">
                      <div className="h-4 bg-zinc-900 rounded animate-pulse w-full" />
                    </td>
                  </tr>
                ))
              ) : jobs.length === 0 ? (
                <tr>
                  <td colSpan={5} className="py-12 text-center text-zinc-500">
                    No template jobs yet
                  </td>
                </tr>
              ) : (
                jobs.map((job) => (
                  <tr key={job.job_id} className="border-b border-zinc-900 hover:bg-zinc-900/50">
                    <td className="py-3 pr-4">
                      <span className="font-mono text-xs text-zinc-300">
                        {job.job_id.slice(0, 8)}...
                      </span>
                    </td>
                    <td className="py-3 pr-4">
                      <span className={`font-medium ${STATUS_COLORS[job.status] || "text-zinc-400"}`}>
                        {job.status.replace("_", " ")}
                      </span>
                    </td>
                    <td className="py-3 pr-4 text-zinc-400">
                      {job.template_id || "—"}
                    </td>
                    <td className="py-3 pr-4 text-zinc-500">
                      {new Date(job.created_at).toLocaleString()}
                    </td>
                    <td className="py-3">
                      <a
                        href={`/template-jobs/${job.job_id}`}
                        className="text-blue-400 hover:text-blue-300 text-xs"
                      >
                        View →
                      </a>
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>

        {/* Pagination */}
        {totalPages > 1 && (
          <div className="flex items-center justify-between mt-6">
            <button
              onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
              disabled={offset === 0}
              className={`px-3 py-1.5 rounded text-sm ${
                offset === 0
                  ? "bg-zinc-900 text-zinc-600 cursor-not-allowed"
                  : "bg-zinc-800 text-zinc-300 hover:bg-zinc-700"
              }`}
            >
              ← Previous
            </button>
            <span className="text-zinc-500 text-sm">
              Page {currentPage} of {totalPages}
            </span>
            <button
              onClick={() => setOffset(offset + PAGE_SIZE)}
              disabled={currentPage >= totalPages}
              className={`px-3 py-1.5 rounded text-sm ${
                currentPage >= totalPages
                  ? "bg-zinc-900 text-zinc-600 cursor-not-allowed"
                  : "bg-zinc-800 text-zinc-300 hover:bg-zinc-700"
              }`}
            >
              Next →
            </button>
          </div>
        )}

        <p className="mt-8 text-center text-xs text-zinc-600">
          <a href="/template" className="underline hover:text-zinc-400">← Back to templates</a>
        </p>
      </div>
    </main>
  );
}
