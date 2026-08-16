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

import { useEffect, useRef, useState } from "react";
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
    meta: "Best with 3+ clips",
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
  /** The item's stored edit_format, unfolded. The no-op guard compares against
      this (not resolvedFormat) so legacy sub-modes — e.g. narrated_planned —
      still upgrade to narrated_ready when their own card is clicked. */
  rawEditFormat: string;
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

/** WAI radio-group keyboard pattern: arrow keys move focus between radios
    (roving tabindex — only the checked card is in the tab order). */
function radioGroupKeyDown(event: React.KeyboardEvent<HTMLDivElement>) {
  const keys = ["ArrowRight", "ArrowDown", "ArrowLeft", "ArrowUp"];
  if (!keys.includes(event.key)) return;
  const radios = Array.from(
    event.currentTarget.querySelectorAll<HTMLElement>('[role="radio"]'),
  );
  const current = radios.indexOf(document.activeElement as HTMLElement);
  if (current === -1 || radios.length === 0) return;
  event.preventDefault();
  const delta = event.key === "ArrowRight" || event.key === "ArrowDown" ? 1 : -1;
  radios[(current + delta + radios.length) % radios.length]?.focus();
}

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
    // Hover-capable pointers only: on touch, a tap fires enter/focus and would
    // download the loop's video bytes for a card that is about to collapse.
    if (typeof window !== "undefined" && !window.matchMedia("(hover: hover)").matches) return;
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
      className={`flex h-7 w-7 shrink-0 items-center justify-center rounded-full border border-zinc-200 bg-white transition-transform duration-[var(--t-accordion-dur)] motion-reduce:transition-none ${
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
  id,
  eyebrow,
  valueLabel,
  thumbSrc,
  open,
  onToggle,
  children,
}: {
  id: string;
  eyebrow: string;
  valueLabel: string;
  thumbSrc: string;
  open: boolean;
  onToggle: () => void;
  children: React.ReactNode;
}) {
  const panelId = `${id}-panel`;
  // Collapsed panels are visually hidden (grid-rows 0fr + overflow-hidden) but
  // their radios/videos would otherwise stay reachable by keyboard. inert
  // removes them from tab order and the a11y tree while closed.
  const panelRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (panelRef.current) panelRef.current.inert = !open;
  }, [open]);
  return (
    <div>
      <button
        type="button"
        aria-expanded={open}
        aria-controls={panelId}
        onClick={onToggle}
        className={`flex min-h-[52px] w-full items-center gap-3.5 rounded-2xl border bg-white px-4 py-2.5 text-left transition-colors focus-visible:outline focus-visible:outline-2 focus-visible:outline-lime-500 ${
          open ? "border-zinc-300" : "border-zinc-200 hover:border-zinc-300"
        }`}
      >
        {/* eslint-disable-next-line @next/next/no-img-element -- static bundled poster */}
        <img
          src={thumbSrc}
          alt=""
          className="h-10 w-[30px] shrink-0 rounded-[7px] object-cover"
        />
        <span className="shrink-0 text-[11px] font-semibold uppercase tracking-[0.18em] text-[#71717a]">
          {eyebrow}
        </span>
        <span className="min-w-0 flex-1 truncate text-sm font-semibold text-[#0c0c0e]">
          {valueLabel}
        </span>
        <Chevron open={open} />
      </button>
      <div
        id={panelId}
        ref={panelRef}
        className="grid transition-[grid-template-rows,opacity] duration-[var(--t-accordion-dur)] ease-[var(--t-accordion-ease)] motion-reduce:transition-none"
        style={{ gridTemplateRows: open ? "1fr" : "0fr", opacity: open ? 1 : 0 }}
      >
        <div className="overflow-hidden">
          <div className="pb-1 pt-3">{children}</div>
        </div>
      </div>
    </div>
  );
}

function MediaRadioCard({
  active,
  saving,
  poster,
  video,
  scrim,
  label,
  desc,
  meta,
  onSelect,
}: {
  active: boolean;
  saving: boolean;
  poster: string;
  video: string;
  /** Scrim height class — taller when a meta line rides the caption block. */
  scrim: "h-1/2" | "h-3/5";
  label: string;
  desc: string;
  meta?: string;
  onSelect: () => void;
}) {
  const { videoRef, play, stop } = useHoverVideo();
  return (
    <button
      type="button"
      role="radio"
      aria-checked={active}
      // aria-disabled (not disabled) so keyboard focus survives the save —
      // disabling the just-clicked element would drop focus to <body>.
      aria-disabled={saving || undefined}
      tabIndex={active ? 0 : -1}
      onClick={() => {
        if (!saving) onSelect();
      }}
      onMouseEnter={play}
      onMouseLeave={stop}
      onFocus={play}
      onBlur={stop}
      className={`relative aspect-[3/4] w-[216px] shrink-0 snap-start overflow-hidden rounded-[18px] text-left transition-transform motion-reduce:transition-none focus-visible:outline focus-visible:outline-2 focus-visible:outline-lime-500 sm:w-auto ${
        saving ? "cursor-wait " : ""
      }${
        active
          ? "ring-[3px] ring-lime-600 shadow-[0_12px_30px_rgba(0,0,0,0.18)]"
          : "border border-zinc-200 motion-safe:hover:scale-[1.01]"
      }`}
    >
      <video
        ref={videoRef}
        aria-hidden="true"
        src={video}
        poster={poster}
        muted
        loop
        playsInline
        preload="none"
        className="absolute inset-0 h-full w-full object-cover"
      />
      <div
        className={`absolute inset-x-0 bottom-0 ${scrim} bg-gradient-to-b from-transparent via-[rgba(12,12,14,0.55)] to-[rgba(12,12,14,0.94)]`}
      />
      {active && (
        <span className="absolute left-3 top-3 rounded-full bg-lime-700 px-2.5 py-1 text-[11px] font-semibold text-white">
          Selected
        </span>
      )}
      <div className="absolute inset-x-0 bottom-0 flex flex-col gap-1 p-4">
        <span className="font-display text-[20px] font-medium leading-tight text-white">
          {label}
        </span>
        <span className="text-[13px] leading-[18px] text-white/[0.82]">{desc}</span>
        {meta && (
          <span className="pt-0.5 text-[11px] font-medium text-white/75">{meta}</span>
        )}
      </div>
    </button>
  );
}

export default function SetupPicker({
  resolvedFormat,
  rawEditFormat,
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
  // Optimistic format: the STYLE section and receipts follow the click
  // immediately; props catch up after PATCH + refetch. Cleared when props
  // match, and dropped outright when the PATCH rejects (patch() catches).
  const [optimisticFormat, setOptimisticFormat] = useState<PickerEditFormat | null>(null);
  useEffect(() => {
    if (optimisticFormat && optimisticFormat === resolvedFormat) {
      setOptimisticFormat(null);
    }
  }, [optimisticFormat, resolvedFormat]);
  const displayFormat = optimisticFormat ?? resolvedFormat;

  const isMontage = displayFormat === "montage";

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
    } catch {
      // Failed save: revert the receipts/STYLE shelf to the server's truth.
      // The card stays clickable, so re-selecting retries the PATCH.
      setOptimisticFormat(null);
    } finally {
      setSaving(false);
    }
  };

  // Already-filmed is the default path: montage skips the shot plan unless a
  // guide already exists, and Voiceover starts in narrated_ready (upload the
  // clips you filmed). The planned flows remain reachable via "Plan this for
  // me" (acceptance restores content_mode) / an existing filming guide, not
  // via a mode toggle.
  const selectType = async (value: PickerEditFormat) => {
    setOpenSection(value === "montage" ? "style" : null);
    // Compare against the stored edit_format, not the folded picker value —
    // a legacy narrated_planned item clicking Voiceover must still upgrade.
    const targetFormat = value === "narrated_planned" ? "narrated_ready" : value;
    if (targetFormat === rawEditFormat) return;
    setOptimisticFormat(value);
    if (value === "montage") {
      await patch({
        edit_format: "montage",
        ...(hasGuide ? {} : { content_mode: "existing_footage" }),
      });
    } else {
      await patch({ edit_format: targetFormat });
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
        id="type"
        eyebrow="Type"
        valueLabel={TYPE_COPY[displayFormat].label}
        thumbSrc={TYPE_MEDIA[displayFormat].poster}
        open={openSection === "type"}
        onToggle={() => setOpenSection(openSection === "type" ? null : "type")}
      >
        <div
          className="-mx-6 flex snap-x snap-mandatory gap-3.5 overflow-x-auto px-6 py-1 [scroll-padding-inline:1.5rem] sm:mx-0 sm:grid sm:grid-cols-2 sm:overflow-visible sm:p-0 lg:grid-cols-4"
          role="radiogroup"
          aria-label="Type"
          onKeyDown={radioGroupKeyDown}
        >
          {typeValues.map((value) => (
            <MediaRadioCard
              key={value}
              active={displayFormat === value}
              saving={saving}
              poster={TYPE_MEDIA[value].poster}
              video={TYPE_MEDIA[value].video}
              scrim="h-3/5"
              label={TYPE_COPY[value].label}
              desc={TYPE_COPY[value].desc}
              meta={TYPE_COPY[value].meta}
              onSelect={() => selectType(value)}
            />
          ))}
        </div>
      </DisclosureSection>

      {/* ---- STYLE (montage only) ---- */}
      {isMontage && (
        <DisclosureSection
          id="style"
          eyebrow="Style"
          valueLabel={activeStyleTile.label}
          thumbSrc={activeStyleTile.poster}
          open={openSection === "style"}
          onToggle={() => setOpenSection(openSection === "style" ? null : "style")}
        >
          <div
            className="-mx-6 flex snap-x snap-mandatory gap-3.5 overflow-x-auto px-6 py-1 [scroll-padding-inline:1.5rem] sm:mx-0 sm:grid sm:grid-cols-2 sm:overflow-visible sm:p-0 lg:grid-cols-4"
            role="radiogroup"
            aria-label="Style"
            onKeyDown={radioGroupKeyDown}
          >
            {STYLE_TILES.map((tile) => (
              <MediaRadioCard
                key={tile.value}
                active={montagePreset === tile.value}
                saving={saving}
                poster={tile.poster}
                video={tile.video}
                scrim="h-1/2"
                label={tile.label}
                desc={tile.desc}
                onSelect={() => selectStyle(tile.value)}
              />
            ))}
          </div>
        </DisclosureSection>
      )}
    </div>
  );
}
