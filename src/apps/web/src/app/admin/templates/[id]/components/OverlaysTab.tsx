"use client";

/**
 * Overlay text editor — the escape hatch when the Layer-2 cumulative-reveal
 * pipeline produces wrong text. Admin sees every overlay in
 * recipe_cached.slots[*].text_overlays[*] as an inline-editable row;
 * Save sends a bulk PATCH to /admin/templates/{id}/overlays.
 *
 * Backed by:
 *   - GET   /admin/templates/{id}/debug          (loads recipe_cached)
 *   - PATCH /admin/templates/{id}/overlays       (writes edits back)
 *
 * Caveat surfaced in the UI: a subsequent reanalyze-agentic will
 * overwrite manual edits when it produces a fresh recipe. The next
 * iteration will add a "manually edited" flag the reanalyze can
 * respect; for now this is documented in-place.
 */

import { useCallback, useEffect, useMemo, useState } from "react";

import {
  type OverlayTextEdit,
  type TemplateDebugResponse,
  adminGetTemplateDebug,
  adminUpdateTemplateOverlays,
} from "@/lib/admin-api";
import {
  expandPhraseEditToMemberTexts,
  groupOverlayRowsIntoPhrases,
  type OverlayRow,
} from "./phrase-grouping";

function extractOverlayRows(
  recipe_cached: Record<string, unknown> | null,
): OverlayRow[] {
  if (!recipe_cached) return [];
  const slots = recipe_cached["slots"];
  if (!Array.isArray(slots)) return [];
  const rows: OverlayRow[] = [];
  slots.forEach((slot, slot_index) => {
    if (!slot || typeof slot !== "object") return;
    const text_overlays = (slot as Record<string, unknown>)["text_overlays"];
    if (!Array.isArray(text_overlays)) return;
    text_overlays.forEach((overlay, overlay_index) => {
      if (!overlay || typeof overlay !== "object") return;
      const ov = overlay as Record<string, unknown>;
      // sample_text is the canonical field; `text` is the legacy fallback.
      const sample_text =
        typeof ov.sample_text === "string"
          ? ov.sample_text
          : typeof ov.text === "string"
            ? ov.text
            : "";
      const start_s = typeof ov.start_s === "number" ? ov.start_s : null;
      const end_s = typeof ov.end_s === "number" ? ov.end_s : null;
      const role = typeof ov.role === "string" ? ov.role : null;
      rows.push({
        slot_index,
        overlay_index,
        original_sample_text: sample_text,
        current_sample_text: sample_text,
        start_s,
        end_s,
        role,
      });
    });
  });
  return rows;
}

export function OverlaysTab({ templateId }: { templateId: string }): JSX.Element {
  const [data, setData] = useState<TemplateDebugResponse | null>(null);
  const [rows, setRows] = useState<OverlayRow[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [lastSavedAt, setLastSavedAt] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const r = await adminGetTemplateDebug(templateId);
      setData(r);
      setRows(extractOverlayRows(r.recipe_cached));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [templateId]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const dirtyRows = useMemo(
    () => rows.filter((r) => r.current_sample_text !== r.original_sample_text),
    [rows],
  );

  const phraseGroups = useMemo(() => groupOverlayRowsIntoPhrases(rows), [rows]);
  const dirtyPhraseCount = useMemo(
    () => phraseGroups.filter((g) => g.dirty).length,
    [phraseGroups],
  );

  // Edit one phrase row → distribute the new text across its underlying
  // overlays. `groupIndex` is into `phraseGroups`; the group carries
  // member_row_indices into `rows` so the splice is unambiguous even when
  // multiple phrases share a slot.
  const handlePhraseEdit = useCallback(
    (groupIndex: number, newText: string) => {
      const group = phraseGroups[groupIndex];
      if (!group) return;
      const memberTexts = expandPhraseEditToMemberTexts(group, newText);
      setRows((prev) => {
        const next = prev.slice();
        group.member_row_indices.forEach((rowIdx, k) => {
          next[rowIdx] = { ...next[rowIdx], current_sample_text: memberTexts[k] };
        });
        return next;
      });
    },
    [phraseGroups],
  );

  const handleSave = useCallback(async () => {
    if (dirtyRows.length === 0) return;
    setSaving(true);
    setSaveError(null);
    try {
      const edits: OverlayTextEdit[] = dirtyRows.map((r) => ({
        slot_index: r.slot_index,
        overlay_index: r.overlay_index,
        sample_text: r.current_sample_text,
      }));
      const updated = await adminUpdateTemplateOverlays(templateId, edits);
      setData(updated);
      setRows(extractOverlayRows(updated.recipe_cached));
      setLastSavedAt(new Date().toLocaleTimeString());
    } catch (e) {
      setSaveError((e as Error).message);
    } finally {
      setSaving(false);
    }
  }, [dirtyRows, templateId]);

  const handleRevert = useCallback(() => {
    setRows((prev) => prev.map((r) => ({ ...r, current_sample_text: r.original_sample_text })));
  }, []);

  if (loading) {
    return <div className="text-sm text-zinc-500">Loading overlays…</div>;
  }
  if (error) {
    return (
      <div className="rounded border border-red-800 bg-red-950/40 px-4 py-3 text-sm text-red-300">
        Failed to load overlays: {error}
      </div>
    );
  }
  if (!data) {
    return <div className="text-sm text-zinc-500">No data.</div>;
  }
  if (rows.length === 0) {
    return (
      <div className="rounded border border-zinc-800 bg-zinc-950 px-4 py-3 text-sm text-zinc-400">
        This template has no overlays to edit. Either analysis hasn&apos;t
        produced a recipe yet, or the recipe has zero text overlays.
      </div>
    );
  }

  // Group phrases by slot for visual structure. `phraseGroups` is already
  // ordered (rows come ordered, grouping preserves slot order), so we just
  // bucket — no extra sort needed.
  const phrasesBySlot = new Map<number, { group: typeof phraseGroups[number]; index: number }[]>();
  phraseGroups.forEach((g, index) => {
    const bucket = phrasesBySlot.get(g.slot_index) ?? [];
    bucket.push({ group: g, index });
    phrasesBySlot.set(g.slot_index, bucket);
  });
  const slotOrder = Array.from(phrasesBySlot.keys()).sort((a, b) => a - b);

  return (
    <div className="space-y-6 max-w-4xl">
      <header className="space-y-2">
        <h2 className="text-base font-semibold text-white">Edit overlay text</h2>
        <p className="text-xs text-zinc-400 leading-relaxed">
          One row per on-screen phrase — type the full line once and the
          cumulative word-by-word reveal still happens at render time.
          Changes apply immediately and the next render against this
          template uses your edits.{" "}
          <span className="text-amber-300">
            Reanalyzing the template will overwrite these edits
          </span>{" "}
          with the agent&apos;s fresh output — re-edit after reanalyze if you
          want the changes back.
        </p>
      </header>

      {saveError && (
        <div className="rounded border border-red-800 bg-red-950/40 px-4 py-3 text-sm text-red-300">
          Save failed: {saveError}
        </div>
      )}

      <div className="flex items-center gap-3 text-xs">
        <button
          type="button"
          onClick={handleSave}
          disabled={saving || dirtyRows.length === 0}
          className="bg-emerald-700 hover:bg-emerald-600 disabled:bg-zinc-800 disabled:text-zinc-500 disabled:cursor-not-allowed text-white px-3 py-1.5 rounded font-medium"
        >
          {saving ? "Saving…" : `Save ${dirtyPhraseCount} phrase${dirtyPhraseCount === 1 ? "" : "s"}`}
        </button>
        <button
          type="button"
          onClick={handleRevert}
          disabled={saving || dirtyRows.length === 0}
          className="bg-zinc-800 hover:bg-zinc-700 disabled:opacity-50 disabled:cursor-not-allowed text-zinc-200 px-3 py-1.5 rounded"
        >
          Revert
        </button>
        {lastSavedAt && (
          <span className="text-emerald-400">Saved at {lastSavedAt}</span>
        )}
        <span className="text-zinc-500 ml-auto">
          {phraseGroups.length} phrase{phraseGroups.length === 1 ? "" : "s"}
          {" · "}
          {rows.length} overlay{rows.length === 1 ? "" : "s"} total
          {dirtyPhraseCount > 0 && (
            <> · <span className="text-amber-400">{dirtyPhraseCount} phrase{dirtyPhraseCount === 1 ? "" : "s"} modified</span></>
          )}
        </span>
      </div>

      <div className="space-y-6">
        {slotOrder.map((slot_index) => {
          const slotPhrases = phrasesBySlot.get(slot_index) ?? [];
          return (
            <section key={slot_index} className="space-y-2">
              <h3 className="text-xs uppercase tracking-wider text-zinc-500">
                Slot {slot_index}
              </h3>
              <div className="space-y-2">
                {slotPhrases.map(({ group, index: groupIndex }) => {
                  const memberCount = group.member_row_indices.length;
                  const patternLabel =
                    group.pattern === "cumulative"
                      ? `cumulative reveal · ${memberCount} stages`
                      : group.pattern === "per_word"
                        ? `per-word reveal · ${memberCount} words`
                        : "single overlay";
                  return (
                    <div
                      key={`s${slot_index}-g${groupIndex}`}
                      className={`rounded border px-3 py-2 ${
                        group.dirty
                          ? "border-amber-600 bg-amber-950/20"
                          : "border-zinc-800 bg-zinc-950"
                      }`}
                    >
                      <div className="flex items-center gap-3 text-[10px] text-zinc-500 mb-1.5">
                        {group.role && (
                          <span className="px-1.5 py-0.5 bg-zinc-800 rounded">
                            {group.role}
                          </span>
                        )}
                        {group.start_s !== null && group.end_s !== null && (
                          <span className="font-mono">
                            {group.start_s.toFixed(2)}s → {group.end_s.toFixed(2)}s
                          </span>
                        )}
                        <span className="text-zinc-600">{patternLabel}</span>
                        {group.dirty && (
                          <span className="text-amber-400 ml-auto">
                            Modified
                          </span>
                        )}
                      </div>
                      <input
                        type="text"
                        value={group.display_text}
                        onChange={(e) => handlePhraseEdit(groupIndex, e.target.value)}
                        placeholder="(empty — overlay hidden)"
                        className="w-full bg-zinc-900 border border-zinc-700 focus:border-emerald-600 outline-none rounded px-2 py-1.5 text-sm text-white font-mono"
                      />
                      {group.dirty && memberCount > 1 && (
                        <div className="text-[10px] text-zinc-500 mt-1 font-mono">
                          {group.member_row_indices.map((rowIdx, k) => {
                            const r = rows[rowIdx];
                            const txt = r.current_sample_text || "(hidden)";
                            return (
                              <span key={rowIdx}>
                                {k > 0 && <span className="text-zinc-700"> · </span>}
                                <span className="text-zinc-400">#{r.overlay_index}</span>{" "}
                                {txt}
                              </span>
                            );
                          })}
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
            </section>
          );
        })}
      </div>
    </div>
  );
}
