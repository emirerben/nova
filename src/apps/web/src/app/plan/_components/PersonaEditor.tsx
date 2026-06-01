"use client";

import { useId, useState } from "react";
import { cn } from "@/lib/cn";
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

// Facts surfaced in the read view as labeled rows (summary leads separately).
const FACT_FIELDS: { key: keyof PersonaContent; label: string }[] = [
  { key: "tone", label: "Tone" },
  { key: "audience", label: "Audience" },
  { key: "posting_cadence", label: "Cadence" },
];

/**
 * Persona step. Leads with an editorial read view (summary + pillar/topic chips +
 * labeled facts) so "here's who we think you are" lands as a moment before we ask
 * for edits. "Tweak" opens the existing form (text fields + chip add/remove for
 * the list fields). Owns its draft + dirty tracking; `onSave` persists, `onContinue`
 * advances the wizard.
 *
 * `startInEdit` forces straight into the form — used by the failed-generation
 * hand-write path, where there's nothing to read yet.
 */
export default function PersonaEditor({
  persona,
  status,
  onSave,
  onContinue,
  continueLabel,
  continuing,
  startInEdit = false,
  onRetuneFromFeedback,
}: {
  persona: PersonaContent;
  status: PersonaStatus;
  onSave: (draft: PersonaContent) => Promise<void>;
  onContinue: () => void;
  continueLabel: string;
  continuing?: boolean;
  startInEdit?: boolean;
  // When provided, shows "Update from feedback" in the read view (feedback loop,
  // Phase 2). Disabled when the persona is hand-edited — an explicit edit is
  // authoritative and never overwritten by inferred feedback ("their say" rule).
  onRetuneFromFeedback?: () => Promise<void>;
}) {
  const [draft, setDraft] = useState<PersonaContent>(persona);
  const [lastSaved, setLastSaved] = useState<PersonaContent>(persona);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState(startInEdit);
  const [retuning, setRetuning] = useState(false);

  async function handleRetune() {
    if (!onRetuneFromFeedback) return;
    setRetuning(true);
    setError(null);
    try {
      await onRetuneFromFeedback();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Couldn't update from feedback");
    } finally {
      setRetuning(false);
    }
  }

  const dirty = JSON.stringify(draft) !== JSON.stringify(lastSaved);

  // Trim + drop blank entries from the list fields. Done ONLY at save time, never
  // per-keystroke — the chip editor keeps a transient "typing" buffer that we
  // don't want trimmed mid-word.
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
      setEditing(false);
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
            {editing
              ? "Edit anything that feels off — this guides every video we make for you."
              : "This is who we think you are. It guides every video we make for you."}
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

      {editing ? (
        <PersonaForm draft={draft} setDraft={setDraft} />
      ) : (
        <PersonaSummary persona={draft} />
      )}

      <div className="mt-10 flex flex-wrap items-center gap-4 border-t border-zinc-800 pt-6">
        <button
          onClick={handleContinue}
          disabled={continuing || saving}
          className="inline-flex min-h-[44px] items-center rounded-full bg-amber-400 px-6 py-3 font-medium text-black transition-colors hover:bg-amber-300 disabled:cursor-not-allowed disabled:bg-zinc-700 disabled:text-zinc-400"
        >
          {continuing || saving ? "Starting…" : continueLabel}
        </button>

        {editing ? (
          <button
            onClick={save}
            disabled={saving || !dirty}
            className="inline-flex min-h-[44px] items-center rounded-full border border-zinc-700 px-5 py-3 text-sm font-medium text-zinc-200 transition-colors hover:border-zinc-400 hover:text-white disabled:opacity-60"
          >
            {saving ? "Saving…" : "Save edits"}
          </button>
        ) : (
          <button
            onClick={() => setEditing(true)}
            disabled={continuing || saving}
            className="inline-flex min-h-[44px] items-center rounded-full border border-zinc-700 px-5 py-3 text-sm font-medium text-zinc-200 transition-colors hover:border-zinc-400 hover:text-white disabled:opacity-60"
          >
            Tweak
          </button>
        )}

        {!dirty && status === "edited" && !editing && (
          <span className="text-sm text-emerald-400">Saved ✓</span>
        )}

        {onRetuneFromFeedback && !editing && (
          <button
            onClick={handleRetune}
            disabled={retuning || continuing || saving || status === "edited"}
            title={
              status === "edited"
                ? "Your hand-edited persona stays as you wrote it. Reset to AI to retune."
                : "Re-tune this persona from your video feedback"
            }
            className="inline-flex min-h-[44px] items-center rounded-full border border-zinc-700 px-5 py-3 text-sm font-medium text-zinc-200 transition-colors hover:border-zinc-400 hover:text-white disabled:opacity-60"
          >
            {retuning ? "Updating…" : "Update from feedback"}
          </button>
        )}
      </div>
    </div>
  );
}

/** Editorial read view: summary leads, pillars/topics as chips, facts as labeled rows. */
function PersonaSummary({ persona }: { persona: PersonaContent }) {
  const pillars = (persona.content_pillars ?? []).filter(Boolean);
  const topics = (persona.sample_topics ?? []).filter(Boolean);
  const facts = FACT_FIELDS.map((f) => ({
    label: f.label,
    value: (persona[f.key] as string)?.trim(),
  })).filter((f) => f.value);

  return (
    <div className="space-y-8">
      {persona.summary?.trim() ? (
        <p className="font-display text-xl leading-relaxed text-zinc-100">{persona.summary}</p>
      ) : (
        <p className="text-zinc-500">No summary yet — tap Tweak to write one.</p>
      )}

      {pillars.length > 0 && (
        <ChipGroup label="Content pillars" items={pillars} />
      )}

      {facts.length > 0 && (
        <dl className="grid grid-cols-1 gap-4 sm:grid-cols-3">
          {facts.map((f) => (
            <div key={f.label}>
              <dt className="text-xs font-medium uppercase tracking-wide text-zinc-500">
                {f.label}
              </dt>
              <dd className="mt-1 text-sm text-zinc-200">{f.value}</dd>
            </div>
          ))}
        </dl>
      )}

      {topics.length > 0 && <ChipGroup label="Sample topics" items={topics} />}
    </div>
  );
}

function ChipGroup({ label, items }: { label: string; items: string[] }) {
  return (
    <div>
      <p className="mb-2 text-xs font-medium uppercase tracking-wide text-zinc-500">{label}</p>
      <ul className="flex flex-wrap gap-2">
        {items.map((c, i) => (
          <li
            key={`${c}-${i}`}
            className="rounded-full border border-zinc-700 bg-zinc-900 px-3 py-1.5 text-sm text-zinc-200"
          >
            {c}
          </li>
        ))}
      </ul>
    </div>
  );
}

/** The editable form (text fields + chip add/remove for list fields). */
function PersonaForm({
  draft,
  setDraft,
}: {
  draft: PersonaContent;
  setDraft: (p: PersonaContent) => void;
}) {
  return (
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
        <ChipListEditor
          key={f.key}
          label={f.label}
          values={(draft[f.key] as string[]) ?? []}
          onChange={(next) => setDraft({ ...draft, [f.key]: next })}
        />
      ))}
    </div>
  );
}

/**
 * Add/remove chip editor for list fields. Type + Enter (or comma) adds a chip;
 * × removes one; Backspace on an empty input removes the last. Replaces the old
 * "one per line" textarea, which read as a wall of text and hid the list shape.
 */
function ChipListEditor({
  label,
  values,
  onChange,
}: {
  label: string;
  values: string[];
  onChange: (next: string[]) => void;
}) {
  const [buffer, setBuffer] = useState("");
  const inputId = useId();

  function commit(raw: string) {
    const v = raw.trim();
    if (!v) return;
    onChange([...values, v]);
    setBuffer("");
  }

  function remove(i: number) {
    onChange(values.filter((_, idx) => idx !== i));
  }

  return (
    <div className="block">
      <label htmlFor={inputId} className="mb-1 block text-sm font-medium text-zinc-300">
        {label}
      </label>
      <div className="flex flex-wrap items-center gap-2 rounded-lg border border-zinc-700 bg-zinc-900 px-2 py-2 focus-within:border-amber-400/60">
        {values.map((c, i) => (
          <span
            key={`${c}-${i}`}
            className="inline-flex items-center gap-1 rounded-full border border-zinc-600 bg-zinc-800 py-1 pl-3 pr-1 text-sm text-zinc-100"
          >
            {c}
            <button
              type="button"
              onClick={() => remove(i)}
              aria-label={`Remove ${c}`}
              className={cn(
                "flex h-5 w-5 items-center justify-center rounded-full text-zinc-400",
                "transition-colors hover:bg-zinc-700 hover:text-white",
              )}
            >
              ×
            </button>
          </span>
        ))}
        <input
          id={inputId}
          value={buffer}
          onChange={(e) => setBuffer(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" || e.key === ",") {
              e.preventDefault();
              commit(buffer);
            } else if (e.key === "Backspace" && buffer === "" && values.length > 0) {
              remove(values.length - 1);
            }
          }}
          onBlur={() => commit(buffer)}
          placeholder={values.length === 0 ? "Type and press Enter…" : "Add another…"}
          className="min-w-[8rem] flex-1 bg-transparent px-1 py-1 text-sm text-white placeholder-zinc-600 focus:outline-none"
        />
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
