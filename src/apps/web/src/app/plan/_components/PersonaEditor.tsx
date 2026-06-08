"use client";

import { useId, useState } from "react";
import { cn } from "@/lib/cn";
import type { PersonaContent, PersonaStatus, TikTokProfile } from "@/lib/plan-api";

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
  tiktokProfile,
  onUpdateAnswers,
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
  // Aha-moment reveal data (chat onboarding)
  tiktokProfile?: TikTokProfile | null;
  // Navigates back to the TikTok pre-screen so returning users can restart the chat.
  onUpdateAnswers?: () => void;
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
      {/* Aha-moment reveal — TikTok stat line */}
      <AhaMoment tiktokProfile={tiktokProfile} />

      <div className="mb-8 flex items-start justify-between gap-4">
        <div>
          <h1
            className="font-display text-3xl text-[#0c0c0e] animate-fade-up"
            style={{ animationDelay: tiktokProfile ? "100ms" : "0ms" }}
          >
            Meet your persona
          </h1>
          <p className="mt-1 text-[#71717a]">
            {editing
              ? "This is the lane your plan builds on — edit anything that feels off."
              : "This is who we think you are. It guides every video we make for you."}
          </p>
        </div>
        <StatusBadge status={status} />
      </div>

      {draft.rationale && (
        <div className="mb-8 rounded-lg border border-zinc-200 bg-white p-4">
          <p className="mb-1 text-xs font-medium text-lime-700">Why this lane</p>
          <p className="text-sm text-[#3f3f46]">{draft.rationale}</p>
        </div>
      )}

      {error && (
        <div className="mb-6 rounded border border-zinc-200 bg-[#fafaf8] px-4 py-3 text-[#3f3f46]">
          {error}
        </div>
      )}

      {editing ? (
        <PersonaForm draft={draft} setDraft={setDraft} />
      ) : (
        <PersonaSummary persona={draft} />
      )}

      <div className="mt-10 flex flex-wrap items-center gap-4 border-t border-zinc-200 pt-6">
        <button
          onClick={handleContinue}
          disabled={continuing || saving}
          className="inline-flex min-h-[44px] items-center rounded-full bg-[#0c0c0e] px-6 py-3 font-medium text-white transition-opacity hover:opacity-80 disabled:cursor-not-allowed disabled:opacity-40"
        >
          {continuing || saving ? "Starting…" : continueLabel}
        </button>

        {editing ? (
          <button
            onClick={save}
            disabled={saving || !dirty}
            className="inline-flex min-h-[44px] items-center rounded-full border border-zinc-200 px-5 py-3 text-sm font-medium text-[#3f3f46] transition-colors hover:border-zinc-400 hover:text-[#0c0c0e] disabled:opacity-60"
          >
            {saving ? "Saving…" : "Save edits"}
          </button>
        ) : (
          <button
            onClick={() => setEditing(true)}
            disabled={continuing || saving}
            className="inline-flex min-h-[44px] items-center rounded-full border border-zinc-200 px-5 py-3 text-sm font-medium text-[#3f3f46] transition-colors hover:border-zinc-400 hover:text-[#0c0c0e] disabled:opacity-60"
          >
            Tweak
          </button>
        )}

        {!dirty && status === "edited" && !editing && (
          <span className="text-sm text-emerald-600">Saved ✓</span>
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
            className="inline-flex min-h-[44px] items-center rounded-full border border-zinc-200 px-5 py-3 text-sm font-medium text-[#3f3f46] transition-colors hover:border-zinc-400 hover:text-[#0c0c0e] disabled:opacity-60"
          >
            {retuning ? "Updating…" : "Update from feedback"}
          </button>
        )}

        {onUpdateAnswers && !editing && (
          <button
            type="button"
            onClick={onUpdateAnswers}
            className="py-3 text-sm text-[#71717a] transition-colors hover:text-[#0c0c0e]"
          >
            Update my answers →
          </button>
        )}
      </div>
    </div>
  );
}

/**
 * Aha-moment reveal shown once above the persona heading.
 * TikTok path: stat line ("We analyzed N of your videos. @handle · N followers").
 * Nothing rendered when not available (legacy flat-questionnaire personas).
 */
function AhaMoment({
  tiktokProfile,
}: {
  tiktokProfile?: TikTokProfile | null;
}) {
  if (tiktokProfile) {
    const { handle, follower_count, video_count } = tiktokProfile;
    const followerStr =
      follower_count != null
        ? follower_count >= 1000
          ? `${(follower_count / 1000).toFixed(1)}K followers`
          : `${follower_count} followers`
        : null;
    return (
      <div className="mb-8 animate-fade-up">
        {video_count != null && (
          <p className="text-sm font-medium uppercase tracking-wide text-lime-700">
            We analyzed {video_count} of your videos.
          </p>
        )}
        <p className="mt-0.5 text-xs text-[#71717a]">
          @{handle}
          {followerStr && <> · {followerStr}</>}
        </p>
      </div>
    );
  }

  return null;
}

/** Editorial read view: summary leads, pillars/topics as chips, facts as labeled rows. */
function PersonaSummary({ persona }: { persona: PersonaContent }) {
  const pillars = (persona.content_pillars ?? []).filter(Boolean);
  const topics = (persona.sample_topics ?? []).filter(Boolean);
  const facts = FACT_FIELDS.map((f) => ({
    label: f.label,
    value: (persona[f.key] as string)?.trim(),
  })).filter((f) => f.value);

  // Show posts_per_week when set; falls after cadence in the grid.
  if (persona.posts_per_week != null) {
    facts.push({ label: "Posts/week", value: String(persona.posts_per_week) });
  }

  return (
    <div className="space-y-8">
      {persona.summary?.trim() ? (
        <p className="font-display text-xl leading-relaxed text-[#0c0c0e]">{persona.summary}</p>
      ) : (
        <p className="text-[#71717a]">No summary yet — tap Tweak to write one.</p>
      )}

      {pillars.length > 0 && (
        <ChipGroup label="Content pillars" items={pillars} />
      )}

      {facts.length > 0 && (
        <dl className="grid grid-cols-1 gap-4 sm:grid-cols-3">
          {facts.map((f) => (
            <div key={f.label}>
              <dt className="text-xs font-medium uppercase tracking-wide text-[#a1a1aa]">
                {f.label}
              </dt>
              <dd className="mt-1 text-sm text-[#3f3f46]">{f.value}</dd>
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
      <p className="mb-2 text-xs font-medium uppercase tracking-wide text-[#a1a1aa]">{label}</p>
      <ul className="flex flex-wrap gap-2">
        {items.map((c, i) => (
          <li
            key={`${c}-${i}`}
            className="rounded-full border border-zinc-200 bg-white px-3 py-1.5 text-sm text-[#3f3f46]"
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
          <span className="mb-1 block text-sm font-medium text-[#3f3f46]">{f.label}</span>
          <textarea
            value={(draft[f.key] as string) ?? ""}
            onChange={(e) => setDraft({ ...draft, [f.key]: e.target.value })}
            rows={f.key === "summary" ? 3 : 1}
            className="w-full resize-y rounded-lg border border-zinc-200 bg-white px-3 py-2 text-[#0c0c0e] transition-colors focus:border-lime-600/60 focus:outline-none"
          />
        </label>
      ))}

      {/* Numeric posts-per-week control — drives how many ideas appear in the plan */}
      <label className="block">
        <span className="mb-1 block text-sm font-medium text-[#3f3f46]">Posts per week</span>
        <input
          type="number"
          min={1}
          max={7}
          value={draft.posts_per_week ?? ""}
          onChange={(e) => {
            const raw = e.target.value;
            if (raw === "") {
              setDraft({ ...draft, posts_per_week: null });
            } else {
              const n = parseInt(raw, 10);
              setDraft({ ...draft, posts_per_week: isNaN(n) ? null : Math.max(1, Math.min(7, n)) });
            }
          }}
          placeholder="1–7"
          className="w-24 rounded-lg border border-zinc-200 bg-white px-3 py-2 text-[#0c0c0e] transition-colors focus:border-lime-600/60 focus:outline-none"
        />
        <p className="mt-1 text-xs text-[#71717a]">
          How many posts per week? This drives your plan&apos;s idea count (blank = inferred from cadence).
        </p>
      </label>

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
      <label htmlFor={inputId} className="mb-1 block text-sm font-medium text-[#3f3f46]">
        {label}
      </label>
      <div className="flex flex-wrap items-center gap-2 rounded-lg border border-zinc-200 bg-white px-2 py-2 focus-within:border-lime-600/60">
        {values.map((c, i) => (
          <span
            key={`${c}-${i}`}
            className="inline-flex items-center gap-1 rounded-full border border-zinc-200 bg-zinc-50 py-1 pl-3 pr-1 text-sm text-[#3f3f46]"
          >
            {c}
            <button
              type="button"
              onClick={() => remove(i)}
              aria-label={`Remove ${c}`}
              className={cn(
                "flex h-5 w-5 items-center justify-center rounded-full text-[#a1a1aa]",
                "transition-colors hover:bg-zinc-100 hover:text-[#0c0c0e]",
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
          className="min-w-[8rem] flex-1 bg-transparent px-1 py-1 text-sm text-[#0c0c0e] placeholder-zinc-400 focus:outline-none"
        />
      </div>
    </div>
  );
}

function StatusBadge({ status }: { status: PersonaStatus }) {
  const label =
    status === "edited" ? "edited" : status === "ready" ? "AI-generated" : status;
  return (
    <span className="shrink-0 rounded-full border border-zinc-200 bg-white px-3 py-1 text-xs text-[#71717a]">
      {label}
    </span>
  );
}
