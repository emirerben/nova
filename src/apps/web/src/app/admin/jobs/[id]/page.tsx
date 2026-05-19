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

import { AgentSection } from "@/app/admin/_shared/AgentSection";
import { JsonTreeView } from "@/components/JsonTreeView";
import {
  adminCancelJob,
  adminGetJobDebug,
  type JobDebugResponse,
  type JobRuntimePayload,
  type PipelineTraceEvent,
} from "@/lib/admin-jobs-api";

import { Timeline } from "./Timeline";

// Status values eligible for cancellation.
//
// Mirror of _CANCELLABLE_STATUSES in
// src/apps/api/app/routes/admin_jobs.py. Update both when adding or
// removing a status. If these drift, the Cancel button renders for a
// status the backend rejects with 409 (or hides for a status it would
// accept) — operator confusion either way.
const CANCELLABLE_STATUSES: ReadonlySet<string> = new Set([
  "queued",
  "processing",
  "matching",
  "rendering",
  "posting",
]);

// Detail page polls runtime more aggressively than the list page because
// you opened it to watch one specific job.
const RUNTIME_POLL_MS = 5_000;

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
  orientation: "bg-orange-600/70",
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

  const refetch = (): Promise<void> =>
    adminGetJobDebug(id)
      .then((d) => setData(d))
      .catch((err: Error) => setError(err.message));

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

  // Poll runtime while the job is still cancellable. Stops automatically
  // when status becomes terminal so we don't waste broker calls.
  //
  // Dep is `data?.job.status` (not `data`) so the interval is only
  // reset when status actually transitions — not on every successful
  // poll. Skipping the new fetch when one is already in-flight avoids
  // pile-up if `inspect()` is slow (5s broker timeout in queue_state.py).
  const status = data?.job.status;
  useEffect(() => {
    if (!status || !CANCELLABLE_STATUSES.has(status)) return;
    let cancelled = false;
    let inFlight = false;
    const t = setInterval(() => {
      if (cancelled || inFlight) return;
      inFlight = true;
      adminGetJobDebug(id)
        .then((d) => {
          if (!cancelled) setData(d);
        })
        .catch(() => {
          // Swallow — the detail render still has the last-good data.
        })
        .finally(() => {
          inFlight = false;
        });
    }, RUNTIME_POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(t);
    };
  }, [id, status]);

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
            <WorkerStatePanel
              runtime={data.runtime}
              status={data.job.status}
              jobId={id}
              onCancelled={refetch}
            />
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

// ── Worker state panel + Cancel button ──────────────────────────────────────

/**
 * Live Celery state for this job, plus the Cancel control.
 *
 * The state rendering is load-bearing: 'NOT FOUND' vs 'UNKNOWN' is the
 * difference between "worker died, this job is dead in the water" and
 * "we couldn't ask the broker, don't make decisions". A misread here
 * leads an operator to cancel a healthy job.
 */
function WorkerStatePanel({
  runtime,
  status,
  jobId,
  onCancelled,
}: {
  runtime: JobRuntimePayload;
  status: string;
  jobId: string;
  onCancelled: () => Promise<void>;
}): JSX.Element {
  const cancellable = CANCELLABLE_STATUSES.has(status);
  const isTerminal = !cancellable;
  return (
    <section className="mt-6 rounded-lg border border-zinc-800 bg-zinc-950 px-4 py-3">
      <div className="flex flex-wrap items-center gap-x-6 gap-y-2">
        <div className="text-xs uppercase tracking-wider text-zinc-500">
          Worker state
        </div>
        <StateChip state={runtime.state} terminal={isTerminal} status={status} />
        {runtime.worker && (
          <span className="text-xs text-zinc-400">
            on <span className="font-mono text-zinc-200">{runtime.worker}</span>
          </span>
        )}
        {runtime.queue_position !== null && runtime.queue_position !== undefined && (
          <span className="text-xs text-zinc-400">
            queue pos:{" "}
            <span className="text-zinc-200 font-medium">
              {runtime.queue_position}
            </span>
          </span>
        )}
        {runtime.task_id && (
          <span className="text-xs text-zinc-500">
            task_id:{" "}
            <span className="font-mono text-zinc-400">
              {runtime.task_id.slice(0, 8)}
            </span>
          </span>
        )}
        <div className="ml-auto">
          {cancellable && (
            <CancelButton jobId={jobId} runtime={runtime} onCancelled={onCancelled} />
          )}
        </div>
      </div>
      {runtime.state === "not_found" && cancellable && (
        <div className="mt-2 text-xs text-red-300/80">
          Worker did not report this task. It probably died mid-task. The
          reaper sweeps every ~5 min; cancel here to clear immediately.
        </div>
      )}
      {runtime.state === "unknown" && (
        <div className="mt-2 text-xs text-zinc-400">
          Broker unreachable. The task may still be running — don&apos;t cancel
          until you&apos;ve confirmed via fly ssh or until the broker recovers.
        </div>
      )}
    </section>
  );
}

function StateChip({
  state,
  terminal,
  status,
}: {
  state: JobRuntimePayload["state"];
  terminal: boolean;
  status: string;
}): JSX.Element {
  // For terminal rows we show the DB status — runtime state isn't
  // meaningful anymore (no worker is asked about a finished job).
  if (terminal) {
    return (
      <span className="px-2 py-0.5 rounded bg-zinc-800 text-zinc-300 text-xs font-medium">
        {status.toUpperCase()}
      </span>
    );
  }
  const style: Record<JobRuntimePayload["state"], string> = {
    active: "bg-green-900/70 text-green-300",
    reserved: "bg-yellow-900/70 text-yellow-300",
    not_found: "bg-red-900/70 text-red-300",
    unknown: "bg-zinc-800 text-zinc-400",
  };
  const label: Record<JobRuntimePayload["state"], string> = {
    active: "ACTIVE",
    reserved: "RESERVED",
    not_found: "NOT FOUND",
    unknown: "UNKNOWN",
  };
  return (
    <span
      className={`px-2 py-0.5 rounded text-xs font-medium ${style[state]}`}
    >
      {label[state]}
    </span>
  );
}

function CancelButton({
  jobId,
  runtime,
  onCancelled,
}: {
  jobId: string;
  runtime: JobRuntimePayload;
  onCancelled: () => Promise<void>;
}): JSX.Element {
  const [confirming, setConfirming] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  if (confirming) {
    return (
      <div className="flex items-center gap-2">
        {err && <span className="text-xs text-red-400">{err}</span>}
        <button
          type="button"
          disabled={submitting}
          onClick={() => {
            setSubmitting(true);
            setErr(null);
            adminCancelJob(jobId)
              .then(() => onCancelled())
              .then(() => {
                setConfirming(false);
              })
              .catch((e: Error) => setErr(e.message))
              .finally(() => setSubmitting(false));
          }}
          className="px-3 py-1 rounded bg-red-700 hover:bg-red-600 text-white text-xs font-medium disabled:opacity-50"
        >
          {submitting
            ? "Cancelling…"
            : runtime.state === "active"
              ? "Yes, terminate task"
              : "Yes, cancel job"}
        </button>
        <button
          type="button"
          disabled={submitting}
          onClick={() => {
            setConfirming(false);
            setErr(null);
          }}
          className="px-3 py-1 rounded bg-zinc-800 hover:bg-zinc-700 text-zinc-300 text-xs"
        >
          Keep running
        </button>
      </div>
    );
  }

  return (
    <button
      type="button"
      onClick={() => setConfirming(true)}
      className="px-3 py-1 rounded bg-zinc-800 hover:bg-red-900 text-red-300 text-xs font-medium border border-red-900/60"
    >
      Cancel job
    </button>
  );
}
