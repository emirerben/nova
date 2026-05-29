"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import {
  type ContentPlan,
  createContentPlan,
  getContentPlan,
  NotAuthenticatedError,
  type PlanItem,
  type PlanItemStatus,
  updatePlanItem,
} from "@/lib/plan-api";

const POLL_MS = 2000;

export default function PlanPage() {
  const [plan, setPlan] = useState<ContentPlan | null>(null);
  const [loading, setLoading] = useState(true);
  const [needsAuth, setNeedsAuth] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [events, setEvents] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const pollRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const load = useCallback(async () => {
    try {
      const p = await getContentPlan();
      setPlan(p);
      return p;
    } catch (err) {
      if (err instanceof NotAuthenticatedError) setNeedsAuth(true);
      else setError(err instanceof Error ? err.message : "Failed to load plan");
      return null;
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    async function tick() {
      const p = await load();
      if (cancelled) return;
      if (p?.plan_status === "generating") pollRef.current = setTimeout(tick, POLL_MS);
    }
    void tick();
    return () => {
      cancelled = true;
      if (pollRef.current) clearTimeout(pollRef.current);
    };
  }, [load]);

  async function handleGenerate() {
    setSubmitting(true);
    setError(null);
    try {
      const p = await createContentPlan(events);
      setPlan(p);
      // Kick off polling for the async generation.
      pollRef.current = setTimeout(async function tick() {
        const next = await load();
        if (next?.plan_status === "generating") pollRef.current = setTimeout(tick, POLL_MS);
      }, POLL_MS);
    } catch (err) {
      if (err instanceof NotAuthenticatedError) setNeedsAuth(true);
      else setError(err instanceof Error ? err.message : "Failed to start plan");
    } finally {
      setSubmitting(false);
    }
  }

  if (needsAuth) {
    return (
      <Shell>
        <Centered>
          <h1 className="mb-3 text-2xl font-semibold">Sign in to build your plan</h1>
          <a
            href="/api/auth/signin"
            className="inline-block rounded bg-white px-6 py-3 font-medium text-black hover:bg-zinc-200"
          >
            Sign in with Google
          </a>
        </Centered>
      </Shell>
    );
  }

  if (loading) {
    return (
      <Shell>
        <p className="py-20 text-center text-zinc-400">Loading…</p>
      </Shell>
    );
  }

  // No plan yet (or a failed one): show the events step.
  if (plan === null || plan.plan_status === "failed") {
    return (
      <Shell>
        <div className="py-12">
          <h1 className="mb-2 text-2xl font-semibold">Plan your next 30 days</h1>
          <p className="mb-6 text-zinc-400">
            Anything coming up we should lean into? Trips, launches, exams, events — optional, but it
            makes the plan feel like yours.
          </p>
          {plan?.plan_status === "failed" && (
            <div className="mb-6 rounded border border-amber-700 bg-amber-950/40 px-4 py-3 text-amber-200">
              Last generation didn&apos;t finish. Try again.
            </div>
          )}
          {error && (
            <div className="mb-6 rounded border border-red-700 bg-red-950/50 px-4 py-3 text-red-200">
              {error}
            </div>
          )}
          <p className="mb-2 text-sm text-zinc-400">
            Need a persona first?{" "}
            <Link href="/plan/persona" className="underline hover:text-white">
              Review it here
            </Link>
            .
          </p>
          <textarea
            value={events}
            onChange={(e) => setEvents(e.target.value)}
            rows={4}
            placeholder="e.g. moving apartments in week 2, gym comp at the end of the month"
            className="w-full resize-y rounded border border-zinc-700 bg-zinc-900 px-3 py-2 text-white placeholder-zinc-600 focus:border-zinc-400 focus:outline-none"
          />
          <button
            onClick={handleGenerate}
            disabled={submitting}
            className="mt-4 rounded bg-white px-6 py-3 font-medium text-black hover:bg-zinc-200 disabled:bg-zinc-700 disabled:text-zinc-400"
          >
            {submitting ? "Starting…" : "Generate my 30-day plan"}
          </button>
        </div>
      </Shell>
    );
  }

  if (plan.plan_status === "generating") {
    return (
      <Shell>
        <Centered>
          <h1 className="mb-3 text-2xl font-semibold">Building your 30-day plan…</h1>
          <p className="text-zinc-400">This takes a few seconds. The page will update itself.</p>
        </Centered>
      </Shell>
    );
  }

  // Ready/edited — render the calendar grouped by week.
  const weeks = groupByWeek(plan.items);
  return (
    <Shell>
      <div className="py-12">
        <h1 className="mb-1 text-2xl font-semibold">Your 30-day plan</h1>
        <p className="mb-8 text-zinc-400">
          Edit any idea. Week 1 is your activation week — film those first.
        </p>
        {error && (
          <div className="mb-6 rounded border border-red-700 bg-red-950/50 px-4 py-3 text-red-200">
            {error}
          </div>
        )}
        {weeks.map(({ week, items }) => (
          <section key={week} className="mb-10">
            <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-zinc-500">
              Week {week}
              {week === 1 && <span className="ml-2 text-amber-400">· activation</span>}
            </h2>
            <div className="space-y-3">
              {items.map((item) => (
                <PlanItemCard key={item.id} item={item} onError={setError} />
              ))}
            </div>
          </section>
        ))}
      </div>
    </Shell>
  );
}

function PlanItemCard({
  item,
  onError,
}: {
  item: PlanItem;
  onError: (msg: string) => void;
}) {
  const [theme, setTheme] = useState(item.theme);
  const [idea, setIdea] = useState(item.idea);
  const [filming, setFilming] = useState(item.filming_suggestion ?? "");
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);

  const dirty =
    theme !== item.theme ||
    idea !== item.idea ||
    filming !== (item.filming_suggestion ?? "");

  async function save() {
    setSaving(true);
    setSaved(false);
    try {
      await updatePlanItem(item.id, { theme, idea, filming_suggestion: filming });
      setSaved(true);
    } catch (err) {
      onError(err instanceof Error ? err.message : "Failed to save item");
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="rounded-lg border border-zinc-800 bg-zinc-900/60 p-4">
      <div className="mb-2 flex items-center gap-3">
        <span className="rounded bg-zinc-800 px-2 py-0.5 text-xs text-zinc-400">
          Day {item.day_index}
        </span>
        <input
          value={theme}
          onChange={(e) => setTheme(e.target.value)}
          className="flex-1 bg-transparent text-sm font-medium text-zinc-200 focus:outline-none"
        />
        <ItemStatusBadge status={item.status} />
      </div>
      <textarea
        value={idea}
        onChange={(e) => setIdea(e.target.value)}
        rows={2}
        className="mb-2 w-full resize-y rounded border border-zinc-800 bg-zinc-950 px-3 py-2 text-sm text-white focus:border-zinc-600 focus:outline-none"
      />
      <input
        value={filming}
        onChange={(e) => setFilming(e.target.value)}
        placeholder="filming tip"
        className="w-full rounded border border-zinc-800 bg-zinc-950 px-3 py-1.5 text-xs text-zinc-400 focus:border-zinc-600 focus:outline-none"
      />
      {(dirty || saved) && (
        <div className="mt-3 flex items-center gap-3">
          {dirty && (
            <button
              onClick={save}
              disabled={saving}
              className="rounded bg-white px-3 py-1 text-xs font-medium text-black hover:bg-zinc-200 disabled:bg-zinc-700"
            >
              {saving ? "Saving…" : "Save"}
            </button>
          )}
          {saved && !dirty && <span className="text-xs text-emerald-400">Saved</span>}
        </div>
      )}
    </div>
  );
}

function ItemStatusBadge({ status }: { status: PlanItemStatus }) {
  const map: Record<PlanItemStatus, string> = {
    idea: "border-zinc-700 text-zinc-400",
    awaiting_clips: "border-sky-700 text-sky-300",
    generating: "border-amber-700 text-amber-300",
    ready: "border-emerald-700 text-emerald-300",
    failed: "border-red-700 text-red-300",
  };
  const label = status === "awaiting_clips" ? "needs clips" : status;
  return (
    <span className={`rounded-full border px-2 py-0.5 text-xs ${map[status]}`}>{label}</span>
  );
}

function groupByWeek(items: PlanItem[]): { week: number; items: PlanItem[] }[] {
  const byWeek = new Map<number, PlanItem[]>();
  const sorted = Array.from(items).sort((a, b) => a.day_index - b.day_index);
  for (const it of sorted) {
    const week = Math.floor((it.day_index - 1) / 7) + 1;
    if (!byWeek.has(week)) byWeek.set(week, []);
    byWeek.get(week)!.push(it);
  }
  return Array.from(byWeek.entries()).map(([week, weekItems]) => ({ week, items: weekItems }));
}

function Shell({ children }: { children: React.ReactNode }) {
  return (
    <main className="min-h-[calc(100vh-3.5rem)] bg-black text-white">
      <div className="mx-auto max-w-2xl px-4">{children}</div>
    </main>
  );
}

function Centered({ children }: { children: React.ReactNode }) {
  return <div className="py-20 text-center">{children}</div>;
}
