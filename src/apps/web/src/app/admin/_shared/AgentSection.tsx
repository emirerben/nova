"use client";

/**
 * Sectioned list of agent_runs. Renders a header with title/subtitle/optional
 * external link, then one expandable panel per run showing input/output/raw.
 *
 * Shared between /admin/jobs/[id] (job-time + template + track sections) and
 * /admin/templates/[id]'s Debug tab.
 */

import Link from "next/link";
import { useState } from "react";

import { JsonTreeView } from "@/components/JsonTreeView";
import type { AgentRunPayload, AgentRunSummaryPayload } from "@/lib/admin-jobs-api";

const OUTCOME_BORDER: Record<string, string> = {
  ok: "border-emerald-700",
  ok_fallback: "border-amber-600",
  terminal_refusal: "border-red-700",
  terminal_schema: "border-red-700",
  terminal_transient: "border-red-700",
  terminal_unknown: "border-red-700",
  terminal_rule_based: "border-red-700",
};

export function AgentSection({
  title,
  subtitle,
  link,
  runs,
  emptyHint,
  ioMode = "full",
}: {
  title: string;
  subtitle: string;
  link: { href: string; label: string } | null;
  runs: Array<AgentRunPayload | AgentRunSummaryPayload>;
  emptyHint?: string;
  ioMode?: "full" | "summary";
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
            <AgentRunPanel key={run.id} run={run} ioMode={ioMode} />
          ))}
        </div>
      )}
    </section>
  );
}

function AgentRunPanel({
  run,
  ioMode,
}: {
  run: AgentRunPayload | AgentRunSummaryPayload;
  ioMode: "full" | "summary";
}): JSX.Element {
  const [open, setOpen] = useState(false);
  const border = OUTCOME_BORDER[run.outcome] ?? "border-zinc-700";
  const failure = !run.outcome.startsWith("ok");
  const canExpand = ioMode === "full";
  const headerClassName =
    "w-full text-left px-4 py-3 flex flex-wrap items-baseline gap-x-4 gap-y-1";
  const contents = (
    <>
      <span className="text-zinc-500 text-xs">
        {canExpand ? (open ? "▾" : "▸") : "·"}
      </span>
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
      {!canExpand && (
        <span
          className="text-[10px] uppercase tracking-wider text-zinc-500"
          title="Input, output, and raw LLM text are not loaded for context rows."
        >
          I/O not loaded
        </span>
      )}
      <span className="ml-auto text-xs text-zinc-500">
        {run.latency_ms ?? "—"} ms · {run.tokens_in ?? 0}↓ / {run.tokens_out ?? 0}↑ ·
        ${run.cost_usd?.toFixed(4) ?? "0.0000"} · attempts {run.attempts}
      </span>
    </>
  );

  return (
    <div className={`rounded border ${border} bg-zinc-950`}>
      {canExpand ? (
        <button
          type="button"
          onClick={() => setOpen((o) => !o)}
          className={`${headerClassName} hover:bg-zinc-900/60`}
        >
          {contents}
        </button>
      ) : (
        <div className={headerClassName}>{contents}</div>
      )}
      {canExpand && open && (
        <div className="border-t border-zinc-800 px-4 py-3 space-y-4 text-xs">
          {run.error_message && (
            <Section title="Error">
              <pre className="whitespace-pre-wrap text-red-300">{run.error_message}</pre>
            </Section>
          )}
          <Section title="Input">
            <JsonTreeView value={(run as AgentRunPayload).input_json} />
          </Section>
          <Section title="Output (parsed)">
            <JsonTreeView value={(run as AgentRunPayload).output_json} />
          </Section>
          {(run as AgentRunPayload).raw_text && (
            <Section title="Raw LLM response">
              <pre className="whitespace-pre-wrap break-all text-amber-200/80 max-h-96 overflow-auto rounded bg-black/40 p-3">
                {(run as AgentRunPayload).raw_text}
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
