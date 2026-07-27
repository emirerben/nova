"use client";

/**
 * inspector-fields — the two form primitives shared by every editor panel that
 * edits typography: the font picker and the hex input.
 *
 * Extracted from InspectorPanel when the Captions drawer took ownership of the
 * variant-wide "All captions" styling (it previously lived in the inspector).
 * Both panels must offer the SAME font list and the same hex-commit semantics —
 * duplicating them is how two pickers drift into offering different fonts.
 *
 * Behavior is byte-identical to the InspectorPanel originals; only the file
 * moved. Anything that changes here changes for both panels by construction.
 */

import { useEffect, useRef, useState } from "react";

import { INTRO_FONTS, resolveCssFont } from "@/lib/overlay-constants";
import { normalizeEditableHex } from "./editor-color";

export function HexInput({
  value,
  onChange,
  ariaLabel = "Fill color hex",
}: {
  value: string;
  onChange: (hex: string) => void;
  /** Disambiguates this input's aria-label when more than one HexInput
   * renders at once (Lane PR-A: "This caption" adds a second color field
   * alongside the existing Fill/Highlight rows). Defaults to the original. */
  ariaLabel?: string;
}) {
  const [draft, setDraft] = useState(value);
  // Follow external changes (e.g. the swatch or a preset).
  useEffect(() => setDraft(value), [value]);
  function commit() {
    const hex = normalizeEditableHex(draft);
    if (hex) onChange(hex);
    else setDraft(value);
  }
  return (
    <input
      type="text"
      aria-label={ariaLabel}
      value={draft}
      onChange={(e) => {
        const next = e.target.value;
        setDraft(next);
        const hex = normalizeEditableHex(next);
        if (hex) onChange(hex);
      }}
      onBlur={commit}
      onKeyDown={(e) => {
        if (e.key === "Enter") commit();
      }}
      className="h-7 w-[76px] rounded-md border border-zinc-200 px-2 text-[12px] uppercase text-[#0c0c0e] focus:border-lime-500/60 focus:outline-none"
    />
  );
}

/** Font picker: button showing the current family in its REAL face, opening a
 * CSS-previewed option list (each INTRO_FONTS entry rendered in itself). */
export function FontSelect({
  value,
  onChange,
  ariaLabelPrefix = "Font",
}: {
  value: string | null;
  onChange: (name: string) => void;
  /** Disambiguates the trigger button's aria-label when more than one
   * FontSelect renders at once (Lane PR-A: "This caption" + "All captions"
   * both show a font picker). Defaults to the original label. */
  ariaLabelPrefix?: string;
}) {
  const [open, setOpen] = useState(false);
  const listRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        setOpen(false);
      }
    };
    document.addEventListener("keydown", onKey, true);
    return () => document.removeEventListener("keydown", onKey, true);
  }, [open]);

  const current = value ?? "Playfair Display";
  const { family, weight } = resolveCssFont(current);

  return (
    <div className="relative">
      <button
        type="button"
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-label={`${ariaLabelPrefix}: ${current}`}
        onClick={() => setOpen((o) => !o)}
        className="flex h-9 w-full items-center justify-between rounded-lg border border-zinc-200 bg-white px-3 text-left text-[13px] text-[#0c0c0e] hover:border-zinc-400 focus:border-lime-500/60 focus:outline-none"
      >
        <span className="truncate" style={{ fontFamily: family, fontWeight: weight }}>
          {current}
        </span>
        <span aria-hidden className="text-[9px] text-[#a1a1aa]">
          ⌄
        </span>
      </button>
      {open && (
        <>
          <div className="fixed inset-0 z-10" aria-hidden onClick={() => setOpen(false)} />
          <div
            ref={listRef}
            role="listbox"
            aria-label="Fonts"
            className="absolute left-0 right-0 z-20 mt-1 max-h-64 overflow-y-auto rounded-lg border border-zinc-200 bg-white py-1 shadow-lg"
          >
            {INTRO_FONTS.map((f) => {
              const selected = f.name === current;
              return (
                <button
                  key={f.name}
                  type="button"
                  role="option"
                  aria-selected={selected}
                  onClick={() => {
                    onChange(f.name);
                    setOpen(false);
                  }}
                  className={`block w-full truncate px-3 py-1.5 text-left text-[14px] hover:bg-zinc-50 ${
                    selected ? "bg-lime-50 text-[#0c0c0e]" : "text-[#3f3f46]"
                  }`}
                  style={{ fontFamily: f.cssFamily, fontWeight: f.weight }}
                >
                  {f.name}
                </button>
              );
            })}
          </div>
        </>
      )}
    </div>
  );
}
