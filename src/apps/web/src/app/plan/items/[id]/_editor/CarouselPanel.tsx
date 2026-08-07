"use client";

/**
 * CarouselPanel — "carousel as an editable visual template" (Visuals drawer).
 *
 * Configures `carousel_moment` on the current variant: a full-screen
 * multi-clip carousel burned in at a position in the edit. Every apply is a
 * full server re-render (~3 min) — same lifecycle as the Classic/Editorial
 * intro_layout switch (EditorShell.handleCopilotOps), so this panel has no
 * live preview and no undo. It only collects the config and hands it to the
 * caller; EditorShell owns the dispatch + post-apply navigation back to the
 * video page, mirroring that same flow byte-for-byte.
 *
 * Effect chips are STATIC CSS mini-mocks (no physics, no canvas) — enough to
 * signal the shape of each effect at a glance, same spirit as
 * LayoutPreviewCard's tile treatment but far cheaper to render.
 */

import { useEffect, useState } from "react";
import type { CarouselMoment } from "@/lib/plan-api";
import { resolveCarouselFocusClipIndex } from "@/lib/generative-api";

export interface CarouselClipThumb {
  clipIndex: number;
  label: string;
  /** Fresh-signed source URL for a poster-frame thumbnail. null = no preview
   *  (source expired / never signed) — the tile still shows the label. */
  signedUrl: string | null;
}

export interface CarouselPanelControl {
  /** Whether the carousel feature is usable on this variant at all. When
   *  false, the caller should never mount this panel — gate the entry point
   *  instead (aria-disabled + onDisabledTap), same as every other tool. */
  capable: boolean;
  reason: string | null;
  /** Current moment (prefill), or null if none is configured yet. */
  current: CarouselMoment | null;
  /** The variant's clips, in timeline order, for the focus-tile selector. */
  clips: CarouselClipThumb[];
  /** True while an add/update/remove request is in flight. */
  busy: boolean;
  onApply: (config: CarouselMoment) => void;
  /** Pressing Remove — the caller owns the confirm step. */
  onRemove: () => void;
}

const EFFECTS: Array<{
  id: NonNullable<CarouselMoment["effect"]>;
  label: string;
}> = [
  { id: "scale_sweep", label: "Scale sweep" },
  { id: "cover_flow", label: "Cover flow" },
  { id: "cards_stack", label: "Card stack" },
  { id: "flipbook", label: "Flipbook" },
];

const POSITIONS: Array<{ id: NonNullable<CarouselMoment["position"]>; label: string }> = [
  { id: "intro", label: "Intro" },
  { id: "middle", label: "Middle" },
  { id: "outro", label: "Outro" },
];

const DURATION_MIN = 2;
const DURATION_MAX = 15;
const DEFAULT_DURATION_S = 6;

const MODE_DESCRIPTION: Record<NonNullable<CarouselMoment["mode"]>, string> = {
  focus: "One tile plays fullscreen while the rest swipe past behind it.",
  rolling: "Every tile plays through the carousel in sequence.",
};

/** "stills" is a legal persisted `mode` (auto-authored moments can land on
 *  it; see director.py) but is deliberately NOT one of the Focus/Rolling
 *  toggle options — there's no UI affordance to create it, only to move off
 *  it. Any other unrecognized value falls into the same bucket, so a
 *  prefill never silently mis-selects a mode the user didn't choose. */
function isPickableMode(mode: CarouselMoment["mode"]): mode is "focus" | "rolling" {
  return mode === "focus" || mode === "rolling";
}

const LEGACY_MODE_HINT = "This moment uses a legacy static style — pick a mode to update it.";

const segmentedBtnClass = (active: boolean) =>
  `min-h-11 flex-1 rounded-lg px-2 text-[12px] transition-colors focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500 ${
    active
      ? "bg-[#0c0c0e] font-semibold text-white"
      : "border border-zinc-200 bg-white text-[#3f3f46] hover:border-zinc-400"
  }`;

export default function CarouselPanel({
  control,
  onBack,
}: {
  control: CarouselPanelControl;
  /** Return to the "Add a block" grid. */
  onBack: () => void;
}) {
  const { current, clips, busy, onApply, onRemove } = control;

  // null = no mode selected yet — only reachable via a prefill whose
  // persisted mode isn't one of the two pickable options ("stills", or any
  // other unrecognized value). A brand-new moment (current == null) always
  // starts on "focus".
  const prefillMode: "focus" | "rolling" | null =
    current == null ? "focus" : isPickableMode(current.mode) ? current.mode : null;

  const [effect, setEffect] = useState<NonNullable<CarouselMoment["effect"]>>(
    current?.effect ?? "scale_sweep",
  );
  const [mode, setMode] = useState<"focus" | "rolling" | null>(prefillMode);
  const [focusClipIndex, setFocusClipIndex] = useState<number | null>(
    resolveCarouselFocusClipIndex(current),
  );
  const [position, setPosition] = useState<NonNullable<CarouselMoment["position"]>>(
    current?.position ?? "middle",
  );
  const [durationS, setDurationS] = useState<number>(current?.duration_s ?? DEFAULT_DURATION_S);
  const [transition, setTransition] = useState<NonNullable<CarouselMoment["transition"]>>(
    current?.transition ?? "crossfade",
  );

  // Resync per-field on the server's current moment (e.g. after this same
  // apply lands and the variant refetches) — same pattern VariantCard uses
  // for its voice/footage mix slider.
  useEffect(() => setEffect(current?.effect ?? "scale_sweep"), [current?.effect]);
  useEffect(() => setMode(prefillMode), [prefillMode]);
  useEffect(
    () => setFocusClipIndex(resolveCarouselFocusClipIndex(current)),
    // `current` may carry the legacy `focus` shape (no `focus_clip_index`)
    // for an already-persisted moment — depend on the whole object rather
    // than a field that isn't in the strict `CarouselMoment` contract type.
    [current],
  );
  useEffect(() => setPosition(current?.position ?? "middle"), [current?.position]);
  useEffect(
    () => setDurationS(current?.duration_s ?? DEFAULT_DURATION_S),
    [current?.duration_s],
  );
  useEffect(() => setTransition(current?.transition ?? "crossfade"), [current?.transition]);

  const isUpdate = current != null;

  return (
    <div className="space-y-5 px-5 pb-5">
      <button
        type="button"
        onClick={onBack}
        className="flex min-h-11 items-center gap-1 text-[12px] font-semibold text-[#3f3f46] hover:text-[#0c0c0e]"
      >
        <span aria-hidden>←</span> Add a block
      </button>

      <section>
        <p className="mb-2 text-[12px] font-semibold text-[#3f3f46]">Effect</p>
        <div role="radiogroup" aria-label="Carousel effect" className="grid grid-cols-2 gap-2">
          {EFFECTS.map((option) => (
            <EffectChip
              key={option.id}
              id={option.id}
              label={option.label}
              selected={effect === option.id}
              disabled={busy}
              onSelect={() => setEffect(option.id)}
            />
          ))}
        </div>
      </section>

      <section>
        <p className="mb-2 text-[12px] font-semibold text-[#3f3f46]">Mode</p>
        <div role="group" aria-label="Carousel mode" className="flex gap-1">
          {(["focus", "rolling"] as const).map((m) => (
            <button
              key={m}
              type="button"
              disabled={busy}
              aria-pressed={mode === m}
              onClick={() => setMode(m)}
              className={segmentedBtnClass(mode === m)}
            >
              {m === "focus" ? "Focus" : "Rolling"}
            </button>
          ))}
        </div>
        {mode === null ? (
          <p className="mt-1.5 text-[11px] text-amber-700">{LEGACY_MODE_HINT}</p>
        ) : (
          <p className="mt-1.5 text-[11px] text-[#71717a]">{MODE_DESCRIPTION[mode]}</p>
        )}
      </section>

      {mode === "focus" && (
        <section>
          <p className="mb-2 text-[12px] font-semibold text-[#3f3f46]">Focus tile</p>
          <div
            role="radiogroup"
            aria-label="Focus clip"
            className="flex gap-2 overflow-x-auto pb-1"
          >
            <FocusTile
              selected={focusClipIndex === null}
              disabled={busy}
              onSelect={() => setFocusClipIndex(null)}
              label="Let Nova pick"
              autoPick
            />
            {clips.map((clipThumb) => (
              <FocusTile
                key={clipThumb.clipIndex}
                selected={focusClipIndex === clipThumb.clipIndex}
                disabled={busy}
                onSelect={() => setFocusClipIndex(clipThumb.clipIndex)}
                label={clipThumb.label}
                signedUrl={clipThumb.signedUrl}
              />
            ))}
          </div>
        </section>
      )}

      <section>
        <p className="mb-2 text-[12px] font-semibold text-[#3f3f46]">Position</p>
        <div role="group" aria-label="Carousel position" className="flex gap-1">
          {POSITIONS.map((p) => (
            <button
              key={p.id}
              type="button"
              disabled={busy}
              aria-pressed={position === p.id}
              onClick={() => setPosition(p.id)}
              className={segmentedBtnClass(position === p.id)}
            >
              {p.label}
            </button>
          ))}
        </div>
      </section>

      <section>
        <div className="mb-2 flex items-center justify-between">
          <label htmlFor="carousel-duration" className="text-[12px] font-semibold text-[#3f3f46]">
            Length
          </label>
          <span className="text-[12px] tabular-nums text-[#71717a]">{durationS}s</span>
        </div>
        <input
          id="carousel-duration"
          type="range"
          min={DURATION_MIN}
          max={DURATION_MAX}
          step={1}
          value={durationS}
          disabled={busy}
          aria-label="Carousel length in seconds"
          onChange={(e) => setDurationS(Number(e.target.value))}
          className="w-full accent-lime-600 disabled:opacity-40"
        />
      </section>

      <section>
        <p className="mb-2 text-[12px] font-semibold text-[#3f3f46]">Transition</p>
        <div role="group" aria-label="Carousel transition" className="flex gap-1">
          {(["crossfade", "none"] as const).map((t) => (
            <button
              key={t}
              type="button"
              disabled={busy}
              aria-pressed={transition === t}
              onClick={() => setTransition(t)}
              className={segmentedBtnClass(transition === t)}
            >
              {t === "crossfade" ? "Crossfade" : "Hard cut"}
            </button>
          ))}
        </div>
      </section>

      <p className="text-[11px] text-[#71717a]">
        {isUpdate ? "Updating" : "Adding"} a carousel re-renders the whole video (about 3
        minutes) and takes you back to the video page while it finishes. This isn&apos;t
        undoable from the editor — use Update or Remove to change it again.
      </p>

      <div className="space-y-2">
        <button
          type="button"
          disabled={busy || mode === null}
          onClick={() => {
            if (mode === null) return; // guarded by disabled above; keeps the payload honest
            onApply({
              effect,
              mode,
              focus_clip_index: mode === "focus" ? focusClipIndex : null,
              position,
              duration_s: durationS,
              transition,
            });
          }}
          className="min-h-11 w-full rounded-full bg-[#0c0c0e] px-4 text-[13px] font-semibold text-white transition-opacity hover:opacity-80 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500 disabled:opacity-40"
        >
          {busy
            ? isUpdate
              ? "Updating…"
              : "Adding…"
            : isUpdate
              ? "Update carousel"
              : "Add carousel"}
        </button>
        {isUpdate && (
          <button
            type="button"
            disabled={busy}
            onClick={onRemove}
            className="min-h-11 w-full rounded-lg border border-red-200 text-[13px] font-semibold text-red-600 hover:bg-red-50 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-red-500 disabled:opacity-40"
          >
            Remove carousel
          </button>
        )}
      </div>
    </div>
  );
}

/** One effect chip: role="radio" + a static CSS mini-mock (aspect-[5/3] dark
 *  tile, same shape language as LayoutPreviewCard). No canvas, no measuring,
 *  no animation — a still snapshot of the effect's geometry. */
function EffectChip({
  id,
  label,
  selected,
  disabled,
  onSelect,
}: {
  id: NonNullable<CarouselMoment["effect"]>;
  label: string;
  selected: boolean;
  disabled?: boolean;
  onSelect: () => void;
}) {
  return (
    <button
      type="button"
      role="radio"
      aria-checked={selected}
      aria-label={`${label} effect`}
      disabled={disabled}
      onClick={onSelect}
      className={[
        "flex min-h-[44px] flex-col gap-1.5 rounded-lg border bg-white p-2 text-left transition-colors disabled:cursor-not-allowed disabled:opacity-50",
        selected ? "border-lime-600 ring-1 ring-lime-600" : "border-zinc-200 hover:border-zinc-400",
      ].join(" ")}
    >
      <span className="flex aspect-[5/3] w-full items-center justify-center overflow-hidden rounded-md bg-[#0c0c0e]">
        <EffectMock kind={id} />
      </span>
      <span className="text-[11px] font-medium text-[#3f3f46]">{label}</span>
    </button>
  );
}

function EffectMock({ kind }: { kind: NonNullable<CarouselMoment["effect"]> }) {
  if (kind === "scale_sweep") {
    return (
      <span className="flex h-full items-center justify-center gap-1">
        <span className="h-[55%] w-[18%] rounded-sm bg-white/25" />
        <span className="h-[85%] w-[30%] rounded-sm bg-white" />
        <span className="h-[55%] w-[18%] rounded-sm bg-white/25" />
      </span>
    );
  }
  if (kind === "cover_flow") {
    return (
      <span
        className="flex h-full items-center justify-center gap-1"
        style={{ perspective: "200px" }}
      >
        <span
          className="h-[70%] w-[22%] rounded-sm bg-white/40"
          style={{ transform: "rotateY(35deg)" }}
        />
        <span className="h-[85%] w-[28%] rounded-sm bg-white" />
        <span
          className="h-[70%] w-[22%] rounded-sm bg-white/40"
          style={{ transform: "rotateY(-35deg)" }}
        />
      </span>
    );
  }
  if (kind === "cards_stack") {
    return (
      <span className="relative flex h-full w-full items-center justify-center">
        <span
          className="absolute h-[70%] w-[46%] rounded-sm bg-white/30"
          style={{ transform: "translate(12%, 8%) rotate(6deg)" }}
        />
        <span
          className="absolute h-[70%] w-[46%] rounded-sm bg-white/55"
          style={{ transform: "translate(-8%, -4%) rotate(-5deg)" }}
        />
        <span className="absolute h-[75%] w-[48%] rounded-sm bg-white" />
      </span>
    );
  }
  // flipbook
  return (
    <span className="relative flex h-full w-full items-center justify-center">
      <span className="absolute h-[75%] w-[50%] rounded-sm bg-white/35" />
      <span
        className="absolute h-[75%] w-[50%] rounded-sm bg-white"
        style={{ transform: "perspective(160px) rotateY(-30deg)", transformOrigin: "left center" }}
      />
    </span>
  );
}

/** One clip tile in the focus-tile strip. Min 44px touch target even though
 *  the visible thumbnail is narrower (portrait 9:16), via padding inside the
 *  button rather than shrinking the hit area. */
function FocusTile({
  label,
  signedUrl,
  selected,
  disabled,
  autoPick,
  onSelect,
}: {
  label: string;
  signedUrl?: string | null;
  selected: boolean;
  disabled?: boolean;
  autoPick?: boolean;
  onSelect: () => void;
}) {
  return (
    <button
      type="button"
      role="radio"
      aria-checked={selected}
      aria-label={label}
      disabled={disabled}
      onClick={onSelect}
      className={[
        "flex min-h-11 min-w-11 shrink-0 flex-col items-center gap-1 rounded-lg border p-1 transition-colors disabled:cursor-not-allowed disabled:opacity-50",
        selected ? "border-lime-600 ring-1 ring-lime-600" : "border-zinc-200 hover:border-zinc-400",
      ].join(" ")}
    >
      <span className="flex h-16 w-9 items-center justify-center overflow-hidden rounded bg-[#0c0c0e]">
        {autoPick ? (
          <span aria-hidden className="text-[16px] text-white">
            ✧
          </span>
        ) : signedUrl ? (
          <video
            src={signedUrl}
            muted
            playsInline
            preload="metadata"
            className="h-full w-full object-cover"
            onLoadedMetadata={(e) => {
              const video = e.currentTarget;
              video.currentTime = Math.min(0.1, video.duration || 0.1);
            }}
          />
        ) : (
          <span aria-hidden className="text-[10px] text-white/40">
            —
          </span>
        )}
      </span>
      <span className="max-w-[44px] truncate text-[10px] text-[#71717a]">{label}</span>
    </button>
  );
}
