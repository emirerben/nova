"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { adminGetAudioUrl, type SongSection } from "@/lib/music-api";
import { matchSectionByBounds } from "@/lib/music-section-match";
import { countSlotsClient } from "@/lib/music-slot-count";

// Extracted from page.tsx (Next.js rejects non-page named exports from
// page files). The audio player + waveform interaction is its own unit so
// it can be tested in isolation — see __tests__/admin/MusicSectionPrecedence.
//
// Tolerance for the per-band ✓ + thicker stroke "isSelected" indicator
// lives in src/lib/music-section-match.ts so the top metadata Row in
// page.tsx uses the same number — otherwise the two surfaces could
// disagree on which band is selected.

type SelectionMode = "start" | "end" | null;

// Color tokens for the 3 ranked agent sections. Most-saturated = rank 1.
// Stays in the page's violet/zinc/green/red palette.
const RANK_COLORS: Record<1 | 2 | 3, { fill: string; stroke: string; text: string }> = {
  1: { fill: "rgba(139,92,246,0.78)", stroke: "#a78bfa", text: "#ffffff" },
  2: { fill: "rgba(139,92,246,0.45)", stroke: "#8b5cf6", text: "#ede9fe" },
  3: { fill: "rgba(139,92,246,0.22)", stroke: "#7c3aed", text: "#ddd6fe" },
};

function pickTickInterval(duration: number): number {
  if (duration <= 30) return 5;
  if (duration <= 60) return 10;
  if (duration <= 120) return 20;
  if (duration <= 300) return 30;
  return 60;
}

function formatTime(seconds: number): string {
  const s = Math.max(0, seconds);
  const m = Math.floor(s / 60);
  const r = s - m * 60;
  return `${m}:${r.toFixed(1).padStart(4, "0")}`;
}

export function AudioPlayer({
  trackId,
  beats,
  duration,
  start,
  end,
  sections,
  slotEveryN = 8,
  onStartChange,
  onEndChange,
}: {
  trackId: string;
  beats: number[];
  duration: number;
  start: number;
  end: number;
  sections: SongSection[] | null;
  /**
   * Current `slot_every_n_beats` from the form (NOT the saved cfg value),
   * so the per-band 0-slot warning updates live as the user edits N.
   * Defaults to 8 to keep existing call sites working.
   */
  slotEveryN?: number;
  onStartChange: (s: number) => void;
  onEndChange: (s: number) => void;
}) {
  const audioRef = useRef<HTMLAudioElement>(null);
  const [audioUrl, setAudioUrl] = useState<string | null>(null);
  const [audioError, setAudioError] = useState<string | null>(null);
  const [playing, setPlaying] = useState(false);
  const [currentTime, setCurrent] = useState(0);
  const [selectMode, setSelectMode] = useState<SelectionMode>(null);
  const [hoverSection, setHoverSection] = useState<SongSection | null>(null);
  const rafRef = useRef<number>(0);
  // Track the active end-of-section timeupdate listener so rapid clicks
  // don't stack listeners that fire on stale `end_s` values and pause the
  // audio mid-section. Cleared whenever a new section-play starts or the
  // current one reaches its end.
  const sectionEndListenerRef = useRef<(() => void) | null>(null);

  // Fetch signed audio URL
  useEffect(() => {
    let cancelled = false;
    adminGetAudioUrl(trackId)
      .then((url) => { if (!cancelled) setAudioUrl(url); })
      .catch((e) => { if (!cancelled) setAudioError(e.message); });
    return () => { cancelled = true; };
  }, [trackId]);

  useEffect(() => {
    const audio = audioRef.current;
    if (!audio) return;
    function tick() {
      if (audio) setCurrent(audio.currentTime);
      rafRef.current = requestAnimationFrame(tick);
    }
    rafRef.current = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(rafRef.current);
  }, [audioUrl]);

  const togglePlay = useCallback(() => {
    const audio = audioRef.current;
    if (!audio) return;
    if (audio.paused) { audio.play(); setPlaying(true); }
    else { audio.pause(); setPlaying(false); }
  }, []);

  // Play the manually-configured section (existing config flow)
  const playSection = useCallback(() => {
    const audio = audioRef.current;
    if (!audio) return;
    if (sectionEndListenerRef.current) {
      audio.removeEventListener("timeupdate", sectionEndListenerRef.current);
    }
    audio.currentTime = start;
    audio.play();
    setPlaying(true);
    const checkEnd = () => {
      if (audio.currentTime >= end) {
        audio.pause();
        setPlaying(false);
        audio.removeEventListener("timeupdate", checkEnd);
        sectionEndListenerRef.current = null;
      }
    };
    audio.addEventListener("timeupdate", checkEnd);
    sectionEndListenerRef.current = checkEnd;
  }, [start, end]);

  // Play one of the agent's ranked sections — the core QA loop.
  const playAgentSection = useCallback((s: SongSection) => {
    const audio = audioRef.current;
    if (!audio) return;
    if (sectionEndListenerRef.current) {
      audio.removeEventListener("timeupdate", sectionEndListenerRef.current);
    }
    audio.currentTime = s.start_s;
    audio.play();
    setPlaying(true);
    const checkEnd = () => {
      if (audio.currentTime >= s.end_s) {
        audio.pause();
        setPlaying(false);
        audio.removeEventListener("timeupdate", checkEnd);
        sectionEndListenerRef.current = null;
      }
    };
    audio.addEventListener("timeupdate", checkEnd);
    sectionEndListenerRef.current = checkEnd;
  }, []);

  // SVG geometry. Ruler on top, optional band area in the middle, beat strip
  // at the bottom. Beat strip height stays 56px so its internals are unchanged.
  const W = 700;
  const barW = 2;
  const RULER_H = 14;
  const BAND_GAP_TOP = 4;
  const BAND_ROW_H = 14;
  const BAND_ROW_GAP = 2;
  const bandRowCount = sections && sections.length > 0 ? Math.min(3, sections.length) : 0;
  const BAND_AREA_H = bandRowCount > 0
    ? bandRowCount * BAND_ROW_H + (bandRowCount - 1) * BAND_ROW_GAP
    : 0;
  const BAND_GAP_BOTTOM = bandRowCount > 0 ? 6 : 0;
  const BEAT_TOP = RULER_H + BAND_GAP_TOP + BAND_AREA_H + BAND_GAP_BOTTOM;
  const BEAT_H = 56;
  const H = BEAT_TOP + BEAT_H;

  function bandY(rank: 1 | 2 | 3): number {
    // rank 1 stacks at the top so it visually dominates.
    return RULER_H + BAND_GAP_TOP + (rank - 1) * (BAND_ROW_H + BAND_ROW_GAP);
  }

  function handleWaveformClick(e: React.MouseEvent<SVGSVGElement>) {
    const svg = e.currentTarget;
    const rect = svg.getBoundingClientRect();
    const x = e.clientX - rect.left;
    const t = (x / rect.width) * duration;

    if (selectMode === "start") {
      onStartChange(Math.round(t * 10) / 10);
      setSelectMode(null);
    } else if (selectMode === "end") {
      onEndChange(Math.round(t * 10) / 10);
      setSelectMode(null);
    } else {
      // Default: seek audio to clicked position
      const audio = audioRef.current;
      if (audio) audio.currentTime = t;
    }
  }

  // Per-band 0-slot warning. Mirrors the backend PATCH validator
  // (admin_music.py via music_recipe.count_slots) so the user sees the
  // incompatibility BEFORE clicking the band — clicking would auto-fill
  // the form into a state that 422s on Save.
  // Hooks MUST run before any early return — keep this above the
  // audioError / !audioUrl guards below.
  const bandWouldZeroSlot = useMemo(() => {
    if (!sections) return new Map<SongSection, boolean>();
    const m = new Map<SongSection, boolean>();
    for (const s of sections) {
      m.set(s, countSlotsClient(beats, s.start_s, s.end_s, slotEveryN) === 0);
    }
    return m;
  }, [sections, beats, slotEveryN]);

  if (audioError) {
    return <p className="text-sm text-red-400">Could not load audio: {audioError}</p>;
  }
  if (!audioUrl) {
    return <p className="text-sm text-zinc-500">Loading audio...</p>;
  }

  const playheadX = duration > 0 ? (currentTime / duration) * W : 0;

  // `hasSections` is still used to hide the legacy 45s wash band and the
  // Set-start/end buttons at render time (only meaningful when no agent
  // sections exist). The beat-strip highlight, however, ALWAYS follows
  // the form-state window: pre-click-to-select rank-1 was the only
  // sensible "active window" (no other section was reachable), but with
  // click-to-select the form IS the active window — beats must agree.
  const hasSections = !!sections && sections.length > 0;
  const activeStart = start;
  const activeEnd = end;
  // Single source of truth for "which band is selected" — used by both
  // the per-band ✓ + thicker stroke below AND the top metadata Row in
  // page.tsx (via the same helper). Computed once, identity-compared
  // inside the band loop.
  const matchedSection = matchSectionByBounds(sections, start, end);

  // Time-ruler tick positions
  const tickStep = pickTickInterval(duration);
  const ticks: number[] = [];
  for (let t = 0; t <= duration + 0.001; t += tickStep) ticks.push(t);

  return (
    <div>
      <audio
        ref={audioRef}
        src={audioUrl}
        preload="auto"
        onEnded={() => setPlaying(false)}
      />

      {/* Transport controls */}
      <div className="flex items-center gap-3 mb-3">
        <button
          onClick={togglePlay}
          className="bg-zinc-700 hover:bg-zinc-600 text-white text-sm font-semibold px-3 py-1.5 rounded-lg transition-colors"
        >
          {playing ? "⏸ Pause" : "▶ Play"}
        </button>
        <button
          onClick={playSection}
          className="bg-violet-700 hover:bg-violet-600 text-white text-sm font-semibold px-3 py-1.5 rounded-lg transition-colors"
        >
          ▶ Play section ({start.toFixed(1)}s – {end.toFixed(1)}s)
        </button>
        <span className="text-xs text-zinc-400 font-mono tabular-nums">
          {currentTime.toFixed(1)}s / {duration.toFixed(1)}s
        </span>
      </div>

      {/* Interactive waveform */}
      <svg
        width={W}
        height={H}
        className="bg-zinc-800 rounded block"
        style={{ cursor: selectMode ? "crosshair" : "pointer" }}
        onClick={handleWaveformClick}
      >
        {/* Diagonal-stripe pattern for bands that would produce 0 slots
            at the current slot_every_n_beats. Defined once; referenced by
            fill={`url(#bandWarnStripes)`} on warning bands. */}
        <defs>
          <pattern
            id="bandWarnStripes"
            patternUnits="userSpaceOnUse"
            width={6}
            height={6}
            patternTransform="rotate(45)"
          >
            <rect width={6} height={6} fill="rgba(245,158,11,0.18)" />
            <line x1={0} y1={0} x2={0} y2={6} stroke="rgba(245,158,11,0.55)" strokeWidth={2} />
          </pattern>
        </defs>

        {/* Time ruler */}
        {ticks.map((t, i) => {
          const x = (t / duration) * W;
          return (
            <g key={`tick-${i}`}>
              <line x1={x} y1={RULER_H - 4} x2={x} y2={RULER_H} stroke="#52525b" strokeWidth={1} />
              <text
                x={Math.min(x + 2, W - 22)}
                y={RULER_H - 5}
                fontSize={9}
                fill="#71717a"
                fontFamily="ui-monospace, monospace"
              >
                {Math.round(t)}s
              </text>
            </g>
          );
        })}

        {/* Agent section bands (1-3 ranked, stacked rank-1 on top).
            isSelected is identity-compared against the matchedSection
            computed above, so the ✓ + thicker stroke here ALWAYS agrees
            with the top metadata Row in page.tsx (same matcher). */}
        {sections?.map((s, i) => {
          const rank = (s.rank in RANK_COLORS ? s.rank : 1) as 1 | 2 | 3;
          const colors = RANK_COLORS[rank];
          const xRaw = (s.start_s / duration) * W;
          const wRaw = ((s.end_s - s.start_s) / duration) * W;
          // Clamp inside SVG width in case stored end_s overran duration.
          const x = Math.max(0, Math.min(W, xRaw));
          const w = Math.max(2, Math.min(W - x, wRaw));
          const y = bandY(rank);
          const showLabel = w > 110;
          const isSelected = matchedSection === s;
          const wouldZeroSlot = bandWouldZeroSlot.get(s) ?? false;
          const label = `${isSelected ? "✓ " : ""}${wouldZeroSlot ? "⚠ " : ""}#${rank} · ${s.label} · ${s.energy}`;
          const warnTitle = wouldZeroSlot
            ? `Would produce 0 slots at N=${slotEveryN} — lower N or pick a wider band`
            : null;
          return (
            // Key by array index — JSONB rows can theoretically duplicate ranks
            // (write-time parse() guards but read path trusts the data); using
            // rank as key would collide and break React identity tracking.
            <g
              key={`section-${i}`}
              // testid keyed by rank for readable test queries — relies on
              // the write-time parse() uniqueness guard (same one called out
              // in the React-key comment below) for duplicate-free rendering.
              data-testid={`section-band-${rank}`}
              data-zero-slot={wouldZeroSlot ? "true" : "false"}
              onMouseEnter={() => setHoverSection(s)}
              onMouseLeave={() => setHoverSection((prev) => (prev === s ? null : prev))}
              onClick={(e) => {
                if (selectMode !== null) return; // let waveform handler claim it
                e.stopPropagation();
                playAgentSection(s);
                // Snap form state to this band's bounds; page.tsx wires these
                // to setBestStart / setBestEnd, so the Timing config inputs
                // update live and the user just clicks Save.
                onStartChange(s.start_s);
                onEndChange(s.end_s);
              }}
              style={{ cursor: selectMode ? "crosshair" : "pointer" }}
            >
              {warnTitle && <title>{warnTitle}</title>}
              <rect
                x={x}
                y={y}
                width={w}
                height={BAND_ROW_H}
                rx={2}
                fill={colors.fill}
                stroke={colors.stroke}
                strokeWidth={isSelected ? 3 : 1}
              />
              {wouldZeroSlot && (
                // Striped overlay on warning bands. Drawn AFTER the fill
                // so the rank color stays readable underneath, but BEFORE
                // the text so the label outranks the pattern.
                <rect
                  x={x}
                  y={y}
                  width={w}
                  height={BAND_ROW_H}
                  rx={2}
                  fill="url(#bandWarnStripes)"
                  stroke="rgba(245,158,11,0.8)"
                  strokeWidth={1.5}
                  pointerEvents="none"
                />
              )}
              {showLabel ? (
                <text
                  x={x + 4}
                  y={y + BAND_ROW_H - 4}
                  fontSize={10}
                  fill={colors.text}
                  fontFamily="ui-sans-serif, system-ui"
                >
                  {label}
                </text>
              ) : (
                <text
                  x={x + 2}
                  y={y + BAND_ROW_H - 4}
                  fontSize={10}
                  fill={colors.text}
                  fontFamily="ui-sans-serif, system-ui"
                >
                  {`${isSelected ? "✓" : ""}${wouldZeroSlot ? "⚠" : ""}#${rank}`}
                </text>
              )}
            </g>
          );
        })}

        {/* Manual-config selected window highlight (over beat strip).
            Only shown when the agent has not yet picked sections; once it
            has, rank-1 IS the canonical window and the numbered bands
            above show it without this redundant wash. */}
        {!hasSections && (
          <rect
            x={(start / duration) * W}
            y={BEAT_TOP}
            width={Math.max(0, ((end - start) / duration) * W)}
            height={BEAT_H}
            fill="rgba(139,92,246,0.15)"
          />
        )}

        {/* Beat markers. `inWindow` is keyed off the form-state window
            (start/end props) regardless of whether agent sections exist —
            click-to-select means the form IS the active window. */}
        {beats.map((b, i) => {
          const x = (b / duration) * W;
          const inWindow = b >= activeStart && b <= activeEnd;
          return (
            <rect
              key={i}
              x={x}
              y={BEAT_TOP + (inWindow ? 4 : 14)}
              width={barW}
              height={inWindow ? BEAT_H - 8 : BEAT_H - 28}
              fill={inWindow ? "#8b5cf6" : "#52525b"}
              rx={1}
            />
          );
        })}

        {/* Start / end markers — paired with the manual-config wash band,
            so they hide for the same reason. The numbered band borders
            (rendered in the section loop above) already mark rank-1's
            start/end. */}
        {!hasSections && (
          <>
            <line
              x1={(start / duration) * W}
              y1={RULER_H}
              x2={(start / duration) * W}
              y2={H}
              stroke="#22c55e"
              strokeWidth={2}
            />
            <line
              x1={(end / duration) * W}
              y1={RULER_H}
              x2={(end / duration) * W}
              y2={H}
              stroke="#ef4444"
              strokeWidth={2}
            />
          </>
        )}

        {/* Playhead — spans full height including ruler so timestamps stay readable */}
        <line
          x1={playheadX}
          y1={0}
          x2={playheadX}
          y2={H}
          stroke="#ffffff"
          strokeWidth={1.5}
          opacity={0.8}
        />
      </svg>

      {/* Section hover detail card. Always render the slot so the layout
          doesn't jump as the user moves between bands.

          Touch/a11y: the per-band ⚠ marker has an SVG <title> tooltip
          that's hover-only, so we surface the same warning info in the
          idle state too. Counts the 0-slot bands and names them — gives
          touch users the same context hover users get without making
          them tap each band. */}
      {bandRowCount > 0 && (
        <div className="mt-2 min-h-[44px] text-xs">
          {hoverSection ? (
            <div className="bg-zinc-800/60 border border-zinc-700 rounded px-3 py-2 leading-snug">
              <div className="flex items-center gap-2 mb-1">
                <span
                  className="inline-block w-2 h-2 rounded-sm"
                  style={{ background: RANK_COLORS[(hoverSection.rank in RANK_COLORS ? hoverSection.rank : 1) as 1 | 2 | 3].fill }}
                />
                <span className="font-semibold text-zinc-100">
                  Rank #{hoverSection.rank} · {hoverSection.label} · {hoverSection.energy}
                </span>
                <span className="text-zinc-500 font-mono">
                  {formatTime(hoverSection.start_s)} – {formatTime(hoverSection.end_s)}
                </span>
                <span className="text-zinc-500">
                  → use as: <span className="text-zinc-300">{hoverSection.suggested_use}</span>
                </span>
              </div>
              <div className="text-zinc-400">{hoverSection.rationale}</div>
              {bandWouldZeroSlot.get(hoverSection) && (
                <div
                  data-testid="band-warn-hover"
                  className="mt-1 text-amber-300"
                >
                  ⚠ Picking this section produces 0 slots at N={slotEveryN}.
                  Lower N or pick a wider band before saving.
                </div>
              )}
            </div>
          ) : (
            <div className="text-zinc-500 px-1 py-2 flex flex-wrap items-center gap-x-3">
              <span>Hover for rationale · click to preview + select as best section</span>
              {(() => {
                const warned = sections?.filter((s) => bandWouldZeroSlot.get(s)) ?? [];
                if (warned.length === 0) return null;
                const ranks = warned.map((s) => `#${s.rank}`).join(", ");
                return (
                  <span
                    data-testid="band-warn-summary"
                    className="text-amber-300"
                  >
                    ⚠ {ranks} would 0-slot at N={slotEveryN}
                  </span>
                );
              })()}
            </div>
          )}
        </div>
      )}

      {/* Section selection buttons — only meaningful when the agent has
          not picked sections. With sections, rank-1 IS the canonical
          window and manual override would just create drift. */}
      {hasSections ? (
        <div className="flex items-center gap-3 mt-2">
          <span className="text-xs text-zinc-500">
            {beats.length} beats · white = playhead
          </span>
        </div>
      ) : (
        <div className="flex items-center gap-3 mt-2">
          <button
            onClick={() => setSelectMode(selectMode === "start" ? null : "start")}
            className={`text-xs font-semibold px-3 py-1 rounded-lg transition-colors ${
              selectMode === "start"
                ? "bg-green-600 text-white"
                : "bg-zinc-700 hover:bg-zinc-600 text-zinc-300"
            }`}
          >
            {selectMode === "start" ? "Click waveform to set start..." : "Set start"}
          </button>
          <button
            onClick={() => setSelectMode(selectMode === "end" ? null : "end")}
            className={`text-xs font-semibold px-3 py-1 rounded-lg transition-colors ${
              selectMode === "end"
                ? "bg-red-600 text-white"
                : "bg-zinc-700 hover:bg-zinc-600 text-zinc-300"
            }`}
          >
            {selectMode === "end" ? "Click waveform to set end..." : "Set end"}
          </button>
          <span className="text-xs text-zinc-500">
            {beats.length} beats · <span className="text-green-400">green</span> = start · <span className="text-red-400">red</span> = end · white = playhead
          </span>
        </div>
      )}
    </div>
  );
}
