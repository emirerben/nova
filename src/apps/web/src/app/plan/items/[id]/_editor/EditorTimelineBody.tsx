"use client";

/**
 * EditorTimelineBody — the editor-shell timeline (plan §6), mounted by
 * UnifiedTimeline when `editorMode` is set. Track order Text → Video (Clips)
 * → Sound (SFX sub-row above the music bed) → Overlays.
 *
 * Everything routes through the px-per-second scale (lib/timeline/timeline-scale):
 * fit = viewport/duration; zoom multiplies it; bars/playhead/scrub math all use
 * secondsToPx / pxToSeconds. Horizontal scroll when zoomed; the left gutter is
 * sticky so mute toggles + labels stay visible.
 *
 * D10 strict-neutral palette — lime appears ONLY as the selection ring. Video
 * shows a Filmstrip texture; Sound is zinc waveform-ish ink; Overlay is white /
 * zinc border. Bars get a subtle value shift on hover; the selection ring +
 * end-trim handles transition 120–180ms (motion-safe).
 */

import { useEffect, useLayoutEffect, useRef, useState } from "react";
import type { TextElementBar } from "@/lib/timeline/text-timeline-reducer";
import { slotWindows, type DraftSlot } from "@/app/generative/timeline-math";
import {
  clampPxPerSecond,
  fitPxPerSecond,
  pxToSeconds,
  rulerTicks,
  scaledTrackWidth,
  secondsToPx,
  tickIntervalForScale,
} from "@/lib/timeline/timeline-scale";
import { formatTimecode } from "@/lib/timeline/time-format";
import type { EditorSelection, EditorSelectionKind } from "./useEditorSelection";
import Filmstrip from "./Filmstrip";

/** Sticky left gutter (mute toggle + lane label). */
const GUTTER_PX = 64;

export interface EditorSfxBar {
  id: string;
  at_s: number;
  end_s?: number | null;
  label?: string | null;
}
export interface EditorOverlayBar {
  id: string;
  start_s: number;
  end_s: number;
  label?: string | null;
}

export interface EditorTimelineBodyProps {
  durationS: number;
  currentTimeS: number;
  /** Zoom factor: 1 = fit-to-width. */
  zoom: number;
  /** Reports the fit scale up so the shell can keep "fit" meaningful. */
  onReportFit?: (fitPxPerSecond: number) => void;

  selection: EditorSelection | null;
  onSelect: (kind: EditorSelectionKind, id: string) => void;
  onClear: () => void;

  textBars: TextElementBar[];

  slots: DraftSlot[];
  grid: number[];
  clipsLoading: boolean;
  filmstripSrc: string | null;

  sfx: EditorSfxBar[];
  hasMusic: boolean;
  musicLabel?: string;
  videoMuted: boolean;
  onToggleVideoMute: () => void;
  soundMuted: boolean;
  onToggleSoundMute: () => void;

  overlays: EditorOverlayBar[];

  onScrub: (seconds: number) => void;
  onScrubStart: () => void;
}

export default function EditorTimelineBody(props: EditorTimelineBodyProps) {
  const {
    durationS,
    currentTimeS,
    zoom,
    onReportFit,
    selection,
    onSelect,
    onClear,
    textBars,
    slots,
    grid,
    clipsLoading,
    filmstripSrc,
    sfx,
    hasMusic,
    musicLabel,
    videoMuted,
    onToggleVideoMute,
    soundMuted,
    onToggleSoundMute,
    overlays,
    onScrub,
    onScrubStart,
  } = props;

  const scrollRef = useRef<HTMLDivElement>(null);
  const [viewportW, setViewportW] = useState(0);

  useLayoutEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const measure = () => setViewportW(el.clientWidth);
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const trackViewportW = Math.max(0, viewportW - GUTTER_PX);
  const fitPps = fitPxPerSecond(trackViewportW, durationS);
  const pps = clampPxPerSecond(fitPps * Math.max(1, zoom));
  const trackW = Math.max(trackViewportW, scaledTrackWidth(durationS, pps));

  useEffect(() => {
    if (fitPps > 0) onReportFit?.(fitPps);
  }, [fitPps, onReportFit]);

  const playheadPx = secondsToPx(currentTimeS, pps);
  const windows = slotWindows(slots, grid);
  const tickInterval = tickIntervalForScale(pps);
  const ticks = rulerTicks(durationS, pps);

  // ── Scrub (ruler click/drag → seek; pauses playback per the contract) ────────
  const scrubbing = useRef(false);
  function scrubToClientX(clientX: number, trackEl: HTMLElement) {
    const rect = trackEl.getBoundingClientRect();
    const localX = clientX - rect.left;
    const sec = Math.max(0, Math.min(durationS, pxToSeconds(localX, pps)));
    onScrub(sec);
  }
  function onRulerPointerDown(e: React.PointerEvent<HTMLDivElement>) {
    scrubbing.current = true;
    onScrubStart();
    (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
    scrubToClientX(e.clientX, e.currentTarget);
  }
  function onRulerPointerMove(e: React.PointerEvent<HTMLDivElement>) {
    if (!scrubbing.current) return;
    scrubToClientX(e.clientX, e.currentTarget);
  }
  function onRulerPointerUp() {
    scrubbing.current = false;
  }

  const isSel = (kind: EditorSelectionKind, id: string) =>
    selection?.kind === kind && selection.id === id;

  const ringCls =
    "outline outline-2 outline-lime-500 outline-offset-[1px] motion-safe:transition-[outline-color,box-shadow] motion-safe:duration-150";

  return (
    <div
      ref={scrollRef}
      role="listbox"
      aria-label="Editor timeline selections"
      className="h-full select-none overflow-x-auto"
      data-testid="editor-timeline"
    >
      <div style={{ width: GUTTER_PX + trackW }}>
        {/* ── Ruler ── */}
        <div className="flex h-6">
          <div className="sticky left-0 z-30 flex-shrink-0 bg-white" style={{ width: GUTTER_PX }} />
          <div
            className="relative flex-1 cursor-ew-resize border-b border-zinc-200 bg-zinc-50"
            style={{ width: trackW }}
            onPointerDown={onRulerPointerDown}
            onPointerMove={onRulerPointerMove}
            onPointerUp={onRulerPointerUp}
            onPointerCancel={onRulerPointerUp}
          >
            {ticks.map((t) => (
              <div
                key={t}
                className="pointer-events-none absolute top-0 h-full"
                style={{ left: secondsToPx(t, pps) }}
              >
                <div className="h-2 w-px bg-zinc-300" />
                <span className="absolute left-1 top-1.5 whitespace-nowrap text-[9px] leading-none text-zinc-400">
                  {tickInterval < 1 ? t.toFixed(1) : formatTimecode(t)}
                </span>
              </div>
            ))}
            <Playline px={playheadPx} withHead />
          </div>
        </div>

        {/* ── Text lane ── */}
        <LaneRow label="Text" trackW={trackW}>
          <Playline px={playheadPx} />
          {textBars.length === 0 ? (
            <GhostRow text="Add text from the Text tool" />
          ) : (
            textBars.map((b) => {
              const left = secondsToPx(b.start_s, pps);
              const width = Math.max(6, secondsToPx(b.end_s - b.start_s, pps));
              const selected = isSel("text", b.id);
              return (
                <BarButton
                  key={b.id}
                  left={left}
                  width={width}
                  selected={selected}
                  ringCls={ringCls}
                  ariaLabel={`Text ${b.text.slice(0, 24)}, ${formatTimecode(b.start_s)}–${formatTimecode(b.end_s)}`}
                  onSelect={() => onSelect("text", b.id)}
                  className="bg-[#0c0c0e] text-white"
                >
                  <span className="pointer-events-none flex items-center gap-1 truncate px-2 text-[10px]">
                    <span className="font-semibold">T</span>
                    <span className="truncate">{b.text || "Text"}</span>
                  </span>
                </BarButton>
              );
            })
          )}
        </LaneRow>

        {/* ── Video lane (Clips + filmstrip) ── */}
        <LaneRow
          label="Video"
          trackW={trackW}
          muteState={{ muted: videoMuted, onToggle: onToggleVideoMute, title: "Original audio" }}
        >
          <Playline px={playheadPx} />
          {filmstripSrc && durationS > 0 && (
            <div className="pointer-events-none absolute inset-y-1 left-0" style={{ width: trackW }}>
              <Filmstrip src={filmstripSrc} durationS={durationS} widthPx={trackW} />
            </div>
          )}
          {clipsLoading ? (
            <div className="absolute inset-1 rounded bg-zinc-200/60 motion-safe:animate-pulse" />
          ) : (
            windows.map((win, i) => {
              const slot = slots[i];
              if (!slot || slot.removed || win.startS == null || win.durationS <= 0) return null;
              const left = secondsToPx(win.startS, pps);
              const width = Math.max(8, secondsToPx(win.durationS, pps));
              const selected = isSel("clip", slot.key);
              return (
                <button
                  key={slot.key}
                  type="button"
                  aria-label={`Clip ${i + 1}, ${formatTimecode(win.startS)}`}
                  aria-pressed={selected}
                  onClick={(e) => {
                    e.stopPropagation();
                    onSelect("clip", slot.key);
                  }}
                  className={[
                    "absolute inset-y-0.5 min-w-11 rounded border transition-colors focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500",
                    selected
                      ? `border-transparent ${ringCls}`
                      : "border-white/50 hover:border-white",
                  ].join(" ")}
                  style={{ left, width }}
                >
                  {i > 0 && <span className="absolute inset-y-0 left-0 w-px bg-white/80" />}
                </button>
              );
            })
          )}
        </LaneRow>

        {/* ── Sound lane (SFX sub-row above the music bed) ── */}
        <LaneRow
          label="Sound"
          trackW={trackW}
          tall
          muteState={{ muted: soundMuted, onToggle: onToggleSoundMute, title: "Music + effects" }}
        >
          <Playline px={playheadPx} />
          {/* SFX sub-row (top half) */}
          <div className="absolute inset-x-0 top-0 h-1/2">
            {sfx.map((s) => {
              const left = secondsToPx(s.at_s, pps);
              const end = s.end_s ?? s.at_s + 0.6;
              const width = Math.max(6, secondsToPx(end - s.at_s, pps));
              const selected = isSel("sfx", s.id);
              return (
                <BarButton
                  key={s.id}
                  left={left}
                  width={width}
                  selected={selected}
                  ringCls={ringCls}
                  ariaLabel={`Sound effect ${s.label ?? ""} at ${formatTimecode(s.at_s)}`}
                  onSelect={() => onSelect("sfx", s.id)}
                  className="inset-y-0.5 bg-zinc-300 text-[#0c0c0e]"
                >
                  <span className="pointer-events-none truncate px-1.5 text-[9px]">
                    {s.label ?? "sfx"}
                  </span>
                </BarButton>
              );
            })}
          </div>
          {/* Music bed (bottom half) — full-width; split disabled on it */}
          <div className="absolute inset-x-0 bottom-0 h-1/2">
            {hasMusic ? (
              <BarButton
                left={0}
                width={secondsToPx(durationS, pps)}
                selected={isSel("music", "bed")}
                ringCls={ringCls}
                ariaLabel={`Music bed ${musicLabel ?? ""}`}
                onSelect={() => onSelect("music", "bed")}
                className="inset-y-0.5 bg-zinc-200 text-[#0c0c0e]"
              >
                <span className="pointer-events-none flex items-center gap-1 truncate px-2 text-[10px]">
                  <span aria-hidden>♫</span>
                  <span className="truncate">{musicLabel ?? "Music"}</span>
                </span>
              </BarButton>
            ) : (
              sfx.length === 0 && (
                <div className="absolute inset-x-1 bottom-0.5 top-0.5 flex items-center rounded border border-dashed border-zinc-300 px-2 text-[10px] text-zinc-400">
                  Add sounds from the Sounds tool
                </div>
              )
            )}
          </div>
        </LaneRow>

        {/* ── Overlays lane ── */}
        <LaneRow label="Overlays" trackW={trackW}>
          <Playline px={playheadPx} />
          {overlays.length === 0 ? (
            <GhostRow text="Overlays appear here" />
          ) : (
            overlays.map((o) => {
              const left = secondsToPx(o.start_s, pps);
              const width = Math.max(8, secondsToPx(o.end_s - o.start_s, pps));
              const selected = isSel("overlay", o.id);
              return (
                <BarButton
                  key={o.id}
                  left={left}
                  width={width}
                  selected={selected}
                  ringCls={ringCls}
                  ariaLabel={`Overlay ${o.label ?? ""}, ${formatTimecode(o.start_s)}–${formatTimecode(o.end_s)}`}
                  onSelect={() => onSelect("overlay", o.id)}
                  className="border border-zinc-300 bg-white text-[#0c0c0e]"
                >
                  <span className="pointer-events-none truncate px-2 text-[10px]">
                    {o.label ?? "Overlay"}
                  </span>
                </BarButton>
              );
            })
          )}
        </LaneRow>
      </div>
    </div>
  );
}

// ── Sub-components ──────────────────────────────────────────────────────────

/** One px-positioned playhead segment (line only; head on the ruler copy). */
function Playline({ px, withHead = false }: { px: number; withHead?: boolean }) {
  return (
    <div
      className="pointer-events-none absolute top-0 bottom-0 z-20 w-px bg-[#0c0c0e]/80"
      style={{ left: px }}
      aria-hidden
    >
      {withHead && (
        <div className="absolute -top-1 left-1/2 h-2 w-2 -translate-x-1/2 rounded-[2px] bg-[#0c0c0e]" />
      )}
    </div>
  );
}

function LaneRow({
  label,
  trackW,
  tall = false,
  muteState,
  children,
}: {
  label: string;
  trackW: number;
  tall?: boolean;
  muteState?: { muted: boolean; onToggle: () => void; title: string };
  children: React.ReactNode;
}) {
  return (
    <div className={`flex ${tall ? "h-16" : "h-12"}`}>
      <div
        className="sticky left-0 z-30 flex flex-shrink-0 items-center gap-1 border-b border-zinc-200 bg-white pl-1.5 pr-1"
        style={{ width: GUTTER_PX }}
      >
        {muteState ? (
          <button
            type="button"
            aria-label={`${muteState.title} ${muteState.muted ? "muted" : "audible"}`}
            aria-pressed={muteState.muted}
            title={muteState.muted ? `${muteState.title}: muted` : `${muteState.title}: audible`}
            onClick={muteState.onToggle}
            className={`flex h-11 w-11 flex-shrink-0 items-center justify-center rounded text-[10px] focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500 ${
              muteState.muted ? "text-zinc-300" : "text-[#3f3f46] hover:bg-zinc-100"
            }`}
          >
            {muteState.muted ? "🔇" : "🔊"}
          </button>
        ) : (
          <span className="w-11 flex-shrink-0" />
        )}
        <span className="truncate text-[9px] font-semibold uppercase tracking-wider text-zinc-500">
          {label}
        </span>
      </div>
      <div className="relative flex-1 overflow-hidden border-b border-zinc-200 bg-zinc-50" style={{ width: trackW }}>
        {children}
      </div>
    </div>
  );
}

function GhostRow({ text }: { text: string }) {
  return (
    <div className="absolute inset-1 flex items-center rounded border border-dashed border-zinc-300 px-2 text-[10px] text-zinc-400">
      {text}
    </div>
  );
}

function BarButton({
  left,
  width,
  selected,
  ringCls,
  ariaLabel,
  onSelect,
  className,
  children,
}: {
  left: number;
  width: number;
  selected: boolean;
  ringCls: string;
  ariaLabel: string;
  onSelect: () => void;
  className: string;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      aria-label={ariaLabel}
      aria-pressed={selected}
      onClick={(e) => {
        e.stopPropagation();
        onSelect();
      }}
      className={[
        "absolute inset-y-0.5 flex min-w-11 items-center rounded transition-[filter,outline-color] hover:brightness-110 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500",
        selected ? ringCls : "",
        className,
      ].join(" ")}
      style={{ left, width }}
    >
      {children}
      {selected && (
        <>
          <TrimHandle side="left" />
          <TrimHandle side="right" />
        </>
      )}
    </button>
  );
}

/** End-trim handle (visual affordance; transitions in with the ring). */
function TrimHandle({ side }: { side: "left" | "right" }) {
  return (
    <span
      aria-hidden
      className={`absolute top-1/2 flex h-full w-6 -translate-y-1/2 items-center justify-center motion-safe:transition-opacity motion-safe:duration-150 ${
        side === "left" ? "left-[-12px]" : "right-[-12px]"
      }`}
    >
      <span className="h-3 w-1 rounded-full bg-lime-500" />
    </span>
  );
}
