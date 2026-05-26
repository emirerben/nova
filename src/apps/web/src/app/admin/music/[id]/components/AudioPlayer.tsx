"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { adminGetAudioUrl, type SongSection } from "@/lib/music-api";

// Extracted from page.tsx (Next.js rejects non-page named exports from
// page files). The audio player + waveform interaction is its own unit so
// it can be tested in isolation — see __tests__/admin/MusicSectionPrecedence.

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
  onStartChange,
  onEndChange,
}: {
  trackId: string;
  beats: number[];
  duration: number;
  start: number;
  end: number;
  sections: SongSection[] | null;
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
    audio.currentTime = start;
    audio.play();
    setPlaying(true);
    const checkEnd = () => {
      if (audio.currentTime >= end) {
        audio.pause();
        setPlaying(false);
        audio.removeEventListener("timeupdate", checkEnd);
      }
    };
    audio.addEventListener("timeupdate", checkEnd);
  }, [start, end]);

  // Play one of the agent's ranked sections — the core QA loop.
  const playAgentSection = useCallback((s: SongSection) => {
    const audio = audioRef.current;
    if (!audio) return;
    audio.currentTime = s.start_s;
    audio.play();
    setPlaying(true);
    const checkEnd = () => {
      if (audio.currentTime >= s.end_s) {
        audio.pause();
        setPlaying(false);
        audio.removeEventListener("timeupdate", checkEnd);
      }
    };
    audio.addEventListener("timeupdate", checkEnd);
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

  if (audioError) {
    return <p className="text-sm text-red-400">Could not load audio: {audioError}</p>;
  }
  if (!audioUrl) {
    return <p className="text-sm text-zinc-500">Loading audio...</p>;
  }

  const playheadX = duration > 0 ? (currentTime / duration) * W : 0;

  // When agent sections exist, rank-1 is the canonical "active window" — the
  // legacy 45s wash band, Set-start/end buttons, and "Best section" header
  // all hide so only the numbered bands tell the story. Beat-strip
  // highlighting re-keys off rank-1's bounds so beats inside section #1 stay
  // bright (the visual cue that "these are the cut points").
  const hasSections = !!sections && sections.length > 0;
  const activeStart = hasSections ? sections![0].start_s : start;
  const activeEnd = hasSections ? sections![0].end_s : end;

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

        {/* Agent section bands (1-3 ranked, stacked rank-1 on top) */}
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
          const label = `#${rank} · ${s.label} · ${s.energy}`;
          return (
            // Key by array index — JSONB rows can theoretically duplicate ranks
            // (write-time parse() guards but read path trusts the data); using
            // rank as key would collide and break React identity tracking.
            <g
              key={`section-${i}`}
              onMouseEnter={() => setHoverSection(s)}
              onMouseLeave={() => setHoverSection((prev) => (prev === s ? null : prev))}
              onClick={(e) => {
                if (selectMode !== null) return; // let waveform handler claim it
                e.stopPropagation();
                playAgentSection(s);
              }}
              style={{ cursor: selectMode ? "crosshair" : "pointer" }}
            >
              <rect
                x={x}
                y={y}
                width={w}
                height={BAND_ROW_H}
                rx={2}
                fill={colors.fill}
                stroke={colors.stroke}
                strokeWidth={1}
              />
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
                  {`#${rank}`}
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

        {/* Beat markers. `inWindow` is keyed off the ACTIVE window — when
            sections exist that's rank-1; otherwise the manual config window. */}
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
          doesn't jump as the user moves between bands. */}
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
            </div>
          ) : (
            <div className="text-zinc-500 px-1 py-2">
              Hover an agent band for its rationale · click to play that section
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
