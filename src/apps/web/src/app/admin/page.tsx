"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import {
  type AdminTemplateListItem,
  adminListTemplates,
} from "@/lib/admin-api";

/**
 * Admin dashboard: attention queue (templates needing action) + full template table.
 */
type AgenticFilter = "all" | "manual" | "agentic";

export default function AdminDashboardPage() {
  const [templates, setTemplates] = useState<AdminTemplateListItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [agenticFilter, setAgenticFilter] = useState<AgenticFilter>("all");

  useEffect(() => {
    adminListTemplates(100, 0)
      .then((r) => setTemplates(r.templates))
      .catch((err) => setError(err.message))
      .finally(() => setLoading(false));
  }, []);

  const filteredTemplates = useMemo(() => {
    if (agenticFilter === "all") return templates;
    if (agenticFilter === "agentic") return templates.filter((t) => t.is_agentic);
    return templates.filter((t) => !t.is_agentic);
  }, [templates, agenticFilter]);

  // Attention queue: templates that need action
  const attention = useMemo(() => {
    const failed = templates.filter((t) => t.analysis_status === "failed");
    const neverTested = templates.filter(
      (t) => t.analysis_status === "ready" && t.job_count === 0 && !t.published_at,
    );
    const readyToPublish = templates.filter(
      (t) => t.analysis_status === "ready" && t.job_count > 0 && !t.published_at && !t.archived_at,
    );
    const analyzing = templates.filter((t) => t.analysis_status === "analyzing");
    return { failed, neverTested, readyToPublish, analyzing };
  }, [templates]);

  if (loading) {
    return (
      <div className="p-8 space-y-4 animate-pulse">
        <div className="h-6 w-40 bg-zinc-800 rounded" />
        <div className="grid grid-cols-4 gap-4">
          {[1, 2, 3, 4].map((i) => (
            <div key={i} className="h-20 bg-zinc-800 rounded" />
          ))}
        </div>
        <div className="h-64 bg-zinc-800 rounded" />
      </div>
    );
  }

  if (error) {
    return (
      <div className="p-8">
        <p className="text-red-400">{error}</p>
      </div>
    );
  }

  const hasAttention =
    attention.failed.length > 0 ||
    attention.neverTested.length > 0 ||
    attention.readyToPublish.length > 0 ||
    attention.analyzing.length > 0;

  return (
    <div className="p-6 space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-semibold">Template Dashboard</h1>
        <Link
          href="/admin/jobs"
          className="text-sm px-3 py-1.5 rounded bg-zinc-800 text-zinc-200 hover:bg-zinc-700"
        >
          Debug jobs →
        </Link>
      </div>

      {/* Attention queue */}
      {hasAttention ? (
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
          <AttentionCard
            label="Analysis Failed"
            count={attention.failed.length}
            color="red"
            items={attention.failed}
          />
          <AttentionCard
            label="Never Tested"
            count={attention.neverTested.length}
            color="amber"
            items={attention.neverTested}
          />
          <AttentionCard
            label="Ready to Publish"
            count={attention.readyToPublish.length}
            color="green"
            items={attention.readyToPublish}
          />
          <AttentionCard
            label="Analyzing"
            count={attention.analyzing.length}
            color="blue"
            items={attention.analyzing}
          />
        </div>
      ) : (
        <div className="bg-zinc-900 border border-zinc-800 rounded p-4 text-center">
          <p className="text-zinc-400 text-sm">All templates healthy</p>
        </div>
      )}

      {/* Full template table */}
      {templates.length === 0 ? (
        <div className="bg-zinc-900 border border-zinc-800 rounded p-8 text-center">
          <p className="text-zinc-400 mb-3">No templates yet.</p>
          <Link
            href="/admin/templates/new"
            className="inline-block px-4 py-2 text-sm bg-white text-black rounded hover:bg-zinc-200"
          >
            Upload one
          </Link>
        </div>
      ) : (
        <div className="space-y-3">
          <AgenticFilterChips value={agenticFilter} onChange={setAgenticFilter} />

          <div className="border border-zinc-800 rounded overflow-hidden">
            <table className="w-full text-sm">
              <thead className="bg-zinc-900">
                <tr>
                  <th className="text-left px-4 py-2.5 text-zinc-500 font-medium">Name</th>
                  <th className="text-left px-4 py-2.5 text-zinc-500 font-medium">Status</th>
                  <th className="text-left px-4 py-2.5 text-zinc-500 font-medium">Published</th>
                  <th className="text-left px-4 py-2.5 text-zinc-500 font-medium">Jobs</th>
                  <th className="text-left px-4 py-2.5 text-zinc-500 font-medium">Created</th>
                </tr>
              </thead>
              <tbody>
                {filteredTemplates.map((t) => (
                  <tr key={t.id} className="border-t border-zinc-800 hover:bg-zinc-900/50">
                    <td className="px-4 py-2.5">
                      <div className="flex items-center gap-2">
                        <Link
                          href={`/admin/templates/${t.id}?tab=recipe`}
                          className="text-white hover:text-blue-400"
                        >
                          {t.name}
                        </Link>
                        {t.is_agentic && <AgenticBadge />}
                      </div>
                      {t.description && (
                        <p className="text-xs text-zinc-600 truncate max-w-[200px]">{t.description}</p>
                      )}
                    </td>
                    <td className="px-4 py-2.5">
                      <StatusBadge status={t.analysis_status} />
                    </td>
                    <td className="px-4 py-2.5">
                      {t.archived_at ? (
                        <span className="text-xs text-zinc-600">Archived</span>
                      ) : t.published_at ? (
                        <span className="text-xs text-green-400">Yes</span>
                      ) : (
                        <span className="text-xs text-zinc-600">No</span>
                      )}
                    </td>
                    <td className="px-4 py-2.5 text-zinc-400">{t.job_count}</td>
                    <td className="px-4 py-2.5 text-zinc-500 text-xs">
                      {new Date(t.created_at).toLocaleDateString()}
                    </td>
                  </tr>
                ))}
                {filteredTemplates.length === 0 && (
                  <tr>
                    <td colSpan={5} className="px-4 py-6 text-center text-xs text-zinc-600">
                      No templates match this filter.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}

// ── Components ─────────────────────────────────────────────────────────────────

function AttentionCard({
  label,
  count,
  color,
  items,
}: {
  label: string;
  count: number;
  color: "red" | "amber" | "green" | "blue";
  items: AdminTemplateListItem[];
}) {
  if (count === 0) return null;

  const colorMap = {
    red: "border-red-900/60 bg-red-950/30",
    amber: "border-amber-900/60 bg-amber-950/30",
    green: "border-green-900/60 bg-green-950/30",
    blue: "border-blue-900/60 bg-blue-950/30",
  };

  const textMap = {
    red: "text-red-400",
    amber: "text-amber-400",
    green: "text-green-400",
    blue: "text-blue-400",
  };

  return (
    <div className={`border rounded p-3 ${colorMap[color]}`}>
      <p className={`text-xs ${textMap[color]}`}>{label}</p>
      <p className="text-2xl font-semibold text-white mt-1">{count}</p>
      <div className="mt-2 space-y-1">
        {items.slice(0, 3).map((t) => (
          <Link
            key={t.id}
            href={`/admin/templates/${t.id}?tab=recipe`}
            className="block text-xs text-zinc-400 hover:text-white truncate"
          >
            {t.name}
          </Link>
        ))}
        {items.length > 3 && (
          <p className="text-xs text-zinc-600">+{items.length - 3} more</p>
        )}
      </div>
    </div>
  );
}

function StatusBadge({ status }: { status: string }) {
  const colors: Record<string, string> = {
    analyzing: "bg-amber-900/40 text-amber-400",
    ready: "bg-green-900/40 text-green-400",
    failed: "bg-red-900/40 text-red-400",
  };
  return (
    <span className={`text-xs px-2 py-0.5 rounded ${colors[status] ?? "bg-zinc-700 text-zinc-300"}`}>
      {status}
    </span>
  );
}

function AgenticBadge() {
  return (
    <span
      className="text-[10px] uppercase tracking-wide px-1.5 py-0.5 rounded bg-emerald-900/40 text-emerald-300 border border-emerald-800/60"
      title="Recipe is generated by agents — overlay editor is locked"
    >
      Agentic
    </span>
  );
}

function AgenticFilterChips({
  value,
  onChange,
}: {
  value: AgenticFilter;
  onChange: (v: AgenticFilter) => void;
}) {
  const options: { key: AgenticFilter; label: string }[] = [
    { key: "all", label: "All" },
    { key: "manual", label: "Manual" },
    { key: "agentic", label: "Agentic" },
  ];
  return (
    <div className="flex items-center gap-2 text-xs">
      <span className="text-zinc-600">Filter:</span>
      {options.map((opt) => (
        <button
          key={opt.key}
          type="button"
          onClick={() => onChange(opt.key)}
          className={`px-2.5 py-1 rounded border transition-colors ${
            value === opt.key
              ? "bg-zinc-100 text-black border-zinc-100"
              : "bg-transparent text-zinc-400 border-zinc-700 hover:border-zinc-500"
          }`}
        >
          {opt.label}
        </button>
      ))}
    </div>
  );
}
