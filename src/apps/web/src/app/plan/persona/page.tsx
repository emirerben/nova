"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import {
  getPersona,
  NotAuthenticatedError,
  type PersonaContent,
  type PersonaResponse,
  updatePersona,
} from "@/lib/plan-api";

const POLL_MS = 2000;

const TEXT_FIELDS: { key: keyof PersonaContent; label: string }[] = [
  { key: "summary", label: "Summary" },
  { key: "tone", label: "Tone" },
  { key: "audience", label: "Audience" },
  { key: "posting_cadence", label: "Posting cadence" },
];

const LIST_FIELDS: { key: keyof PersonaContent; label: string }[] = [
  { key: "content_pillars", label: "Content pillars" },
  { key: "sample_topics", label: "Sample topics" },
];

export default function PersonaPage() {
  const [row, setRow] = useState<PersonaResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [needsAuth, setNeedsAuth] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [draft, setDraft] = useState<PersonaContent | null>(null);
  const [saving, setSaving] = useState(false);
  const pollRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const load = useCallback(async () => {
    try {
      const r = await getPersona();
      setRow(r);
      // Seed the editable draft once the persona is available.
      if (r?.persona && draft === null) setDraft(r.persona);
      return r;
    } catch (err) {
      if (err instanceof NotAuthenticatedError) setNeedsAuth(true);
      else setError(err instanceof Error ? err.message : "Failed to load persona");
      return null;
    } finally {
      setLoading(false);
    }
  }, [draft]);

  useEffect(() => {
    let cancelled = false;
    async function tick() {
      const r = await load();
      if (cancelled) return;
      // Keep polling only while the agent is still generating.
      if (r?.persona_status === "generating") {
        pollRef.current = setTimeout(tick, POLL_MS);
      }
    }
    void tick();
    return () => {
      cancelled = true;
      if (pollRef.current) clearTimeout(pollRef.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function handleSave() {
    if (!row || !draft) return;
    setSaving(true);
    setError(null);
    try {
      const updated = await updatePersona(row.id, draft);
      setRow(updated);
      if (updated.persona) setDraft(updated.persona);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to save");
    } finally {
      setSaving(false);
    }
  }

  if (needsAuth) {
    return (
      <Shell>
        <div className="text-center py-20">
          <h1 className="text-2xl font-semibold mb-3">Sign in to view your persona</h1>
          <a
            href="/api/auth/signin"
            className="inline-block rounded bg-white px-6 py-3 font-medium text-black hover:bg-zinc-200"
          >
            Sign in with Google
          </a>
        </div>
      </Shell>
    );
  }

  if (loading) {
    return (
      <Shell>
        <p className="text-zinc-400 py-20 text-center">Loading…</p>
      </Shell>
    );
  }

  if (row === null) {
    return (
      <Shell>
        <div className="text-center py-20">
          <h1 className="text-2xl font-semibold mb-3">No persona yet</h1>
          <p className="text-zinc-400 mb-8">Answer a few questions to get started.</p>
          <Link
            href="/plan/onboarding"
            className="inline-block rounded bg-white px-6 py-3 font-medium text-black hover:bg-zinc-200"
          >
            Start onboarding
          </Link>
        </div>
      </Shell>
    );
  }

  if (row.persona_status === "generating") {
    return (
      <Shell>
        <div className="text-center py-20">
          <h1 className="text-2xl font-semibold mb-3">Building your persona…</h1>
          <p className="text-zinc-400">This takes a few seconds. The page will update itself.</p>
        </div>
      </Shell>
    );
  }

  if (row.persona_status === "failed" && !draft) {
    return (
      <Shell>
        <div className="py-16">
          <h1 className="text-2xl font-semibold mb-3">Generation didn&apos;t finish</h1>
          <p className="text-zinc-400 mb-2">
            {row.error_detail ?? "The persona generator hit an error."}
          </p>
          <p className="text-zinc-400 mb-8">
            You can write your persona by hand below — saving unblocks the rest of onboarding.
          </p>
          <button
            onClick={() => setDraft(blankPersona())}
            className="rounded bg-white px-6 py-3 font-medium text-black hover:bg-zinc-200"
          >
            Write it myself
          </button>
        </div>
      </Shell>
    );
  }

  const d = draft ?? blankPersona();

  return (
    <Shell>
      <div className="py-12">
        <div className="mb-8 flex items-center justify-between">
          <div>
            <h1 className="text-2xl font-semibold">Your persona</h1>
            <p className="text-zinc-400">Edit anything — this guides every video we make for you.</p>
          </div>
          <StatusBadge status={row.persona_status} />
        </div>

        {error && (
          <div className="mb-6 rounded border border-red-700 bg-red-950/50 px-4 py-3 text-red-200">
            {error}
          </div>
        )}

        <div className="space-y-6">
          {TEXT_FIELDS.map((f) => (
            <label key={f.key} className="block">
              <span className="mb-1 block text-sm font-medium text-zinc-300">{f.label}</span>
              <textarea
                value={(d[f.key] as string) ?? ""}
                onChange={(e) => setDraft({ ...d, [f.key]: e.target.value })}
                rows={f.key === "summary" ? 3 : 1}
                className="w-full resize-y rounded border border-zinc-700 bg-zinc-900 px-3 py-2 text-white focus:border-zinc-400 focus:outline-none"
              />
            </label>
          ))}

          {LIST_FIELDS.map((f) => (
            <label key={f.key} className="block">
              <span className="mb-1 block text-sm font-medium text-zinc-300">
                {f.label} <span className="text-zinc-500">(one per line)</span>
              </span>
              <textarea
                value={((d[f.key] as string[]) ?? []).join("\n")}
                onChange={(e) =>
                  setDraft({
                    ...d,
                    [f.key]: e.target.value.split("\n").map((s) => s.trim()).filter(Boolean),
                  })
                }
                rows={5}
                className="w-full resize-y rounded border border-zinc-700 bg-zinc-900 px-3 py-2 text-white focus:border-zinc-400 focus:outline-none"
              />
            </label>
          ))}
        </div>

        <button
          onClick={handleSave}
          disabled={saving}
          className="mt-8 rounded bg-white px-6 py-3 font-medium text-black hover:bg-zinc-200 disabled:bg-zinc-700 disabled:text-zinc-400"
        >
          {saving ? "Saving…" : "Save persona"}
        </button>
      </div>
    </Shell>
  );
}

function Shell({ children }: { children: React.ReactNode }) {
  return (
    <main className="min-h-[calc(100vh-3.5rem)] bg-black text-white">
      <div className="max-w-2xl mx-auto px-4">{children}</div>
    </main>
  );
}

function StatusBadge({ status }: { status: string }) {
  const label = status === "edited" ? "edited" : status === "ready" ? "AI-generated" : status;
  return (
    <span className="rounded-full border border-zinc-700 bg-zinc-900 px-3 py-1 text-xs text-zinc-300">
      {label}
    </span>
  );
}

function blankPersona(): PersonaContent {
  return {
    summary: "",
    content_pillars: [],
    tone: "",
    audience: "",
    posting_cadence: "",
    sample_topics: [],
  };
}
