"use client";

/**
 * CaptionsDrawer — the left-rail Captions tool.
 *
 * Owns everything caption-SCOPED: the cue list (with inline text editing),
 * find-and-fix, the subtitles switch, the caption language, and the
 * variant-wide "All captions" styling. The right inspector keeps only per-cue
 * detail ("This caption": emphasize, merge, per-cue font/color/size).
 *
 * ── Why three zones, and why only the middle one scrolls ──────────────────
 * Zone 1 (subtitles / language / find) and zone 3 ("All captions") are FIXED;
 * only the cue list scrolls. This is the load-bearing layout decision, not a
 * style preference: a 45s talking-head edit carries 30-40 cues, so globals
 * living inside the same scroller would sit ~40 rows below the fold. That is
 * exactly the discoverability bug this drawer exists to fix (before it, the
 * variant-wide styling was reachable only by first selecting one arbitrary
 * cue in the inspector) — putting them in the scroller would relocate the bug
 * rather than close it.
 *
 * Edits flow through the SAME session state as every other lane: cue text via
 * `onEditCueText` → patchBar → captionDirty, globals via `onPatchMeta` →
 * captionMetaPatch. There is no separate Apply here — Save commits captions
 * with everything else through commitEditorSession. Captions must never grow a
 * second commit model.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import type { CaptionMetaPatch } from "@/lib/edit-copilot/ops";
import type { CopilotCaptionMetaSnapshot } from "@/lib/edit-copilot/snapshot";
import type { CaptionLanguage } from "@/lib/plan-api";
import { formatTimecode } from "@/lib/timeline/time-format";
import { normalizeEditableHex } from "./editor-color";
import { FontSelect, HexInput } from "./inspector-fields";
import { InfoDot } from "@/components/ui/InfoDot";
import { Label } from "@/components/ui/label";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import {
  CAPTION_SIZE_MAX,
  CAPTION_SIZE_MIN,
  CAPTION_STROKE_MAX,
  DEFAULT_CAPTION_COLOR,
  DEFAULT_CAPTION_SIZE_PX,
} from "./caption-control-options";

const LANGUAGE_LABELS: Record<string, string> = { en: "English", tr: "Türkçe" };

export interface CaptionCueRow {
  id: string;
  text: string;
  start_s: number;
  end_s: number;
}

/** What the drawer is waiting on. Named stages, never one anonymous spinner —
 *  "Saving your edits" and "Re-transcribing" fail differently and the user
 *  needs to know which one they are in when it goes wrong. */
export type CaptionsBusyState = "idle" | "saving" | "transcribing";

export interface CaptionsDrawerControl {
  cues: CaptionCueRow[];
  /** Currently selected bar id, so the drawer highlights what the inspector shows. */
  selectedId: string | null;
  /** Playhead seconds — drives the "playing" row. */
  currentTime: number;
  meta: CopilotCaptionMetaSnapshot | null;
  /** "en" | "tr" for subtitled edits; null hides the language row (narrated). */
  language: string | null;
  readOnly: boolean;
  busy: CaptionsBusyState;
  error: string | null;
  /** Seek + select. The shell passes preserveOverlayTool so the drawer survives
   *  selection in overlay layout mode. */
  onSelectCue: (id: string) => void;
  onEditCueText: (id: string, text: string) => void;
  onPatchMeta: (patch: CaptionMetaPatch) => void;
  /** Returns how many cues changed. One undo step for the whole sweep. */
  onReplaceAll?: (find: string, replace: string) => number;
  /** Commits the session FIRST, then re-transcribes (both are destructive to
   *  cue text, and the session holds unsaved edits across every lane). */
  onChangeLanguage?: (language: CaptionLanguage) => void;
  /** Zero-cue recovery: a caption edit whose transcript came back empty. */
  onRetranscribe?: () => void;
}

export default function CaptionsDrawer({
  cues,
  selectedId,
  currentTime,
  meta,
  language,
  readOnly,
  busy,
  error,
  onSelectCue,
  onEditCueText,
  onPatchMeta,
  onReplaceAll,
  onChangeLanguage,
  onRetranscribe,
}: CaptionsDrawerControl) {
  const [editingId, setEditingId] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [replaceDraft, setReplaceDraft] = useState("");
  const [matchCursor, setMatchCursor] = useState(0);
  const [globalsOpen, setGlobalsOpen] = useState(false);
  const [pendingLang, setPendingLang] = useState<CaptionLanguage | null>(null);
  const [replacedNotice, setReplacedNotice] = useState<string | null>(null);
  const listRef = useRef<HTMLUListElement>(null);
  const findRef = useRef<HTMLInputElement>(null);

  const enabled = meta?.enabled ?? true;
  const working = busy !== "idle";
  const locked = readOnly || working;

  const activeIndex = useMemo(
    () => cues.findIndex((c) => currentTime >= c.start_s && currentTime < c.end_s),
    [cues, currentTime],
  );

  const matches = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return [] as number[];
    return cues.reduce<number[]>((acc, cue, i) => {
      if (cue.text.toLowerCase().includes(needle)) acc.push(i);
      return acc;
    }, []);
  }, [cues, query]);

  // Keep the cursor inside the match list as the text (and so the matches) change
  // under it — an edit that fixes the last match must not strand the counter.
  useEffect(() => {
    setMatchCursor((cursor) => (matches.length === 0 ? 0 : Math.min(cursor, matches.length - 1)));
  }, [matches.length]);

  // Follow the playhead. Only while playing-and-not-editing: yanking the list
  // out from under someone mid-keystroke is worse than losing the sync.
  useEffect(() => {
    if (editingId !== null || activeIndex < 0) return;
    const row = listRef.current?.querySelector<HTMLElement>(`[data-cue-index="${activeIndex}"]`);
    // Feature-detected: playhead following is a nicety, and jsdom (plus any
    // environment without it) must not throw out of this effect over it.
    if (typeof row?.scrollIntoView === "function") {
      row.scrollIntoView({ block: "nearest" });
    }
  }, [activeIndex, editingId]);

  const openCue = useCallback(
    (cue: CaptionCueRow, { edit }: { edit: boolean }) => {
      onSelectCue(cue.id);
      if (edit && !locked) setEditingId(cue.id);
    },
    [locked, onSelectCue],
  );

  const stepMatch = useCallback(
    (delta: number) => {
      if (matches.length === 0) return;
      const next = (matchCursor + delta + matches.length) % matches.length;
      setMatchCursor(next);
      const cue = cues[matches[next]];
      if (cue) onSelectCue(cue.id);
    },
    [cues, matchCursor, matches, onSelectCue],
  );

  const replaceAll = useCallback(() => {
    if (!onReplaceAll || matches.length === 0) return;
    const changed = onReplaceAll(query, replaceDraft);
    setReplacedNotice(
      changed === 1 ? "Replaced 1 line. Cmd+Z to undo." : `Replaced ${changed} lines. Cmd+Z to undo.`,
    );
  }, [matches.length, onReplaceAll, query, replaceDraft]);

  useEffect(() => {
    if (!replacedNotice) return;
    const t = setTimeout(() => setReplacedNotice(null), 6000);
    return () => clearTimeout(t);
  }, [replacedNotice]);

  // Cmd/Ctrl+F focuses Find while the drawer is mounted, instead of the browser's
  // own find — inside an editor, "find" means find in the captions.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "f") {
        e.preventDefault();
        findRef.current?.focus();
        findRef.current?.select();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, []);

  /** Roving ↑/↓ across cue rows, so the list is navigable without a mouse. */
  const onListKeyDown = useCallback((e: React.KeyboardEvent<HTMLUListElement>) => {
    if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
    const rows = Array.from(
      listRef.current?.querySelectorAll<HTMLButtonElement>("[data-cue-index]") ?? [],
    );
    if (rows.length === 0) return;
    const current = rows.findIndex((row) => row === document.activeElement);
    if (current === -1) return;
    e.preventDefault();
    const next = e.key === "ArrowDown" ? current + 1 : current - 1;
    rows[Math.max(0, Math.min(rows.length - 1, next))]?.focus();
  }, []);

  const patch = useCallback(
    (next: CaptionMetaPatch) => {
      if (locked) return;
      onPatchMeta(next);
    },
    [locked, onPatchMeta],
  );

  const sizePx = Math.round(meta?.size_px ?? DEFAULT_CAPTION_SIZE_PX);
  const color = normalizeEditableHex(meta?.color ?? null) ?? DEFAULT_CAPTION_COLOR;
  const strokeWidth = meta?.stroke_width ?? 4;
  const shadowEnabled = meta?.shadow_enabled ?? true;
  const fontLabel = meta?.font ?? "Default";

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      {/* ── ZONE 1 · fixed ─────────────────────────────────────────────── */}
      <div className="flex-none px-5 pb-3">
        <div className="flex min-h-11 items-center justify-between">
          <span className="text-[12px] font-semibold text-[#3f3f46]">Subtitles</span>
          <Button
            type="button"
            variant="ghost"
            size="icon-sm"
            role="switch"
            aria-checked={enabled}
            aria-label="Subtitles"
            disabled={locked}
            onClick={() => patch({ enabled: !enabled })}
            className={`relative h-6 w-11 shrink-0 rounded-full p-0 transition-colors focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500 disabled:cursor-not-allowed disabled:opacity-50 ${
              enabled ? "bg-[#0c0c0e] hover:bg-[#0c0c0e]" : "bg-zinc-200 hover:bg-zinc-200"
            }`}
          >
            <span
              aria-hidden
              className={`absolute top-1 h-4 w-4 rounded-full bg-white transition-transform ${
                enabled ? "translate-x-6" : "translate-x-1"
              }`}
            />
          </Button>
        </div>

        {language && onChangeLanguage && (
          <div className="mt-1">
            {pendingLang === null ? (
              <div className="flex flex-wrap items-center gap-x-2">
                <span className="inline-flex items-center rounded-full border border-lime-200 bg-lime-50 px-3 py-1 text-[11px] font-medium text-lime-800">
                  Captions in {LANGUAGE_LABELS[language] ?? language}
                </span>
                <Button
                  type="button"
                  variant="link"
                  aria-label="Change caption language"
                  disabled={locked}
                  onClick={() => setPendingLang(language === "tr" ? "en" : "tr")}
                  className="h-auto min-h-11 items-center px-1 text-[11px] font-semibold text-lime-700 underline underline-offset-2 hover:text-lime-800 disabled:cursor-not-allowed disabled:opacity-50 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500"
                >
                  Change
                </Button>
              </div>
            ) : (
              // Honest about BOTH halves: this saves the whole session (every
              // lane, not just captions) and then throws away every caption line.
              <div className="rounded-lg border border-zinc-200 bg-white px-3 py-2 text-[12px] text-[#3f3f46]">
                Switching to {LANGUAGE_LABELS[pendingLang] ?? pendingLang} saves your current
                edits, then re-transcribes. Every caption line is rewritten — your caption text
                edits are replaced.
                <div className="mt-2 flex flex-wrap items-center gap-2">
                  <Button
                    type="button"
                    variant="default"
                    disabled={locked}
                    onClick={() => {
                      const next = pendingLang;
                      setPendingLang(null);
                      onChangeLanguage(next);
                    }}
                    className="h-auto min-h-10 rounded-lg bg-[#0c0c0e] px-3 text-[12px] font-semibold text-white hover:bg-[#27272a] disabled:cursor-not-allowed disabled:opacity-50 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500"
                  >
                    Save &amp; re-transcribe
                  </Button>
                  <Button
                    type="button"
                    variant="link"
                    onClick={() => setPendingLang(null)}
                    className="h-auto min-h-10 px-1 text-[12px] text-[#71717a] underline underline-offset-2 hover:text-[#0c0c0e] focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500"
                  >
                    Cancel
                  </Button>
                </div>
              </div>
            )}
          </div>
        )}

        {enabled && cues.length > 0 && (
          <div className="mt-2">
            <Label
              htmlFor="captions-find"
              className="block text-[12px] font-semibold text-[#3f3f46]"
            >
              Find in captions
            </Label>
            <div className="mt-1 flex items-center gap-1.5">
              <Input
                id="captions-find"
                ref={findRef}
                type="text"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") {
                    e.preventDefault();
                    stepMatch(e.shiftKey ? -1 : 1);
                  }
                  if (e.key === "Escape" && query) {
                    e.preventDefault();
                    e.stopPropagation();
                    setQuery("");
                  }
                }}
                className="min-w-0 flex-1"
              />
              <span
                role="status"
                className="w-[52px] shrink-0 text-right text-[11px] tabular-nums text-[#71717a]"
              >
                {query.trim() === ""
                  ? ""
                  : matches.length === 0
                    ? "0"
                    : `${matchCursor + 1} of ${matches.length}`}
              </span>
              <Button
                type="button"
                variant="ghost"
                size="icon-sm"
                aria-label="Previous match"
                disabled={matches.length === 0}
                onClick={() => stepMatch(-1)}
                className="h-11 w-7 shrink-0 rounded-lg text-[11px] text-[#3f3f46] hover:bg-zinc-100 disabled:cursor-not-allowed disabled:opacity-40 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500"
              >
                ▲
              </Button>
              <Button
                type="button"
                variant="ghost"
                size="icon-sm"
                aria-label="Next match"
                disabled={matches.length === 0}
                onClick={() => stepMatch(1)}
                className="h-11 w-7 shrink-0 rounded-lg text-[11px] text-[#3f3f46] hover:bg-zinc-100 disabled:cursor-not-allowed disabled:opacity-40 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500"
              >
                ▼
              </Button>
            </div>

            {matches.length > 0 && onReplaceAll && (
              <div className="mt-1.5 flex items-center gap-1.5">
                <Input
                  type="text"
                  aria-label="Replace matches with"
                  placeholder="Replace with"
                  value={replaceDraft}
                  onChange={(e) => setReplaceDraft(e.target.value)}
                  className="min-h-11 min-w-0 flex-1 rounded-lg border-zinc-200 px-2.5 text-[13px] text-[#0c0c0e] placeholder:text-[#a1a1aa] focus-visible:border-lime-500/60"
                />
                <Button
                  type="button"
                  variant="outline"
                  disabled={locked}
                  onClick={replaceAll}
                  className="h-auto min-h-11 shrink-0 rounded-lg border-zinc-200 bg-white px-2.5 text-[12px] font-semibold text-[#0c0c0e] hover:border-zinc-400 disabled:cursor-not-allowed disabled:opacity-50 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500"
                >
                  Replace all
                </Button>
              </div>
            )}

            {replacedNotice && (
              <p role="status" className="mt-1.5 text-[11px] text-[#3f3f46]">
                {replacedNotice}
              </p>
            )}
          </div>
        )}
      </div>

      {/* ── ZONE 2 · scrolls ───────────────────────────────────────────── */}
      {!enabled ? (
        <div className="min-h-0 flex-1 px-5">
          <p className="rounded-xl border border-zinc-200 bg-white px-3 py-2 text-[12px] text-[#3f3f46]">
            Subtitles are off. Your caption lines are saved — turn subtitles on to edit them.
          </p>
        </div>
      ) : cues.length === 0 ? (
        <div className="min-h-0 flex-1 px-5">
          <p className="text-[13px] text-[#3f3f46]">No caption lines yet.</p>
          <p className="mt-1 text-[12px] text-[#71717a]">
            This edit&rsquo;s audio didn&rsquo;t produce a transcript.
          </p>
          {onRetranscribe && (
            <Button
              type="button"
              variant="secondary"
              disabled={locked}
              onClick={onRetranscribe}
              className="mt-3 h-auto min-h-11 w-full rounded-lg bg-zinc-100 text-[13px] font-semibold text-[#0c0c0e] hover:bg-zinc-200 disabled:cursor-not-allowed disabled:opacity-50 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500"
            >
              Re-transcribe
            </Button>
          )}
        </div>
      ) : (
        <ul
          ref={listRef}
          onKeyDown={onListKeyDown}
          aria-label="Caption lines"
          className="min-h-0 flex-1 space-y-0.5 overflow-y-auto px-5 pb-2"
        >
          {cues.map((cue, i) => {
            const playing = i === activeIndex;
            const isMatch = matches.includes(i);
            const atCursor = matches[matchCursor] === i;
            // Editing row is a plain <li> holding the <input> — an <input>
            // nested inside a <button> swallows clicks and keystrokes
            // (CaptionEditor learned this the hard way).
            if (editingId === cue.id) {
              return (
                <li
                  key={cue.id}
                  className="flex min-h-11 items-center gap-2 rounded-lg bg-lime-50 px-2 py-1"
                >
                  <span className="w-10 shrink-0 text-[11px] tabular-nums text-zinc-400">
                    {formatTimecode(cue.start_s)}
                  </span>
                  <Input
                    autoFocus
                    value={cue.text}
                    aria-label={`Edit caption at ${formatTimecode(cue.start_s)}`}
                    onChange={(e) => onEditCueText(cue.id, e.target.value)}
                    onBlur={() => setEditingId(null)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter" || e.key === "Escape") {
                        e.preventDefault();
                        e.stopPropagation();
                        setEditingId(null);
                      }
                    }}
                    className="h-auto min-h-9 min-w-0 flex-1 rounded border-lime-400 px-2 text-[13px] text-[#18181b] focus-visible:ring-0"
                  />
                </li>
              );
            }
            return (
              <li key={cue.id}>
                <Button
                  type="button"
                  variant="ghost"
                  data-cue-index={i}
                  aria-current={playing ? "true" : undefined}
                  aria-label={`Caption at ${formatTimecode(cue.start_s)}, ${cue.text}`}
                  onClick={() => openCue(cue, { edit: true })}
                  className={`flex h-auto min-h-11 w-full items-center justify-start gap-2 rounded-lg border-l-2 px-2 py-1 text-left text-[13px] font-normal transition-colors hover:text-inherit focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500 ${
                    playing
                      ? // Lime fill AND a left border: the active row must not be
                        // signalled by colour alone.
                        "border-lime-600 bg-lime-50 text-lime-900 hover:bg-lime-50"
                      : selectedId === cue.id
                        ? "border-zinc-300 bg-zinc-50 text-[#0c0c0e] hover:bg-zinc-50"
                        : "border-transparent text-[#3f3f46] hover:bg-zinc-50"
                  } ${atCursor ? "ring-1 ring-lime-400" : ""}`}
                >
                  <span className="w-10 shrink-0 text-[11px] tabular-nums text-zinc-400">
                    {formatTimecode(cue.start_s)}
                  </span>
                  <span className="min-w-0 flex-1">
                    {isMatch ? highlight(cue.text, query) : cue.text}
                  </span>
                </Button>
              </li>
            );
          })}
        </ul>
      )}

      {/* ── ZONE 3 · fixed ─────────────────────────────────────────────── */}
      {enabled && (
        <div className="flex-none border-t border-zinc-100 px-5 py-2">
          <div className="flex items-center gap-1">
            <Button
              type="button"
              variant="ghost"
              aria-expanded={globalsOpen}
              onClick={() => setGlobalsOpen((o) => !o)}
              className="h-auto min-h-11 min-w-0 flex-1 items-center justify-between gap-2 rounded-none px-0 text-left font-normal hover:bg-transparent focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500"
            >
              <span className="min-w-0 truncate">
                <span className="text-[12px] font-semibold text-[#3f3f46]">All captions</span>
                {!globalsOpen && (
                  // Informative while closed — read the current styling without opening.
                  <span className="ml-2 text-[11px] text-[#a1a1aa]">
                    {fontLabel} · {sizePx}
                  </span>
                )}
              </span>
              <span aria-hidden className="shrink-0 text-[9px] text-[#a1a1aa]">
                {globalsOpen ? "⌄" : "⌃"}
              </span>
            </Button>
            <InfoDot label="All captions" size="compact">
              Style changes here apply to every caption line.
            </InfoDot>
          </div>

          {globalsOpen && (
            <div className="pb-1">
              <FontSelect
                value={meta?.font ?? null}
                onChange={(name) => patch({ font: name })}
                ariaLabelPrefix="All captions font"
              />
              <div className="mt-2 flex items-center gap-2">
                <span className="w-[44px] shrink-0 text-[12px] font-semibold text-[#3f3f46]">
                  Size
                </span>
                <input
                  type="range"
                  aria-label="All captions font size"
                  min={CAPTION_SIZE_MIN}
                  max={CAPTION_SIZE_MAX}
                  step={1}
                  value={Math.min(CAPTION_SIZE_MAX, Math.max(CAPTION_SIZE_MIN, sizePx))}
                  disabled={locked}
                  onChange={(e) => patch({ size_px: Number(e.target.value) })}
                  className="min-w-0 flex-1 accent-[#0c0c0e] disabled:cursor-not-allowed"
                />
                <span className="w-8 shrink-0 text-right text-[12px] tabular-nums text-[#71717a]">
                  {sizePx}
                </span>
              </div>
              <div className="mt-2 flex items-center gap-2">
                <span className="w-[44px] shrink-0 text-[12px] font-semibold text-[#3f3f46]">
                  Color
                </span>
                <input
                  type="color"
                  aria-label="All captions fill color"
                  value={color}
                  disabled={locked}
                  onChange={(e) => patch({ color: e.target.value.toUpperCase() })}
                  className="h-6 w-8 shrink-0 cursor-pointer rounded border border-zinc-300 bg-white p-0 disabled:cursor-not-allowed"
                />
                <HexInput
                  value={color}
                  onChange={(hex) => patch({ color: hex })}
                  ariaLabel="All captions fill color hex"
                />
              </div>
              <div className="mt-2 flex items-center gap-2">
                <span className="w-[44px] shrink-0 text-[12px] font-semibold text-[#3f3f46]">
                  Stroke
                </span>
                <input
                  type="range"
                  aria-label="All captions stroke width"
                  min={0}
                  max={CAPTION_STROKE_MAX}
                  step={1}
                  value={Math.min(CAPTION_STROKE_MAX, Math.max(0, Math.round(strokeWidth)))}
                  disabled={locked}
                  onChange={(e) => patch({ stroke_width: Number(e.target.value) })}
                  className="min-w-0 flex-1 accent-[#0c0c0e] disabled:cursor-not-allowed"
                />
                <span className="w-8 shrink-0 text-right text-[12px] tabular-nums text-[#71717a]">
                  {Math.round(strokeWidth)}
                </span>
              </div>
              <div className="mt-2 flex min-h-11 items-center justify-between">
                <span className="text-[12px] font-semibold text-[#3f3f46]">Shadow</span>
                <Button
                  type="button"
                  variant="ghost"
                  size="icon-sm"
                  role="switch"
                  aria-checked={shadowEnabled}
                  aria-label="All captions shadow"
                  disabled={locked}
                  onClick={() => patch({ shadow_enabled: !shadowEnabled })}
                  className={`relative h-6 w-11 shrink-0 rounded-full p-0 transition-colors focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500 disabled:cursor-not-allowed disabled:opacity-50 ${
                    shadowEnabled ? "bg-[#0c0c0e] hover:bg-[#0c0c0e]" : "bg-zinc-200 hover:bg-zinc-200"
                  }`}
                >
                  <span
                    aria-hidden
                    className={`absolute top-1 h-4 w-4 rounded-full bg-white transition-transform ${
                      shadowEnabled ? "translate-x-6" : "translate-x-1"
                    }`}
                  />
                </Button>
              </div>
            </div>
          )}
        </div>
      )}

      {(working || error) && (
        <div className="flex-none px-5 pb-3">
          {working && (
            <p role="status" className="text-[12px] text-[#3f3f46]">
              {busy === "saving" ? "Saving your edits…" : "Re-transcribing…"}
            </p>
          )}
          {error && <p className="text-[12px] text-red-600">{error}</p>}
        </div>
      )}
    </div>
  );
}

/** Wraps every case-insensitive occurrence of `query` in a <mark>. */
function highlight(text: string, query: string) {
  const needle = query.trim();
  if (!needle) return text;
  const parts: React.ReactNode[] = [];
  const lower = text.toLowerCase();
  const lowerNeedle = needle.toLowerCase();
  let from = 0;
  for (;;) {
    const at = lower.indexOf(lowerNeedle, from);
    if (at === -1) break;
    if (at > from) parts.push(text.slice(from, at));
    parts.push(
      <mark key={at} className="rounded-sm bg-lime-200 text-[#0c0c0e]">
        {text.slice(at, at + needle.length)}
      </mark>,
    );
    from = at + needle.length;
  }
  parts.push(text.slice(from));
  return parts;
}
