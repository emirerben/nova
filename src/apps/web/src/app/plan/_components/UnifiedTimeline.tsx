"use client";

/**
 * UnifiedTimeline — horizontal multi-lane timeline for plan-item variant editing.
 *
 * Four lanes, one shared playhead:
 *   SFX      — fully interactive (drag/trim/add/remove/undo-redo).
 *   Overlays — fully interactive (drag start_s/end_s, trim video clips,
 *               per-card popover for position/scale/remove, upload zone).
 *   Text     — expandable inline panel (click bar → toggle textPanel content).
 *   Clips    — read-only bar  (click → open "clips" tab / TimelineEditor sheet).
 *
 * All SFX mutations flow through the SFX reducer (owned by SfxLane).
 * Overlay mutations flow through onUpdateCard/onRemoveCard/onClearOverlays
 * (owned by OverlayLane).
 * Text mutations flow through textPanel (rendered inline when expanded, owned by TextLane).
 *
 * Kill switch: NEXT_PUBLIC_UNIFIED_TIMELINE_ENABLED (default on).
 *
 * Lane components live in sibling files (T0 refactor):
 *   SfxLane.tsx     — SFX drag system, glossary picker, undo/redo
 *   OverlayLane.tsx — Overlay drag system, trim lane, upload zone
 *   TextLane.tsx    — Text expand/collapse stub (T5 will make it interactive)
 *   ClipsLane.tsx   — Clips expand/collapse
 */

import type { SoundEffectPlacement, MediaOverlay } from "@/lib/plan-api";
import type { SoundEffectSummary } from "@/lib/sfx-api";
import { Playhead } from "@/lib/timeline/Playhead";
import { formatTimecode } from "@/lib/timeline/time-format";
import type { UploadFile } from "./UnifiedTimelineTypes";
import SfxLane from "./SfxLane";
import OverlayLane from "./OverlayLane";
import ClipsLane from "./ClipsLane";
import TextLane from "./TextLane";

// ── Helpers ───────────────────────────────────────────────────────────────────

function tickIntervalFor(totalS: number): number {
  if (totalS <= 10) return 1;
  if (totalS <= 30) return 2;
  if (totalS <= 60) return 5;
  if (totalS <= 120) return 10;
  return 15;
}

// ── Types ─────────────────────────────────────────────────────────────────────

export interface UnifiedTimelineProps {
  totalDurationS: number;
  currentTimeS: number;
  // SFX -----------------------------------------------------------------------
  sfxPlacements: SoundEffectPlacement[];
  sfxGlossaryEffects: SoundEffectSummary[];
  sfxGlossaryLoading: boolean;
  sfxRendering: boolean;
  sfxUploading: boolean;
  onSfxChange: (placements: SoundEffectPlacement[]) => void;
  onSfxUploadRequest: (files: UploadFile[]) => Promise<void>;
  // Overlays (interactive) ----------------------------------------------------
  overlayCards: MediaOverlay[];
  overlaysEnabled: boolean;
  overlayUploading: boolean;
  localPreviewUrls: Record<string, string>;
  onOverlayUploadRequest: (files: UploadFile[]) => void;
  onUpdateCard: (id: string, patch: Partial<MediaOverlay>) => void;
  onRemoveCard: (id: string) => void;
  onClearOverlays: () => void;
  // Text lane (inline editing) -----------------------------------------------
  hasText: boolean;
  /** Inline text/font editing controls — rendered inside the Text lane when expanded. */
  textPanel?: React.ReactNode;
  /** Called when the Text lane expands or collapses — parent can use to switch hero mode. */
  onTextPanelChange?: (open: boolean) => void;
  // Clips lane (inline editing) ----------------------------------------------
  /** Inline clips editor — rendered inside the Clips lane when expanded. */
  clipsPanel?: React.ReactNode;
  /** Called when the Clips lane expands or collapses. */
  onClipsPanelChange?: (open: boolean) => void;
}

// ── Component ─────────────────────────────────────────────────────────────────

export default function UnifiedTimeline({
  totalDurationS,
  currentTimeS,
  sfxPlacements,
  sfxGlossaryEffects,
  sfxGlossaryLoading,
  sfxRendering,
  sfxUploading,
  onSfxChange,
  onSfxUploadRequest,
  overlayCards,
  overlaysEnabled,
  overlayUploading,
  localPreviewUrls,
  onOverlayUploadRequest,
  onUpdateCard,
  onRemoveCard,
  onClearOverlays,
  hasText,
  textPanel,
  onTextPanelChange,
  clipsPanel,
  onClipsPanelChange,
}: UnifiedTimelineProps) {
  // ── Ruler ─────────────────────────────────────────────────────────────────────

  const tickInterval = tickIntervalFor(totalDurationS);
  const ticks =
    totalDurationS > 0
      ? Array.from(
          { length: Math.floor(totalDurationS / tickInterval) + 1 },
          (_, i) => i * tickInterval,
        )
      : [0];

  // ── Render ────────────────────────────────────────────────────────────────────

  return (
    <div className="select-none overflow-x-auto" data-testid="unified-timeline">
      {/* ── Ruler ── */}
      <div className="flex h-5" style={{ minWidth: "100%" }}>
        <div className="flex-shrink-0 w-14" />
        <div className="relative flex-1 bg-zinc-900/40 border-b border-zinc-800/60">
          {totalDurationS > 0 &&
            ticks.map((t) => {
              const pct = (t / totalDurationS) * 100;
              return (
                <div
                  key={t}
                  className="absolute top-0 h-full pointer-events-none"
                  style={{ left: `${pct}%` }}
                >
                  <div className="w-px h-2 bg-zinc-700" />
                  <span className="absolute left-0.5 top-2 text-[8px] leading-none text-zinc-500 whitespace-nowrap">
                    {formatTimecode(t)}
                  </span>
                </div>
              );
            })}
        </div>
      </div>

      {/* ── Clips lane ── */}
      <ClipsLane
        totalDurationS={totalDurationS}
        currentTimeS={currentTimeS}
        clipsPanel={clipsPanel}
        onClipsPanelChange={onClipsPanelChange}
      />

      {/* ── Text lane ── */}
      <TextLane
        totalDurationS={totalDurationS}
        currentTimeS={currentTimeS}
        hasText={hasText}
        textPanel={textPanel}
        onTextPanelChange={onTextPanelChange}
      />

      {/* ── Overlays lane ── */}
      {(overlayCards.length > 0 || overlaysEnabled) && (
        <OverlayLane
          totalDurationS={totalDurationS}
          currentTimeS={currentTimeS}
          overlayCards={overlayCards}
          overlaysEnabled={overlaysEnabled}
          overlayUploading={overlayUploading}
          localPreviewUrls={localPreviewUrls}
          onOverlayUploadRequest={onOverlayUploadRequest}
          onUpdateCard={onUpdateCard}
          onRemoveCard={onRemoveCard}
          onClearOverlays={onClearOverlays}
        />
      )}

      {/* ── SFX lane ── */}
      <SfxLane
        totalDurationS={totalDurationS}
        currentTimeS={currentTimeS}
        sfxPlacements={sfxPlacements}
        sfxGlossaryEffects={sfxGlossaryEffects}
        sfxGlossaryLoading={sfxGlossaryLoading}
        sfxRendering={sfxRendering}
        sfxUploading={sfxUploading}
        onSfxChange={onSfxChange}
        onSfxUploadRequest={onSfxUploadRequest}
      />

      <p className="pl-14 pt-1.5 text-[9px] text-zinc-600">
        Clips lane — click to expand inline · Text lane — click to expand inline
      </p>
    </div>
  );
}

// ── Utility sub-components (available for future lane use) ─────────────────────

interface ReadOnlyLaneProps {
  label: string;
  totalDurationS: number;
  currentTimeS: number;
  onClick: () => void;
  children: React.ReactNode;
}

// eslint-disable-next-line @typescript-eslint/no-unused-vars
function ReadOnlyLane({ label, totalDurationS, currentTimeS, onClick, children }: ReadOnlyLaneProps) {
  return (
    <div
      role="button"
      tabIndex={0}
      className="flex h-10 group cursor-pointer"
      onClick={onClick}
      onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onClick(); } }}
    >
      <div className="flex-shrink-0 w-14 flex items-center justify-end pr-2">
        <span className="text-[9px] font-semibold text-zinc-400 uppercase tracking-wider truncate">
          {label}
        </span>
      </div>
      <div className="relative flex-1 bg-zinc-800/15 border-y border-zinc-700/30 overflow-hidden group-hover:bg-zinc-800/30 transition-colors">
        <Playhead currentTimeS={currentTimeS} totalDurationS={totalDurationS} />
        {children}
      </div>
    </div>
  );
}

interface FullWidthBarProps {
  label: string;
  colorClass: string;
  onClick: () => void;
}

// eslint-disable-next-line @typescript-eslint/no-unused-vars
function FullWidthBar({ label, colorClass, onClick }: FullWidthBarProps) {
  return (
    <button
      type="button"
      className={["absolute inset-1 rounded flex items-center px-2 border transition-colors", colorClass].join(" ")}
      onClick={(e) => { e.stopPropagation(); onClick(); }}
    >
      <span className="text-[10px] truncate pointer-events-none">{label}</span>
    </button>
  );
}
