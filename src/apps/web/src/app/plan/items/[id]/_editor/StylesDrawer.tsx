"use client";

import { useEffect, useState } from "react";
import StyleChip from "@/components/ui/StyleChip";
import {
  getGenerativeStyleSets,
  type GenerativeStyleSet,
  type LookPreset,
} from "@/lib/generative-api";
import { lookPresetLabel, lookPreviewStyles } from "@/lib/look-presets";

export default function StylesDrawer({
  sampleText,
  appliedStyleSetId,
  onRestyleAll,
  availableLookPresets = [],
  selectedLookPreset = null,
  lookPresetMixed = false,
  lookPreviewUrl = null,
  onSelectLook,
}: {
  sampleText: string | null;
  appliedStyleSetId: string | null;
  onRestyleAll?: (styleSet: GenerativeStyleSet) => void;
  availableLookPresets?: LookPreset[];
  selectedLookPreset?: LookPreset | null;
  lookPresetMixed?: boolean;
  lookPreviewUrl?: string | null;
  onSelectLook?: (preset: LookPreset) => void;
}) {
  const textStylesEnabled = !!onRestyleAll;
  const [styleSets, setStyleSets] = useState<GenerativeStyleSet[]>([]);
  const [loading, setLoading] = useState(textStylesEnabled);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!textStylesEnabled) {
      setStyleSets([]);
      setLoading(false);
      setError(null);
      return;
    }
    let active = true;
    setLoading(true);
    getGenerativeStyleSets()
      .then((sets) => {
        if (!active) return;
        setStyleSets(sets);
        setError(null);
      })
      .catch(() => {
        if (!active) return;
        setStyleSets([]);
        setError("Styles could not load.");
      })
      .finally(() => {
        if (active) setLoading(false);
      });

    return () => {
      active = false;
    };
  }, [textStylesEnabled]);

  const hasVideoLooks = availableLookPresets.length > 0;

  return (
    <div className="min-h-0 flex-1 overflow-y-auto px-5 pb-5">
      {hasVideoLooks && (
        <section aria-labelledby="video-look-heading">
          <div className="mb-3 flex items-center justify-between gap-3">
            <p id="video-look-heading" className="text-[12px] font-semibold text-[#3f3f46]">
              Video look
            </p>
            {lookPresetMixed && (
              <span className="text-[11px] text-[#71717a]">Mixed</span>
            )}
          </div>
          <div role="radiogroup" aria-label="Video look" className="grid grid-cols-2 gap-2.5">
            {availableLookPresets.map((preset) => (
              <VideoLookCard
                key={preset}
                preset={preset}
                selected={selectedLookPreset === preset}
                previewUrl={lookPreviewUrl}
                disabled={!onSelectLook}
                onSelect={() => onSelectLook?.(preset)}
              />
            ))}
          </div>
        </section>
      )}

      {onRestyleAll && (
        <section
          aria-labelledby="text-style-heading"
          className={hasVideoLooks ? "mt-5 border-t border-zinc-100 pt-5" : undefined}
        >
          <p id="text-style-heading" className="mb-3 text-[12px] font-semibold text-[#3f3f46]">
            Text style
          </p>

          {loading && (
            <div className="grid grid-cols-2 gap-2.5" aria-label="Loading styles">
              {Array.from({ length: 4 }).map((_, index) => (
                <div
                  key={index}
                  className="h-[84px] rounded-lg border border-zinc-200 bg-zinc-100 motion-safe:animate-pulse"
                />
              ))}
            </div>
          )}

          {!loading && error && (
            <p className="rounded-lg border border-zinc-200 bg-zinc-50 px-3 py-2 text-[12px] text-[#71717a]">
              {error}
            </p>
          )}

          {!loading && !error && styleSets.length === 0 && (
            <p className="rounded-lg border border-zinc-200 bg-zinc-50 px-3 py-2 text-[12px] text-[#71717a]">
              No styles available.
            </p>
          )}

          {!loading && !error && styleSets.length > 0 && (
            <div role="radiogroup" aria-label="Text styles" className="grid grid-cols-2 gap-2.5">
              {styleSets.map((styleSet) => (
                <StyleChip
                  key={styleSet.id}
                  styleSet={styleSet}
                  selected={appliedStyleSetId === styleSet.id}
                  sampleText={sampleText ?? undefined}
                  darkTile
                  onSelect={() => onRestyleAll(styleSet)}
                />
              ))}
            </div>
          )}
        </section>
      )}
    </div>
  );
}

function VideoLookCard({
  preset,
  selected,
  previewUrl,
  disabled,
  onSelect,
}: {
  preset: LookPreset;
  selected: boolean;
  previewUrl: string | null;
  disabled: boolean;
  onSelect: () => void;
}) {
  const preview = lookPreviewStyles(preset);
  return (
    <button
      type="button"
      role="radio"
      aria-checked={selected}
      disabled={disabled}
      onClick={onSelect}
      className={`group overflow-hidden rounded-lg border text-left focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500 disabled:cursor-not-allowed disabled:opacity-50 ${
        selected
          ? "border-[#0c0c0e] ring-1 ring-[#0c0c0e]"
          : "border-zinc-200 hover:border-zinc-400"
      }`}
    >
      <span className="relative block h-[76px] overflow-hidden bg-[linear-gradient(145deg,#b9c1c7,#6d747b)]">
        {previewUrl && (
          <video
            aria-hidden
            muted
            playsInline
            preload="metadata"
            src={`${previewUrl}#t=0.05`}
            className="h-full w-full object-cover"
            style={preview.video}
          />
        )}
        {preview.tint && (
          <span aria-hidden className="pointer-events-none absolute inset-0" style={preview.tint} />
        )}
        {preview.grain && (
          <span aria-hidden className="pointer-events-none absolute inset-0" style={preview.grain} />
        )}
      </span>
      <span
        className={`block px-2.5 py-2 text-[11px] font-semibold ${
          selected ? "bg-[#0c0c0e] text-white" : "bg-white text-[#3f3f46]"
        }`}
      >
        {lookPresetLabel(preset)}
      </span>
    </button>
  );
}
