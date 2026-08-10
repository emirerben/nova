"use client";

/**
 * CarouselPanel — controls for the staged, undoable Carousel block.
 *
 * Configures `carousel_moment` on the current variant: a full-screen
 * multi-clip carousel burned in at a position in the edit. Lane C
 * (carousel-blocks train) retired the old dispatch-on-apply flow (one
 * immediate full re-render per Add/Update/Remove, no undo, no batching) in
 * favor of the SAME staged model every other editor block uses: every
 * control here patches the working `carousel_moment` immediately (an undo
 * step each), and the change rides the next batched Save alongside
 * everything else. There is no Apply/Update button and no Remove confirm —
 * both are one click, both are undoable via ⌘Z.
 *
 * Effect chips are STATIC CSS mini-mocks (no physics, no canvas) — enough to
 * signal the shape of each effect at a glance, same spirit as
 * LayoutPreviewCard's tile treatment but far cheaper to render.
 */

import { useMemo } from "react";
import type { CarouselMoment } from "@/lib/plan-api";
import { resolveCarouselFocusClipIndex } from "@/lib/generative-api";
import { MAX_CARDS, naturalFocusTimelineLengthS } from "./carousel-preview-impl/geometry";

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
  /** The session's EFFECTIVE moment: the staged value once the panel has
   *  been touched, else the persisted `variant.carousel_moment`, else null
   *  (no carousel configured — a brand-new block). The caller (EditorShell)
   *  owns this precedence; the panel just renders whatever it's handed. */
  current: CarouselMoment | null;
  /** The variant's clips, in timeline order, for the focus-tile selector. */
  clips: CarouselClipThumb[];
  /** Stage a FULL replacement config immediately — every control below
   *  calls this on change (no separate submit step). Each call is one undo
   *  step; the change rides the next batched Save. */
  onChange: (config: CarouselMoment) => void;
  /** Stage removal (`null`) immediately — undoable, no confirm dialog. */
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

export function createDefaultCarouselMoment(clipCount: number): CarouselMoment {
  const naturalDurationS = naturalFocusTimelineLengthS(
    Math.min(Math.max(0, clipCount), MAX_CARDS),
    null,
  );
  return {
    effect: "scale_sweep",
    mode: "focus",
    focus_clip_index: null,
    position: "middle",
    duration_s: Math.max(DURATION_MIN, Math.min(Math.ceil(naturalDurationS), DURATION_MAX)),
    transition: "crossfade",
  };
}

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
}: {
  control: CarouselPanelControl;
}) {
  const { current, clips, onChange, onRemove } = control;

  const effect: NonNullable<CarouselMoment["effect"]> = current?.effect ?? "scale_sweep";
  // null = no mode selected yet — only reachable via a prefill whose
  // persisted mode isn't one of the two pickable options ("stills", or any
  // other unrecognized value). A brand-new moment (current == null) always
  // starts on "focus".
  const mode: "focus" | "rolling" | null =
    current == null ? "focus" : isPickableMode(current.mode) ? current.mode : null;
  const focusClipIndex = resolveCarouselFocusClipIndex(current);
  const position: NonNullable<CarouselMoment["position"]> = current?.position ?? "middle";
  const transition: NonNullable<CarouselMoment["transition"]> = current?.transition ?? "crossfade";

  // Natural (unfitted) length of the focus choreography for this clip pool —
  // the SAME engine the preview renders through (buildTimeline via
  // geometry.ts), just without the fit-to-durationS step. A focus arc
  // (lead-in + flick + settle + zoom-in + hold + zoom-out + settle) runs
  // ~10-13s; a short duration_s truncates it before the zoom plays. Cheap
  // (pure math, no DOM) and memoized on the only two inputs that change it.
  const nCardsForPreview = Math.min(clips.length, MAX_CARDS);
  const naturalFocusDurationS = useMemo(
    () => naturalFocusTimelineLengthS(nCardsForPreview, focusClipIndex),
    [nCardsForPreview, focusClipIndex],
  );
  const focusDefaultDurationS = Math.max(
    DURATION_MIN,
    Math.min(Math.ceil(naturalFocusDurationS), DURATION_MAX),
  );
  // Rolling keeps the flat 6s default unconditionally. Focus defaults to the
  // natural arc length instead — ONLY as a fallback when nothing explicit is
  // staged yet; once a value is staged (by the user OR by the mode-switch
  // handler below), it's honored as-is.
  const durationS =
    current?.duration_s ?? (mode === "focus" ? focusDefaultDurationS : DEFAULT_DURATION_S);
  const focusDurationTooShort = mode === "focus" && durationS < naturalFocusDurationS;

  const isUpdate = current != null;
  // Legacy "stills" prefill: no resolvable mode yet. Every OTHER control
  // stays disabled until the user picks Focus or Rolling — the same gate
  // the old submit-on-Apply model enforced (nothing could be applied until
  // mode resolved), now applied per-control since there's no single submit
  // step to gate. Mode itself is always pickable, so it's exempt.
  const stillsGated = mode === null;

  /** Merge a partial change over the current effective config and stage the
   *  whole thing immediately — every control below is "instant apply". */
  function patch(partial: Partial<CarouselMoment>) {
    const resolvedMode = partial.mode ?? mode ?? "focus";
    onChange({
      effect,
      mode: resolvedMode,
      focus_clip_index: resolvedMode === "focus" ? focusClipIndex : null,
      position,
      duration_s: durationS,
      transition,
      ...partial,
    });
  }

  return (
    <div className="space-y-5 px-5 pb-5">
      <section>
        <p className="mb-2 text-[12px] font-semibold text-[#3f3f46]">Effect</p>
        <div role="radiogroup" aria-label="Carousel effect" className="grid grid-cols-2 gap-2">
          {EFFECTS.map((option) => (
            <EffectChip
              key={option.id}
              id={option.id}
              label={option.label}
              selected={effect === option.id}
              disabled={stillsGated}
              onSelect={() => patch({ effect: option.id })}
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
              aria-pressed={mode === m}
              onClick={() =>
                patch({
                  mode: m,
                  focus_clip_index: m === "focus" ? focusClipIndex : null,
                  // Any ACTUAL mode change resets Length to that mode's own
                  // default — focus and rolling have different natural
                  // paces (a focus zoom needs ~10-13s; rolling has none of
                  // that structure), so a length tuned for one rarely
                  // applies to the other. Entering focus resets to the
                  // natural arc length (task a); entering rolling always
                  // resets to the flat 6s default (task c — "unchanged"
                  // means rolling's own default never varies, regardless of
                  // what focus's default currently computes to). Re-clicking
                  // the already-active mode is a no-op here (falls through
                  // to patch()'s own default, i.e. whatever's on screen).
                  ...(mode !== m
                    ? {
                        duration_s:
                          m === "focus" ? focusDefaultDurationS : DEFAULT_DURATION_S,
                      }
                    : {}),
                })
              }
              className={segmentedBtnClass(mode === m)}
            >
              {m === "focus" ? "Focus" : "Rolling"}
            </button>
          ))}
        </div>
        {mode === null ? (
          <p className="mt-1.5 text-[11px] text-[#3f3f46]">{LEGACY_MODE_HINT}</p>
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
              disabled={stillsGated}
              onSelect={() => patch({ focus_clip_index: null })}
              label="Let Nova pick"
              autoPick
            />
            {clips.map((clipThumb) => (
              <FocusTile
                key={clipThumb.clipIndex}
                selected={focusClipIndex === clipThumb.clipIndex}
                disabled={stillsGated}
                onSelect={() => patch({ focus_clip_index: clipThumb.clipIndex })}
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
              disabled={stillsGated}
              aria-pressed={position === p.id}
              onClick={() => patch({ position: p.id })}
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
          disabled={stillsGated}
          aria-label="Carousel length in seconds"
          onChange={(e) => patch({ duration_s: Number(e.target.value) })}
          className="w-full accent-lime-600 disabled:opacity-40"
        />
        {focusDurationTooShort && (
          <p className="mt-1.5 text-[11px] text-[#3f3f46]">
            Focus zoom needs ~{Math.ceil(naturalFocusDurationS)}s — shorter lengths cut it off
          </p>
        )}
      </section>

      <section>
        <p className="mb-2 text-[12px] font-semibold text-[#3f3f46]">Transition</p>
        <div role="group" aria-label="Carousel transition" className="flex gap-1">
          {(["crossfade", "none"] as const).map((t) => (
            <button
              key={t}
              type="button"
              disabled={stillsGated}
              aria-pressed={transition === t}
              onClick={() => patch({ transition: t })}
              className={segmentedBtnClass(transition === t)}
            >
              {t === "crossfade" ? "Crossfade" : "Hard cut"}
            </button>
          ))}
        </div>
      </section>

      {isUpdate && (
        <button
          type="button"
          onClick={onRemove}
          className="min-h-11 w-full rounded-lg border border-zinc-200 bg-white text-[13px] font-semibold text-[#3f3f46] hover:bg-zinc-100 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500"
        >
          Remove carousel
        </button>
      )}
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
        "flex min-h-[44px] flex-col gap-1.5 rounded-lg border bg-white p-2 text-left transition-colors focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500 disabled:cursor-not-allowed disabled:opacity-50",
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
        "flex min-h-11 min-w-11 shrink-0 flex-col items-center gap-1 rounded-lg border p-1 transition-colors focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500 disabled:cursor-not-allowed disabled:opacity-50",
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
