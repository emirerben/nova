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

import { useEffect, useState } from "react";
import type { SoundEffectPlacement, MediaOverlay } from "@/lib/plan-api";
import type { SoundEffectSummary } from "@/lib/sfx-api";
import { Playhead } from "@/lib/timeline/Playhead";
import { formatTimecode } from "@/lib/timeline/time-format";
import type { TextElementBar } from "@/lib/timeline/text-timeline-reducer";
import type { UploadFile, SuggestionLaneEntry } from "./UnifiedTimelineTypes";
import SfxLane from "./SfxLane";
import OverlayLane from "./OverlayLane";
import ClipsLane from "./ClipsLane";
import TextLane from "./TextLane";
import type { ClipTimelineHandle } from "./useClipTimeline";
import EditorTimelineBody, {
  type EditorTimelineBodyProps,
} from "../items/[id]/_editor/EditorTimelineBody";

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
  /** True while any render is in flight (shared render_status) — disables the lane. */
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
  /** record:false marks interim drag patches so history-owning parents can coalesce. */
  onUpdateCard: (
    id: string,
    patch: Partial<MediaOverlay>,
    options?: { record?: boolean },
  ) => void;
  onRemoveCard: (id: string) => void;
  onClearOverlays: () => void;
  /**
   * 006 T3 (005-4A): pending AI overlay suggestions rendered as editable
   * provenance cards in the Overlays lane (+ read-only sfx diamonds in the
   * SFX lane). Edits fire onSuggestionEdit — never the manual card callbacks.
   */
  overlaySuggestions?: SuggestionLaneEntry[];
  onSuggestionEdit?: (suggestionId: string, patch: Partial<MediaOverlay>) => void;
  /**
   * 009 T3: intro-text window for the hatched zinc keep-out band in the
   * Overlays lane and the "Covers your intro text" fullscreen warning.
   * The timing MUST come from the variant's intro fields upstream (page.tsx
   * owns the wiring) — the timeline never derives a duplicate source of truth.
   */
  introTextWindow?: { start_s: number; end_s: number } | null;
  /**
   * 009 T3: resolves aspect/pixel metadata for an overlay's src_gcs_path
   * (overlays only carry the path) so the fullscreen popover can raise
   * crop/low-res warnings. Optional — warnings degrade gracefully
   * (suppressed, never faked) while unwired or while a field is missing.
   */
  resolveAssetMeta?: (
    srcGcsPath: string,
  ) => { aspect?: number; width?: number; height?: number } | undefined;
  /**
   * 009 T5 (D5/E9): when set, fullscreen promotion is unavailable on the
   * focused variant (lyrics) — the Overlays-lane popover renders the
   * "Full screen" option disabled with this copy. The page owns the wiring
   * ("Full-screen cutaways aren't available on lyric edits.").
   */
  fullscreenDisabledReason?: string | null;
  /**
   * R2 (review C8): web twin of the api FULLSCREEN_CUTAWAYS_ENABLED. When false
   * (default in Vercel until the Fly deploy carrying display_mode is live), the
   * NEW fullscreen PROMOTE affordances hide (segmented "Full screen" option, the
   * "Make full screen →" max-scale affordance, and the F-to-fullscreen chip
   * shortcut) so a previewed fullscreen can't bake as pip against an old api.
   * Existing fullscreen cards still render and demote paths stay live. Defaults
   * to true so unwired callers keep the pre-flag behavior.
   */
  fullscreenPromoteEnabled?: boolean;
  /**
   * 009 T3 external-edit contract (hero preview click-to-edit): when this
   * changes to a card id present in the Overlays lane, that card's popover
   * opens and onExternalEditHandled() fires so the page can clear its
   * handoff state.
   */
  externalEditCardId?: string | null;
  /** Ack for externalEditCardId — called once the popover has been opened. */
  onExternalEditHandled?: () => void;
  // Text lane (interactive multi-block) ----------------------------------------
  /**
   * Text element bars to display. T6 will wire real API data here.
   * Defaults to [] (empty state) when not provided.
   */
  textElements?: TextElementBar[];
  /**
   * Called after every user edit to text bars (move, trim, add, delete).
   * T6 will wire this to the API persist path.
   */
  onTextElementsChange?: (bars: TextElementBar[]) => void;
  /**
   * T7: Called when the user clicks "Apply" in the text property panel.
   * Triggers an immediate API persist (bypasses the debounce in page.tsx).
   */
  onTextApply?: (bars: TextElementBar[]) => void;
  /**
   * T10 State 4: called when a trim drag is clamped to the minimum bar duration.
   * Parent (page.tsx) can show a brief "Minimum 0.Xs" note.
   */
  onTextTrimClamped?: () => void;
  /**
   * T8: true when the variant is an Editorial (sequence) variant and the user
   * hasn't made any text-element edits yet.  Forwarded to TextLane.
   */
  isFirstSequenceEdit?: boolean;
  // Clips lane (inline editing) ----------------------------------------------
  /** Inline clips editor — rendered inside the Clips lane when expanded. */
  clipsPanel?: React.ReactNode;
  /** Called when the Clips lane expands or collapses. */
  onClipsPanelChange?: (open: boolean) => void;
  /**
   * Clip timeline handle from useClipTimeline in the parent.
   * When provided, the Clips lane renders per-clip segment bars with drag;
   * the expanded panel (clipsPanel) also receives this handle so both share
   * one draft.
   */
  clipTimelineHandle?: ClipTimelineHandle;
  /**
   * Plan C fix: called when the user clicks a clip bar body in the lane.
   * The key is the clicked slot.key so the parent can pre-select it in
   * InlineClipsEditor and show only that clip's trim panel.
   */
  onClipBodyClick?: (key: string) => void;
  /**
   * 3-column layout: when provided, the TextPropertyPanel for the selected bar
   * is portaled into this element (forwarded to TextLane).
   */
  textPanelPortalTarget?: HTMLElement | null;
  /**
   * 3-column layout: called whenever the text bar selection changes so the
   * parent can conditionally show/hide other right-panel content.
   */
  onTextBarSelect?: (id: string | null) => void;
  /**
   * Editor-shell mode (plan §6, T4). When provided, UnifiedTimeline renders the
   * scale-driven editor timeline (Text → Video → Sound → Overlays, zoom, scrub,
   * lime selection, mutes, filmstrip) instead of the item-page lanes. Absent =
   * the item-page timeline is byte-identical to before (all props above only).
   */
  editorMode?: EditorTimelineBodyProps;
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
  overlaySuggestions,
  onSuggestionEdit,
  introTextWindow,
  resolveAssetMeta,
  fullscreenDisabledReason,
  fullscreenPromoteEnabled = true,
  externalEditCardId,
  onExternalEditHandled,
  textElements,
  onTextElementsChange,
  onTextApply,
  onTextTrimClamped,
  isFirstSequenceEdit,
  clipsPanel,
  onClipsPanelChange,
  clipTimelineHandle,
  onClipBodyClick,
  textPanelPortalTarget,
  onTextBarSelect,
  editorMode,
}: UnifiedTimelineProps) {
  // ── Editor-shell mode: delegate to the scale-driven editor timeline ──────────
  // (early return so the item-page render path below is untouched.)

  // ── Text lane selection (controlled here so T7 can read expandedBarId) ────────

  const [textExpandedBarId, setTextExpandedBarId] = useState<string | null>(null);

  // Notify parent when selection changes (for 3-column layout right-panel control).
  useEffect(() => {
    onTextBarSelect?.(textExpandedBarId);
  }, [textExpandedBarId, onTextBarSelect]);

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

  // Editor-shell mode wins the render (all hooks above still run, so hook order
  // is stable). The item-page path below is unchanged.
  if (editorMode) return <EditorTimelineBody {...editorMode} />;

  return (
    <div className="select-none overflow-x-auto" data-testid="unified-timeline">
      {/* ── Ruler ── */}
      <div className="flex h-5" style={{ minWidth: "100%" }}>
        <div className="flex-shrink-0 w-14" />
        <div className="relative flex-1 bg-zinc-50 border-b border-zinc-200">
          {totalDurationS > 0 &&
            ticks.map((t) => {
              const pct = (t / totalDurationS) * 100;
              return (
                <div
                  key={t}
                  className="absolute top-0 h-full pointer-events-none"
                  style={{ left: `${pct}%` }}
                >
                  <div className="w-px h-2 bg-zinc-300" />
                  <span className="absolute left-0.5 top-2 text-[8px] leading-none text-zinc-400 whitespace-nowrap">
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
        clipHandle={clipTimelineHandle}
        onClipBodyClick={onClipBodyClick}
      />

      {/* ── Text lane ── */}
      <TextLane
        textElements={textElements ?? []}
        durationSeconds={totalDurationS}
        currentTime={currentTimeS}
        onTextElementsChange={onTextElementsChange ?? (() => {})}
        expandedBarId={textExpandedBarId}
        onBarSelect={setTextExpandedBarId}
        onApply={onTextApply}
        onTrimClamped={onTextTrimClamped}
        isFirstSequenceEdit={isFirstSequenceEdit}
        textPanelPortalTarget={textPanelPortalTarget}
      />

      {/* ── Overlays lane ── */}
      {(overlayCards.length > 0 ||
        (overlaySuggestions?.length ?? 0) > 0 ||
        overlaysEnabled) && (
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
          suggestions={overlaySuggestions}
          onSuggestionEdit={onSuggestionEdit}
          introTextWindow={introTextWindow}
          resolveAssetMeta={resolveAssetMeta}
          fullscreenDisabledReason={fullscreenDisabledReason}
          fullscreenPromoteEnabled={fullscreenPromoteEnabled}
          externalEditCardId={externalEditCardId}
          onExternalEditHandled={onExternalEditHandled}
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
        suggestionSfx={(overlaySuggestions ?? [])
          .filter((s) => s.sfx != null)
          .map((s) => ({ id: s.id, sfx: s.sfx!, staged: s.staged }))}
      />

      <p className="pl-14 pt-1.5 text-[9px] text-zinc-400">
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
        <span className="text-[9px] font-semibold text-zinc-500 uppercase tracking-wider truncate">
          {label}
        </span>
      </div>
      <div className="relative flex-1 bg-zinc-50 border-y border-zinc-200 overflow-hidden group-hover:bg-zinc-100 transition-colors">
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
