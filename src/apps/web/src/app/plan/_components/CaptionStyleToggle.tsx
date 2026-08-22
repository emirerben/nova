"use client";

import { Button } from "@/components/ui/button";
import type { VoiceoverCaptionStyle } from "@/lib/plan-api";

/**
 * Shared sentence/word caption-style segmented control — used by the post-gen
 * Captions tab for both narrated and talking-to-camera variants. Extracted so the
 * two archetypes don't carry two near-identical copies of this markup.
 */
export default function CaptionStyleToggle({
  value,
  onChange,
  saving = false,
}: {
  value: VoiceoverCaptionStyle;
  onChange: (style: VoiceoverCaptionStyle) => void;
  saving?: boolean;
}) {
  const options: Array<{ value: VoiceoverCaptionStyle; label: string }> = [
    { value: "sentence", label: "Sentence" },
    { value: "word", label: "Word-by-word" },
  ];
  return (
    <div>
      <div className="grid grid-cols-2 gap-2">
        {options.map((opt) => {
          const active = value === opt.value;
          return (
            <Button
              key={opt.value}
              type="button"
              variant={active ? "secondary" : "outline"}
              aria-pressed={active}
              disabled={saving}
              onClick={() => onChange(opt.value)}
              className="h-auto justify-start px-3 py-2 text-left"
            >
              <span className="block text-sm font-semibold">{opt.label}</span>
            </Button>
          );
        })}
      </div>
      {saving && <p className="mt-1 text-xs text-zinc-400">Saving…</p>}
    </div>
  );
}
