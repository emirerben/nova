"use client";

/**
 * Admin job debug detail view. Surfaces every agent's input/output/raw,
 * every non-LLM pipeline decision, every JSONB column on the Job row,
 * plus the rendered video. The point: when a video looks bad, scan this
 * page top-to-bottom and pinpoint whether the cause was an agent, the
 * agent's parameters, or the assembly stage.
 *
 * Backed by:
 *   GET /admin/jobs/{id}/debug
 * → src/apps/api/app/routes/admin_jobs.py
 */

import Link from "next/link";
import { useEffect, useState } from "react";

import { JsonTreeView } from "@/components/JsonTreeView";
import {
  adminGetJobDebug,
  type AgentRunPayload,
  type JobDebugResponse,
  type PipelineTraceEvent,
} from "@/lib/admin-jobs-api";

import { Timeline } from "./Timeline";

type Tab = "agents" | "timeline" | "recipe" | "trace" | "raw";

const TAB_LABEL: Record<Tab, string> = {
  agents: "Agents",
  timeline: "Timeline",
  recipe: "Recipe",
  trace: "Pipeline Trace",
  raw: "Raw Job",
};

const STAGE_COLOR: Record<string, string> = {
  interstitial: "bg-purple-600/70",
  transition: "bg-yellow-600/70",
  overlay: "bg-cyan-600/70",
  beat_snap: "bg-pink-600/70",
  reframe: "bg-emerald-600/70",
  audio_mix: "bg-blue-600/70",
  assembly: "bg-zinc-600/70",
};

const OUTCOME_BORDER: Record<string, string> = {
  ok: "border-emerald-700",
  ok_fallback: "border-amber-600",
  terminal_refusal: "border-red-700",
  terminal_schema: "border-red-700",
  terminal_transient: "border-red-700",
  terminal_unknown: "border-red-700",
  terminal_rule_based: "border-red-700",
};

export default function JobDebugPage({
  params,
}: {
  params: { id: string };
}): JSX.Element {
  const { id } = params;
  const [data, setData] = useState<JobDebugResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [tab, setTab] = useState<Tab>("agents");

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    adminGetJobDebug(id)
      .then((d) => {
        if (!cancelled) setData(d);
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
  }, [id]);

  return (
    <main className="min-h-screen bg-black text-white px-4 py-10">
      <div className="max-w-7xl mx-auto">
        <Link
          href="/admin/jobs"
          className="text-sm text-zinc-400 hover:text-white"
        >
          ← Jobs
        </Link>

        {loading && (
          <div className="mt-6 text-zinc-400 text-sm">Loading debug payload…</div>
        )}
        {error && (
          <div className="mt-6 rounded border border-red-800 bg-red-950/40 px-4 py-3 text-sm text-red-300">
            {error}
          </div>
        )}

        {data && (
          <>
            <Header data={data} />
            <Tabs tab={tab} onChange={setTab} />
            <div className="mt-6">
              {tab === "agents" && <AgentsTab data={data} />}
              {tab === "timeline" && <Timeline data={data} />}
              {tab === "recipe" && <RecipeTab data={data} />}
              {tab === "trace" && (
                <TraceTab events={(data.job.pipeline_trace ?? []) as PipelineTraceEvent[]} />
              )}
              {tab === "raw" && <RawTab data={data} />}
            </div>
          </>
        )}
      </div>
    </main>
  );
}

// ── Header ────────────────────────────────────────────────────────────────────

function Header({ data }: { data: JobDebugResponse }): JSX.Element {
  const { job, job_clips, template, music_track } = data;
  const finalVideo =
    job.job_type === "music"
      ? (job.assembly_plan as { output_url?: string } | null)?.output_url
      : job_clips.find((c) => c.render_status === "ready")?.video_path;
  return (
    <header className="mt-4 grid grid-cols-1 lg:grid-cols-3 gap-6">
      <section className="lg:col-span-2">
        <h1 className="text-xl font-bold mb-2">Job debug</h1>
        <div className="text-xs font-mono text-zinc-500 mb-3 break-all">{job.id}</div>
        <dl className="grid grid-cols-2 sm:grid-cols-4 gap-3 text-sm">
          <Field label="Type" value={job.job_type} />
          <Field label="Mode" value={job.mode ?? "—"} />
          <Field label="Status" value={job.status} />
          <Field label="Phase" value={job.current_phase ?? "—"} />
          <Field label="Template" value={template?.name ?? job.template_id ?? "—"} />
          <Field
            label="Music track"
            value={music_track ? `${music_track.title} — ${music_track.artist}` : job.music_track_id ?? "—"}
          />
          <Field label="Created" value={new Date(job.created_at).toLocaleString()} />
          <Field
            label="Finished"
            value={job.finished_at ? new Date(job.finished_at).toLocaleString() : "—"}
          />
        </dl>
        {job.failure_reason && (
          <div className="mt-3 rounded border border-red-800 bg-red-950/40 px-3 py-2 text-sm text-red-200">
            <div className="font-medium">Failure: {job.failure_reason}</div>
            {job.error_detail && (
              <div className="mt-1 text-xs text-red-300/80 whitespace-pre-wrap">
                {job.error_detail}
              </div>
            )}
          </div>
        )}
      </section>
      <section>
        <div className="text-xs uppercase tracking-wider text-zinc-500 mb-1">
          Output
        </div>
        {finalVideo ? (
          <video src={finalVideo} controls className="w-full max-h-[60vh] rounded" />
        ) : (
          <div className="rounded border border-dashed border-zinc-800 px-4 py-8 text-center text-sm text-zinc-500">
            No rendered output yet.
          </div>
        )}
      </section>
    </header>
  );
}

function Field({ label, value }: { label: string; value: string }): JSX.Element {
  return (
    <div>
      <dt className="text-xs uppercase tracking-wider text-zinc-500">{label}</dt>
      <dd className="text-zinc-200 truncate" title={value}>
        {value}
      </dd>
    </div>
  );
}

// ── Tabs ──────────────────────────────────────────────────────────────────────

function Tabs({ tab, onChange }: { tab: Tab; onChange: (t: Tab) => void }): JSX.Element {
  return (
    <nav className="mt-8 border-b border-zinc-800 flex flex-wrap gap-1">
      {(Object.keys(TAB_LABEL) as Tab[]).map((t) => (
        <button
          key={t}
          type="button"
          onClick={() => onChange(t)}
          className={`px-4 py-2 text-sm font-medium transition-colors ${
            tab === t
              ? "border-b-2 border-white text-white"
              : "text-zinc-400 hover:text-zinc-200"
          }`}
        >
          {TAB_LABEL[t]}
        </button>
      ))}
    </nav>
  );
}

// ── Agents tab ────────────────────────────────────────────────────────────────

function AgentsTab({ data }: { data: JobDebugResponse }): JSX.Element {
  const { agent_runs, template_agent_runs, track_agent_runs, template, music_track } = data;

  const total = agent_runs.length + template_agent_runs.length + track_agent_runs.length;
  if (total === 0) {
    return (
      <div className="rounded border border-zinc-800 px-4 py-8 text-center text-sm text-zinc-500">
        No agent runs captured for this job. Either the job pre-dates the
        agent_run capture feature, or no agents ran for this job type.
      </div>
    );
  }

  return (
    <div className="space-y-8">
      {template_agent_runs.length > 0 && (
        <AgentSection
          title="Template analysis"
          subtitle={
            template
              ? `Ran when the template ${template.name} was analyzed`
              : "Ran during template analysis"
          }
          link={
            template
              ? { href: `/admin/templates/${template.id}`, label: "open template" }
              : null
          }
          runs={template_agent_runs}
        />
      )}
      {track_agent_runs.length > 0 && (
        <AgentSection
          title="Music track analysis"
          subtitle={
            music_track
              ? `Ran when "${music_track.title}" — ${music_track.artist} was analyzed`
              : "Ran during music-track analysis"
          }
          link={
            music_track
              ? { href: `/admin/music/${music_track.id}`, label: "open track" }
              : null
          }
          runs={track_agent_runs}
        />
      )}
      <AgentSection
        title="Job-time agents"
        subtitle="Ran inside this job's Celery task"
        link={null}
        runs={agent_runs}
        emptyHint="No job-time agents fired — check that orchestration started."
      />
    </div>
  );
}

function AgentSection({
  title,
  subtitle,
  link,
  runs,
  emptyHint,
}: {
  title: string;
  subtitle: string;
  link: { href: string; label: string } | null;
  runs: AgentRunPayload[];
  emptyHint?: string;
}): JSX.Element {
  return (
    <section>
      <header className="flex flex-wrap items-baseline gap-x-3 gap-y-1 mb-3">
        <h2 className="text-sm font-semibold uppercase tracking-wider text-zinc-300">
          {title}
        </h2>
        <span className="text-xs text-zinc-500">
          {subtitle} · {runs.length} run{runs.length === 1 ? "" : "s"}
        </span>
        {link && (
          <Link
            href={link.href}
            className="text-xs text-zinc-400 hover:text-white underline-offset-2 hover:underline"
          >
            {link.label} →
          </Link>
        )}
      </header>
      {runs.length === 0 ? (
        <div className="rounded border border-dashed border-zinc-800 px-4 py-3 text-xs text-zinc-500">
          {emptyHint ?? "No runs in this section."}
        </div>
      ) : (
        <div className="space-y-3">
          {runs.map((run) => (
            <AgentRunPanel key={run.id} run={run} />
          ))}
        </div>
      )}
    </section>
  );
}

function AgentRunPanel({ run }: { run: AgentRunPayload }): JSX.Element {
  const [open, setOpen] = useState(false);
  const border = OUTCOME_BORDER[run.outcome] ?? "border-zinc-700";
  const failure = !run.outcome.startsWith("ok");

  return (
    <div className={`rounded border ${border} bg-zinc-950`}>
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="w-full text-left px-4 py-3 flex flex-wrap items-baseline gap-x-4 gap-y-1 hover:bg-zinc-900/60"
      >
        <span className="text-zinc-500 text-xs">{open ? "▾" : "▸"}</span>
        <span className="text-sm font-medium text-white">{run.agent_name}</span>
        {run.segment_idx !== null && (
          <span className="text-xs text-zinc-500">[clip {run.segment_idx}]</span>
        )}
        <span
          className={`text-xs px-2 py-0.5 rounded ${
            failure ? "bg-red-900/60 text-red-200" : "bg-emerald-900/60 text-emerald-200"
          }`}
        >
          {run.outcome}
        </span>
        <span className="text-xs text-zinc-500">
          {run.model} · v{run.prompt_version}
        </span>
        <span className="ml-auto text-xs text-zinc-500">
          {run.latency_ms ?? "—"} ms · {run.tokens_in ?? 0}↓ / {run.tokens_out ?? 0}↑ ·
          ${run.cost_usd?.toFixed(4) ?? "0.0000"} · attempts {run.attempts}
        </span>
      </button>
      {open && (
        <div className="border-t border-zinc-800 px-4 py-3 space-y-4 text-xs">
          {run.error_message && (
            <Section title="Error">
              <pre className="whitespace-pre-wrap text-red-300">{run.error_message}</pre>
            </Section>
          )}
          <Section title="Input">
            <JsonTreeView value={run.input_json} />
          </Section>
          <Section title="Output (parsed)">
            <JsonTreeView value={run.output_json} />
          </Section>
          {run.raw_text && (
            <Section title="Raw LLM response">
              <pre className="whitespace-pre-wrap break-all text-amber-200/80 max-h-96 overflow-auto rounded bg-black/40 p-3">
                {run.raw_text}
              </pre>
            </Section>
          )}
        </div>
      )}
    </div>
  );
}

function Section({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}): JSX.Element {
  return (
    <div>
      <div className="uppercase tracking-wider text-zinc-500 mb-2 text-[10px]">
        {title}
      </div>
      <div>{children}</div>
    </div>
  );
}

// ── Recipe / Assembly / Trace / Raw tabs ──────────────────────────────────────

function RecipeTab({ data }: { data: JobDebugResponse }): JSX.Element {
  const recipe =
    data.music_track?.recipe_cached ?? data.template?.recipe_cached ?? null;
  return (
    <div className="rounded border border-zinc-800 bg-zinc-950 px-4 py-3">
      <div className="text-xs uppercase tracking-wider text-zinc-500 mb-2">
        {data.music_track ? "Music recipe (cached)" : "Template recipe (cached)"}
      </div>
      {recipe ? (
        <JsonTreeView value={recipe} defaultDepth={3} />
      ) : (
        <div className="text-sm text-zinc-500">No recipe available.</div>
      )}
    </div>
  );
}

function TraceTab({ events }: { events: PipelineTraceEvent[] }): JSX.Element {
  if (events.length === 0) {
    return (
      <div className="rounded border border-zinc-800 px-4 py-8 text-center text-sm text-zinc-500">
        No pipeline events captured. Either this job pre-dates the trace
        feature, or no recorded decision points fired.
      </div>
    );
  }
  // Sort by ts to recover wall-clock order regardless of DB interleaving.
  const sorted = [...events].sort((a, b) => a.ts.localeCompare(b.ts));
  return (
    <div className="space-y-2">
      {sorted.map((ev, i) => {
        const color = STAGE_COLOR[ev.stage] ?? "bg-zinc-600/70";
        return (
          <div
            key={`${ev.ts}-${i}`}
            className="rounded border border-zinc-800 bg-zinc-950 px-3 py-2 flex flex-wrap items-baseline gap-x-3 gap-y-1 text-xs"
          >
            <span className={`px-2 py-0.5 rounded text-white ${color}`}>
              {ev.stage}
            </span>
            <span className="text-zinc-200 font-medium">{ev.event}</span>
            <span className="ml-auto text-zinc-500">
              {new Date(ev.ts).toLocaleTimeString()}
            </span>
            <div className="basis-full pl-1">
              <JsonTreeView value={ev.data} defaultDepth={1} />
            </div>
          </div>
        );
      })}
    </div>
  );
}

function RawTab({ data }: { data: JobDebugResponse }): JSX.Element {
  return (
    <div className="rounded border border-zinc-800 bg-zinc-950 px-4 py-3">
      <div className="text-xs uppercase tracking-wider text-zinc-500 mb-2">
        Full debug payload (Job + template + music + agent_runs)
      </div>
      <JsonTreeView value={data} defaultDepth={1} />
    </div>
  );
}
