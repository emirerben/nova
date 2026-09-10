"use client";

/**
 * Per-slide editor for one photo or video inside a slide post (plans/024
 * follow-up eng-review, 2026-09-10). Deliberately NOT the main video
 * editor's `EditorShell` — that component is ~9,300 lines and every major
 * piece of its state assumes a rendered video `Job` with a variant and a
 * timeline; a bare `PlanItemAsset` has none of that. This is a small,
 * purpose-built shell that reuses render PRIMITIVES only (the look-preset
 * labels here, the FFmpeg filter fragments server-side in
 * `app/pipeline/slide_post/build.py`).
 *
 * v1 scope is deliberately narrow: one optional text overlay in a fixed
 * position, plus a look preset. Both apply identically to photo AND video
 * slides — there is no kind-based tool restriction in v1 (Overlays,
 * Captions, and Sounds are the tools that differ by kind or don't apply at
 * all, and none of the three ship in v1 — see the plan's "NOT in scope").
 */

import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { LOOK_PRESET_LABELS } from "@/lib/look-presets";
import type { LookPreset } from "@/lib/generative-api";
import type { SlideEdits, SlideRef, SlideTextOverlay } from "@/lib/plan-api";

const POSITIONS: SlideTextOverlay["position"][] = ["top", "center", "bottom"];
const POSITION_LABELS: Record<SlideTextOverlay["position"], string> = {
  top: "Top",
  center: "Center",
  bottom: "Bottom",
};

// v1 offers every preset EXCEPT the two whose adjustments (grain/vignette
// intensity) are tuned for full video footage, not a single still — keeps
// the picker to presets that read well on one frame. Matches look-presets.ts
// LOOK_PRESET_LABELS order.
const SLIDE_LOOK_PRESETS: LookPreset[] = [
  "none",
  "olive_film",
  "smoky_split_tone",
  "golden_hour",
  "faded_analog",
];

export type SlideEditModalProps = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  slide: SlideRef;
  previewUrl: string | null;
  onSave: (edits: SlideEdits | null) => void | Promise<void>;
};

function isDefaultEdits(edits: SlideEdits): boolean {
  return !edits.text && edits.look_preset === "none";
}

export default function SlideEditModal({
  open,
  onOpenChange,
  slide,
  previewUrl,
  onSave,
}: SlideEditModalProps) {
  const [textEnabled, setTextEnabled] = useState(Boolean(slide.edits?.text));
  const [content, setContent] = useState(slide.edits?.text?.content ?? "");
  const [position, setPosition] = useState<SlideTextOverlay["position"]>(
    slide.edits?.text?.position ?? "bottom",
  );
  const [lookPreset, setLookPreset] = useState<LookPreset>(slide.edits?.look_preset ?? "none");
  const [saving, setSaving] = useState(false);

  // Re-sync when a different slide opens in the same modal instance.
  useEffect(() => {
    setTextEnabled(Boolean(slide.edits?.text));
    setContent(slide.edits?.text?.content ?? "");
    setPosition(slide.edits?.text?.position ?? "bottom");
    setLookPreset(slide.edits?.look_preset ?? "none");
  }, [slide.id, slide.edits]);

  const trimmedContent = content.trim();
  const canSave = !textEnabled || trimmedContent.length > 0;

  async function handleSave() {
    const next: SlideEdits = {
      text:
        textEnabled && trimmedContent
          ? { content: trimmedContent.slice(0, 120), position }
          : null,
      look_preset: lookPreset,
    };
    setSaving(true);
    try {
      await onSave(isDefaultEdits(next) ? null : next);
      onOpenChange(false);
    } finally {
      setSaving(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>Edit {slide.kind === "video" ? "video" : "photo"}</DialogTitle>
        </DialogHeader>

        <div className="flex flex-col gap-4">
          <div className="relative mx-auto aspect-[3/4] w-40 overflow-hidden rounded-lg border border-zinc-200 bg-zinc-50">
            {previewUrl ? (
              slide.kind === "video" ? (
                // eslint-disable-next-line jsx-a11y/media-has-caption
                <video
                  src={previewUrl}
                  muted
                  playsInline
                  className="absolute inset-0 h-full w-full object-cover"
                />
              ) : (
                // eslint-disable-next-line @next/next/no-img-element
                <img
                  src={previewUrl}
                  alt=""
                  className="absolute inset-0 h-full w-full object-cover"
                />
              )
            ) : null}
          </div>

          <div>
            <div className="mb-1.5 flex items-center justify-between">
              <span className="text-[11px] font-semibold uppercase tracking-[0.14em] text-[#71717a]">
                Text
              </span>
              <Button
                type="button"
                size="sm"
                variant={textEnabled ? "secondary" : "outline"}
                onClick={() => setTextEnabled((v) => !v)}
              >
                {textEnabled ? "Remove" : "Add text"}
              </Button>
            </div>
            {textEnabled && (
              <div className="space-y-2">
                <Input
                  value={content}
                  onChange={(e) => setContent(e.target.value)}
                  maxLength={120}
                  placeholder="sold out in 2 days"
                  aria-label="Overlay text"
                />
                <div className="flex gap-1.5">
                  {POSITIONS.map((p) => (
                    <Button
                      key={p}
                      type="button"
                      size="sm"
                      variant={position === p ? "secondary" : "outline"}
                      onClick={() => setPosition(p)}
                      aria-pressed={position === p}
                    >
                      {POSITION_LABELS[p]}
                    </Button>
                  ))}
                </div>
              </div>
            )}
          </div>

          <div>
            <p className="mb-1.5 text-[11px] font-semibold uppercase tracking-[0.14em] text-[#71717a]">
              Styles
            </p>
            <div className="flex flex-wrap gap-1.5">
              {SLIDE_LOOK_PRESETS.map((preset) => (
                <Button
                  key={preset}
                  type="button"
                  size="sm"
                  variant={lookPreset === preset ? "secondary" : "outline"}
                  onClick={() => setLookPreset(preset)}
                  aria-pressed={lookPreset === preset}
                >
                  {LOOK_PRESET_LABELS[preset]}
                </Button>
              ))}
            </div>
          </div>
        </div>

        <DialogFooter>
          <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button type="button" onClick={() => void handleSave()} disabled={!canSave || saving}>
            {saving ? "Saving…" : "Save"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
