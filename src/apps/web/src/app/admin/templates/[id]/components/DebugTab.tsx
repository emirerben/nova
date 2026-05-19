"use client";

/**
 * Template debug view. Surfaces the agent_runs that shaped the template's
 * analysis (template_recipe, creative_direction, song_classifier,
 * song_sections, music_matcher, beat_aligner) outside of any specific job.
 *
 * Sibling to /admin/jobs/[id]'s Agents tab — same AgentSection component,
 * different data source (agent_run.template_id instead of agent_run.job_id).
 *
 * Backed by: GET /admin/templates/{id}/debug
 * → src/apps/api/app/routes/admin.py::get_template_debug
 */

import { useEffect, useState } from "react";

import { AgentSection } from "@/app/admin/_shared/AgentSection";
import { JsonTreeView } from "@/components/JsonTreeView";
import {
  type TemplateDebugResponse,
  adminGetTemplateDebug,
} from "@/lib/admin-api";

export function DebugTab({ templateId }: { templateId: string }): JSX.Element {
  const [data, setData] = useState<TemplateDebugResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    adminGetTemplateDebug(templateId)
      .then((r) => {
        if (!cancelled) setData(r);
      })
      .catch((e: Error) => {
        if (!cancelled) setError(e.message);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [templateId]);

  if (loading) {
    return (
      <div className="text-sm text-zinc-500">Loading template debug…</div>
    );
  }
  if (error) {
    return (
      <div className="rounded border border-red-800 bg-red-950/40 px-4 py-3 text-sm text-red-300">
        Failed to load debug payload: {error}
      </div>
    );
  }
  if (!data) {
    return <div className="text-sm text-zinc-500">No data.</div>;
  }

  const { template, template_agent_runs, recipe_cached } = data;
  const isFailed = template.analysis_status === "failed";
  const isAnalyzing = template.analysis_status === "analyzing";

  return (
    <div className="space-y-8 max-w-5xl">
      <header className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <h2 className="text-base font-semibold text-white">Template analysis</h2>
        <span
          className={`text-xs px-2 py-0.5 rounded ${
            isFailed
              ? "bg-red-900/60 text-red-200"
              : isAnalyzing
                ? "bg-amber-900/60 text-amber-200"
                : "bg-emerald-900/60 text-emerald-200"
          }`}
        >
          {template.analysis_status}
        </span>
        {template.is_agentic && (
          <span className="text-xs bg-emerald-900/40 text-emerald-300 border border-emerald-800/60 px-2 py-0.5 rounded">
            Agentic
          </span>
        )}
        <span className="text-xs text-zinc-500">
          {template.template_type} · {template_agent_runs.length} run
          {template_agent_runs.length === 1 ? "" : "s"}
        </span>
      </header>

      {isFailed && template.error_detail && (
        <div className="rounded border border-red-800 bg-red-950/40 px-4 py-3">
          <div className="text-[10px] uppercase tracking-wider text-red-400 mb-1">
            Error detail
          </div>
          <pre className="whitespace-pre-wrap text-xs text-red-200">
            {template.error_detail}
          </pre>
        </div>
      )}

      <AgentSection
        title="Agents that shaped this template"
        subtitle="Runs persisted via agent_run.template_id"
        link={null}
        runs={template_agent_runs}
        emptyHint={
          isAnalyzing
            ? "Analysis is still running — refresh in a few seconds."
            : "No agent runs recorded. This template may have been created before agent_run.template_id existed, or analysis hasn't started."
        }
      />

      <details className="rounded border border-zinc-800 bg-zinc-950 px-4 py-3 text-xs">
        <summary className="cursor-pointer text-zinc-400 hover:text-white">
          Raw template metadata
        </summary>
        <div className="mt-3 space-y-3">
          <Field label="id" value={template.id} />
          <Field label="gcs_path" value={template.gcs_path ?? "—"} />
          <Field label="audio_gcs_path" value={template.audio_gcs_path ?? "—"} />
          <Field label="music_track_id" value={template.music_track_id ?? "—"} />
          <Field
            label="recipe_cached_at"
            value={template.recipe_cached_at ?? "—"}
          />
          <Field label="created_at" value={template.created_at} />
        </div>
      </details>

      <details className="rounded border border-zinc-800 bg-zinc-950 px-4 py-3 text-xs">
        <summary className="cursor-pointer text-zinc-400 hover:text-white">
          Cached recipe JSON
        </summary>
        <div className="mt-3">
          {recipe_cached ? (
            <JsonTreeView value={recipe_cached} />
          ) : (
            <span className="text-zinc-500">No recipe cached yet.</span>
          )}
        </div>
      </details>
    </div>
  );
}

function Field({ label, value }: { label: string; value: string }): JSX.Element {
  return (
    <div className="flex gap-3">
      <div className="w-32 text-zinc-500">{label}</div>
      <div className="text-zinc-200 font-mono break-all">{value}</div>
    </div>
  );
}
