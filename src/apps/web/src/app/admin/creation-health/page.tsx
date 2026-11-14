"use client";

/**
 * /admin/creation-health: daily success rate and top failure reasons for creations (KRI-1382).
 *
 * Read-only. Two stages are shown side by side and never added together: a creation is a chat
 * turn that plans a draft, then a render Job. The definitions the numbers follow come from the
 * API response and are printed under the tables.
 */

import { useEffect, useState } from "react";

import {
  adminCreationHealth,
  type CreationHealth,
  type StageHealth,
} from "@/lib/admin-creation-health-api";

const WINDOWS = [7, 14, 30] as const;

function percent(rate: number | null): string {
  return rate === null ? "-" : `${(rate * 100).toFixed(1)}%`;
}

function breakdown(stage: StageHealth): string {
  const parts = (["refused", "blocked", "recovery", "failed"] as const)
    .filter((key) => (stage[key] ?? 0) > 0)
    .map((key) => `${stage[key]} ${key}`);
  return parts.length ? parts.join(", ") : "none";
}

function StageTile({ title, stage }: { title: string; stage: StageHealth }) {
  return (
    <section className="rounded-lg bg-zinc-900 p-4">
      <h2 className="text-sm text-zinc-400">{title}</h2>
      <p className="mt-1 text-3xl font-semibold">{percent(stage.success_rate)}</p>
      <p className="mt-1 text-sm text-zinc-400">
        {stage.successes} of {stage.attempts} attempts succeeded
      </p>
      <p className="text-xs text-zinc-500">
        Not successful: {breakdown(stage)}. In progress: {stage.in_progress}.
      </p>
    </section>
  );
}

export default function AdminCreationHealthPage() {
  const [days, setDays] = useState<number>(7);
  const [data, setData] = useState<CreationHealth | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    adminCreationHealth(days)
      .then((result) => {
        if (cancelled) return;
        setData(result);
        setError(null);
      })
      .catch((err: Error) => {
        if (!cancelled) setError(err.message);
      });
    return () => {
      cancelled = true;
    };
  }, [days]);

  return (
    <main className="min-h-screen bg-black px-4 py-10 text-white">
      <div className="mx-auto max-w-4xl space-y-8">
        <header className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <h1 className="text-2xl font-bold">Creation health</h1>
            <p className="mt-1 text-sm text-zinc-400">
              Success rate and top failure reasons, per UTC day.
            </p>
          </div>
          <div className="flex gap-2" role="group" aria-label="Window">
            {WINDOWS.map((option) => (
              <button
                key={option}
                type="button"
                onClick={() => setDays(option)}
                aria-pressed={days === option}
                className={`rounded-full px-3 py-1 text-sm ${
                  days === option ? "bg-white text-black" : "bg-zinc-800 text-zinc-300"
                }`}
              >
                {option} days
              </button>
            ))}
          </div>
        </header>

        {error && (
          <p role="alert" className="rounded-lg bg-red-950 p-3 text-sm text-red-300">
            {error}
          </p>
        )}
        {!data && !error && <p className="text-sm text-zinc-500">Loading...</p>}

        {data && (
          <>
            <div className="grid gap-4 sm:grid-cols-2">
              <StageTile title="Chat turns" stage={data.turns} />
              <StageTile title="Renders" stage={data.renders} />
            </div>

            <section>
              <h2 className="mb-2 text-lg font-semibold">Top failure reasons</h2>
              {data.top_failure_reasons.length === 0 ? (
                <p className="text-sm text-zinc-500">No failures in this window.</p>
              ) : (
                <table className="w-full text-left text-sm">
                  <thead className="text-zinc-500">
                    <tr>
                      <th className="py-1 pr-3 font-normal">Stage</th>
                      <th className="py-1 pr-3 font-normal">Kind</th>
                      <th className="py-1 pr-3 font-normal">Reason</th>
                      <th className="py-1 pr-3 text-right font-normal">Count</th>
                      <th className="py-1 text-right font-normal">Share</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.top_failure_reasons.map((reason) => (
                      <tr key={`${reason.source}-${reason.kind}-${reason.code}`}>
                        <td className="py-1 pr-3">{reason.source}</td>
                        <td className="py-1 pr-3">{reason.kind}</td>
                        <td className="py-1 pr-3">
                          <code className="text-zinc-300">{reason.code}</code>
                          {reason.label && (
                            <span className="block text-xs text-zinc-500">{reason.label}</span>
                          )}
                        </td>
                        <td className="py-1 pr-3 text-right tabular-nums">{reason.count}</td>
                        <td className="py-1 text-right tabular-nums">
                          {(reason.share * 100).toFixed(0)}%
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </section>

            <section>
              <h2 className="mb-2 text-lg font-semibold">By day</h2>
              <table className="w-full text-left text-sm">
                <thead className="text-zinc-500">
                  <tr>
                    <th className="py-1 pr-3 font-normal">Day</th>
                    <th className="py-1 pr-3 text-right font-normal">Turns</th>
                    <th className="py-1 pr-3 text-right font-normal">Turn rate</th>
                    <th className="py-1 pr-3 text-right font-normal">Renders</th>
                    <th className="py-1 text-right font-normal">Render rate</th>
                  </tr>
                </thead>
                <tbody>
                  {[...data.daily].reverse().map((day) => (
                    <tr key={day.date}>
                      <td className="py-1 pr-3">{day.date}</td>
                      <td className="py-1 pr-3 text-right tabular-nums">{day.turns.attempts}</td>
                      <td className="py-1 pr-3 text-right tabular-nums">
                        {percent(day.turns.success_rate)}
                      </td>
                      <td className="py-1 pr-3 text-right tabular-nums">{day.renders.attempts}</td>
                      <td className="py-1 text-right tabular-nums">
                        {percent(day.renders.success_rate)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </section>

            <details className="text-sm text-zinc-400">
              <summary className="cursor-pointer text-zinc-300">What the numbers count</summary>
              <dl className="mt-2 space-y-2">
                {Object.entries(data.definitions).map(([key, text]) => (
                  <div key={key}>
                    <dt className="text-zinc-300">{key}</dt>
                    <dd>{text}</dd>
                  </div>
                ))}
              </dl>
            </details>
          </>
        )}
      </div>
    </main>
  );
}
