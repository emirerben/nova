"use client";

import Link from "next/link";
import { useState } from "react";
import { type PlanItem, type PlanItemStatus, updatePlanItem } from "@/lib/plan-api";

// Glyph + text so status reads without relying on color alone (a11y).
const STATUS_META: Record<PlanItemStatus, { glyph: string; label: string; cls: string }> = {
  idea: { glyph: "○", label: "idea", cls: "border-zinc-700 text-zinc-400" },
  awaiting_clips: { glyph: "◔", label: "needs clips", cls: "border-sky-700 text-sky-300" },
  generating: { glyph: "◐", label: "generating", cls: "border-amber-700 text-amber-300" },
  ready: { glyph: "●", label: "ready", cls: "border-emerald-700 text-emerald-300" },
  failed: { glyph: "✕", label: "failed", cls: "border-red-700 text-red-300" },
};

export function ItemStatusBadge({ status }: { status: PlanItemStatus }) {
  const m = STATUS_META[status];
  return (
    <span
      className={`inline-flex shrink-0 items-center gap-1 rounded-full border px-2 py-0.5 text-xs ${m.cls}`}
    >
      <span aria-hidden="true">{m.glyph}</span>
      {m.label}
    </span>
  );
}

function actionLabel(item: PlanItem): string {
  if (item.status === "ready") return "View videos →";
  return item.clip_gcs_paths.length > 0 ? "Continue →" : "Upload clips & generate →";
}

/**
 * A plan day. Read-only by default (scan the month without 30 open textareas);
 * an explicit "Edit" reveals bordered fields. Decouples scanning from editing —
 * the borderless-transparent-input pattern this replaces was undiscoverable.
 */
export default function PlanItemCard({
  item,
  onError,
}: {
  item: PlanItem;
  onError: (msg: string) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [theme, setTheme] = useState(item.theme);
  const [idea, setIdea] = useState(item.idea);
  const [filming, setFilming] = useState(item.filming_suggestion ?? "");
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  // Track the last-persisted values so `dirty` resets after a save (the parent
  // doesn't refetch the item prop, so comparing against `item` would re-flag dirty).
  const [baseline, setBaseline] = useState({
    theme: item.theme,
    idea: item.idea,
    filming: item.filming_suggestion ?? "",
  });

  const dirty =
    theme !== baseline.theme || idea !== baseline.idea || filming !== baseline.filming;

  async function save() {
    setSaving(true);
    setSaved(false);
    try {
      await updatePlanItem(item.id, { theme, idea, filming_suggestion: filming });
      setBaseline({ theme, idea, filming });
      setSaved(true);
      setEditing(false);
    } catch (err) {
      onError(err instanceof Error ? err.message : "Failed to save item");
    } finally {
      setSaving(false);
    }
  }

  function cancel() {
    setTheme(baseline.theme);
    setIdea(baseline.idea);
    setFilming(baseline.filming);
    setEditing(false);
  }

  return (
    <div className="rounded-xl border border-zinc-800 bg-zinc-900/60 p-4 transition-colors hover:border-zinc-700">
      <div className="flex items-center gap-3">
        <span className="rounded bg-zinc-800 px-2 py-0.5 text-xs text-zinc-400">
          Day {item.day_index}
        </span>
        <h3 className="flex-1 truncate text-sm font-medium text-zinc-200">
          {theme || "Untitled idea"}
        </h3>
        <ItemStatusBadge status={item.status} />
      </div>

      {!editing ? (
        <>
          {idea && <p className="mt-2 line-clamp-2 text-sm text-zinc-400">{idea}</p>}
          <div className="mt-3 flex items-center justify-between border-t border-zinc-800 pt-3">
            <Link
              href={`/plan/items/${item.id}`}
              className="text-xs font-medium text-amber-300 transition-colors hover:text-amber-200"
            >
              {actionLabel(item)}
            </Link>
            <div className="flex items-center gap-3">
              {saved && <span className="text-xs text-emerald-400">Saved ✓</span>}
              <button
                type="button"
                onClick={() => setEditing(true)}
                className="text-xs text-zinc-500 transition-colors hover:text-white"
              >
                Edit
              </button>
            </div>
          </div>
        </>
      ) : (
        <div className="mt-3 space-y-3">
          <label className="block">
            <span className="mb-1 block text-xs text-zinc-500">Theme</span>
            <input
              value={theme}
              onChange={(e) => setTheme(e.target.value)}
              className="w-full rounded border border-zinc-700 bg-zinc-950 px-3 py-2 text-sm text-zinc-100 transition-colors focus:border-amber-400/60 focus:outline-none"
            />
          </label>
          <label className="block">
            <span className="mb-1 block text-xs text-zinc-500">Idea</span>
            <textarea
              value={idea}
              onChange={(e) => setIdea(e.target.value)}
              rows={3}
              className="w-full resize-y rounded border border-zinc-700 bg-zinc-950 px-3 py-2 text-sm text-white transition-colors focus:border-amber-400/60 focus:outline-none"
            />
          </label>
          <label className="block">
            <span className="mb-1 block text-xs text-zinc-500">Filming tip</span>
            <input
              value={filming}
              onChange={(e) => setFilming(e.target.value)}
              placeholder="optional"
              className="w-full rounded border border-zinc-700 bg-zinc-950 px-3 py-1.5 text-xs text-zinc-300 transition-colors focus:border-amber-400/60 focus:outline-none"
            />
          </label>
          <div className="flex items-center gap-3 pt-1">
            <button
              type="button"
              onClick={save}
              disabled={saving || !dirty}
              className="rounded bg-white px-3 py-1 text-xs font-medium text-black transition-colors hover:bg-zinc-200 disabled:bg-zinc-700 disabled:text-zinc-400"
            >
              {saving ? "Saving…" : "Save"}
            </button>
            <button
              type="button"
              onClick={cancel}
              disabled={saving}
              className="text-xs text-zinc-500 transition-colors hover:text-white disabled:opacity-60"
            >
              Cancel
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
