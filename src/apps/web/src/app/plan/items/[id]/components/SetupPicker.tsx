"use client";

/**
 * SetupPicker — the TYPE / STYLE accordion for the plan-item setup zone.
 *
 * Design source: Paper file "Nova — Product Design Audit", page "Item page —
 * Format card explorations" (boards A, A3a, A3b, A4–A6). One visual section is
 * open at a time: picking a TYPE collapses the poster rail into a receipt bar;
 * for montage the STYLE shelf opens next and collapses the same way. Receipt
 * "Change" reopens its section. TYPE and STYLE cards share one size.
 *
 * Cards are muted video loops: poster frame at rest, playback on hover/focus
 * (skipped under prefers-reduced-motion). Poster/loop media is bundled
 * placeholder footage under /public/plan/{type-posters,style-tiles} — swap for
 * curated brand loops without touching this component.
 */

import { useRef, useState } from "react";
import type { MontagePreset } from "@/lib/plan-api";
import type { PickerEditFormat } from "@/lib/edit-format";

/** Subset of updatePlanItem's PATCH body this picker can send. */
export type SetupPatch = {
  edit_format?: string;
  montage_preset?: MontagePreset;
  content_mode?: "existing_footage" | "create_new";
};

const TYPE_MEDIA: Record<PickerEditFormat, { poster: string; video: string }> = {
  montage: { poster: "/plan/type-posters/montage.jpg", video: "/plan/type-posters/montage.mp4" },
  narrated_planned: { poster: "/plan/type-posters/voiceover.jpg", video: "/plan/type-posters/voiceover.mp4" },
  subtitled: { poster: "/plan/type-posters/talking.jpg", video: "/plan/type-posters/talking.mp4" },
  talking_head: { poster: "/plan/type-posters/broll.jpg", video: "/plan/type-posters/broll.mp4" },
};

const TYPE_COPY: Record<
  PickerEditFormat,
  { label: string; desc: string; meta: string }
> = {
  montage: {
    label: "Montage",
    desc: "Your clips cut to music, beat by beat",
    meta: "Needs 3+ clips",
  },
  narrated_planned: {
    label: "Voiceover",
    desc: "Your voice tells the story over your footage",
    meta: "Voice + clips",
  },
  subtitled: {
    label: "Talking to camera",
    desc: "You on screen, with editable captions",
    meta: "1 clip of you talking",
  },
  talking_head: {
    label: "Talking-head B-roll",
    desc: "Your talking clip, with other footage cut in",
    meta: "1 talking clip + extra footage",
  },
};

const STYLE_TILES: {
  value: MontagePreset;
  label: string;
  desc: string;
  poster: string;
  video: string;
}[] = [
  {
    value: "classic",
    label: "Classic",
    desc: "Full-screen cuts in sequence",
    poster: "/plan/style-tiles/classic.jpg",
    video: "/plan/style-tiles/classic.mp4",
  },
  {
    value: "masonry",
    label: "Masonry collage",
    desc: "Rounded clips on a white wall",
    poster: "/plan/style-tiles/masonry.jpg",
    video: "/plan/style-tiles/masonry.mp4",
  },
  {
    value: "polaroid_wall",
    label: "Polaroid wall",
    desc: "Oversized photo cards on a wall",
    poster: "/plan/style-tiles/polaroid.jpg",
    video: "/plan/style-tiles/polaroid.mp4",
  },
];

type OpenSection = "type" | "style" | null;

export type SetupPickerProps = {
  resolvedFormat: PickerEditFormat;
  isNarratedReady: boolean;
  contentMode: string;
  montagePreset: MontagePreset;
  subtitledEnabled: boolean;
  showTalkingHead: boolean;
  /** Item is already mid-setup (guide accepted / clips uploaded) — open on
      receipts instead of the poster rail. */
  startCollapsed?: boolean;
  onPatch: (updates: SetupPatch) => Promise<void>;
};

function prefersReducedMotion(): boolean {
  return (
    typeof window !== "undefined" &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches
  );
}

/** Shared hover-to-play media card. The whole button is the hover target so
    playback starts even when the pointer sits on the text scrim. */
function useHoverVideo() {
  const videoRef = useRef<HTMLVideoElement>(null);
  const play = () => {
    if (prefersReducedMotion()) return;
    videoRef.current?.play().catch(() => undefined);
  };
  const stop = () => {
    const v = videoRef.current;
    if (v) {
      v.pause();
      v.currentTime = 0;
    }
  };
  return { videoRef, play, stop };
}

function Eyebrow({ children }: { children: React.ReactNode }) {
  return (
    <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-[#71717a]">
      {children}
    </p>
  );
}

function Receipt({
  eyebrow,
  value,
  thumbSrc,
  onChange,
}: {
  eyebrow: string;
  value: string;
  thumbSrc: string;
  onChange: () => void;
}) {
  return (
    <div className="flex min-h-[44px] items-center gap-3.5 rounded-[14px] border border-zinc-200 bg-white px-4 py-3">
      {/* eslint-disable-next-line @next/next/no-img-element -- static bundled poster */}
      <img src={thumbSrc} alt="" className="h-9 w-[52px] shrink-0 rounded-md object-cover" />
      <div className="flex min-w-0 flex-1 items-baseline gap-2.5">
        <span className="shrink-0 text-[11px] font-semibold uppercase tracking-[0.18em] text-[#71717a]">
          {eyebrow}
        </span>
        <span className="truncate text-sm font-semibold text-[#0c0c0e]">{value}</span>
      </div>
      <button
        type="button"
        onClick={onChange}
        className="min-h-11 shrink-0 text-[13px] text-[#71717a] underline underline-offset-2 transition-colors hover:text-[#0c0c0e] focus-visible:outline focus-visible:outline-2 focus-visible:outline-lime-500 sm:min-h-7"
      >
        Change
      </button>
    </div>
  );
}

function SubModeChips({
  options,
  activeValue,
  onSelect,
}: {
  options: { value: string; label: string }[];
  activeValue: string;
  onSelect: (value: string) => void;
}) {
  return (
    <div className="flex flex-wrap gap-2" role="radiogroup">
      {options.map(({ value, label }) => {
        const active = value === activeValue;
        return (
          <button
            key={value}
            type="button"
            role="radio"
            aria-checked={active}
            onClick={() => {
              if (!active) onSelect(value);
            }}
            className={`min-h-11 rounded-full border px-4 py-2 text-[13px] transition-colors focus-visible:outline focus-visible:outline-2 focus-visible:outline-lime-500 sm:min-h-9 ${
              active
                ? "border-lime-600 bg-lime-50 font-semibold text-lime-800"
                : "border-zinc-200 bg-white font-medium text-[#71717a] hover:border-zinc-300"
            }`}
          >
            {label}
          </button>
        );
      })}
    </div>
  );
}

function TypeCard({
  value,
  active,
  saving,
  onSelect,
}: {
  value: PickerEditFormat;
  active: boolean;
  saving: boolean;
  onSelect: (value: PickerEditFormat) => void;
}) {
  const { videoRef, play, stop } = useHoverVideo();
  const copy = TYPE_COPY[value];
  const media = TYPE_MEDIA[value];
  return (
    <button
      type="button"
      role="radio"
      aria-checked={active}
      disabled={saving}
      onClick={() => onSelect(value)}
      onMouseEnter={play}
      onMouseLeave={stop}
      onFocus={play}
      onBlur={stop}
      className={`relative aspect-[3/4] w-[216px] shrink-0 snap-start overflow-hidden rounded-[18px] text-left transition-transform focus-visible:outline focus-visible:outline-2 focus-visible:outline-lime-500 disabled:cursor-wait sm:w-auto ${
        active
          ? "shadow-[0_0_0_3px_#65a30d,0_12px_30px_rgba(0,0,0,0.18)]"
          : "border border-zinc-200 hover:scale-[1.01]"
      }`}
    >
      <video
        ref={videoRef}
        src={media.video}
        poster={media.poster}
        muted
        loop
        playsInline
        preload="none"
        className="absolute inset-0 h-full w-full object-cover"
      />
      <div className="absolute inset-x-0 bottom-0 h-3/5 bg-gradient-to-b from-transparent via-[rgba(12,12,14,0.55)] to-[rgba(12,12,14,0.94)]" />
      {active && (
        <span className="absolute left-3 top-3 rounded-full bg-lime-600 px-2.5 py-1 text-[11px] font-semibold text-white">
          Selected
        </span>
      )}
      <div className="absolute inset-x-0 bottom-0 flex flex-col gap-1 p-4">
        <span className="font-display text-[21px] font-medium leading-tight text-white">
          {copy.label}
        </span>
        <span className="text-[12.5px] leading-[18px] text-white/[0.82]">{copy.desc}</span>
        <span className="pt-0.5 text-[11px] font-medium text-white/60">{copy.meta}</span>
      </div>
    </button>
  );
}

function StyleTile({
  tile,
  active,
  saving,
  onSelect,
}: {
  tile: (typeof STYLE_TILES)[number];
  active: boolean;
  saving: boolean;
  onSelect: (value: MontagePreset) => void;
}) {
  const { videoRef, play, stop } = useHoverVideo();
  return (
    <button
      type="button"
      role="radio"
      aria-checked={active}
      disabled={saving}
      onClick={() => onSelect(tile.value)}
      onMouseEnter={play}
      onMouseLeave={stop}
      onFocus={play}
      onBlur={stop}
      className={`relative aspect-[3/4] w-[216px] shrink-0 snap-start overflow-hidden rounded-[18px] text-left transition-transform focus-visible:outline focus-visible:outline-2 focus-visible:outline-lime-500 disabled:cursor-wait sm:w-auto ${
        active
          ? "shadow-[0_0_0_3px_#65a30d,0_12px_30px_rgba(0,0,0,0.18)]"
          : "border border-zinc-200 hover:scale-[1.01]"
      }`}
    >
      <video
        ref={videoRef}
        src={tile.video}
        poster={tile.poster}
        muted
        loop
        playsInline
        preload="none"
        className="absolute inset-0 h-full w-full object-cover"
      />
      <div className="absolute inset-x-0 bottom-0 h-1/2 bg-gradient-to-b from-transparent via-[rgba(12,12,14,0.55)] to-[rgba(12,12,14,0.94)]" />
      {active && (
        <span className="absolute left-3 top-3 rounded-full bg-lime-600 px-2.5 py-1 text-[11px] font-semibold text-white">
          Selected
        </span>
      )}
      <div className="absolute inset-x-0 bottom-0 flex flex-col gap-1 p-4">
        <span className="font-display text-[21px] font-medium leading-tight text-white">
          {tile.label}
        </span>
        <span className="text-[12.5px] leading-[18px] text-white/[0.82]">{tile.desc}</span>
      </div>
    </button>
  );
}

export default function SetupPicker({
  resolvedFormat,
  isNarratedReady,
  contentMode,
  montagePreset,
  subtitledEnabled,
  showTalkingHead,
  startCollapsed = false,
  onPatch,
}: SetupPickerProps) {
  // The poster rail is the page's opening moment; after a choice it collapses
  // to a receipt and (for montage) hands the stage to the STYLE shelf. Items
  // already mid-setup start on receipts so returning users land on a calm page.
  const [openSection, setOpenSection] = useState<OpenSection>(
    startCollapsed ? null : "type",
  );
  const [saving, setSaving] = useState(false);

  const isMontage = resolvedFormat === "montage";

  const typeValues: PickerEditFormat[] = [
    "montage",
    "narrated_planned",
    ...(subtitledEnabled ? (["subtitled"] as PickerEditFormat[]) : []),
    ...(showTalkingHead ? (["talking_head"] as PickerEditFormat[]) : []),
  ];

  const patch = async (updates: SetupPatch) => {
    setSaving(true);
    try {
      await onPatch(updates);
    } finally {
      setSaving(false);
    }
  };

  const selectType = async (value: PickerEditFormat) => {
    setOpenSection(value === "montage" ? "style" : null);
    if (value !== resolvedFormat) {
      await patch({ edit_format: value });
    }
  };

  const selectStyle = async (value: MontagePreset) => {
    setOpenSection(null);
    if (value !== montagePreset) {
      await patch({ montage_preset: value });
    }
  };

  const activeStyleTile =
    STYLE_TILES.find((t) => t.value === montagePreset) ?? STYLE_TILES[0];

  return (
    <div className="mb-4 space-y-2.5" data-testid="setup-picker">
      {/* ---- TYPE ---- */}
      {openSection === "type" ? (
        <div className="animate-fade-up space-y-3">
          <div className="flex items-baseline justify-between gap-3">
            <Eyebrow>Type</Eyebrow>
            <p className="hidden text-[13px] text-[#71717a] sm:block">
              You can change this until the first render
            </p>
          </div>
          <div
            className="-mx-1 flex snap-x gap-3.5 overflow-x-auto px-1 pb-1 sm:mx-0 sm:grid sm:grid-cols-2 sm:overflow-visible sm:px-0 lg:grid-cols-4"
            role="radiogroup"
            aria-label="Type"
          >
            {typeValues.map((value) => (
              <TypeCard
                key={value}
                value={value}
                active={resolvedFormat === value}
                saving={saving}
                onSelect={selectType}
              />
            ))}
          </div>
        </div>
      ) : (
        <div className="animate-fade-up">
          <Receipt
            eyebrow="Type"
            value={TYPE_COPY[resolvedFormat].label}
            thumbSrc={TYPE_MEDIA[resolvedFormat].poster}
            onChange={() => setOpenSection("type")}
          />
        </div>
      )}

      {/* Narrated sub-mode — quiet chips under the TYPE section */}
      {resolvedFormat === "narrated_planned" && (
        <SubModeChips
          options={[
            { value: "narrated_planned", label: "Planning to film" },
            { value: "narrated_ready", label: "Already filmed" },
          ]}
          activeValue={isNarratedReady ? "narrated_ready" : "narrated_planned"}
          onSelect={(value) => patch({ edit_format: value })}
        />
      )}

      {/* Montage sub-mode — content_mode override */}
      {isMontage && (
        <SubModeChips
          options={[
            { value: "create_new", label: "Planning to film" },
            { value: "existing_footage", label: "Already filmed" },
          ]}
          activeValue={contentMode === "existing_footage" ? "existing_footage" : "create_new"}
          onSelect={(value) => patch({ content_mode: value as "create_new" | "existing_footage" })}
        />
      )}

      {/* ---- STYLE (montage only) ---- */}
      {isMontage &&
        (openSection === "style" ? (
          <div className="animate-fade-up space-y-3 pt-2">
            <Eyebrow>Style</Eyebrow>
            <div
              className="-mx-1 flex snap-x gap-3.5 overflow-x-auto px-1 pb-1 sm:mx-0 sm:grid sm:grid-cols-2 sm:overflow-visible sm:px-0 lg:grid-cols-4"
              role="radiogroup"
              aria-label="Style"
            >
              {STYLE_TILES.map((tile) => (
                <StyleTile
                  key={tile.value}
                  tile={tile}
                  active={montagePreset === tile.value}
                  saving={saving}
                  onSelect={selectStyle}
                />
              ))}
            </div>
          </div>
        ) : (
          <div className="animate-fade-up">
            <Receipt
              eyebrow="Style"
              value={activeStyleTile.label}
              thumbSrc={activeStyleTile.poster}
              onChange={() => setOpenSection("style")}
            />
          </div>
        ))}
    </div>
  );
}
