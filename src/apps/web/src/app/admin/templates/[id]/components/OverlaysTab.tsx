"use client";

/**
 * Overlay text editor — the escape hatch when the Layer-2 cumulative-reveal
 * pipeline produces wrong text. Admin sees every on-screen phrase (one or more
 * overlays in recipe_cached.slots[*].text_overlays[*]) as an inline-editable
 * row; Save routes each edit through POST /admin/templates/{id}/retime-phrase,
 * which re-derives the stage count and timing from the new wording.
 *
 * Backed by:
 *   - GET  /admin/templates/{id}/debug          (loads recipe_cached)
 *   - POST /admin/templates/{id}/retime-phrase  (writes each edited phrase)
 *
 * Re-running agents PRESERVES these edits (the build carries overlays forward).
 * To intentionally regenerate overlays from the agent output, use the
 * "Overwrite overlays from agents" button below.
 *
 * Editing model: each phrase's input is backed by a raw text buffer
 * (`editBuffers`), not the derived token view, so spaces (including trailing
 * ones while typing a multi-word phrase) are preserved verbatim. Tokenization
 * happens only at save time, server-side.
 */

import { useCallback, useEffect, useMemo, useState } from "react";

import {
  type TemplateDebugResponse,
  adminGetTemplateDebug,
  adminReanalyzeAgentic,
  adminReanalyzeTemplate,
  adminRetimeTemplatePhrase,
} from "@/lib/admin-api";
import {
  expandPhraseEditToMemberTexts,
  groupOverlayRowsIntoPhrases,
  type OverlayRow,
  type PhraseGroup,
} from "./phrase-grouping";

// Stable identity for a phrase across re-renders: slot index + the first
// member overlay's index. Used to key the raw edit buffer (group array indices
// shift when a phrase's stage count changes, so they can't be the key).
function phraseKey(rows: OverlayRow[], group: PhraseGroup): string {
  const firstOverlayIndex = rows[group.member_row_indices[0]]?.overlay_index ?? -1;
  return `${group.slot_index}:${firstOverlayIndex}`;
}

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
  // Raw per-phrase edit buffers, keyed by phraseKey(). Holds exactly what the
  // admin typed (spaces and all); absent key = unedited. Tokenized only on save.
  const [editBuffers, setEditBuffers] = useState<Record<string, string>>({});
  const [error, setError] = useState<string | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [overwriting, setOverwriting] = useState(false);
  const [lastSavedAt, setLastSavedAt] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const r = await adminGetTemplateDebug(templateId);
      setData(r);
      setRows(extractOverlayRows(r.recipe_cached));
      setEditBuffers({});
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [templateId]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  // Groups derive from rows only (rows are never mutated by editing now), so
  // group.display_text always reflects the persisted text. Dirtiness lives in
  // editBuffers instead.
  const phraseGroups = useMemo(() => groupOverlayRowsIntoPhrases(rows), [rows]);

  const isGroupDirty = useCallback(
    (group: PhraseGroup): boolean => {
      const buf = editBuffers[phraseKey(rows, group)];
      return buf !== undefined && buf.trim() !== group.display_text.trim();
    },
    [editBuffers, rows],
  );

  const dirtyGroups = useMemo(
    () => phraseGroups.filter(isGroupDirty),
    [phraseGroups, isGroupDirty],
  );
  const dirtyPhraseCount = dirtyGroups.length;

  const handlePhraseEdit = useCallback(
    (key: string, newText: string) => {
      // Store the raw string verbatim — no tokenize/trim — so trailing spaces
      // survive while typing a multi-word phrase. Splitting happens at save.
      setEditBuffers((prev) => ({ ...prev, [key]: newText }));
    },
    [],
  );

  const handleSave = useCallback(async () => {
    if (dirtyGroups.length === 0) return;
    setSaving(true);
    setSaveError(null);
    try {
      // Route every edited phrase through retime-phrase: the backend re-derives
      // the stage COUNT (= word count) and per-word timings, so changing the
      // wording reflows the reveal (a text-only PATCH would leave stale stages
      // and timings). Process in reverse order (slot desc, then first
      // overlay_index desc) so replacing a later phrase's overlays — which can
      // change the count — never invalidates the overlay_index of an earlier,
      // not-yet-processed phrase.
      const sorted = [...dirtyGroups].sort((a, b) => {
        if (a.slot_index !== b.slot_index) return b.slot_index - a.slot_index;
        return (
          rows[b.member_row_indices[0]].overlay_index -
          rows[a.member_row_indices[0]].overlay_index
        );
      });
      let updated: TemplateDebugResponse | null = data;
      for (const g of sorted) {
        const member_overlay_indices = g.member_row_indices.map(
          (ri) => rows[ri].overlay_index,
        );
        const rawText = editBuffers[phraseKey(rows, g)] ?? g.display_text;
        updated = await adminRetimeTemplatePhrase(templateId, {
          slot_index: g.slot_index,
          member_overlay_indices,
          new_text: rawText,
          // pattern tells the backend whether to reflow into N reveal stages
          // (cumulative/per_word) or keep ONE static overlay (singleton).
          pattern: g.pattern,
        });
      }
      if (updated) {
        setData(updated);
        setRows(extractOverlayRows(updated.recipe_cached));
      }
      setEditBuffers({});
      setLastSavedAt(new Date().toLocaleTimeString());
    } catch (e) {
      setSaveError((e as Error).message);
    } finally {
      setSaving(false);
    }
  }, [dirtyGroups, rows, data, templateId, editBuffers]);

  const handleRevert = useCallback(() => {
    setEditBuffers({});
  }, []);

  const handleOverwriteFromAgents = useCallback(async () => {
    if (
      !confirm(
        "Overwrite overlays from agents?\n\n" +
          "This re-runs the agent stack and REPLACES the current text overlays " +
          "with freshly generated ones — your manual overlay edits will be lost. " +
          "(Normal 'Re-run agents' preserves overlays; this button is the explicit " +
          "opt-in to regenerate them.)\n\n" +
          "Takes a few minutes; overlays update once analysis completes. Continue?",
      )
    ) {
      return;
    }
    setOverwriting(true);
    setSaveError(null);
    try {
      if (data?.template.is_agentic) {
        await adminReanalyzeAgentic(templateId, true, true);
      } else {
        await adminReanalyzeTemplate(templateId, true);
      }
      setEditBuffers({});
      setLastSavedAt(null);
      alert(
        "Re-running agents to regenerate overlays. This takes a few minutes — " +
          "refresh this tab once analysis completes to see the new overlays.",
      );
    } catch (e) {
      setSaveError((e as Error).message);
    } finally {
      setOverwriting(false);
    }
  }, [data, templateId]);

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
          <span className="text-emerald-300">
            Re-running the agents preserves these edits.
          </span>{" "}
          To regenerate overlays from scratch, use{" "}
          <span className="text-amber-300">Overwrite overlays from agents</span>.
        </p>
      </header>

      {saveError && (
        <div className="rounded border border-red-800 bg-red-950/40 px-4 py-3 text-sm text-red-300">
          {saveError}
        </div>
      )}

      <div className="flex items-center gap-3 text-xs">
        <button
          type="button"
          onClick={handleSave}
          disabled={saving || overwriting || dirtyPhraseCount === 0}
          className="bg-emerald-700 hover:bg-emerald-600 disabled:bg-zinc-800 disabled:text-zinc-500 disabled:cursor-not-allowed text-white px-3 py-1.5 rounded font-medium"
        >
          {saving ? "Saving…" : `Save ${dirtyPhraseCount} phrase${dirtyPhraseCount === 1 ? "" : "s"}`}
        </button>
        <button
          type="button"
          onClick={handleRevert}
          disabled={saving || overwriting || dirtyPhraseCount === 0}
          className="bg-zinc-800 hover:bg-zinc-700 disabled:opacity-50 disabled:cursor-not-allowed text-zinc-200 px-3 py-1.5 rounded"
        >
          Revert
        </button>
        <button
          type="button"
          onClick={handleOverwriteFromAgents}
          disabled={saving || overwriting}
          title="Re-run the agents and replace these overlays with freshly generated ones"
          className="bg-amber-800 hover:bg-amber-700 disabled:opacity-50 disabled:cursor-not-allowed text-amber-100 px-3 py-1.5 rounded"
        >
          {overwriting ? "Starting…" : "Overwrite overlays from agents"}
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
                {slotPhrases.map(({ group }) => {
                  const memberCount = group.member_row_indices.length;
                  const key = phraseKey(rows, group);
                  const dirty = isGroupDirty(group);
                  const value = editBuffers[key] ?? group.display_text;
                  const patternLabel =
                    group.pattern === "cumulative"
                      ? `cumulative reveal · ${memberCount} stages`
                      : group.pattern === "per_word"
                        ? `per-word reveal · ${memberCount} words`
                        : "single overlay";
                  // Stage preview from the live buffer (singletons stay one line).
                  const previewTexts =
                    dirty && memberCount > 1 && group.pattern !== "singleton"
                      ? expandPhraseEditToMemberTexts(group, value)
                      : null;
                  return (
                    <div
                      key={key}
                      className={`rounded border px-3 py-2 ${
                        dirty
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
                        {dirty && (
                          <span className="text-amber-400 ml-auto">
                            Modified
                          </span>
                        )}
                      </div>
                      <input
                        type="text"
                        value={value}
                        onChange={(e) => handlePhraseEdit(key, e.target.value)}
                        placeholder="(empty — overlay hidden)"
                        className="w-full bg-zinc-900 border border-zinc-700 focus:border-emerald-600 outline-none rounded px-2 py-1.5 text-sm text-white font-mono"
                      />
                      {previewTexts && (
                        <div className="text-[10px] text-zinc-500 mt-1 font-mono">
                          {previewTexts.map((txt, k) => (
                            <span key={k}>
                              {k > 0 && <span className="text-zinc-700"> · </span>}
                              {txt || "(hidden)"}
                            </span>
                          ))}
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
