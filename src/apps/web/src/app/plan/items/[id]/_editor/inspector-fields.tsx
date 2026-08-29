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

import { useEffect, useState } from "react";

import { INTRO_FONTS, resolveCssFont } from "@/lib/overlay-constants";
import { normalizeEditableHex } from "./editor-color";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { cn } from "@/lib/cn";

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
    <Input
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
      className="h-7 w-[76px] rounded-md border-zinc-200 px-2 text-[12px] uppercase text-[#0c0c0e]"
    />
  );
}

/** Font picker: button showing the current family in its REAL face, opening a
 * CSS-previewed option list (each INTRO_FONTS entry rendered in itself). */
export function FontSelect({
  value,
  onChange,
  ariaLabelPrefix = "Font",
  triggerClassName,
}: {
  value: string | null;
  onChange: (name: string) => void;
  /** Disambiguates the trigger button's aria-label when more than one
   * FontSelect renders at once (Lane PR-A: "This caption" + "All captions"
   * both show a font picker). Defaults to the original label. */
  ariaLabelPrefix?: string;
  /** Presentation-only sizing; font data and behavior remain shared. */
  triggerClassName?: string;
}) {
  const [open, setOpen] = useState(false);

  const current = value ?? "Playfair Display";
  const { family, weight } = resolveCssFont(current);

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <Button
          type="button"
          variant="outline"
          aria-haspopup="listbox"
          aria-expanded={open}
          aria-label={`${ariaLabelPrefix}: ${current}`}
          className={cn(
            "flex h-9 w-full items-center justify-between rounded-lg px-3 text-left text-[13px] font-normal text-[#0c0c0e]",
            triggerClassName,
          )}
        >
          <span className="truncate" style={{ fontFamily: family, fontWeight: weight }}>
            {current}
          </span>
          <span aria-hidden className="text-[9px] text-[#a1a1aa]">
            ⌄
          </span>
        </Button>
      </PopoverTrigger>
      <PopoverContent
        role="listbox"
        aria-label="Fonts"
        align="start"
        className="max-h-64 w-[--radix-popover-trigger-width] overflow-y-auto rounded-lg border-zinc-200 bg-white p-1 shadow-lg"
      >
        {INTRO_FONTS.map((f) => {
          const selected = f.name === current;
          return (
            <Button
              key={f.name}
              type="button"
              variant="ghost"
              role="option"
              aria-selected={selected}
              onClick={() => {
                onChange(f.name);
                setOpen(false);
              }}
              className={`block h-auto w-full justify-start truncate rounded-md px-2 py-1.5 text-left text-[14px] font-normal ${
                selected ? "bg-lime-50 text-[#0c0c0e]" : "text-[#3f3f46]"
              }`}
              style={{ fontFamily: f.cssFamily, fontWeight: f.weight }}
            >
              {f.name}
            </Button>
          );
        })}
      </PopoverContent>
    </Popover>
  );
}
