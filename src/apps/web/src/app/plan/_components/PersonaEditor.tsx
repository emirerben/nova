"use client";

import { useState } from "react";
import type { PersonaContent, PersonaStatus } from "@/lib/plan-api";

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

/**
 * Editable persona with a primary "continue to plan" CTA. Owns its draft +
 * dirty tracking; `onSave` persists an edit, `onContinue` advances the wizard
 * (the old persona page dead-ended here with no way forward).
 */
export default function PersonaEditor({
  persona,
  status,
  onSave,
  onContinue,
  continueLabel,
  continuing,
}: {
  persona: PersonaContent;
  status: PersonaStatus;
  onSave: (draft: PersonaContent) => Promise<void>;
  onContinue: () => void;
  continueLabel: string;
  continuing?: boolean;
}) {
  const [draft, setDraft] = useState<PersonaContent>(persona);
  const [lastSaved, setLastSaved] = useState<PersonaContent>(persona);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const dirty = JSON.stringify(draft) !== JSON.stringify(lastSaved);

  // Trim + drop blank entries from the list fields. Done ONLY at save time, never
  // per-keystroke — trimming/filtering on every change strips the trailing space
  // you just typed (so you can't start the next word) and deletes blank lines as
  // you make them, which is what made the list fields feel uneditable.
  function normalize(p: PersonaContent): PersonaContent {
    return {
      ...p,
      content_pillars: (p.content_pillars ?? []).map((s) => s.trim()).filter(Boolean),
      sample_topics: (p.sample_topics ?? []).map((s) => s.trim()).filter(Boolean),
    };
  }

  async function save(): Promise<boolean> {
    setSaving(true);
    setError(null);
    try {
      const clean = normalize(draft);
      await onSave(clean);
      setDraft(clean);
      setLastSaved(clean);
      return true;
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to save");
      return false;
    } finally {
      setSaving(false);
    }
  }

  // Flush unsaved edits before advancing. Critical for the failed-generation
  // hand-write path: the plan endpoint 409s unless the persona is persisted as
  // "ready"/"edited", and a save is what flips a hand-written persona to
  // "edited". Also guarantees plan generation reads the latest edits.
  async function handleContinue() {
    if (dirty && !(await save())) return;
    onContinue();
  }

  return (
    <div className="animate-fade-up py-2">
      <div className="mb-8 flex items-start justify-between gap-4">
        <div>
          <h1 className="font-display text-3xl text-white">Meet your persona</h1>
          <p className="mt-1 text-zinc-400">
            This guides every video we make for you. Tweak anything that feels off.
          </p>
        </div>
        <StatusBadge status={status} />
      </div>

      {draft.rationale && (
        <div className="mb-8 rounded-lg border border-zinc-800 bg-zinc-950/40 p-4">
          <p className="mb-1 text-xs font-medium text-amber-300/80">Why this lane</p>
          <p className="text-sm text-zinc-300">{draft.rationale}</p>
        </div>
      )}

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
              value={(draft[f.key] as string) ?? ""}
              onChange={(e) => setDraft({ ...draft, [f.key]: e.target.value })}
              rows={f.key === "summary" ? 3 : 1}
              className="w-full resize-y rounded-lg border border-zinc-700 bg-zinc-900 px-3 py-2 text-white transition-colors focus:border-amber-400/60 focus:outline-none"
            />
          </label>
        ))}

        {LIST_FIELDS.map((f) => (
          <label key={f.key} className="block">
            <span className="mb-1 block text-sm font-medium text-zinc-300">
              {f.label} <span className="text-zinc-500">(one per line)</span>
            </span>
            <textarea
              value={((draft[f.key] as string[]) ?? []).join("\n")}
              onChange={(e) =>
                // Preserve raw text (incl. spaces + blank lines) while typing;
                // normalize() handles trim/dedupe at save time.
                setDraft({ ...draft, [f.key]: e.target.value.split("\n") })
              }
              rows={5}
              className="w-full resize-y rounded-lg border border-zinc-700 bg-zinc-900 px-3 py-2 text-white transition-colors focus:border-amber-400/60 focus:outline-none"
            />
          </label>
        ))}
      </div>

      <div className="mt-10 flex flex-wrap items-center gap-4 border-t border-zinc-800 pt-6">
        <button
          onClick={handleContinue}
          disabled={continuing || saving}
          className="rounded-full bg-amber-400 px-6 py-3 font-medium text-black transition-colors hover:bg-amber-300 disabled:cursor-not-allowed disabled:bg-zinc-700 disabled:text-zinc-400"
        >
          {continuing || saving ? "Starting…" : continueLabel}
        </button>
        {dirty && (
          <button
            onClick={save}
            disabled={saving}
            className="rounded-full border border-zinc-700 px-5 py-3 text-sm font-medium text-zinc-200 transition-colors hover:border-zinc-400 hover:text-white disabled:opacity-60"
          >
            {saving ? "Saving…" : "Save edits"}
          </button>
        )}
        {!dirty && status === "edited" && (
          <span className="text-sm text-emerald-400">Saved ✓</span>
        )}
      </div>
    </div>
  );
}

function StatusBadge({ status }: { status: PersonaStatus }) {
  const label =
    status === "edited" ? "edited" : status === "ready" ? "AI-generated" : status;
  return (
    <span className="shrink-0 rounded-full border border-zinc-700 bg-zinc-900 px-3 py-1 text-xs text-zinc-300">
      {label}
    </span>
  );
}
