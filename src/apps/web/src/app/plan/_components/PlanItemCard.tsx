"use client";

import Link from "next/link";
import { useState } from "react";
import { type PlanItem, type PlanItemStatus, updatePlanItem } from "@/lib/plan-api";

export function ItemStatusBadge({ status }: { status: PlanItemStatus }) {
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

/** Inline-editable plan idea with a deep link into the item's upload/generate page. */
export default function PlanItemCard({
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
    <div className="rounded-xl border border-zinc-800 bg-zinc-900/60 p-4 transition-colors hover:border-zinc-700">
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
        className="mb-2 w-full resize-y rounded border border-zinc-800 bg-zinc-950 px-3 py-2 text-sm text-white transition-colors focus:border-zinc-600 focus:outline-none"
      />
      <input
        value={filming}
        onChange={(e) => setFilming(e.target.value)}
        placeholder="filming tip"
        className="w-full rounded border border-zinc-800 bg-zinc-950 px-3 py-1.5 text-xs text-zinc-400 transition-colors focus:border-zinc-600 focus:outline-none"
      />
      <div className="mt-3 flex items-center justify-between border-t border-zinc-800 pt-3">
        <Link
          href={`/plan/items/${item.id}`}
          className="text-xs font-medium text-amber-300 transition-colors hover:text-amber-200"
        >
          {item.status === "ready"
            ? "View videos →"
            : item.clip_gcs_paths.length > 0
              ? "Continue →"
              : "Upload clips & generate →"}
        </Link>
        {(dirty || saved) && (
          <div className="flex items-center gap-3">
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
    </div>
  );
}
