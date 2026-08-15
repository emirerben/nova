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
  montagePreset: MontagePreset;
  subtitledEnabled: boolean;
  showTalkingHead: boolean;
  /** Item already carries an accepted filming guide — keep its planned flow
      instead of forcing the already-filmed default on re-selection. */
  hasGuide?: boolean;
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


function Chevron({ open }: { open: boolean }) {
  return (
    <span
      aria-hidden="true"
      className={`flex h-7 w-7 shrink-0 items-center justify-center rounded-full border border-zinc-200 bg-white transition-transform duration-[var(--t-accordion-dur,300ms)] motion-reduce:transition-none ${
        open ? "rotate-180" : ""
      }`}
    >
      <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
        <path
          d="M3.5 5.25 7 8.75l3.5-3.5"
          stroke="#3f3f46"
          strokeWidth="1.6"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      </svg>
    </span>
  );
}

/** Disclosure section: the receipt row is always present; the card panel
    expands/collapses beneath it (t-accordion: grid-rows 0fr↔1fr + opacity).
    No swap, no layout jump — the row is the toggle, the chevron points the way. */
function DisclosureSection({
  eyebrow,
  valueLabel,
  thumbSrc,
  open,
  hint,
  onToggle,
  children,
}: {
  eyebrow: string;
  valueLabel: string;
  thumbSrc: string;
  open: boolean;
  hint?: string;
  onToggle: () => void;
  children: React.ReactNode;
}) {
  const panelId = `${eyebrow.toLowerCase()}-panel`;
  return (
    <div>
      <button
        type="button"
        aria-expanded={open}
        aria-controls={panelId}
        onClick={onToggle}
        className={`flex min-h-[52px] w-full items-center gap-3.5 rounded-[14px] border bg-white px-4 py-3 text-left transition-colors focus-visible:outline focus-visible:outline-2 focus-visible:outline-lime-500 ${
          open ? "border-zinc-300" : "border-zinc-200 hover:border-zinc-300"
        }`}
      >
        {/* eslint-disable-next-line @next/next/no-img-element -- static bundled poster */}
        <img
          src={thumbSrc}
          alt=""
          className="h-9 w-[52px] shrink-0 rounded-md object-cover"
        />
        <span className="shrink-0 text-[11px] font-semibold uppercase tracking-[0.18em] text-[#71717a]">
          {eyebrow}
        </span>
        <span className="min-w-0 flex-1 truncate text-sm font-semibold text-[#0c0c0e]">
          {valueLabel}
        </span>
        {hint && !open && (
          <span className="hidden shrink-0 text-[13px] text-[#71717a] md:block">{hint}</span>
        )}
        <Chevron open={open} />
      </button>
      <div
        id={panelId}
        className="grid transition-[grid-template-rows,opacity] duration-[var(--t-accordion-dur,300ms)] ease-[var(--t-accordion-ease,cubic-bezier(0.23,1,0.32,1))] motion-reduce:transition-none"
        style={{ gridTemplateRows: open ? "1fr" : "0fr", opacity: open ? 1 : 0 }}
      >
        <div className="overflow-hidden">
          <div className="pb-1 pt-3">{children}</div>
        </div>
      </div>
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
  montagePreset,
  subtitledEnabled,
  showTalkingHead,
  hasGuide = false,
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

  // Already-filmed is the default path: montage skips the shot plan unless a
  // guide already exists, and Voiceover starts in narrated_ready (upload the
  // clips you filmed). The planned flows remain reachable via "Plan this for
  // me" / an existing filming guide, not via a mode toggle.
  const selectType = async (value: PickerEditFormat) => {
    setOpenSection(value === "montage" ? "style" : null);
    if (value === resolvedFormat) return;
    if (value === "montage") {
      await patch({
        edit_format: "montage",
        ...(hasGuide ? {} : { content_mode: "existing_footage" }),
      });
    } else if (value === "narrated_planned") {
      await patch({ edit_format: "narrated_ready" });
    } else {
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
      <DisclosureSection
        eyebrow="Type"
        valueLabel={TYPE_COPY[resolvedFormat].label}
        thumbSrc={TYPE_MEDIA[resolvedFormat].poster}
        open={openSection === "type"}
        hint="You can change this until the first render"
        onToggle={() => setOpenSection(openSection === "type" ? null : "type")}
      >
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
      </DisclosureSection>

      {/* ---- STYLE (montage only) ---- */}
      {isMontage && (
        <DisclosureSection
          eyebrow="Style"
          valueLabel={activeStyleTile.label}
          thumbSrc={activeStyleTile.poster}
          open={openSection === "style"}
          onToggle={() => setOpenSection(openSection === "style" ? null : "style")}
        >
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
        </DisclosureSection>
      )}
    </div>
  );
}
