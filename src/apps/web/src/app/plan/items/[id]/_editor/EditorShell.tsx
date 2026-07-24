"use client";

/**
 * EditorShell — the full-screen TikTok-parity editor at
 * /plan/items/[id]/edit?variant=<id> (plan §1, approved mockup Variant A).
 *
 * Full-viewport grid: 56px top bar / minmax(480px,1fr) canvas row / 260px
 * timeline region. Middle row: ToolRail · ToolDrawer · canvas · InspectorPanel
 * (~320px, PERMANENTLY reserved — the canvas never reflows on select/deselect,
 * D6) · InspectorRail (~72px).
 *
 * First paint: drawer closed, no selection, inspector empty state, Select
 * tool active, playhead 0:00, video paused on frame 0.
 *
 * Working state = local reducer bars (text-timeline-reducer) + title. No
 * mid-edit server writes; Save persists once via commitEditorSession
 * (lib/editor-commit.ts — endpoint lands with the API task; a local 404
 * surfaces as the quiet retry notice and working state is preserved).
 */

import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import {
  changePlanItemStyle,
  getPlanItem,
  getPlanItemJobStatus,
  getSfxAudioUrl,
  deletePoolAsset,
  editPlanItemVariant,
  getLyricSeeds,
  LyricSeedsError,
  NotAuthenticatedError,
  confirmOverlayUploads,
  listPoolAssets,
  registerPoolAsset,
  retimeVisualBlock,
  requestOverlayUploadUrls,
  requestPoolAssetUploadUrls,
  sha256HexOfFile,
  uploadToGcs,
  type MediaOverlay,
  type OverlaySuggestion,
  type PlanItem,
  type PlanItemVariant,
  type PoolAsset,
  type SoundEffectPlacement,
  type TextElement,
  type VisualBlock,
} from "@/lib/plan-api";
import { getSoundEffects, type SoundEffectSummary } from "@/lib/sfx-api";
import { getMusicTracks, type MusicTrackSummary } from "@/lib/music-api";
import { canvasForOrientation } from "@/lib/overlay-constants";
import { type TextBoxHorizontalPosition } from "@/lib/overlay-layout";
import {
  buildEditorCommitRequest,
  commitEditorSession,
  editorCommitBaseGeneration,
  EditorCommitConflictError,
  type AcceptedSuggestionRef,
  type EditorCommitLyricsRequest,
} from "@/lib/editor-commit";
import { captionMetaFromVariant } from "@/lib/caption-meta";
import {
  buildPlanItemEditorReturnHref,
  editorCommitStartedRender,
} from "@/lib/editor-return";
import { FONT_FACES } from "@/lib/font-faces";
import { type GenerativeStyleSet } from "@/lib/generative-api";
import { formatTimecode } from "@/lib/timeline/time-format";
import { DEFAULT_TEXT_PRESET, TEXT_PRESETS, type TextPreset } from "@/lib/text-presets";
import { applyCopilotOps, type ApplyCopilotOpsResult } from "@/lib/edit-copilot/apply-ops";
import {
  allowedOpFamiliesFromCapabilities,
  buildCopilotSnapshot,
  type CopilotCaptionMetaSnapshot,
  type CopilotSnapshot,
} from "@/lib/edit-copilot/snapshot";
import { useEditCopilot } from "@/lib/edit-copilot/useEditCopilot";
import type { CaptionMetaPatch, CopilotOp } from "@/lib/edit-copilot/ops";
import {
  initTextEditorState,
  textReducer,
  type TextElementBar,
} from "@/lib/timeline/text-timeline-reducer";
import { InkButton } from "@/components/ui/InkButton";
import { ConfirmDialog } from "@/components/ui/ConfirmDialog";
import { useFocusTrap } from "@/components/ui/useFocusTrap";
import UnifiedTimeline from "@/app/plan/_components/UnifiedTimeline";
import { useClipTimeline } from "@/app/plan/_components/useClipTimeline";
import type { DraftSlot } from "@/app/generative/timeline-math";
import {
  barsToCaptionCues,
  barsToPreviewTextElements,
  barsToTextElements,
  buildLyricLineOverrides,
  isLyricBar,
  seedBarsFromLyricSeeds,
  seedBarsFromVariant,
} from "./editor-bars";
import { isCaptionArchetype } from "@/lib/variant-editor/eligibility";
import {
  CAPTIONS_TAB_REASON,
  computeToolDisabledReasons,
  editorReasonCopy,
  isElementsLyricsModel,
  lyricsFeatureAvailable as computeLyricsFeatureAvailable,
  textElementsLockedCopy,
} from "./editor-capabilities";
import {
  resolveSmartPlacementAssignments,
  isMasonryVariant,
  resolveSmartPlacementCandidate,
  resolveSmartPlacementCandidates,
  smartPlacementPatchForBar,
  collageMotionForTextBar,
  textBoxPositionPatchForBar,
  textBoxScreenXFrac,
} from "./editor-smart-placement";
import {
  buildTimedTextSequence,
  TEXT_ELEMENTS_API_MAX,
} from "./editor-text-composition";
import { splitSlotAt, deleteSlotEnforceFloor, activeSlotCount } from "./slot-split";
import {
  applyClipTimingInput,
  applyTextTimingInput,
  outputTimeForSlotBoundary,
  rangesDiffer,
  sequentialSlotLayout,
} from "./editor-bar-drag";
import TransportBar from "./TransportBar";
import type { EditorTimelineBodyProps } from "./EditorTimelineBody";
import EditorCanvas from "./EditorCanvas";
import OverlaySuggestions, { type PendingUpload } from "./OverlaySuggestions";
import { computeReseedSections } from "./editor-reseed";
import InspectorPanel from "./InspectorPanel";
import InspectorRail, { type InspectorTab } from "./InspectorRail";
import ToolDrawer from "./ToolDrawer";
import ToolRail, { type EditorTool } from "./ToolRail";
import type { SongWindowState } from "./SongWindowSelector";
import PresetGrid, { presetMatchesFields } from "./PresetGrid";
import { useVirtualPreview } from "./useVirtualPreview";
import { useEditorLayoutMode } from "./useEditorLayoutMode";
import type { EditorLayoutMode } from "./useEditorLayoutMode";
import { slotsDifferFromBaseline } from "./virtual-timeline";
import {
  deleteKeyAllowed,
  escapeAction,
  nudgeBarStart,
  type EditorSelectionKind,
  useEditorSelection,
} from "./useEditorSelection";
import {
  draftKey,
  deserializeDraft,
  serializeDraft,
  useEditorHistory,
  type EditorDocument,
} from "./useEditorHistory";
import {
  isUnavailableError,
  SUGGESTION_POLL_INTERVAL_MS,
  useEditorOverlaySuggestions,
} from "./useEditorOverlaySuggestions";

const ZOOM_OPTIONS = [100, 125, 150] as const;

/** Default duration + look of a freshly added text bar (plan §2). */
const NEW_TEXT_DURATION_S = 2.0;
const NEW_TEXT_CONTENT = "Add a title";
const NEW_TEXT_Y_FRAC = 0.4;
const NEW_TEXT_SIZE_PX = 64;
const COMPOSITION_TEXT_PRESET =
  TEXT_PRESETS.find((preset) => preset.id === "editorial-italic") ?? DEFAULT_TEXT_PRESET;
const COMPOSITION_Y_FRACS = [0.36, 0.44, 0.52] as const;
const COPILOT_SAVE_NOTICE_KEY = "nova-copilot-save-expectation-dismissed";
const MEDIA_OVERLAYS_RAW = (process.env.NEXT_PUBLIC_MEDIA_OVERLAYS_ENABLED ?? "").trim();
const MEDIA_OVERLAYS_UI_ENABLED =
  MEDIA_OVERLAYS_RAW.toLowerCase() === "true" || MEDIA_OVERLAYS_RAW === "1";
const SOUND_EFFECTS_UI_ENABLED = process.env.NEXT_PUBLIC_SOUND_EFFECTS_ENABLED === "true";
const VISUAL_BLOCKS_UI_ENABLED =
  process.env.NEXT_PUBLIC_VISUAL_BLOCKS_ENABLED === "true";
const LYRICS_EDITOR_UI = process.env.NEXT_PUBLIC_LYRICS_EDITOR_ENABLED === "true";
// Lyrics-optional "elements" model: instant toggle-insert/remove of
// beat-synced lyric bars, no render round-trip. Independent of
// LYRICS_EDITOR_UI (the legacy baked-lyrics bar editor) — see
// lyricsFeatureAvailable/isElementsLyricsModel in editor-capabilities.ts.
const LYRICS_OPTIONAL_UI = process.env.NEXT_PUBLIC_LYRICS_OPTIONAL_ENABLED === "true";
const LANDSCAPE_UI = process.env.NEXT_PUBLIC_LANDSCAPE_OUTPUT_ENABLED === "true";

type EditorOrientation = "portrait" | "landscape";

function patchVisualBlockConcreteTiming(
  block: VisualBlock,
  patch: Partial<VisualBlock>,
): VisualBlock {
  const next = { ...block, ...patch } as VisualBlock;
  if (
    next.kind !== "montage" ||
    (typeof patch.start_s !== "number" && typeof patch.end_s !== "number")
  ) {
    return next;
  }
  const oldDuration = Math.max(0.001, block.end_s - block.start_s);
  const newDuration = Math.max(0.001, next.end_s - next.start_s);
  let offset = 0;
  next.shots = next.shots.map((shot, index) => {
    const duration_s =
      index === next.shots.length - 1
        ? newDuration - offset
        : (shot.duration_s / oldDuration) * newDuration;
    const resized = { ...shot, start_offset_s: offset, duration_s };
    offset += duration_s;
    return resized;
  });
  return next;
}

function retimeLinkedTextBar(
  bar: TextElementBar,
  block: VisualBlock,
  start_s: number,
  end_s: number,
): Pick<TextElementBar, "start_s" | "end_s"> {
  const oldDuration = Math.max(0.001, block.end_s - block.start_s);
  const newDuration = Math.max(0.001, end_s - start_s);
  return {
    start_s:
      start_s +
      Math.max(0, Math.min(1, (bar.start_s - block.start_s) / oldDuration)) *
        newDuration,
    end_s:
      start_s +
      Math.max(0, Math.min(1, (bar.end_s - block.start_s) / oldDuration)) *
        newDuration,
  };
}
const POOL_MIME_TYPES = [
  "image/jpeg",
  "image/png",
  "image/webp",
  "image/heic",
  "video/mp4",
  "video/quicktime",
];

type LyricsCapability = NonNullable<
  NonNullable<PlanItemVariant["editor_capabilities"]>["lyrics"]
>;

function textTimingAtPlayhead({
  currentTime,
  previewDuration,
}: {
  currentTime: number;
  previewDuration: number;
}): Pick<TextElementBar, "start_s" | "end_s"> {
  const start = Math.max(0, Math.round(currentTime * 10) / 10);
  const end =
    previewDuration > 0
      ? Math.min(previewDuration, start + NEW_TEXT_DURATION_S)
      : start + NEW_TEXT_DURATION_S;
  return {
    start_s: start,
    end_s: Math.max(end, start + 0.5),
  };
}

function newTextBar({
  id,
  text,
  timing,
  preset,
}: {
  id: string;
  text: string;
  timing: Pick<TextElementBar, "start_s" | "end_s">;
  preset: TextPreset;
}): TextElementBar {
  return {
    id,
    text,
    start_s: timing.start_s,
    end_s: timing.end_s,
    role: "generative_intro",
    x_frac: 0.5,
    y_frac: NEW_TEXT_Y_FRAC,
    position: "custom",
    size_px: NEW_TEXT_SIZE_PX,
    alignment: "center",
    font_family: preset.fields.font_family ?? undefined,
    color: preset.fields.color ?? undefined,
    highlight_color: preset.fields.highlight_color ?? undefined,
    stroke_width: preset.fields.stroke_width ?? undefined,
    shadow_enabled: false,
    effect: preset.fields.effect ?? undefined,
  };
}

function defaultLyricsCapability(variant: PlanItemVariant | null): LyricsCapability {
  return {
    editable: false,
    enabled: variant?.text_mode === "lyrics",
    can_toggle_on: false,
    reason: "disabled",
  };
}

function persistedLyricsEnabled(variant: PlanItemVariant | null): boolean {
  return variant?.lyrics_enabled ?? (variant?.text_mode === "lyrics");
}

function persistedOrientation(variant: PlanItemVariant | null): EditorOrientation {
  return variant?.orientation === "landscape" ? "landscape" : "portrait";
}

function stableJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(stableJson).join(",")}]`;
  if (value && typeof value === "object") {
    return `{${Object.keys(value as Record<string, unknown>)
      .sort()
      .map((key) => `${JSON.stringify(key)}:${stableJson((value as Record<string, unknown>)[key])}`)
      .join(",")}}`;
  }
  return JSON.stringify(value);
}

function lyricsToggleHint(reason: "disabled" | "no_track" | "no_renderable_lyrics" | null): string | null {
  if (reason === "no_track") return "Add a song first — use Swap song";
  if (reason === "no_renderable_lyrics") return "This song doesn't have synced lyrics";
  if (reason === "disabled") return "Lyrics editing is turned off right now";
  return null;
}

function orientationDisabledHint(reason: string | null | undefined): string | null {
  if (reason === "orientation_unsupported") {
    return "This edit style doesn't support landscape yet";
  }
  if (reason === "disabled") return "Landscape output is turned off right now";
  return null;
}

export function spaceShortcutAllowed(target: HTMLElement | null): boolean {
  if (!deleteKeyAllowed(target)) return false;
  return (target?.tagName ?? "").toUpperCase() !== "BUTTON";
}

export function shouldCloseToolOnSelection({
  layoutMode,
  activeTool,
  preserveOverlayTool,
}: {
  layoutMode: EditorLayoutMode;
  activeTool: EditorTool | null;
  preserveOverlayTool?: boolean;
}): boolean {
  return layoutMode === "overlay" && activeTool !== "nova" && !preserveOverlayTool;
}

export function resolveCopilotApplyFeedback({
  result,
  bars,
  beforeSlots,
  grid,
}: {
  result: ApplyCopilotOpsResult;
  bars: TextElementBar[];
  beforeSlots: DraftSlot[];
  grid: number[];
}): {
  textIds: string[];
  slotIds: string[];
  first:
    | { kind: "text"; id: string; seekS: number }
    | { kind: "clip"; id: string; seekS: number }
    | null;
} {
  const textIds = result.textActions
    .map((action) => ("id" in action ? action.id : action.type === "ADD_TEXT" ? action.bar.id : null))
    .filter((id): id is string => !!id);
  const slotIds = result.nextSlots
    ? result.nextSlots
        .filter((slot) => {
          const before = beforeSlots.find((s) => s.key === slot.key);
          return !before || JSON.stringify(before) !== JSON.stringify(slot);
        })
        .map((slot) => slot.key)
    : [];

  // Never select/seek to a just-deleted element — selecting a DELETE_BAR
  // target points at a ghost id (and light mode would open the edit sheet for
  // a bar that no longer exists) (review F6). Deleted targets still flash on
  // the timeline; selection goes to the first SURVIVING changed element.
  const firstTextAction = result.textActions.find((action) => action.type !== "DELETE_BAR");
  if (firstTextAction) {
    const id =
      "id" in firstTextAction
        ? firstTextAction.id
        : firstTextAction.type === "ADD_TEXT"
          ? firstTextAction.bar.id
          : null;
    const bar =
      firstTextAction.type === "ADD_TEXT"
        ? firstTextAction.bar
        : id
          ? bars.find((b) => b.id === id) ?? null
          : null;
    if (id && bar) {
      return { textIds, slotIds, first: { kind: "text", id, seekS: (bar.start_s + bar.end_s) / 2 } };
    }
  }

  if (result.nextSlots) {
    const layout = sequentialSlotLayout(result.nextSlots, grid);
    for (const slotId of slotIds) {
      const nextIndex = result.nextSlots.findIndex((slot) => slot.key === slotId);
      const slot = result.nextSlots[nextIndex];
      if (!slot || slot.removed) continue;
      const win = layout.windows[nextIndex];
      return {
        textIds,
        slotIds,
        first: { kind: "clip", id: slotId, seekS: win?.startS ?? 0 },
      };
    }
  }

  return { textIds, slotIds, first: null };
}

function SelectCursorIcon() {
  return (
    <svg
      aria-hidden="true"
      viewBox="0 0 24 24"
      className="h-[18px] w-[18px]"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d="M4 3l8 18 2.2-7.2L21 11 4 3z" />
      <path d="M13.5 13.5 19 19" />
    </svg>
  );
}

function PanHandIcon() {
  return (
    <svg
      aria-hidden="true"
      viewBox="0 0 24 24"
      className="h-[18px] w-[18px]"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d="M8 11V6.5a2 2 0 0 1 4 0V11" />
      <path d="M12 11V5.5a2 2 0 0 1 4 0V12" />
      <path d="M16 12V8.5a2 2 0 0 1 4 0V15" />
      <path d="M8 12.5V10a2 2 0 0 0-4 0v4.5C4 19 7 22 12 22h1c4 0 7-3 7-7" />
    </svg>
  );
}

function SaveSpinner() {
  return (
    <span
      aria-hidden="true"
      className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-white/30 border-t-white"
    />
  );
}

function OrientationToggle({
  value,
  disabled,
  disabledHint,
  onChange,
}: {
  value: EditorOrientation;
  disabled: boolean;
  disabledHint: string | null;
  onChange: (orientation: EditorOrientation) => void;
}) {
  const title = disabled ? (disabledHint ?? "Landscape isn't available for this edit.") : "Output format";
  return (
    <div
      role="group"
      aria-label="Output format"
      title={title}
      className="flex min-h-11 items-center rounded-lg border border-zinc-200 bg-white p-0.5"
    >
      {(["portrait", "landscape"] as const).map((option) => {
        const selected = value === option;
        return (
          <button
            key={option}
            type="button"
            aria-label={option === "portrait" ? "Use 9:16 output" : "Use 16:9 output"}
            aria-pressed={selected}
            disabled={disabled}
            onClick={() => onChange(option)}
            className={[
              "min-h-10 min-w-[54px] rounded-md px-2 text-[12px] font-semibold",
              "focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500",
              selected ? "bg-[#0c0c0e] text-white" : "text-[#3f3f46] hover:bg-zinc-100",
              disabled ? "cursor-not-allowed opacity-45 hover:bg-transparent" : "",
            ].join(" ")}
          >
            {option === "portrait" ? "9:16" : "16:9"}
          </button>
        );
      })}
    </div>
  );
}

/** The Captions-tab deep link — shared by the read-only banner and the
 * text-locked notice so both surfaces point at the same target identically. */
function CaptionsTabLink({ itemId, variantId }: { itemId: string; variantId?: string | null }) {
  const href = `/plan/items/${itemId}?tab=captions${variantId ? `&variant=${encodeURIComponent(variantId)}` : ""}`;
  return (
    <a
      href={href}
      className="font-semibold underline decoration-zinc-300 underline-offset-4 hover:text-[#0c0c0e] focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500"
    >
      Open the item page Captions tab
    </a>
  );
}

export default function EditorShell({
  itemId,
  variantParam,
}: {
  itemId: string;
  variantParam: string | null;
}) {
  const router = useRouter();

  // ── Data ────────────────────────────────────────────────────────────────────
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [needsAuth, setNeedsAuth] = useState(false);
  const [item, setItem] = useState<PlanItem | null>(null);
  const [variants, setVariants] = useState<PlanItemVariant[]>([]);
  const [loadNonce, setLoadNonce] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setLoadError(null);
    (async () => {
      try {
        const it = await getPlanItem(itemId);
        const job = it.current_job_id
          ? await getPlanItemJobStatus(it.current_job_id)
          : null;
        if (cancelled) return;
        setItem(it);
        setVariants(job?.variants ?? []);
        setLoading(false);
      } catch (err) {
        if (cancelled) return;
        if (err instanceof NotAuthenticatedError) setNeedsAuth(true);
        else setLoadError(err instanceof Error ? err.message : "Couldn't load this video.");
        setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [itemId, loadNonce]);

  const variant = useMemo(() => {
    if (variants.length === 0) return null;
    return (
      variants.find((v) => v.variant_id === variantParam) ??
      variants.find((v) => v.output_url || v.base_video_url) ??
      variants[0]
    );
  }, [variants, variantParam]);

  // ── Working state ───────────────────────────────────────────────────────────
  const [state, dispatch] = useReducer(textReducer, initTextEditorState([]));
  // Originals by id — Save merges bar edits OVER these so fields the editor
  // doesn't model (reveal_s, word_timings, …) survive untouched.
  const originalsRef = useRef<Map<string, TextElement>>(new Map());
  const seededVariantIdRef = useRef<string | null>(null);
  // Conflict-tile Reload: the refetched variant must replace working state in
  // sections the user hasn't touched (an AI auto-apply or another tab moved
  // them), while dirty sections keep the user's edits. Without this, the
  // seeding guard above skips the refetch entirely and the NEXT Save clobbers
  // the other writer's changes with a freshly-blessed baseline.
  const conflictReseedRef = useRef(false);
  const [title, setTitle] = useState("");
  // Last style-set applied via restyle-all — drives the StyleChip ring.
  const [appliedStyleSetId, setAppliedStyleSetId] = useState<string | null>(null);
  const [localSfx, setLocalSfx] = useState<SoundEffectPlacement[]>([]);
  const [localSfxAudioUrls, setLocalSfxAudioUrls] = useState<Record<string, string>>({});
  const [localOverlays, setLocalOverlays] = useState<MediaOverlay[]>([]);
  const [localVisualBlocks, setLocalVisualBlocks] = useState<VisualBlock[]>([]);
  // AI-suggestion provenance (Overlays drawer): accepted envelope id + the
  // overlay card id it staged. Kept OFF the MediaOverlay objects — the save
  // filters these against the staged overlay ids, so an undone accept is
  // never resolved server-side.
  const [acceptedSuggestions, setAcceptedSuggestions] = useState<AcceptedSuggestionRef[]>([]);
  const suggestedOverlayIds = useMemo(
    () => new Set(acceptedSuggestions.map((a) => a.overlayId)),
    [acceptedSuggestions],
  );
  const [localOverlayPreviewUrls, setLocalOverlayPreviewUrls] = useState<Record<string, string>>({});
  const localOverlayPreviewUrlsRef = useRef<Record<string, string>>({});
  const [sfxDirty, setSfxDirty] = useState(false);
  const [overlaysDirty, setOverlaysDirty] = useState(false);
  const [visualBlocksDirty, setVisualBlocksDirty] = useState(false);
  const [mixLevel, setMixLevel] = useState<number | null>(null);
  const [mixDirty, setMixDirty] = useState(false);
  const [textDirty, setTextDirty] = useState(false);
  const [lyricsEnabled, setLyricsEnabled] = useState(false);
  const [orientation, setOrientation] = useState<EditorOrientation>("portrait");
  const [titleDirty, setTitleDirty] = useState(false);
  const [captionMeta, setCaptionMeta] = useState<CopilotCaptionMetaSnapshot | null>(null);
  const [captionMetaDirty, setCaptionMetaDirty] = useState(false);
  const [captionMetaPatch, setCaptionMetaPatch] = useState<CaptionMetaPatch>({});
  const lyricsCap = variant?.editor_capabilities?.lyrics ?? defaultLyricsCapability(variant);
  // Lyrics-optional "elements" model: dual-gated by the FE flag AND the
  // variant's lyrics_model — flag-off or legacy (baked) variants take every
  // existing code path below untouched.
  const isNewLyricsModel = isElementsLyricsModel(variant?.editor_capabilities);
  const lyricsOptionalActive = LYRICS_OPTIONAL_UI && isNewLyricsModel;
  // Toggle visibility, unified across both models (see editor-capabilities.ts).
  const lyricsFeatureAvailable = computeLyricsFeatureAvailable(variant?.editor_capabilities);
  // Legacy-only: whether lyric_line elements are projected into text_elements
  // for local bar editing via the OLD lyricOverrides route. Always false on
  // elements-model variants — those bars are inserted/removed by the toggle,
  // not projected from the read adapter.
  const lyricBarsAvailable = LYRICS_EDITOR_UI && !isNewLyricsModel && lyricsCap.editable;
  // Elements-model: cache fetched lyric-seed bars per variant (once per
  // session) + track the in-flight/error state that drives the toggle's
  // loading/disabled copy.
  // Cached as raw TextElement[] (not bars) — inserting re-derives bars AND
  // re-merges into originalsRef so word_timings/reveal_s/fade_out_ms/z (fields
  // the bar type doesn't model) survive the barsToTextElements merge-over-
  // original on Save, exactly like server-seeded bars from a variant read.
  const lyricSeedsCacheRef = useRef<Map<string, TextElement[]>>(new Map());
  const [lyricSeedsLoading, setLyricSeedsLoading] = useState(false);
  const [lyricSeedsError, setLyricSeedsError] = useState<"not_found" | "no_lyrics" | null>(null);
  useEffect(() => {
    setLyricSeedsError(null);
    setLyricSeedsLoading(false);
  }, [variant?.variant_id]);
  const hasLyricBars = useMemo(() => state.bars.some(isLyricBar), [state.bars]);
  const previewOrientation = LANDSCAPE_UI ? orientation : "portrait";
  const activeCanvas = useMemo(
    () => canvasForOrientation(previewOrientation),
    [previewOrientation],
  );

  useEffect(() => {
    if (!variant) return;
    const sameVariant = seededVariantIdRef.current === variant.variant_id;
    const conflictReseed = conflictReseedRef.current && sameVariant;
    if (sameVariant && !conflictReseed) return;
    conflictReseedRef.current = false;
    seededVariantIdRef.current = variant.variant_id;
    const sections = computeReseedSections(
      { textDirty, sfxDirty, overlaysDirty, mixDirty },
      conflictReseed,
    );
    // Visual blocks and their linked TextElements are one atomic document. On
    // a baseline conflict, preserve or reload them together so neither half can
    // point at state from the other tab.
    const keepCoupledVisualDocument =
      conflictReseed && (visualBlocksDirty || textDirty);
    if (sections.text && !keepCoupledVisualDocument) {
      originalsRef.current = new Map(
        (variant.text_elements ?? []).map((el) => [el.id, el]),
      );
      dispatch({
        type: "RESET",
        // Elements-model bars are ordinary persisted text_elements once
        // saved — always include them so a reload reflects a prior toggle-on
        // save without re-fetching seeds. Legacy still gates on
        // lyricBarsAvailable (projection-for-editing only when the old
        // editor UI is on).
        bars: seedBarsFromVariant(variant, {
          includeLyrics: lyricBarsAvailable || lyricsOptionalActive,
        }),
      });
      setLyricsEnabled(lyricsFeatureAvailable && persistedLyricsEnabled(variant));
      setTextDirty(false);
    }
    if (sections.sfx) {
      setLocalSfx((variant.sound_effects ?? []).map((p) => ({ ...p })));
      setLocalSfxAudioUrls({});
      setSfxDirty(false);
    }
    if (sections.overlays) {
      setLocalOverlays((variant.media_overlays ?? []).map((o) => ({ ...o })));
      setLocalOverlayPreviewUrls((current) => {
        Object.values(current).forEach((url) => URL.revokeObjectURL(url));
        return {};
      });
      // Re-seeded from the server ⇒ any accepted-but-unsaved cards are gone.
      setAcceptedSuggestions([]);
      setOverlaysDirty(false);
    }
    if (!keepCoupledVisualDocument) {
      setLocalVisualBlocks((variant.visual_blocks ?? []).map((block) => ({ ...block })));
      setVisualBlocksDirty(false);
    }
    if (sections.titleAndStyle) setTitleDirty(false);
    const keepLocalOrientation =
      conflictReseed && orientation !== persistedOrientation(variant);
    if (!keepLocalOrientation) setOrientation(persistedOrientation(variant));
    if (sections.mix) {
      const seededMix =
        typeof variant.mix === "number"
          ? variant.mix
          : typeof variant.voiceover_bed_level === "number"
            ? variant.voiceover_bed_level
            : null;
      setMixLevel(seededMix);
      setMixDirty(false);
      setSoundMuted(seededMix === 0);
    }
    if (!conflictReseed || !captionMetaDirty) {
      setCaptionMeta(captionMetaFromVariant(variant));
      setCaptionMetaDirty(false);
      setCaptionMetaPatch({});
    }
    if (sections.titleAndStyle) setAppliedStyleSetId(null);
    // Dirty flags are read as a snapshot when a (re)seed fires; they must not
    // retrigger it.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [variant, lyricBarsAvailable, lyricsFeatureAvailable, lyricsOptionalActive, orientation]);

  useEffect(() => {
    localOverlayPreviewUrlsRef.current = localOverlayPreviewUrls;
  }, [localOverlayPreviewUrls]);

  useEffect(() => {
    return () => {
      Object.values(localOverlayPreviewUrlsRef.current).forEach((url) => URL.revokeObjectURL(url));
    };
  }, []);

  useEffect(() => {
    if (!item || titleDirty) return;
    setTitle(item.theme ?? "");
  }, [item, titleDirty]);

  // ── View state ──────────────────────────────────────────────────────────────
  const layoutMode = useEditorLayoutMode();
  const { selection, select, clear } = useEditorSelection();
  const [activeTool, setActiveTool] = useState<EditorTool | null>(null); // drawer CLOSED at first paint
  const [inspectorTab, setInspectorTab] = useState<InspectorTab>("basic");
  const [lightSheetOpen, setLightSheetOpen] = useState(false);
  const [canvasTool, setCanvasTool] = useState<"select" | "pan">("select");
  const [zoomPct, setZoomPct] = useState<number>(100);
  const [flashTextIds, setFlashTextIds] = useState<Set<string>>(new Set());
  const [flashOverlayIds, setFlashOverlayIds] = useState<Set<string>>(new Set());
  const [flashTimelineIds, setFlashTimelineIds] = useState<Set<string>>(new Set());
  const [sessionHasCopilotEdits, setSessionHasCopilotEdits] = useState(false);
  const [copilotSaveNoticeDismissed, setCopilotSaveNoticeDismissed] = useState(true);
  const panEnabled = zoomPct > 100;
  const [currentTime, setCurrentTime] = useState(0);
  const [duration, setDuration] = useState(0);
  const videoRef = useRef<HTMLVideoElement>(null);
  const renderedMusicAudioRef = useRef<HTMLAudioElement>(null);
  const contentRef = useRef<HTMLTextAreaElement>(null);

  // ── Timeline view state (plan §6) ───────────────────────────────────────────
  const [playing, setPlaying] = useState(false);
  const [zoom, setZoom] = useState(1); // 1 = fit-to-width
  const [timelineFitRequestKey, setTimelineFitRequestKey] = useState(0);
  const [videoMuted, setVideoMuted] = useState(false);
  const [soundMuted, setSoundMuted] = useState(false);
  const [toast, setToast] = useState<string | null>(null);
  const [timelineDirty, setTimelineDirty] = useState(false);
  const [sfxGlossaryEffects, setSfxGlossaryEffects] = useState<SoundEffectSummary[]>([]);
  const [sfxGlossaryLoading, setSfxGlossaryLoading] = useState(false);
  const [musicTracks, setMusicTracks] = useState<MusicTrackSummary[]>([]);
  const [musicTracksLoaded, setMusicTracksLoaded] = useState(false);
  const [musicTracksLoading, setMusicTracksLoading] = useState(false);
  const [selectedMusicTrackId, setSelectedMusicTrackId] = useState<string | null>(
    variant?.music_track_id ?? variant?.background_music?.track_id ?? null,
  );
  const [musicStartS, setMusicStartS] = useState<number>(
    variant?.music_preview_start_s ?? variant?.background_music?.start_s ?? 0,
  );
  const [musicDirty, setMusicDirty] = useState(false);
  const musicHydratedVariantIdRef = useRef<string | null>(null);
  const [overlayUploading, setOverlayUploading] = useState(false);
  const [poolAssets, setPoolAssets] = useState<PoolAsset[]>([]);
  const [maxPoolAssets, setMaxPoolAssets] = useState(20);
  const [pendingPoolUploads, setPendingPoolUploads] = useState<PendingUpload[]>([]);
  const [poolUnavailable, setPoolUnavailable] = useState(false);
  const [poolError, setPoolError] = useState<string | null>(null);

  // Clip slots — the shell's local working state for split/delete (seeded from
  // the shared clip-timeline handle, then edited locally; persisted via
  // editor-commit `timeline_slots`).
  const timelineVariantId = variant?.variant_id ?? variantParam ?? "";
  const clip = useClipTimeline(itemId, timelineVariantId, "plan-item");
  const [localSlots, setLocalSlots] = useState<DraftSlot[] | null>(null);
  const slotsSeededRef = useRef<string | null>(null);
  useEffect(() => {
    if (!variant || timelineVariantId !== variant.variant_id || clip.loadState !== "ready") return;
    if (slotsSeededRef.current === variant?.variant_id) return;
    slotsSeededRef.current = variant?.variant_id ?? null;
    setLocalSlots(clip.state.slots.map((s) => ({ ...s })));
    setTimelineDirty(false);
  }, [clip.loadState, clip.state.slots, timelineVariantId, variant]);
  useEffect(() => {
    const nextVariantId = variant?.variant_id ?? null;
    const changedVariant = musicHydratedVariantIdRef.current !== nextVariantId;
    if (!changedVariant && musicDirty) return;
    musicHydratedVariantIdRef.current = nextVariantId;
    setSelectedMusicTrackId(variant?.music_track_id ?? variant?.background_music?.track_id ?? null);
    setMusicStartS(variant?.music_preview_start_s ?? variant?.background_music?.start_s ?? 0);
    setMusicDirty(false);
  }, [
    musicDirty,
    variant?.variant_id,
    variant?.music_track_id,
    variant?.background_music?.track_id,
    variant?.background_music?.start_s,
    variant?.music_preview_start_s,
  ]);
  const slots = localSlots ?? clip.state.slots;
  const reloadClipTimeline = clip.reload;
  const clipDirty = useMemo(
    () => slotsDifferFromBaseline(clip.state.baseline, slots),
    [clip.state.baseline, slots],
  );
  const [virtualFallback, setVirtualFallback] = useState(false);
  const virtualRefetchAttemptedRef = useRef(false);
  const virtualRefetchInFlightRef = useRef(false);

  // Virtual-preview music recovery state. The retry budget is one refetch per
  // edit session per track — a missing audio blob mints a fresh (still broken)
  // signed URL on every fetch, so re-arming on success would loop forever.
  const [virtualMusicUnavailable, setVirtualMusicUnavailable] = useState(false);
  const musicRefetchAttemptedRef = useRef(false);
  const virtualMusicAutoFetchRef = useRef(false);
  const musicTracksFetchRef = useRef<Promise<void> | null>(null);
  // Local blob copy of the track audio (see the fetch effect below) — declared
  // here so the error handler can drop it before retrying with a fresh URL.
  const [virtualMusicBlob, setVirtualMusicBlob] = useState<{
    trackId: string;
    url: string;
  } | null>(null);

  const refreshMusicTracks = useCallback((): Promise<void> => {
    if (musicTracksFetchRef.current) return musicTracksFetchRef.current;
    setMusicTracksLoading(true);
    const fetchPromise = getMusicTracks()
      .then((res) => {
        setMusicTracks(res.tracks);
        setMusicTracksLoaded(true);
      })
      .catch(() => {
        // Keep whatever tracks we already have and leave `musicTracksLoaded`
        // false so the picker/virtual-preview gates can trigger a retry later.
        setToast("Couldn't load music.");
      })
      .finally(() => {
        setMusicTracksLoading(false);
        musicTracksFetchRef.current = null;
      });
    musicTracksFetchRef.current = fetchPromise;
    return fetchPromise;
  }, []);

  useEffect(() => {
    if (!clipDirty && !musicDirty) {
      setVirtualFallback(false);
      virtualRefetchAttemptedRef.current = false;
      virtualRefetchInFlightRef.current = false;
      setVirtualMusicUnavailable(false);
      musicRefetchAttemptedRef.current = false;
      virtualMusicAutoFetchRef.current = false;
    }
  }, [clipDirty, musicDirty]);

  // Toast auto-clear.
  useEffect(() => {
    if (!toast) return;
    const t = window.setTimeout(() => setToast(null), 2600);
    return () => window.clearTimeout(t);
  }, [toast]);

  // ── Read-only capability gate (plan §9 / E4) ────────────────────────────────
  // A variant whose editor_capabilities are ALL false is read-only: banner +
  // Save disabled + every mutating command no-ops. The server's honest reason
  // is surfaced verbatim.
  const capabilities = variant?.editor_capabilities;
  const readOnly =
    !!capabilities &&
    capabilities.text_elements === false &&
    !lyricsFeatureAvailable &&
    capabilities.timeline === false &&
    capabilities.split_clips === false &&
    capabilities.mix === false &&
    capabilities.sfx === false &&
    capabilities.overlays === false &&
    capabilities.visual_blocks !== true &&
    capabilities.orientation?.editable !== true &&
    capabilities.music_window?.editable !== true &&
    capabilities.background_music !== true;
  const readOnlyReason = editorReasonCopy(capabilities?.reason);
  // Text-elements gate (plan 010 OV-1): once sfx/overlays flip true on
  // subtitled variants the shell is editable, but on-video text still lives
  // in the Captions tab — every add-text path must stay blocked.
  const textElementsLocked =
    !readOnly && capabilities?.text_elements === false && !lyricsFeatureAvailable;
  // Legacy lyrics variants still use the old whole-style-set route when the
  // frontend lyrics editor is off. With the new gate on, projected lyric bars
  // are edited locally and saved through editor-commit's `lyrics` section.
  const isLyrics = variant?.text_mode === "lyrics";
  // Caption archetypes edit captions in the item-page Captions tab, not this
  // shell. Keyed off the archetype (+ base video) via isCaptionArchetype, NOT
  // capabilities.text_elements — that flips to `true` for subtitled once
  // SUBTITLED_TEXT_LANE_ENABLED ships, at which point a text_elements===false
  // gate would silently drop the Captions signpost for the exact archetype that
  // needs it. See isCaptionArchetype / DECISIONS (caption-edit discoverability).
  const isCaptionEdit = !!variant && isCaptionArchetype(variant);
  // ANY server timeline ineligibility locks the clip lane — a reason-whitelist
  // here let lyrics_sync (and any future reason) edit clips freely in the UI
  // only to 422 at save time.
  const clipEditingLocked =
    capabilities?.timeline === false || variant?.resolved_archetype === "narrated";
  const clipDisabledReason =
    capabilities?.reason === "voiceover_bed_fit" ||
    capabilities?.reason === "locked_to_voiceover" ||
    variant?.resolved_archetype === "narrated"
      ? "locked to your voiceover"
      : editorReasonCopy(capabilities?.reason);

  // ── Unified undo/redo (plan §7, task T8) ────────────────────────────────────
  const getCurrent = useCallback(
    (): EditorDocument => ({
      bars: state.bars,
      slots: localSlots,
      sfx: localSfx,
      overlays: localOverlays,
      visualBlocks: localVisualBlocks,
      captionMeta,
      captionMetaDirty,
      captionMetaPatch,
      videoMuted,
      soundMuted,
      mixLevel,
      mixDirty,
      musicTrackId: selectedMusicTrackId,
      musicStartS,
      musicDirty,
      lyricsEnabled,
      orientation,
      title,
    }),
    [
      state.bars,
      localSlots,
      localSfx,
      localOverlays,
      localVisualBlocks,
      captionMeta,
      captionMetaDirty,
      captionMetaPatch,
      videoMuted,
      soundMuted,
      mixLevel,
      mixDirty,
      selectedMusicTrackId,
      musicStartS,
      musicDirty,
      lyricsEnabled,
      orientation,
      title,
    ],
  );

  const applyDocument = useCallback(
    (doc: EditorDocument) => {
      const beforeIds = new Set(state.bars.map((b) => b.id));
      dispatch({ type: "RESET", bars: doc.bars });
      setLocalSlots(doc.slots);
      setLocalSfx(doc.sfx ?? []);
      setLocalOverlays(doc.overlays ?? []);
      setLocalVisualBlocks(doc.visualBlocks ?? []);
      setVideoMuted(doc.videoMuted);
      setSoundMuted(doc.soundMuted);
      setMixLevel(doc.mixLevel ?? null);
      setMixDirty(doc.mixDirty ?? false);
      setSelectedMusicTrackId(doc.musicTrackId ?? variant?.music_track_id ?? null);
      setMusicStartS(doc.musicStartS ?? variant?.music_preview_start_s ?? 0);
      setMusicDirty(doc.musicDirty ?? false);
      setLyricsEnabled(doc.lyricsEnabled ?? persistedLyricsEnabled(variant));
      setOrientation(doc.orientation ?? persistedOrientation(variant));
      setCaptionMeta(doc.captionMeta ?? null);
      setCaptionMetaDirty(doc.captionMetaDirty ?? false);
      setCaptionMetaPatch(doc.captionMetaPatch ?? {});
      setTitle(doc.title);
      setTextDirty(true);
      // Sections the active variant can't accept (e.g. visual_blocks on a
      // lyrics variant, sfx/overlays gated off) ride along in the undo
      // snapshot as an untouched echo — don't blanket-dirty them, or the next
      // save ships a section the backend editor-commit guard 422s the WHOLE
      // commit for (see agents/DECISIONS.md undo/redo dirty-flag bug).
      if (capabilities?.sfx !== false) setSfxDirty(true);
      if (capabilities?.overlays !== false) setOverlaysDirty(true);
      if (capabilities?.visual_blocks !== false) setVisualBlocksDirty(true);
      setTitleDirty(true);
      // Undo of a delete (or redo of an add) resurrects a bar → re-select it
      // (plan §5 — the one selection rule that reaches into undo).
      const resurrected = doc.bars.find((b) => !beforeIds.has(b.id));
      if (resurrected) {
        select("text", resurrected.id);
        setInspectorTab("basic");
      }
    },
    [state.bars, select, variant, capabilities],
  );

  const history = useEditorHistory({ getCurrent, apply: applyDocument });

  const visibleTextBars = useMemo(() => {
    // Elements model: state.bars already IS the source of truth (the toggle
    // inserts/removes lyric bars directly) — nothing to filter.
    if (lyricsOptionalActive) return state.bars;
    return lyricBarsAvailable && lyricsEnabled
      ? state.bars
      : state.bars.filter((bar) => !isLyricBar(bar));
  }, [lyricBarsAvailable, lyricsEnabled, lyricsOptionalActive, state.bars]);
  const subtitledCaptionTimelineBars = useMemo<TextElementBar[]>(() => {
    if (variant?.resolved_archetype !== "subtitled") return [];
    return (variant.caption_cues ?? []).map((cue, index) => ({
      id: `subtitled-caption-${index}`,
      text: cue.text,
      start_s: cue.start_s,
      end_s: cue.end_s,
      role: "narrated_caption",
    }));
  }, [variant?.caption_cues, variant?.resolved_archetype]);
  const timelineTextBars = useMemo(
    () => [...visibleTextBars, ...subtitledCaptionTimelineBars],
    [subtitledCaptionTimelineBars, visibleTextBars],
  );
  const lyricLineOverrides = useMemo(
    () =>
      lyricBarsAvailable
        ? buildLyricLineOverrides(state.bars, originalsRef.current)
        : {},
    [lyricBarsAvailable, state.bars],
  );
  const lyricOverridesDirty =
    lyricBarsAvailable &&
    stableJson(lyricLineOverrides) !== stableJson(variant?.lyric_line_overrides ?? {});
  // Elements model never sends the legacy `lyrics` commit section — toggling
  // on/off mutates state.bars via ADD_LYRIC_BARS/REMOVE_LYRIC_BARS, which
  // already drives `dirty` through the normal undo-history + textDirty path.
  const lyricsDirty =
    !lyricsOptionalActive &&
    lyricsFeatureAvailable &&
    (lyricsEnabled !== persistedLyricsEnabled(variant) || lyricOverridesDirty);
  const orientationDirty = LANDSCAPE_UI && orientation !== persistedOrientation(variant);

  // Every mutation (text, slots, mutes, title) records into the undo stack.
  // A redo-only stack is clean only when the original baseline is still
  // reachable; after the bounded stack evicts it, empty `past` remains dirty.
  const dirty =
    !history.isAtBaseline || musicDirty || captionMetaDirty || lyricsDirty || orientationDirty;

  // ── Save / cancel state ─────────────────────────────────────────────────────
  // saveState: idle → saving → {conflict | error | partial} (all preserve
  // working state); full success navigates away.
  const [saveState, setSaveState] = useState<
    "idle" | "saving" | "conflict" | "error" | "partial"
  >("idle");
  const [saveMessage, setSaveMessage] = useState<string | null>(null);
  const saving = saveState === "saving";
  const [confirmLeave, setConfirmLeave] = useState(false);
  const [musicAlignmentPrompt, setMusicAlignmentPrompt] = useState(false);
  // Resume-draft notice (plan §9 crash recovery). Non-null → show the notice.
  const [draftDoc, setDraftDoc] = useState<EditorDocument | null>(null);
  // A full save is a terminal navigation handoff. Derived dirty checks still
  // compare against the pre-save variant for one render, so fence draft writes
  // after commit success or they can recreate the draft we just removed.
  const draftPersistenceSuspendedRef = useRef(false);

  // ── Derived ─────────────────────────────────────────────────────────────────
  const elements = useMemo(
    () => barsToPreviewTextElements(visibleTextBars, originalsRef.current),
    [visibleTextBars],
  );

  const selectedBar = useMemo(
    () =>
      selection?.kind === "text"
        ? (visibleTextBars.find((b) => b.id === selection.id) ?? null)
        : null,
    [selection, visibleTextBars],
  );
  const clipSourceDurations = useMemo(() => {
    const out: Record<string, number | null> = {};
    for (const slot of slots) {
      out[slot.key] = clip.state.clipDurations[slot.clipIndex] ?? null;
    }
    return out;
  }, [clip.state.clipDurations, slots]);

  const slotLayout = useMemo(
    () => sequentialSlotLayout(slots, clip.state.grid),
    [clip.state.grid, slots],
  );
  const timelineDuration =
    slotLayout.totalDurationS > 0 ? slotLayout.totalDurationS : duration;

  const selectedClip = useMemo(() => {
    if (selection?.kind !== "clip") return null;
    const idx = slots.findIndex((s) => s.key === selection.id);
    const slot = idx >= 0 ? slots[idx] : null;
    if (!slot) return null;
    const source = clip.clips.find((c) => c.clip_index === slot.clipIndex) ?? null;
    const windowDurationS = slotLayout.windows[idx]?.durationS ?? 0;
    return {
      slot,
      clipNumber: idx + 1,
      durationS: slot.durationS ?? windowDurationS,
      sourceDurationS: source?.duration_s ?? clipSourceDurations[slot.key] ?? null,
      sourceUrl: source?.signed_url ?? null,
    };
  }, [clip.clips, clipSourceDurations, selection, slotLayout.windows, slots]);

  const selectedSfx = useMemo(
    () =>
      selection?.kind === "sfx"
        ? (localSfx.find((s) => s.id === selection.id) ?? null)
        : null,
    [localSfx, selection],
  );

  const previewSfxPlacements = useMemo(
    () => (soundMuted ? [] : localSfx),
    [localSfx, soundMuted],
  );

  const selectedOverlay = useMemo(
    () =>
      selection?.kind === "overlay"
        ? (localOverlays.find((o) => o.id === selection.id) ?? null)
        : null,
    [localOverlays, selection],
  );

  const handleVirtualSourceError = useCallback(() => {
    if (virtualRefetchInFlightRef.current) return;
    if (!virtualRefetchAttemptedRef.current) {
      virtualRefetchAttemptedRef.current = true;
      virtualRefetchInFlightRef.current = true;
      void Promise.resolve(reloadClipTimeline()).finally(() => {
        virtualRefetchInFlightRef.current = false;
      });
      return;
    }
    setVirtualFallback(true);
  }, [reloadClipTimeline]);

  // Expired-signature recovery for the virtual-preview music element: one
  // refetch (fresh signed URLs), then give up honestly — decks stay muted and
  // the "preview after Save" hint covers the silent music.
  const handleVirtualMusicError = useCallback(() => {
    // Drop any blob copy first — if it errored (or masked a bad fetch), the
    // retry must go back to a freshly-signed remote URL.
    setVirtualMusicBlob((prev) => {
      if (prev) URL.revokeObjectURL(prev.url);
      return null;
    });
    if (!musicRefetchAttemptedRef.current) {
      musicRefetchAttemptedRef.current = true;
      void refreshMusicTracks();
      return;
    }
    setVirtualMusicUnavailable(true);
  }, [refreshMusicTracks]);

  const effectiveMusicTrackId =
    musicDirty
      ? selectedMusicTrackId
      : selectedMusicTrackId ??
        variant?.music_track_id ??
        variant?.background_music?.track_id ??
        null;
  const virtualMusicTrack = effectiveMusicTrackId
    ? musicTracks.find((track) => track.id === effectiveMusicTrackId) ?? null
    : null;
  const musicWindowCapability = capabilities?.music_window;
  const songWindowState = useMemo<SongWindowState | null>(() => {
    if (!musicWindowCapability || !effectiveMusicTrackId) return null;
    const isCurrentTrack = effectiveMusicTrackId === variant?.music_track_id;
    const trackDurationS = isCurrentTrack
      ? musicWindowCapability.track_duration_s
      : (virtualMusicTrack?.duration_s ?? 0);
    const beats = isCurrentTrack
      ? musicWindowCapability.beat_timestamps_s
      : (virtualMusicTrack?.beat_timestamps_s ?? []);
    const videoDurationS =
      clipDirty && timelineDuration > 0
        ? timelineDuration
        : musicWindowCapability.video_duration_s;
    const reason = isCurrentTrack
      ? musicWindowCapability.reason
      : trackDurationS <= 0
        ? "track_duration_unknown"
        : trackDurationS + 0.02 < videoDurationS
          ? "song_shorter_than_video"
          : beats.length === 0
            ? "timing_metadata_unavailable"
            : null;
    return {
      startS: musicStartS,
      videoDurationS,
      trackDurationS,
      recommendedStartS: isCurrentTrack
        ? musicWindowCapability.recommended_start_s
        : (virtualMusicTrack?.preview_start_s ?? 0),
      beatTimestampsS: beats,
      editable: reason === null,
      reason,
    };
  }, [
    effectiveMusicTrackId,
    clipDirty,
    musicStartS,
    musicWindowCapability,
    timelineDuration,
    variant?.music_track_id,
    virtualMusicTrack,
  ]);
  // All mutators and draft/history restores maintain musicDirty against the
  // persisted start. Keying this solely to that flag avoids a one-render false
  // positive while a newly loaded variant hydrates its local start offset.
  const musicWindowDirty = !!songWindowState && musicDirty;
  const virtualPreviewRequested =
    (clipDirty || musicWindowDirty) && !virtualFallback && clip.loadState === "ready";
  const musicPreviewRequested = musicWindowDirty || virtualPreviewRequested;
  const effectiveMusicTitle =
    virtualMusicTrack?.title ??
    variant?.track_title ??
    variant?.background_music?.track_title ??
    "Music";
  // Fallback for tracks the public gallery doesn't list (the matcher considers
  // unpublished tracks): the status response carries a fresh-signed preview URL
  // for the variant's OWN matched track. Only valid while the effective track
  // is still the variant's — a picker selection must never reuse it.
  const variantMusicFallbackActive =
    !!variant?.music_track_id && effectiveMusicTrackId === variant.music_track_id;
  const virtualMusicRemoteUrl = virtualMusicUnavailable
    ? null
    : virtualMusicTrack?.preview_audio_url ??
      (variantMusicFallbackActive ? variant?.music_preview_url ?? null : null);
  const virtualMusicStartS = musicStartS;

  // Blob-cache the track audio (a few MB of m4a) once per track: streaming the
  // signed GCS URL rebuffers mid-preview on real networks (measured: 5 music
  // `waiting` stalls in an 18s preview), and every rebuffer is an audible gap.
  // A local object URL can never starve. Best-effort — CORS/network failure
  // just keeps streaming from the remote URL.
  useEffect(() => {
    if (!musicPreviewRequested || !effectiveMusicTrackId || !virtualMusicRemoteUrl) return;
    if (virtualMusicBlob?.trackId === effectiveMusicTrackId) return;
    const controller = new AbortController();
    let cancelled = false;
    fetch(virtualMusicRemoteUrl, { signal: controller.signal })
      .then((res) => {
        if (!res.ok) throw new Error(`music fetch ${res.status}`);
        return res.blob();
      })
      .then((blob) => {
        if (cancelled) return;
        setVirtualMusicBlob((prev) => {
          if (prev) URL.revokeObjectURL(prev.url);
          return { trackId: effectiveMusicTrackId, url: URL.createObjectURL(blob) };
        });
      })
      .catch(() => {
        // Keep streaming the remote URL.
      });
    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [musicPreviewRequested, effectiveMusicTrackId, virtualMusicRemoteUrl, virtualMusicBlob]);
  useEffect(
    () => () => {
      setVirtualMusicBlob((prev) => {
        if (prev) URL.revokeObjectURL(prev.url);
        return null;
      });
    },
    [],
  );
  const virtualMusicAudioUrl =
    virtualMusicBlob?.trackId === effectiveMusicTrackId && !virtualMusicUnavailable
      ? virtualMusicBlob.url
      : virtualMusicRemoteUrl;

  // Picking a different track supplies a brand-new URL — re-arm the retry
  // budget and clear the gave-up flag.
  useEffect(() => {
    setVirtualMusicUnavailable(false);
    musicRefetchAttemptedRef.current = false;
    virtualMusicAutoFetchRef.current = false;
  }, [effectiveMusicTrackId]);

  // The virtual preview starts the moment a clip edit lands, but the music
  // track list loads lazily — make sure the active track's preview URL is
  // being fetched when the preview needs it (once per edit session).
  useEffect(() => {
    if (!musicPreviewRequested || !effectiveMusicTrackId) return;
    if (musicTracksLoaded || musicTracksLoading) return;
    if (virtualMusicAutoFetchRef.current) return;
    virtualMusicAutoFetchRef.current = true;
    void refreshMusicTracks();
  }, [
    musicPreviewRequested,
    effectiveMusicTrackId,
    musicTracksLoaded,
    musicTracksLoading,
    refreshMusicTracks,
  ]);
  const virtualPreview = useVirtualPreview({
    enabled: virtualPreviewRequested,
    slots,
    clips: clip.clips,
    grid: clip.state.grid,
    currentTime,
    muted: videoMuted,
    musicAudioUrl: virtualMusicAudioUrl,
    musicStartS: virtualMusicStartS,
    soundMuted,
    musicTrackActive: effectiveMusicTrackId != null,
    onTimeUpdate: setCurrentTime,
    onDuration: () => {},
    onPlayingChange: setPlaying,
    onSourceError: handleVirtualSourceError,
    onMusicError: handleVirtualMusicError,
  });
  const virtualPreviewActive =
    virtualPreviewRequested &&
    !virtualPreview.timeline.hasMissingSource &&
    virtualPreview.timeline.entries.length > 0;
  const renderedMusicPreviewActive =
    musicWindowDirty && !virtualPreviewActive && !!virtualMusicAudioUrl;

  // Music-only edits on variants without an editable clip timeline (notably
  // legacy song_lyrics) preview against the rendered video. The baked mix is
  // always muted while a separate audio element follows its transport.
  useEffect(() => {
    const video = videoRef.current;
    const audio = renderedMusicAudioRef.current;
    if (!video) return;
    video.muted = videoMuted || soundMuted || renderedMusicPreviewActive;
    if (!audio || !renderedMusicPreviewActive) {
      audio?.pause();
      return;
    }
    audio.muted = soundMuted;
    const sync = () => {
      const target = Math.max(0, musicStartS + video.currentTime);
      if (Number.isFinite(audio.duration) && audio.duration > 0) {
        audio.currentTime = Math.min(target, Math.max(0, audio.duration - 0.01));
      } else {
        audio.currentTime = target;
      }
    };
    const play = () => {
      sync();
      void audio.play().catch(() => {});
    };
    const pause = () => audio.pause();
    const keepSynced = () => {
      const target = musicStartS + video.currentTime;
      if (Math.abs(audio.currentTime - target) > 0.15) sync();
    };
    video.addEventListener("play", play);
    video.addEventListener("pause", pause);
    video.addEventListener("seeking", sync);
    video.addEventListener("timeupdate", keepSynced);
    sync();
    if (!video.paused) play();
    return () => {
      video.removeEventListener("play", play);
      video.removeEventListener("pause", pause);
      video.removeEventListener("seeking", sync);
      video.removeEventListener("timeupdate", keepSynced);
      audio.pause();
    };
  }, [
    musicStartS,
    renderedMusicPreviewActive,
    soundMuted,
    variant,
    videoMuted,
    virtualMusicAudioUrl,
  ]);
  const pauseVirtualPreview = virtualPreview.pause;
  const seekVirtualPreview = virtualPreview.seekTo;
  const toggleVirtualPreview = virtualPreview.toggle;
  // Rendered-video playback already has the lyrics burned into the pixels
  // (base and final are both lyric-burned) — DOM-render a lyric bar there only
  // when it's dirty, so unedited lines don't double-display. Virtual preview
  // composes raw source clips (no burned text), so it needs every lyric bar.
  // Elements-model variants are NEVER lyric-burned (product decision: lyrics
  // are not in the render) — every lyric_line element must show in BOTH
  // preview paths, same as any other text element.
  const previewElements = useMemo(() => {
    if (virtualPreviewActive || lyricsOptionalActive) return elements;
    return elements.filter((el) => {
      if (el.role !== "lyric_line") return true;
      const sourceKey = typeof el.source_params?.key === "string" ? el.source_params.key : null;
      const key = sourceKey ?? el.id.match(/^lyric_(L\d+)$/)?.[1] ?? null;
      return key != null && key in lyricLineOverrides;
    });
  }, [elements, virtualPreviewActive, lyricsOptionalActive, lyricLineOverrides]);
  const previewDuration = virtualPreviewActive
    ? virtualPreview.timeline.totalDurationS
    : duration;
  const smartPlacementCandidates = useMemo(() => {
    const targetBars = isMasonryVariant(variant)
      ? visibleTextBars.filter((bar) => bar.role !== "narrated_caption")
      : selectedBar
        ? [selectedBar]
        : [];
    return resolveSmartPlacementCandidates(variant, targetBars, previewDuration);
  }, [previewDuration, selectedBar, visibleTextBars, variant]);
  const smartPlacementCandidate = selectedBar ? (smartPlacementCandidates[0] ?? null) : null;
  const smartPlaceAllAvailable =
    !readOnly &&
    isMasonryVariant(variant) &&
    visibleTextBars.some((bar) => bar.role !== "narrated_caption") &&
    smartPlacementCandidates.length > 0;

  useEffect(() => {
    if (!virtualPreviewRequested) return;
    if (virtualPreview.timeline.hasMissingSource || virtualPreview.timeline.entries.length === 0) {
      handleVirtualSourceError();
    }
  }, [
    handleVirtualSourceError,
    virtualPreview.timeline.entries.length,
    virtualPreview.timeline.hasMissingSource,
    virtualPreviewRequested,
  ]);

  useEffect(() => {
    if (virtualPreviewActive) {
      const rendered = videoRef.current;
      if (rendered && !rendered.paused) rendered.pause();
      if (currentTime > virtualPreview.timeline.totalDurationS) {
        seekVirtualPreview(virtualPreview.timeline.totalDurationS);
      }
      return;
    }
    const rendered = videoRef.current;
    if (!rendered) return;
    const clamped = Math.max(0, Math.min(duration || currentTime, currentTime));
    if (Math.abs(rendered.currentTime - clamped) > 0.15) {
      rendered.currentTime = clamped;
    }
  }, [
    currentTime,
    duration,
    seekVirtualPreview,
    virtualPreview.timeline.totalDurationS,
    virtualPreviewActive,
  ]);

  const pausePlayback = useCallback(() => {
    if (virtualPreviewActive) pauseVirtualPreview();
    else {
      const v = videoRef.current;
      if (v && !v.paused) v.pause();
    }
  }, [pauseVirtualPreview, virtualPreviewActive]);

  const seekPlaybackTo = useCallback(
    (seconds: number) => {
      const maxDuration = virtualPreviewActive ? virtualPreview.timeline.totalDurationS : duration;
      const clamped = Math.max(0, Math.min(maxDuration || seconds, seconds));
      if (virtualPreviewActive) seekVirtualPreview(clamped);
      else {
        const v = videoRef.current;
        if (v) {
          if (!v.paused) v.pause();
          v.currentTime = clamped;
        }
        setCurrentTime(clamped);
      }
    },
    [
      duration,
      seekVirtualPreview,
      virtualPreview.timeline.totalDurationS,
      virtualPreviewActive,
    ],
  );

  // Selection on a deleted/vanished bar clears itself.
  useEffect(() => {
    if (selection?.kind === "text" && !visibleTextBars.some((b) => b.id === selection.id)) {
      clear();
      setLightSheetOpen(false);
    }
  }, [selection, visibleTextBars, clear]);

  useEffect(() => {
    if (layoutMode === "light") {
      setActiveTool((tool) => (tool === "nova" ? tool : null));
      setCanvasTool("select");
    } else {
      setLightSheetOpen(false);
    }
  }, [layoutMode]);

  useEffect(() => {
    if (!panEnabled && canvasTool === "pan") {
      setCanvasTool("select");
    }
  }, [canvasTool, panEnabled]);

  useEffect(() => {
    try {
      setCopilotSaveNoticeDismissed(
        window.localStorage.getItem(COPILOT_SAVE_NOTICE_KEY) === "true",
      );
    } catch {
      setCopilotSaveNoticeDismissed(true);
    }
  }, []);

  useEffect(() => {
    if (
      (activeTool !== "sounds" && activeTool !== "nova" && localSfx.length === 0) ||
      sfxGlossaryEffects.length > 0
    ) {
      return;
    }
    let cancelled = false;
    setSfxGlossaryLoading(true);
    void getSoundEffects()
      .then((effects) => {
        if (!cancelled) setSfxGlossaryEffects(effects);
      })
      .catch(() => {
        if (!cancelled) setToast("Couldn't load sound effects.");
      })
      .finally(() => {
        if (!cancelled) setSfxGlossaryLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [activeTool, localSfx.length, sfxGlossaryEffects.length]);

  useEffect(() => {
    if (localSfx.length === 0) return;
    let cancelled = false;
    const missingUserPaths = Array.from(
      new Set(
        localSfx
          .map((placement) => placement.src_gcs_path ?? "")
          .filter((path) => path.startsWith("users/") && !localSfxAudioUrls[path]),
      ),
    );
    if (missingUserPaths.length === 0) return;
    void Promise.all(
      missingUserPaths.map(async (path) => {
        try {
          const url = await getSfxAudioUrl(itemId, path);
          return { path, url };
        } catch {
          return null;
        }
      }),
    ).then((rows) => {
      if (cancelled) return;
      setLocalSfxAudioUrls((current) => {
        const next = { ...current };
        let changed = false;
        for (const row of rows) {
          if (!row || next[row.path] === row.url) continue;
          next[row.path] = row.url;
          changed = true;
        }
        return changed ? next : current;
      });
    });
    return () => {
      cancelled = true;
    };
  }, [itemId, localSfx, localSfxAudioUrls]);

  const musicPickerShouldLoad =
    (!!variant?.music_track_id ||
      !!selectedMusicTrackId ||
      activeTool === "sounds" ||
      activeTool === "nova" ||
      selection?.kind === "music") &&
    !musicTracksLoaded;
  useEffect(() => {
    if (!musicPickerShouldLoad) return;
    void refreshMusicTracks();
  }, [musicPickerShouldLoad, refreshMusicTracks]);

  useEffect(() => {
    if (localSfx.length === 0 || sfxGlossaryEffects.length === 0) return;
    setLocalSfxAudioUrls((current) => {
      const next = { ...current };
      let changed = false;
      const effectsById = new Map(sfxGlossaryEffects.map((effect) => [effect.id, effect]));
      for (const placement of localSfx) {
        const effectId = placement.sound_effect_id ?? null;
        if (!effectId) continue;
        const url = effectsById.get(effectId)?.preview_audio_url;
        if (!url) continue;
        if (next[placement.id] !== url) {
          next[placement.id] = url;
          changed = true;
        }
        if (placement.src_gcs_path && next[placement.src_gcs_path] !== url) {
          next[placement.src_gcs_path] = url;
          changed = true;
        }
      }
      return changed ? next : current;
    });
  }, [localSfx, sfxGlossaryEffects]);

  const overlayPoolShouldLoad =
    (MEDIA_OVERLAYS_UI_ENABLED &&
      capabilities?.overlays !== false &&
      (activeTool === "nova" || activeTool === "overlays")) ||
    (VISUAL_BLOCKS_UI_ENABLED &&
      capabilities?.visual_blocks !== false &&
      activeTool === "visuals");
  useEffect(() => {
    if (!overlayPoolShouldLoad) return;
    let cancelled = false;
    listPoolAssets(itemId)
      .then((res) => {
        if (cancelled) return;
        setPoolAssets(res.assets);
        setMaxPoolAssets(res.max_assets);
        setPoolUnavailable(false);
      })
      .catch((err) => {
        if (cancelled) return;
        if (isUnavailableError(err)) setPoolUnavailable(true);
        else setPoolError(err instanceof Error ? err.message : "Couldn't load your visuals.");
      });
    return () => {
      cancelled = true;
    };
  }, [itemId, overlayPoolShouldLoad]);

  const hasBusyPoolAssets = poolAssets.some(
    (a) => a.status === "analyzing" || a.status === "uploaded" || a.status === "uploading",
  );
  useEffect(() => {
    if (!overlayPoolShouldLoad || !hasBusyPoolAssets || poolUnavailable) return;
    const id = setInterval(() => {
      listPoolAssets(itemId)
        .then((res) => {
          setPoolAssets(res.assets);
          setMaxPoolAssets(res.max_assets);
        })
        .catch(() => {});
    }, SUGGESTION_POLL_INTERVAL_MS);
    return () => clearInterval(id);
  }, [hasBusyPoolAssets, itemId, overlayPoolShouldLoad, poolUnavailable]);

  const overlaySuggestionsEnabled =
    process.env.NEXT_PUBLIC_OVERLAY_AUTOPLACE_ENABLED === "true" &&
    capabilities?.suggestions === true &&
    !readOnly;
  const overlaySuggestionsShouldLoad =
    overlaySuggestionsEnabled && (activeTool === "nova" || activeTool === "overlays");
  const overlaySuggestions = useEditorOverlaySuggestions({
    itemId,
    variantId: variant?.variant_id ?? variantParam ?? "",
    enabled: overlaySuggestionsShouldLoad,
  });

  const handlePoolFiles = useCallback(
    (fileList: FileList | File[] | null) => {
      if (!fileList) return;
      const files = Array.from(fileList).filter((f) => POOL_MIME_TYPES.includes(f.type));
      if (files.length === 0) return;
      setPoolError(null);

      const locals: PendingUpload[] = files.map((f, i) => ({
        localId: `pending-${Date.now()}-${i}-${f.name}`,
        filename: f.name,
      }));
      setPendingPoolUploads((prev) => [...prev, ...locals]);

      void (async () => {
        for (let i = 0; i < files.length; i++) {
          const file = files[i];
          const local = locals[i];
          try {
            const [signed] = await requestPoolAssetUploadUrls(itemId, [
              { filename: file.name, content_type: file.type, file_size_bytes: file.size },
            ]);
            await uploadToGcs(signed.upload_url, file);
            const contentHash = await sha256HexOfFile(file);
            const registered = await registerPoolAsset(itemId, {
              gcs_path: signed.gcs_path,
              content_type: file.type,
              content_hash: contentHash,
              source_filename: file.name,
            });
            setPendingPoolUploads((prev) => prev.filter((p) => p.localId !== local.localId));
            if (!registered.deduped) setPoolAssets((prev) => [...prev, registered]);
          } catch (err) {
            setPendingPoolUploads((prev) => prev.filter((p) => p.localId !== local.localId));
            if (isUnavailableError(err)) setPoolUnavailable(true);
            else setPoolError(err instanceof Error ? err.message : "Upload failed");
          }
        }
      })();
    },
    [itemId],
  );

  const handleRemovePoolAsset = useCallback(
    (asset: PoolAsset) => {
      void deletePoolAsset(itemId, asset.id)
        .then(() => setPoolAssets((prev) => prev.filter((a) => a.id !== asset.id)))
        .catch((err) => {
          if (isUnavailableError(err)) setPoolUnavailable(true);
          else setPoolError(err instanceof Error ? err.message : "Couldn't remove that file");
        });
    },
    [itemId],
  );

  const sampleWord = useMemo(() => {
    const first = selectedBar?.text.trim().split(/\s+/)[0];
    return first && first.length > 0 ? first.slice(0, 8).toUpperCase() : null;
  }, [selectedBar]);

  // "Applied" is DERIVED (field comparison), not bookkept — a preset ring
  // stays honest even after manual tweaks diverge from the preset.
  const appliedPresetId = useMemo(() => {
    if (!selectedBar) return null;
    return TEXT_PRESETS.find((p) => presetMatchesFields(p, selectedBar))?.id ?? null;
  }, [selectedBar]);

  // ── Actions ─────────────────────────────────────────────────────────────────

  const selectElement = useCallback(
    (
      kind: EditorSelectionKind,
      id: string,
      options: { preserveOverlayTool?: boolean } = {},
    ) => {
      select(kind, id);
      if (
        shouldCloseToolOnSelection({
          layoutMode,
          activeTool,
          preserveOverlayTool: options.preserveOverlayTool,
        })
      ) {
        setActiveTool(null);
      }
      if (kind === "text") {
        setInspectorTab("basic"); // selecting anything activates + switches to Basic (D6)
        if (layoutMode === "light") setLightSheetOpen(true);
      } else if (kind === "clip") {
        setInspectorTab("basic");
        const startS = outputTimeForSlotBoundary({
          slots,
          grid: clip.state.grid,
          key: id,
          boundary: "start",
          rendered: !virtualPreviewActive,
          renderedOutputDurationS: duration,
          fallbackOverlapS: 0,
        });
        if (startS != null) {
          seekPlaybackTo(startS);
        }
      } else if (kind === "sfx") {
        setInspectorTab("basic");
        const sfx = localSfx.find((p) => p.id === id);
        if (sfx) seekPlaybackTo(sfx.at_s ?? 0);
      } else if (kind === "overlay") {
        setInspectorTab("basic");
        const overlay = localOverlays.find((o) => o.id === id);
        if (overlay) seekPlaybackTo(overlay.start_s);
      }
    },
    [activeTool, clip.state.grid, duration, layoutMode, localOverlays, localSfx, seekPlaybackTo, select, slots, virtualPreviewActive],
  );

  const selectText = useCallback(
    (id: string) => selectElement("text", id),
    [selectElement],
  );

  const patchBar = useCallback(
    (id: string, patch: Partial<Omit<TextElementBar, "id" | "role">>) => {
      if (readOnly) return;
      const target = state.bars.find((bar) => bar.id === id);
      history.record();
      if (lyricsOptionalActive || !isLyricBar(target)) setTextDirty(true);
      dispatch({ type: "PATCH_BAR", id, patch });
    },
    [readOnly, state.bars, lyricsOptionalActive, history],
  );

  const selectedTextMotion = useMemo(
    () => collageMotionForTextBar(variant, previewDuration, selectedBar),
    [previewDuration, selectedBar, variant],
  );
  const selectedTextBoxScreenXFrac = selectedBar
    ? textBoxScreenXFrac(selectedTextMotion, currentTime, selectedBar.x_frac ?? 0.5)
    : undefined;
  const setSelectedTextBoxPosition = useCallback(
    (position: TextBoxHorizontalPosition) => {
      if (!selectedBar) return;
      patchBar(
        selectedBar.id,
        textBoxPositionPatchForBar({
          motion: selectedTextMotion,
          currentTimeS: currentTime,
          bar: selectedBar,
          position,
        }),
      );
    },
    [currentTime, patchBar, selectedBar, selectedTextMotion],
  );

  // Elements-model Lyrics toggle: ON fetches (or reuses the cached) seed bars
  // and inserts them as one undoable ADD_LYRIC_BARS action — no render
  // round-trip, same as adding any other text bar. OFF removes every
  // lyric_line bar in one undoable REMOVE_LYRIC_BARS action. Both flip
  // textDirty so Save ships them through the normal text_elements commit.
  const toggleLyricsOptional = useCallback(
    async (next: boolean) => {
      if (readOnly || !variant) return;
      if (next === hasLyricBars) return;
      if (!next) {
        history.record("lyrics-toggle-off");
        setTextDirty(true);
        dispatch({ type: "REMOVE_LYRIC_BARS" });
        return;
      }
      if (!lyricsCap.can_toggle_on && !lyricsCap.enabled) {
        setToast(lyricsToggleHint(lyricsCap.reason) ?? "Lyrics can't be enabled for this edit.");
        return;
      }
      const variantId = variant.variant_id;
      const cached = lyricSeedsCacheRef.current.get(variantId);
      if (cached) {
        cached.forEach((el) => originalsRef.current.set(el.id, el));
        history.record("lyrics-toggle-on");
        setTextDirty(true);
        dispatch({ type: "ADD_LYRIC_BARS", bars: seedBarsFromLyricSeeds(cached) });
        return;
      }
      setLyricSeedsLoading(true);
      setLyricSeedsError(null);
      try {
        const res = await getLyricSeeds(itemId, variantId);
        lyricSeedsCacheRef.current.set(variantId, res.elements);
        // The active variant changed while this request was in flight —
        // don't insert seeds for a variant no longer on screen.
        if (seededVariantIdRef.current !== variantId) return;
        res.elements.forEach((el) => originalsRef.current.set(el.id, el));
        history.record("lyrics-toggle-on");
        setTextDirty(true);
        dispatch({ type: "ADD_LYRIC_BARS", bars: seedBarsFromLyricSeeds(res.elements) });
      } catch (err) {
        if (err instanceof LyricSeedsError) {
          setLyricSeedsError(err.reason);
          setToast(err.message);
        } else {
          setToast(err instanceof Error ? err.message : "Couldn't load lyrics.");
        }
      } finally {
        setLyricSeedsLoading(false);
      }
    },
    [readOnly, variant, hasLyricBars, lyricsCap, itemId, history],
  );

  const applySmartPlacement = useCallback(() => {
    if (readOnly) return;
    if (isMasonryVariant(variant)) {
      const targetBars = visibleTextBars.filter(
        (bar) => bar.role !== "narrated_caption" && !isLyricBar(bar),
      );
      if (targetBars.length === 0) return;
      const assignments = resolveSmartPlacementAssignments(
        variant,
        targetBars,
        previewDuration,
        currentTime,
      );
      if (!assignments) {
        setToast("Not enough empty collage pockets for all overlapping text blocks.");
        return;
      }
      history.record();
      setTextDirty(true);
      targetBars.forEach((bar, index) => {
        const candidate = assignments[index];
        dispatch({
          type: "PATCH_BAR",
          id: bar.id,
          patch: smartPlacementPatchForBar(bar, candidate),
        });
      });
      return;
    }
    if (!selectedBar || !smartPlacementCandidate) return;
    patchBar(selectedBar.id, smartPlacementPatchForBar(selectedBar, smartPlacementCandidate));
  }, [
    history,
    currentTime,
    patchBar,
    previewDuration,
    readOnly,
    selectedBar,
    smartPlacementCandidate,
    visibleTextBars,
    variant,
  ]);

  const applySelectedSmartPlacement = useCallback(() => {
    if (readOnly || !selectedBar) return;
    const candidate = isMasonryVariant(variant)
      ? resolveSmartPlacementCandidate(variant, selectedBar, previewDuration, currentTime)
      : smartPlacementCandidate;
    if (!candidate) {
      if (isMasonryVariant(variant)) {
        setToast("No visible collage pocket can fit this text at this time.");
      }
      return;
    }
    patchBar(selectedBar.id, smartPlacementPatchForBar(selectedBar, candidate));
  }, [
    currentTime,
    patchBar,
    previewDuration,
    readOnly,
    selectedBar,
    smartPlacementCandidate,
    variant,
  ]);

  const pickMusicTrack = useCallback(
    (trackId: string) => {
      if (!variant) return;
      if (readOnly || (!variant.music_track_id && capabilities?.background_music !== true)) return;
      if (trackId === selectedMusicTrackId) return;
      history.record();
      setSelectedMusicTrackId(trackId);
      const selectedTrack = musicTracks.find((track) => track.id === trackId);
      const nextStartS = selectedTrack?.preview_start_s ?? 0;
      setMusicStartS(nextStartS);
      const baselineTrackId = variant.music_track_id ?? variant.background_music?.track_id ?? null;
      const baselineStartS =
        variant.music_preview_start_s ?? variant.background_music?.start_s ?? 0;
      setMusicDirty(
        trackId !== baselineTrackId || Math.abs(nextStartS - baselineStartS) > 0.005,
      );
    },
    [capabilities?.background_music, history, musicTracks, readOnly, selectedMusicTrackId, variant],
  );

  const removeMusicTrack = useCallback(() => {
    if (readOnly || !selectedMusicTrackId || !variant) return;
    if (!variant.music_track_id && capabilities?.background_music !== true) return;
    history.record();
    setSelectedMusicTrackId(null);
    setMusicDirty(!!variant.music_track_id || !!variant.background_music?.track_id);
  }, [
    capabilities?.background_music,
    history,
    readOnly,
    selectedMusicTrackId,
    variant,
  ]);

  const patchMusicStart = useCallback(
    (startS: number) => {
      if (readOnly || !songWindowState?.editable || !Number.isFinite(startS)) return;
      const maxStart = Math.max(
        0,
        songWindowState.trackDurationS - songWindowState.videoDurationS,
      );
      const nextStartS = Math.max(0, Math.min(maxStart, startS));
      setMusicStartS(nextStartS);
      setMusicDirty(
        selectedMusicTrackId !== variant?.music_track_id ||
          Math.abs(nextStartS - (variant?.music_preview_start_s ?? 0)) > 0.005,
      );
    }, [readOnly, selectedMusicTrackId, songWindowState, variant],
  );

  const musicWindowControl = songWindowState
    ? {
        value: songWindowState,
        onPreview: patchMusicStart,
        onChange: patchMusicStart,
        onBegin: () => history.record(),
      }
    : undefined;

  const previewTextTiming = useCallback(
    (id: string, patch: Pick<TextElementBar, "start_s" | "end_s">) => {
      if (readOnly) return;
      if (state.bars.find((bar) => bar.id === id)?.role === "lyric_line") return;
      setTextDirty(true);
      dispatch({
        type: "RESET",
        bars: state.bars.map((b) => (b.id === id ? { ...b, ...patch } : b)),
      });
    },
    [readOnly, state.bars],
  );

  const patchSelectedTextTiming = useCallback(
    (patch: { start_s?: number; end_s?: number }) => {
      if (!selectedBar || readOnly) return;
      const next = applyTextTimingInput({
        startS: patch.start_s ?? selectedBar.start_s,
        endS: patch.end_s ?? selectedBar.end_s,
        videoDurationS: previewDuration,
      });
      if (!rangesDiffer(selectedBar, next)) return;
      patchBar(selectedBar.id, next);
    },
    [patchBar, previewDuration, readOnly, selectedBar],
  );

  const previewClipTiming = useCallback(
    (
      key: string,
      patch: Pick<DraftSlot, "inS" | "durationS" | "durationBeats">,
    ) => {
      if (readOnly || clipEditingLocked) return;
      setLocalSlots((cur) =>
        (cur ?? slots).map((s) => (s.key === key ? { ...s, ...patch } : s)),
      );
      setTimelineDirty(true);
    },
    [clipEditingLocked, readOnly, slots],
  );

  const patchSelectedClipTiming = useCallback(
    (patch: { inS?: number; outS?: number; durationS?: number }) => {
      if (!selectedClip || readOnly || clipEditingLocked) return;
      const current = selectedClip.slot;
      const currentDuration = selectedClip.durationS;
      const next = applyClipTimingInput({
        inS: patch.inS ?? current.inS,
        outS: patch.outS,
        durationS:
          patch.durationS ??
          (patch.outS == null ? currentDuration : undefined),
        sourceDurationS: selectedClip.sourceDurationS,
      });
      if (
        current.inS === next.inS &&
        current.durationS === next.durationS &&
        current.durationBeats === next.durationBeats
      ) {
        return;
      }
      history.record();
      previewClipTiming(current.key, next);
    },
    [clipEditingLocked, history, previewClipTiming, readOnly, selectedClip],
  );

  const previewSelectedClipTiming = useCallback(
    (patch: { inS: number; durationS: number }) => {
      if (!selectedClip || readOnly || clipEditingLocked) return;
      previewClipTiming(selectedClip.slot.key, {
        inS: patch.inS,
        durationS: patch.durationS,
        durationBeats: null,
      });
      const slotIndex = slots.findIndex((s) => s.key === selectedClip.slot.key);
      const startS = slotLayout.windows[slotIndex]?.startS;
      if (startS != null) {
        const boundaryS =
          Math.abs(patch.inS - selectedClip.slot.inS) > 1e-6
            ? startS
            : startS + patch.durationS;
        seekPlaybackTo(boundaryS);
      }
    },
    [
      clipEditingLocked,
      previewClipTiming,
      readOnly,
      seekPlaybackTo,
      selectedClip,
      slotLayout.windows,
      slots,
    ],
  );

  const seekPreviewToOutput = useCallback(
    (seconds: number) => {
      seekPlaybackTo(seconds);
    },
    [seekPlaybackTo],
  );

  const previewSfxTiming = useCallback(
    (id: string, patch: { at_s: number; end_s?: number | null }) => {
      if (readOnly) return;
      setLocalSfx((cur) =>
        cur.map((s) => {
          if (s.id !== id) return s;
          const trimStart = s.trim_start_s ?? 0;
          const sourceEnd = s.duration_s ?? s.trim_end_s ?? null;
          const next: SoundEffectPlacement = { ...s, at_s: patch.at_s };
          if (patch.end_s != null && sourceEnd != null) {
            next.trim_end_s = Math.max(trimStart + 0.1, patch.end_s - patch.at_s + trimStart);
          }
          return next;
        }),
      );
      setSfxDirty(true);
    },
    [readOnly],
  );

  const addSfxFromGlossary = useCallback(
    (effect: SoundEffectSummary) => {
      if (readOnly || capabilities?.sfx === false) return;
      history.record();
      const placement: SoundEffectPlacement = {
        id: crypto.randomUUID(),
        sound_effect_id: effect.id,
        src_gcs_path: "",
        at_s: Math.min(Math.max(0, currentTime), Math.max(0, previewDuration - 0.1)),
        gain: 1,
        duration_s: effect.duration_s ?? null,
        label: effect.name,
      };
      setLocalSfx((cur) => [...cur, placement]);
      if (effect.preview_audio_url) {
        setLocalSfxAudioUrls((cur) => ({
          ...cur,
          [placement.id]: effect.preview_audio_url as string,
        }));
      }
      setSfxDirty(true);
      select("sfx", placement.id);
      setInspectorTab("basic");
    },
    [capabilities?.sfx, currentTime, history, previewDuration, readOnly, select],
  );

  const patchSfx = useCallback(
    (id: string, patch: Partial<SoundEffectPlacement>) => {
      if (readOnly || capabilities?.sfx === false) return;
      history.record();
      setLocalSfx((cur) => cur.map((s) => (s.id === id ? { ...s, ...patch } : s)));
      setSfxDirty(true);
    },
    [capabilities?.sfx, history, readOnly],
  );

  const removeSfx = useCallback(
    (id: string) => {
      if (readOnly || capabilities?.sfx === false) return;
      history.record();
      setLocalSfx((cur) => cur.filter((s) => s.id !== id));
      setSfxDirty(true);
      clear();
    },
    [capabilities?.sfx, clear, history, readOnly],
  );

  const previewOverlayTiming = useCallback(
    (id: string, patch: Pick<MediaOverlay, "start_s" | "end_s">) => {
      if (readOnly) return;
      setLocalOverlays((cur) => cur.map((o) => (o.id === id ? { ...o, ...patch } : o)));
      setOverlaysDirty(true);
    },
    [readOnly],
  );

  const previewVisualTiming = useCallback(
    (id: string, patch: Pick<VisualBlock, "start_s" | "end_s">) => {
      if (readOnly) return;
      setLocalVisualBlocks((blocks) =>
        blocks.map((block) =>
          block.id === id
            ? patchVisualBlockConcreteTiming(block, {
                ...patch,
                timing_mode: "manual",
              } as Partial<VisualBlock>)
            : block,
        ),
      );
      setVisualBlocksDirty(true);
      const current = localVisualBlocks.find((block) => block.id === id);
      if (current?.kind === "text_card") {
        state.bars
          .filter((bar) => bar.visual_block_id === id)
          .forEach((bar) =>
            dispatch({
              type: "PATCH_BAR",
              id: bar.id,
              patch: retimeLinkedTextBar(
                bar,
                current,
                patch.start_s,
                patch.end_s,
              ),
            }),
          );
        setTextDirty(true);
      }
    },
    [localVisualBlocks, readOnly, state.bars],
  );

  const previewOverlayPatch = useCallback(
    (id: string, patch: Partial<MediaOverlay>) => {
      if (readOnly) return;
      setLocalOverlays((cur) => cur.map((o) => (o.id === id ? { ...o, ...patch } : o)));
      setOverlaysDirty(true);
    },
    [readOnly],
  );

  const patchOverlay = useCallback(
    (id: string, patch: Partial<MediaOverlay>, options: { record?: boolean } = {}) => {
      if (readOnly || capabilities?.overlays === false) return;
      if (options.record !== false) history.record();
      setLocalOverlays((cur) => cur.map((o) => (o.id === id ? { ...o, ...patch } : o)));
      setOverlaysDirty(true);
    },
    [capabilities?.overlays, history, readOnly],
  );

  const removeOverlay = useCallback(
    (id: string) => {
      if (readOnly || capabilities?.overlays === false) return;
      history.record();
      setLocalOverlays((cur) => cur.filter((o) => o.id !== id));
      setOverlaysDirty(true);
      clear();
    },
    [capabilities?.overlays, clear, history, readOnly],
  );

  const patchMixLevel = useCallback(
    (level: number) => {
      if (readOnly || capabilities?.mix === false) return;
      const next = Math.max(0, Math.min(1, level));
      history.record("mix");
      setMixLevel(next);
      setSoundMuted(next === 0);
      setMixDirty(true);
    },
    [capabilities?.mix, history, readOnly],
  );

  const handleOverlayUpload = useCallback(
    async (
      files: { file: File; filename: string; content_type: string; file_size_bytes: number }[],
    ) => {
      if (readOnly || capabilities?.overlays === false || files.length === 0) return;
      setOverlayUploading(true);
      try {
        const uploadUrls = await requestOverlayUploadUrls(
          itemId,
          files.map((f) => ({
            filename: f.filename,
            content_type: f.content_type,
            file_size_bytes: f.file_size_bytes,
          })),
        );
        await Promise.all(uploadUrls.map((u, i) => uploadToGcs(u.upload_url, files[i].file)));
        const confirmed = await confirmOverlayUploads(
          itemId,
          uploadUrls.map((u, i) => ({
            gcs_path: u.gcs_path,
            content_type: files[i].content_type,
          })),
        );
        const confirmedByPath = new Map(confirmed.map((c) => [c.gcs_path, c]));
        const previewUrls: Record<string, string> = {};
        const start = Math.min(Math.max(0, currentTime), Math.max(0, previewDuration - 0.3));
        const cards: MediaOverlay[] = uploadUrls.map((u, i) => {
          const file = files[i];
          const id = crypto.randomUUID();
          previewUrls[id] = URL.createObjectURL(file.file);
          const confirmedUpload = confirmedByPath.get(u.gcs_path);
          return {
            id,
            kind: file.content_type.startsWith("video/") ? "video" : "image",
            src_gcs_path: u.gcs_path,
            preview_gcs_path: confirmedUpload?.preview_gcs_path ?? null,
            preview_url: confirmedUpload?.preview_url ?? null,
            position: "center",
            x_frac: 0.5,
            y_frac: 0.5,
            scale: 0.35,
            start_s: start,
            end_s: Math.min(previewDuration || start + 5, start + 5),
            z: localOverlays.length + i,
          };
        });
        history.record();
        setLocalOverlays((cur) => [...cur, ...cards]);
        setLocalOverlayPreviewUrls((cur) => ({ ...cur, ...previewUrls }));
        setOverlaysDirty(true);
        if (cards[0]) {
          select("overlay", cards[0].id);
          setInspectorTab("basic");
        }
      } catch (err) {
        setToast(err instanceof Error ? err.message : "Couldn't upload that overlay.");
      } finally {
        setOverlayUploading(false);
      }
    },
    [
      capabilities?.overlays,
      currentTime,
      history,
      itemId,
      localOverlays.length,
      previewDuration,
      readOnly,
      select,
    ],
  );

  // Accept an AI overlay suggestion (Overlays drawer): the envelope's card
  // (and sound, when present) joins the working state as ONE undoable command
  // — same record-then-mutate shape as handleOverlayUpload/addSfxFromGlossary.
  // Persistence rides the normal Save (editor-commit accepted_suggestion_ids).
  const handleAcceptSuggestion = useCallback(
    (suggestion: OverlaySuggestion) => {
      if (readOnly || capabilities?.overlays === false) return;
      history.record();
      setLocalOverlays((cur) => [...cur, { ...suggestion.overlay }]);
      setOverlaysDirty(true);
      // SFX child rides only when the sfx section can actually commit —
      // staging it with sound effects disabled would 404 the whole Save.
      if (suggestion.sfx && capabilities?.sfx !== false) {
        const sfx = { ...suggestion.sfx };
        setLocalSfx((cur) => [...cur, sfx]);
        setSfxDirty(true);
      }
      setAcceptedSuggestions((cur) =>
        cur.some((a) => a.id === suggestion.id)
          ? cur
          : [...cur, { id: suggestion.id, overlayId: suggestion.overlay.id }],
      );
      select("overlay", suggestion.overlay.id);
      setInspectorTab("basic");
    },
    [capabilities?.overlays, capabilities?.sfx, history, readOnly, select],
  );

  const recordTimelineDrag = useCallback(() => {
    if (readOnly) return;
    history.record();
  }, [history, readOnly]);

  const focusContent = useCallback(() => {
    // Double-click contract: focus the inspector textarea with select-all.
    // Deferred a frame so the inspector has populated for a fresh selection.
    requestAnimationFrame(() => {
      contentRef.current?.focus({ preventScroll: true });
      contentRef.current?.select();
    });
  }, []);

  const addTextAtPlayhead = useCallback(
    (preset: TextPreset = DEFAULT_TEXT_PRESET) => {
      if (readOnly) return;
      if (textElementsLocked) {
        // OV-1: the rail disables the Text/Styles buttons, but this callback
        // is also reachable via preset picks — same gate, honest toast. The
        // copy is text-specific (never the whole-shell "can't be edited").
        setToast(textElementsLockedCopy(capabilities));
        return;
      }
      history.record();
      setTextDirty(true);
      const bar = newTextBar({
        id: crypto.randomUUID(),
        text: NEW_TEXT_CONTENT,
        timing: textTimingAtPlayhead({ currentTime, previewDuration }),
        preset,
      });
      dispatch({ type: "ADD_TEXT", bar });
      selectText(bar.id);
    },
    [
      currentTime,
      previewDuration,
      selectText,
      readOnly,
      textElementsLocked,
      capabilities,
      history,
    ],
  );

  const splitAndPlaceText = useCallback(
    (text: string): boolean => {
      if (readOnly) return false;
      if (textElementsLocked) {
        setToast(textElementsLockedCopy(capabilities));
        return false;
      }
      const draft = text.trim();
      if (!draft) return false;
      const existingElementCount = barsToTextElements(state.bars, originalsRef.current, {
        includeLyrics: lyricsOptionalActive,
      }).length;
      const remainingElementCount = Math.max(0, TEXT_ELEMENTS_API_MAX - existingElementCount);
      const sequence = buildTimedTextSequence(
        draft,
        currentTime,
        previewDuration,
        0.5,
        remainingElementCount,
      );
      if (sequence === null) {
        setToast(
          `This edit has room for ${remainingElementCount} more text beat${remainingElementCount === 1 ? "" : "s"}.`,
        );
        return false;
      }
      if (sequence.length === 0) return false;
      const bars = sequence.map((item, index) => ({
        ...newTextBar({
          id: crypto.randomUUID(),
          text: item.text,
          timing: item,
          preset: COMPOSITION_TEXT_PRESET,
        }),
        role: "generative_sequence" as const,
        effect: "static",
        y_frac: COMPOSITION_Y_FRACS[index % COMPOSITION_Y_FRACS.length],
      }));
      history.record();
      setTextDirty(true);
      bars.forEach((bar) => dispatch({ type: "ADD_TEXT", bar }));
      selectText(bars[0].id);
      setInspectorTab("basic");
      return true;
    },
    [
      capabilities,
      currentTime,
      history,
      previewDuration,
      readOnly,
      selectText,
      state.bars,
      textElementsLocked,
      lyricsOptionalActive,
    ],
  );

  // Restyle ALL text bars with a style set — ONE undoable command with instant
  // canvas update (plan §2 Styles v1, task wiring). record() once, then patch
  // every bar (each PATCH_BAR is a reducer dispatch; the single record collapses
  // them into one undo step).
  const restyleAll = useCallback(
    (styleSet: GenerativeStyleSet) => {
      if (readOnly) return;
      if (visibleTextBars.length === 0) {
        setToast("Add text first, then apply a style.");
        return;
      }
      const patch: Partial<Omit<TextElementBar, "id" | "role">> = {
        font_family: styleSet.font_family ?? styleSet.intro?.font_family ?? undefined,
        color: styleSet.text_color ?? styleSet.intro?.text_color ?? undefined,
        highlight_color:
          styleSet.highlight_color ?? styleSet.intro?.highlight_color ?? undefined,
        stroke_width: styleSet.intro?.stroke_width ?? undefined,
        effect: styleSet.effect ?? styleSet.intro?.effect ?? undefined,
      };
      history.record();
      if (lyricsOptionalActive || visibleTextBars.some((bar) => !isLyricBar(bar))) {
        setTextDirty(true);
      }
      visibleTextBars.forEach((b) => dispatch({ type: "PATCH_BAR", id: b.id, patch }));
      setAppliedStyleSetId(styleSet.id);
    },
    [readOnly, visibleTextBars, lyricsOptionalActive, history],
  );

  // Legacy lyrics-variant restyle for flag-off clients: route through the
  // existing style endpoint instead of local lyric-bar edits.
  const restyleLyrics = useCallback(
    async (styleSet: GenerativeStyleSet) => {
      if (readOnly || !variant || saveState === "saving") return;
      setSaveState("saving");
      setSaveMessage(null);
      try {
        await changePlanItemStyle(itemId, variant.variant_id, styleSet.id);
        setAppliedStyleSetId(styleSet.id);
        setSaveState("idle");
        router.push(`/plan/items/${itemId}`);
      } catch (err) {
        setSaveState("error");
        setSaveMessage(err instanceof Error ? err.message : "Couldn't apply that style.");
      }
    },
    [readOnly, variant, saveState, itemId, router],
  );

  // Single entry point both StylesDrawer instances bind to — branches per
  // variant type so callers don't need to know about the lyrics special case.
  // Elements-model variants always take the normal bar-patch route, even if
  // text_mode happens to read "lyrics" — their lyric_line bars are ordinary
  // text_elements, never the legacy whole-style-set route.
  const onRestyleAll =
    isLyrics && !lyricBarsAvailable && !lyricsOptionalActive ? restyleLyrics : restyleAll;

  const pickPreset = useCallback(
    (preset: TextPreset) => {
      if (selectedBar) {
        // Apply to the selected element.
        patchBar(selectedBar.id, {
          font_family: preset.fields.font_family ?? undefined,
          color: preset.fields.color ?? undefined,
          highlight_color: preset.fields.highlight_color ?? undefined,
          stroke_width: preset.fields.stroke_width ?? 0,
          effect: preset.fields.effect ?? undefined,
        });
      } else {
        // No selection → create a text element at the playhead with this
        // preset and select it (D6).
        addTextAtPlayhead(preset);
      }
    },
    [selectedBar, patchBar, addTextAtPlayhead],
  );

  const nextVisualBlockWindow = useCallback(
    (requestedDuration: number) => {
      const maxDuration = Math.max(0.75, previewDuration || duration || 60);
      let start = Math.max(0, Math.min(currentTime, Math.max(0, maxDuration - 0.75)));
      const ordered = [...localVisualBlocks].sort((a, b) => a.start_s - b.start_s);
      for (const block of ordered) {
        if (start + requestedDuration <= block.start_s) break;
        if (start < block.end_s && start + requestedDuration > block.start_s) {
          start = block.end_s;
        }
      }
      const end = Math.min(maxDuration, start + requestedDuration);
      return { start, end };
    },
    [currentTime, duration, localVisualBlocks, previewDuration],
  );

  const addTextCard = useCallback(
    (preset: "card" | "quote" | "statistic" | "transition") => {
      if (readOnly || capabilities?.visual_blocks === false) return;
      const { start, end } = nextVisualBlockWindow(2.5);
      if (end - start < 0.75) {
        setToast("There isn't enough open timeline space for a text card.");
        return;
      }
      const id = crypto.randomUUID();
      const labels = {
        card: "Add a key idea",
        quote: "“Add a quote”",
        statistic: "Add a statistic",
        transition: "New section",
      } as const;
      const block: VisualBlock = {
        version: 1,
        id,
        kind: "text_card",
        start_s: start,
        end_s: end,
        timing_mode: "manual",
        origin: "user",
        transition_in: "cut",
        transition_out: "cut",
        audio_policy: { base: "continue", sfx: "continue" },
        style_preset_id: `nova-${preset}`,
        background: { type: "solid", color: preset === "statistic" ? "#172035" : "#26382F" },
      };
      const bar = {
        ...newTextBar({
          id: crypto.randomUUID(),
          text: labels[preset],
          timing: { start_s: start, end_s: end },
          preset: DEFAULT_TEXT_PRESET,
        }),
        visual_block_id: id,
        font_family: "PlayfairDisplay-Bold",
        color: "#FFFFFF",
        size_px: 72,
        y_frac: 0.5,
        max_width_frac: 0.82,
        effect: "fade-in",
      } satisfies TextElementBar;
      history.record();
      setLocalVisualBlocks((current) => [...current, block]);
      setVisualBlocksDirty(true);
      setTextDirty(true);
      dispatch({ type: "ADD_TEXT", bar });
      selectText(bar.id);
      setActiveTool("visuals");
      seekPlaybackTo(start);
    },
    [
      capabilities?.visual_blocks,
      history,
      nextVisualBlockWindow,
      readOnly,
      seekPlaybackTo,
      selectText,
    ],
  );

  const addMontageBlock = useCallback(
    (assetIds: string[]) => {
      if (readOnly || capabilities?.visual_blocks === false) return;
      const selectedAssets = assetIds
        .map((id) => poolAssets.find((asset) => asset.id === id))
        .filter((asset): asset is PoolAsset => !!asset && asset.status === "ready")
        .slice(0, 12);
      if (selectedAssets.length < 3) {
        setToast("Choose at least three ready visuals for a montage.");
        return;
      }
      const { start, end } = nextVisualBlockWindow(3.0);
      if (end - start < 1.2) {
        setToast("There isn't enough open timeline space for a montage.");
        return;
      }
      const perShot = (end - start) / selectedAssets.length;
      let offset = 0;
      const motions = ["zoom_in", "pan_right", "zoom_out", "pan_left"] as const;
      const block: VisualBlock = {
        version: 1,
        id: crypto.randomUUID(),
        kind: "montage",
        start_s: start,
        end_s: end,
        timing_mode: "auto",
        origin: "user",
        transition_in: "cut",
        transition_out: "cut",
        audio_policy: { base: "continue", sfx: "continue" },
        shots: selectedAssets.map((asset, index) => {
          const shotDuration =
            index === selectedAssets.length - 1 ? end - start - offset : perShot;
          const shot = {
            id: crypto.randomUUID(),
            asset_id: asset.id,
            src_gcs_path: asset.gcs_path,
            kind: asset.kind,
            start_offset_s: Number(offset.toFixed(6)),
            duration_s: Number(shotDuration.toFixed(6)),
            crop: { x_frac: 0.5, y_frac: 0.5, scale: 1 },
            motion: motions[index % motions.length],
          };
          offset += shotDuration;
          return shot;
        }),
      };
      history.record();
      setLocalVisualBlocks((current) => [...current, block]);
      setVisualBlocksDirty(true);
      seekPlaybackTo(start);
    },
    [
      capabilities?.visual_blocks,
      history,
      nextVisualBlockWindow,
      poolAssets,
      readOnly,
      seekPlaybackTo,
    ],
  );

  const addVisualBlockText = useCallback(
    (blockId: string) => {
      if (readOnly || textElementsLocked) return;
      const block = localVisualBlocks.find(
        (candidate) => candidate.id === blockId && candidate.kind === "text_card",
      );
      if (!block) return;
      const existingCount = state.bars.filter(
        (bar) => bar.visual_block_id === blockId,
      ).length;
      const bar = {
        ...newTextBar({
          id: crypto.randomUUID(),
          text: existingCount === 0 ? "Add a key idea" : "Add supporting text",
          timing: { start_s: block.start_s, end_s: block.end_s },
          preset: DEFAULT_TEXT_PRESET,
        }),
        visual_block_id: blockId,
        color: "#FFFFFF",
        y_frac: Math.min(0.75, 0.45 + existingCount * 0.12),
        max_width_frac: 0.82,
        effect: "fade-in",
      } satisfies TextElementBar;
      history.record();
      dispatch({ type: "ADD_TEXT", bar });
      setTextDirty(true);
      selectText(bar.id);
      seekPlaybackTo(block.start_s);
    },
    [
      history,
      localVisualBlocks,
      readOnly,
      seekPlaybackTo,
      selectText,
      state.bars,
      textElementsLocked,
    ],
  );

  const patchVisualBlock = useCallback(
    (id: string, patch: Partial<VisualBlock>) => {
      if (readOnly) return;
      const current = localVisualBlocks.find((block) => block.id === id);
      if (!current) return;
      history.record();
      const next = patchVisualBlockConcreteTiming(current, patch);
      setLocalVisualBlocks((blocks) => blocks.map((block) => (block.id === id ? next : block)));
      setVisualBlocksDirty(true);
      if (current.kind === "text_card") {
        const nextStart = typeof patch.start_s === "number" ? patch.start_s : current.start_s;
        const nextEnd = typeof patch.end_s === "number" ? patch.end_s : current.end_s;
        state.bars
          .filter((bar) => bar.visual_block_id === id)
          .forEach((bar) =>
            dispatch({
              type: "PATCH_BAR",
              id: bar.id,
              patch: retimeLinkedTextBar(bar, current, nextStart, nextEnd),
            }),
          );
        setTextDirty(true);
      }
    },
    [history, localVisualBlocks, readOnly, state.bars],
  );

  const deleteVisualBlock = useCallback(
    (id: string) => {
      if (readOnly) return;
      history.record();
      setLocalVisualBlocks((blocks) => blocks.filter((block) => block.id !== id));
      setVisualBlocksDirty(true);
      state.bars
        .filter((bar) => bar.visual_block_id === id)
        .forEach((bar) => dispatch({ type: "DELETE_BAR", id: bar.id }));
      setTextDirty(true);
    },
    [history, readOnly, state.bars],
  );

  const duplicateVisualBlock = useCallback(
    (id: string) => {
      if (readOnly || capabilities?.visual_blocks === false) return;
      const source = localVisualBlocks.find((block) => block.id === id);
      if (!source) return;
      const durationS = source.end_s - source.start_s;
      const { start, end } = nextVisualBlockWindow(durationS);
      if (end - start < durationS - 1 / 30) {
        setToast("There isn't enough open timeline space to duplicate this block.");
        return;
      }
      const newId = crypto.randomUUID();
      const copied: VisualBlock = source.kind === "montage"
        ? {
            ...source,
            id: newId,
            start_s: start,
            end_s: end,
            timing_mode: "manual",
            origin: "user",
            rationale: null,
            shots: source.shots.map((shot) => ({ ...shot, id: crypto.randomUUID() })),
          }
        : {
            ...source,
            id: newId,
            start_s: start,
            end_s: end,
            timing_mode: "manual",
            origin: "user",
            rationale: null,
            background:
              source.background.type === "asset"
                ? {
                    ...source.background,
                    shot: { ...source.background.shot, id: crypto.randomUUID() },
                  }
                : { ...source.background },
          };
      history.record();
      setLocalVisualBlocks((blocks) => [...blocks, copied]);
      setVisualBlocksDirty(true);
      if (source.kind === "text_card") {
        const sourceDuration = Math.max(0.001, source.end_s - source.start_s);
        state.bars
          .filter((bar) => bar.visual_block_id === source.id)
          .forEach((bar) => {
            const relativeStart = (bar.start_s - source.start_s) / sourceDuration;
            const relativeEnd = (bar.end_s - source.start_s) / sourceDuration;
            dispatch({
              type: "ADD_TEXT",
              bar: {
                ...bar,
                id: crypto.randomUUID(),
                visual_block_id: newId,
                start_s: start + relativeStart * (end - start),
                end_s: start + relativeEnd * (end - start),
              },
            });
          });
        setTextDirty(true);
      }
      seekPlaybackTo(start);
    },
    [
      capabilities?.visual_blocks,
      history,
      localVisualBlocks,
      nextVisualBlockWindow,
      readOnly,
      seekPlaybackTo,
      state.bars,
    ],
  );

  const retimeBlock = useCallback(
    (id: string) => {
      const block = localVisualBlocks.find((candidate) => candidate.id === id);
      if (!block || block.kind !== "montage" || !variant) return;
      void retimeVisualBlock(itemId, variant.variant_id, block)
        .then(({ visual_block }) => {
          history.record();
          setLocalVisualBlocks((blocks) =>
            blocks.map((candidate) => (candidate.id === id ? visual_block : candidate)),
          );
          setVisualBlocksDirty(true);
        })
        .catch((error) =>
          setToast(error instanceof Error ? error.message : "Couldn't retime that montage."),
        );
    },
    [history, itemId, localVisualBlocks, variant],
  );

  // Clip-split capability gate (plan §7): missing capabilities → allowed for
  // montage agent_text variants (song_text / original_text), disabled otherwise.
  const splitClipsAllowed =
    capabilities?.split_clips !== undefined
      ? capabilities.split_clips !== false
      : variant?.text_mode === "agent_text";
  const toolDisabledReasons = useMemo<Partial<Record<EditorTool, string>>>(
    () => computeToolDisabledReasons({ capabilities, readOnly, readOnlyReason, isLyrics }),
    [capabilities, readOnly, readOnlyReason, isLyrics],
  );

  const buildCopilotDraftSnapshot = useCallback(() => {
    const openTools = (["text", "visuals", "sounds", "overlays", "styles"] as const).filter((tool) => {
      if (toolDisabledReasons[tool]) return false;
      if (tool === "sounds") return SOUND_EFFECTS_UI_ENABLED;
      if (tool === "overlays") return MEDIA_OVERLAYS_UI_ENABLED;
      if (tool === "visuals") return VISUAL_BLOCKS_UI_ENABLED;
      return true;
    });
    // Narrated: cues live as editor bars — full caption editing. Subtitled:
    // transcript cues are owned by the Captions tab, but the copilot still gets
    // a META-ONLY captions section (style/font/enabled/position via
    // set_caption_meta) so "make the captions word by word" works in chat.
    const captionCuesEditable = variant?.resolved_archetype !== "subtitled";
    const captionsPresent =
      captionMeta != null &&
      (variant?.resolved_archetype === "narrated"
        ? visibleTextBars.some((bar) => bar.role === "narrated_caption")
        : variant?.resolved_archetype === "subtitled" &&
          (variant?.caption_cues?.length ?? 0) > 0 &&
          // Matches the server's _is_editable_caption_variant predicate — a
          // subtitled variant without the caption-free base can't reburn, so
          // don't offer a captions section whose Save would 422.
          !!variant?.base_video_path);
    const musicSwappable = !!variant?.music_track_id && !readOnly;
    const mixAllowed = capabilities?.mix !== false && mixLevel !== undefined;
    const introText = variant?.intro_text?.trim() ?? "";
    const introWordCount = introText ? introText.split(/\s+/).filter(Boolean).length : 0;
    const sequenceCapable = variant?.sequence_synced === true || variant?.intro_mode === "sequence";
    const intro =
      variant?.text_mode === "agent_text" && (introText || sequenceCapable)
        ? {
            layout:
              sequenceCapable || variant.intro_layout === "cluster"
                ? "cluster" as const
                : "linear" as const,
            mode: variant.intro_mode ?? null,
            text: introText || null,
            word_count: introWordCount,
            sequence_capable: sequenceCapable,
            cluster_eligible: sequenceCapable || (introWordCount >= 3 && introWordCount <= 6),
            switch_blocked_reason: readOnly
              ? "read_only" as const
              : variant.render_status === "rendering"
                ? "rendering" as const
                : variant.text_elements_user_edited
                  ? "manual_text_edits" as const
                  : dirty
                    ? "unsaved_edits" as const
                    : null,
          }
        : undefined;
    const renderLayoutSwitchable = intro != null && intro.switch_blocked_reason === null;
    const allowedFamilies = allowedOpFamiliesFromCapabilities(capabilities, {
      sfxEnabled: SOUND_EFFECTS_UI_ENABLED,
      overlaysEnabled: MEDIA_OVERLAYS_UI_ENABLED,
      captionsPresent,
      musicSwappable,
      mixAllowed,
      renderLayoutSwitchable,
      titleEditable: !readOnly,
      openTools,
      readOnly,
    });
    return buildCopilotSnapshot(visibleTextBars, slots, clip.clips, capabilities, clip.state.grid, {
      sfxEnabled: SOUND_EFFECTS_UI_ENABLED,
      overlaysEnabled: MEDIA_OVERLAYS_UI_ENABLED,
      captionsPresent,
      musicSwappable,
      mixAllowed,
      titleEditable: !readOnly,
      openTools,
      // Slot-less variants (subtitled) have a 0 layout total — the real video
      // duration keeps every timing clamp from collapsing at_s values to 0.
      videoDurationS: previewDuration,
      sfxPlacements: localSfx,
      sfxCatalog: sfxGlossaryEffects,
      // Speech marks describe the PERSISTED render's timeline — hide them while
      // local clip edits have shifted it (same staleness discipline the prompt
      // applies to beat marks). Saving refreshes the map.
      speechMap: clipDirty ? null : variant?.speech_map ?? null,
      sfxSuggestions: clipDirty ? null : variant?.pending_sfx_suggestions ?? null,
      overlayCards: localOverlays,
      poolAssets,
      pendingSuggestions: overlaySuggestions.rows,
      captionMeta: captionsPresent ? captionMeta : undefined,
      captionCuesEditable,
      captionTotalCues: captionCuesEditable ? undefined : variant?.caption_cues?.length ?? 0,
      musicState: {
        swappable: musicSwappable,
        currentTrackId: effectiveMusicTrackId,
        currentTrackTitle: effectiveMusicTitle,
        candidates: musicTracks,
      },
      mixLevel,
      intro,
      renderLayoutSwitchable,
      title,
      readOnly: readOnly || allowedFamilies.length === 0,
    });
  }, [
    capabilities,
    captionMeta,
    clip.clips,
    clip.state.grid,
    clipDirty,
    effectiveMusicTitle,
    effectiveMusicTrackId,
    dirty,
    localOverlays,
    localSfx,
    mixLevel,
    musicTracks,
    overlaySuggestions.rows,
    poolAssets,
    previewDuration,
    readOnly,
    sfxGlossaryEffects,
    slots,
    visibleTextBars,
    title,
    toolDisabledReasons,
    variant?.base_video_path,
    variant?.caption_cues?.length,
    variant?.music_track_id,
    variant?.intro_layout,
    variant?.intro_mode,
    variant?.intro_text,
    variant?.pending_sfx_suggestions,
    variant?.render_status,
    variant?.resolved_archetype,
    variant?.sequence_synced,
    variant?.speech_map,
    variant?.text_elements_user_edited,
    variant?.text_mode,
  ]);

  const applyCopilotDraftOps = useCallback(
    (ops: CopilotOp[], snapshot: CopilotSnapshot) =>
      applyCopilotOps(ops, {
        bars: state.bars,
        slots,
        snapshot,
        capabilities,
        grid: clip.state.grid,
        videoDurationS: previewDuration,
        sfx: localSfx,
        sfxCatalog: sfxGlossaryEffects,
        overlays: localOverlays,
        poolAssets,
        pendingSuggestions: overlaySuggestions.rows,
        musicTrackId: effectiveMusicTrackId,
        mixLevel,
        title,
        captionMeta,
        makeTextBarId: () => crypto.randomUUID(),
        makeSlotKey: (slot) => `${slot.key}-split-${crypto.randomUUID()}`,
        makeSfxPlacementId: () => crypto.randomUUID(),
        makeOverlayId: () => crypto.randomUUID(),
      }),
    [
      capabilities,
      captionMeta,
      clip.state.grid,
      effectiveMusicTrackId,
      localOverlays,
      localSfx,
      mixLevel,
      overlaySuggestions.rows,
      poolAssets,
      previewDuration,
      sfxGlossaryEffects,
      slots,
      state.bars,
      title,
    ],
  );

  const flashTimerRef = useRef<number | null>(null);
  const copilotRenderNavTimerRef = useRef<number | null>(null);
  const flashCopilotTargets = useCallback(
    (targets: {
      textIds?: string[];
      overlayIds?: string[];
      timelineIds?: string[];
    }) => {
      // One flash timer at a time: a prior turn's timer must not truncate a
      // newer flash mid-animation, and the timer is cleared on unmount (F7).
      if (flashTimerRef.current !== null) window.clearTimeout(flashTimerRef.current);
      setFlashTextIds(new Set(targets.textIds ?? []));
      setFlashOverlayIds(new Set(targets.overlayIds ?? []));
      setFlashTimelineIds(new Set(targets.timelineIds ?? []));
      flashTimerRef.current = window.setTimeout(() => {
        flashTimerRef.current = null;
        setFlashTextIds(new Set());
        setFlashOverlayIds(new Set());
        setFlashTimelineIds(new Set());
      }, 1600);
    },
    [],
  );
  useEffect(
    () => () => {
      if (flashTimerRef.current !== null) window.clearTimeout(flashTimerRef.current);
      if (copilotRenderNavTimerRef.current !== null) {
        window.clearTimeout(copilotRenderNavTimerRef.current);
      }
    },
    [],
  );

  const handleCopilotOps = useCallback(
    (result: ApplyCopilotOpsResult): { undoVersion?: number } => {
      if (result.renderRequest) {
        if (!readOnly && variant) {
          void editPlanItemVariant(itemId, variant.variant_id, {
            intro_layout: result.renderRequest.layout,
          })
            .then(() => {
              if (copilotRenderNavTimerRef.current !== null) {
                window.clearTimeout(copilotRenderNavTimerRef.current);
              }
              copilotRenderNavTimerRef.current = window.setTimeout(() => {
                copilotRenderNavTimerRef.current = null;
                router.push(`/plan/items/${itemId}`);
              }, 1400);
            })
            .catch((err) => {
              setToast(err instanceof Error ? err.message : "Couldn't update the intro layout.");
            });
        }
        return {};
      }
      const hasAppliedChanges =
        result.textActions.length > 0 ||
        result.nextSlots !== null ||
        result.nextSfx != null ||
        result.nextOverlays != null ||
        (result.acceptedSuggestionRefs?.length ?? 0) > 0 ||
        result.nextMusicTrackId !== undefined ||
        result.nextMixLevel !== undefined ||
        result.nextTitle !== undefined ||
        result.captionMetaPatch !== undefined;
      if (!hasAppliedChanges) {
        if (result.openTool) setActiveTool(result.openTool);
        return {};
      }
      if (readOnly) return {};

      const version = history.record();
      const beforeSfxIds = new Set(localSfx.map((sfx) => sfx.id));
      const beforeOverlayById = new Map(localOverlays.map((overlay) => [overlay.id, overlay]));
      result.textActions.forEach((action) => dispatch(action));
      if (
        lyricsOptionalActive ||
        result.textActions.some((action) => {
          if (action.type === "ADD_TEXT") return true;
          if (!("id" in action)) return false;
          return !isLyricBar(state.bars.find((bar) => bar.id === action.id));
        })
      ) {
        setTextDirty(true);
      }
      if (result.nextSlots) {
        setLocalSlots(result.nextSlots);
        setTimelineDirty(true);
      }
      if (result.nextSfx) {
        setLocalSfx(result.nextSfx);
        setSfxDirty(true);
      }
      if (result.nextOverlays) {
        setLocalOverlays(result.nextOverlays);
        setOverlaysDirty(true);
      }
      if (result.acceptedSuggestionRefs?.length) {
        setAcceptedSuggestions((cur) => {
          const seen = new Set(cur.map((ref) => ref.id));
          return [
            ...cur,
            ...result.acceptedSuggestionRefs!.filter((ref) => !seen.has(ref.id)),
          ];
        });
        for (const ref of result.acceptedSuggestionRefs) {
          overlaySuggestions.removeRow(ref.id, { accepted: true });
        }
      }
      if (result.nextMusicTrackId !== undefined) {
        setSelectedMusicTrackId(result.nextMusicTrackId);
        setMusicDirty(result.nextMusicTrackId !== variant?.music_track_id);
      }
      if (result.nextMixLevel !== undefined) {
        setMixLevel(result.nextMixLevel);
        setSoundMuted(result.nextMixLevel === 0);
        setMixDirty(true);
      }
      if (result.nextTitle !== undefined) {
        setTitle(result.nextTitle);
        setTitleDirty(true);
      }
      if (result.captionMetaPatch !== undefined) {
        setCaptionMeta((current) => {
          const base = current ?? (variant ? captionMetaFromVariant(variant) : null);
          return base ? { ...base, ...result.captionMetaPatch } : base;
        });
        setCaptionMetaPatch((current) => ({ ...current, ...result.captionMetaPatch }));
        setCaptionMetaDirty(true);
      }
      if (result.openTool) setActiveTool(result.openTool);
      setSessionHasCopilotEdits(true);

      const feedback = resolveCopilotApplyFeedback({
        result,
        bars: state.bars,
        beforeSlots: slots,
        grid: clip.state.grid,
      });
      const changedOverlayIds = result.nextOverlays
        ? result.nextOverlays
            .filter((overlay) => JSON.stringify(beforeOverlayById.get(overlay.id)) !== JSON.stringify(overlay))
            .map((overlay) => overlay.id)
        : [];
      const addedSfx = result.nextSfx?.find((sfx) => !beforeSfxIds.has(sfx.id)) ?? null;
      flashCopilotTargets({
        textIds: feedback.textIds,
        overlayIds: changedOverlayIds,
        timelineIds: [
          ...feedback.textIds,
          ...feedback.slotIds,
          ...(result.nextSfx ? result.nextSfx.map((sfx) => sfx.id) : []),
          ...changedOverlayIds,
        ],
      });

      if (addedSfx) {
        pausePlayback();
        seekPlaybackTo(addedSfx.at_s ?? 0);
        selectElement("sfx", addedSfx.id, { preserveOverlayTool: true });
      } else if (feedback.first) {
        pausePlayback();
        seekPlaybackTo(feedback.first.seekS);
        selectElement(feedback.first.kind, feedback.first.id, { preserveOverlayTool: true });
      }

      return { undoVersion: version };
    },
    [
      clip.state.grid,
      flashCopilotTargets,
      history,
      localOverlays,
      localSfx,
      overlaySuggestions,
      pausePlayback,
      readOnly,
      router,
      seekPlaybackTo,
      selectElement,
      slots,
      state.bars,
      itemId,
      variant,
      lyricsOptionalActive,
    ],
  );

  const copilot = useEditCopilot({
    itemId,
    variantId: variant?.variant_id ?? variantParam ?? "",
    buildSnapshot: buildCopilotDraftSnapshot,
    applyOps: applyCopilotDraftOps,
    onApplied: handleCopilotOps,
  });

  const deleteSelected = useCallback(() => {
    if (!selection || readOnly) return;
    if (selection.kind === "text") {
      const selected = state.bars.find((bar) => bar.id === selection.id);
      if (
        selected?.visual_block_id &&
        state.bars.filter((bar) => bar.visual_block_id === selected.visual_block_id).length <= 1
      ) {
        deleteVisualBlock(selected.visual_block_id);
        clear();
        return;
      }
      history.record();
      setTextDirty(true);
      dispatch({ type: "DELETE_BAR", id: selection.id });
      clear();
    } else if (selection.kind === "clip" && !clipEditingLocked) {
      const res = deleteSlotEnforceFloor(slots, selection.id);
      if (res.didDelete) {
        history.record();
        setLocalSlots(res.slots);
        setTimelineDirty(true);
        clear();
      } else {
        setToast("Keep at least one clip.");
      }
    } else if (selection.kind === "sfx") {
      removeSfx(selection.id);
    } else if (selection.kind === "overlay") {
      removeOverlay(selection.id);
    } else if (selection.kind === "visual") {
      deleteVisualBlock(selection.id);
      clear();
    }
  }, [
    clipEditingLocked,
    selection,
    clear,
    slots,
    readOnly,
    history,
    removeSfx,
    removeOverlay,
    deleteVisualBlock,
    state.bars,
  ]);

  const splitAtPlayhead = useCallback(() => {
    if (!selection || readOnly) return;
    if (selection.kind === "text") {
      // Guard before recording so an out-of-bounds split (reducer no-op) never
      // pushes a spurious undo step.
      const bar = selectedBar;
      if (!bar) return;
      if (isLyricBar(bar)) {
        setToast("Lyric timing is locked to the vocal.");
        return;
      }
      const at = Math.round(currentTime * 10) / 10;
      const MIN = 0.2;
      if (at <= bar.start_s + MIN - 1e-9 || at >= bar.end_s - MIN + 1e-9) {
        setToast("Move the playhead over the text to split it.");
        return;
      }
      history.record();
      setTextDirty(true);
      dispatch({
        type: "SPLIT_BAR",
        id: selection.id,
        at_s: currentTime,
        newId: crypto.randomUUID(),
      });
    } else if (selection.kind === "clip") {
      if (!splitClipsAllowed) return;
      const res = splitSlotAt(
        slots,
        clip.state.grid,
        selection.id,
        currentTime,
        `split-${crypto.randomUUID()}`,
      );
      if (res.didSplit) {
        history.record();
        setLocalSlots(res.slots);
        setTimelineDirty(true);
      } else {
        setToast("Move the playhead over the clip to split it.");
      }
    }
  }, [
    selection,
    currentTime,
    slots,
    clip.state.grid,
    splitClipsAllowed,
    readOnly,
    selectedBar,
    history,
  ]);

  const togglePlay = useCallback(() => {
    if (virtualPreviewActive) {
      toggleVirtualPreview();
      return;
    }
    const v = videoRef.current;
    if (!v) return;
    if (v.paused) void v.play();
    else v.pause();
  }, [toggleVirtualPreview, virtualPreviewActive]);

  const seekTo = useCallback((sec: number) => {
    seekPlaybackTo(sec);
  }, [seekPlaybackTo]);

  const nudgeSelectedText = useCallback(
    (deltaS: number) => {
      if (readOnly || selection?.kind !== "text") return;
      const bar = selectedBar;
      if (!bar) return;
      if (isLyricBar(bar)) return;
      const start_s = nudgeBarStart(bar, deltaS, previewDuration);
      if (start_s === bar.start_s) return;
      history.record();
      setTextDirty(true);
      dispatch({ type: "MOVE_BAR", id: bar.id, start_s });
    },
    [history, previewDuration, readOnly, selection, selectedBar],
  );

  // Transport enablement (plan §6).
  const canSplit =
    (selection?.kind === "text" && !!selectedBar && !isLyricBar(selectedBar)) ||
    (selection?.kind === "clip" && splitClipsAllowed && !clipEditingLocked);
  const splitReason =
    selection?.kind === "music"
      ? "Music fits the cut automatically"
      : selection?.kind === "text" && selectedBar && isLyricBar(selectedBar)
        ? "Lyric timing is locked to the vocal."
      : selection?.kind === "clip" && clipEditingLocked
        ? "locked to your voiceover"
      : selection?.kind === "clip" && !splitClipsAllowed
        ? "This variant's clips can't be split"
        : undefined;
  const canDelete =
    (selection?.kind === "text" && !!selectedBar && !isLyricBar(selectedBar)) ||
    (selection?.kind === "clip" && !clipEditingLocked && activeSlotCount(slots) > 1) ||
    selection?.kind === "sfx" ||
    selection?.kind === "overlay" ||
    selection?.kind === "visual";

  // ── Keyboard: Escape ladder + Delete with focus guard (plan §5/§9) ──────────
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      // ⌘Z / ⇧⌘Z (⌃Z / ⌃⇧Z, ⌘Y) — document undo/redo. Guarded: when focus is
      // in a text field, let the browser's native text undo win.
      if ((e.metaKey || e.ctrlKey) && (e.key === "z" || e.key === "Z")) {
        if (!deleteKeyAllowed(e.target as HTMLElement | null)) return;
        e.preventDefault();
        if (e.shiftKey) history.redo();
        else history.undo();
        return;
      }
      if ((e.metaKey || e.ctrlKey) && (e.key === "y" || e.key === "Y")) {
        if (!deleteKeyAllowed(e.target as HTMLElement | null)) return;
        e.preventDefault();
        history.redo();
        return;
      }
      if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
        if (!deleteKeyAllowed(e.target as HTMLElement | null)) return;
        if (selection?.kind !== "text") return;
        e.preventDefault();
        const step = e.shiftKey ? 1 : 0.1;
        nudgeSelectedText(e.key === "ArrowLeft" ? -step : step);
        return;
      }
      if (e.key === " " || e.key === "Spacebar") {
        if (!spaceShortcutAllowed(e.target as HTMLElement | null)) return;
        e.preventDefault();
        togglePlay();
        return;
      }
      if (e.key === "Escape") {
        if (layoutMode === "light" && lightSheetOpen) {
          e.preventDefault();
          setLightSheetOpen(false);
          return;
        }
        const target = e.target as HTMLElement | null;
        // One press, one effect: leaving a text field is that effect.
        if (target && !deleteKeyAllowed(target)) {
          target.blur();
          return;
        }
        const action = escapeAction({
          drawerOpen: activeTool !== null,
          hasSelection: selection !== null,
        });
        if (action === "close-drawer") setActiveTool(null);
        else if (action === "clear-selection") clear();
      } else if (e.key === "Delete" || e.key === "Backspace") {
        if (!deleteKeyAllowed(e.target as HTMLElement | null)) return;
        if (canDelete) {
          e.preventDefault();
          deleteSelected();
        }
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [
    activeTool,
    selection,
    clear,
    deleteSelected,
    canDelete,
    history,
    layoutMode,
    lightSheetOpen,
    nudgeSelectedText,
    togglePlay,
  ]);

  // ── Save / leave ────────────────────────────────────────────────────────────

  const clearDraft = useCallback(() => {
    if (!variant) return;
    try {
      window.sessionStorage.removeItem(draftKey(itemId, variant.variant_id));
    } catch {
      /* privacy mode / quota — nothing to clean up */
    }
  }, [itemId, variant]);

  const handleSave = useCallback(async (
    musicAlignment?: "preserve_cuts" | "resync_beats",
  ) => {
    if (!variant || saveState === "saving" || readOnly) return;
    const commitMusicWindow = musicWindowDirty && !!songWindowState?.editable;
    if (commitMusicWindow && !musicAlignment) {
      setMusicAlignmentPrompt(true);
      return;
    }
    if (!commitMusicWindow && musicDirty && (timelineDirty || clipDirty)) {
      const proceed = window.confirm(
        "Changing the song resets clip cuts to the new beat grid. Save with the new song?",
      );
      if (!proceed) return;
    }
    setMusicAlignmentPrompt(false);
    setSaveState("saving");
    setSaveMessage(null);
    try {
      const captionCues = barsToCaptionCues(state.bars);
      const lyricsRequest: EditorCommitLyricsRequest = {
        ...(lyricsEnabled !== persistedLyricsEnabled(variant) ? { enabled: lyricsEnabled } : {}),
        ...(lyricOverridesDirty ? { line_overrides: lyricLineOverrides } : {}),
      };
      const commitRequest = buildEditorCommitRequest({
        // Elements-model lyric_line bars ride the normal text_elements
        // section (they're ordinary persisted elements on that model);
        // legacy leaves them out — they persist via the `lyrics` section
        // below instead (lyricsRequest / lyricsDirty, forced off above).
        elements: barsToTextElements(state.bars, originalsRef.current, {
          includeLyrics: lyricsOptionalActive,
        }),
        captionCues,
        captionMeta: captionMetaPatch,
        textDirty: textDirty && captionCues.length === 0,
        captionDirty: textDirty && captionCues.length > 0,
        captionMetaDirty,
        timelineDirty,
        slots,
        mixDirty,
        mixLevel,
        musicDirty,
        musicTrackId: selectedMusicTrackId,
        backgroundMusicLevel: variant.background_music?.level ?? 0.22,
        musicWindow:
          commitMusicWindow && musicAlignment
            ? { startS: songWindowState.startS, alignment: musicAlignment }
            : undefined,
        sfxDirty,
        soundEffects: localSfx,
        overlaysDirty,
        mediaOverlays: localOverlays,
        visualBlocksDirty,
        visualBlocks: localVisualBlocks,
        // Filtered against the staged overlay ids inside the builder — an
        // accepted suggestion the user undid must not be resolved.
        acceptedSuggestions,
        titleDirty,
        title,
        lyricsDirty,
        lyrics: lyricsRequest,
        orientationDirty,
        orientation,
        variant,
      });
      const res = await commitEditorSession(
        itemId,
        variant.variant_id,
        commitRequest,
      );
      // Partial: persist landed (we got a 2xx) but the render kick failed —
      // the response's `ok` flag tells us. Working state stays, Retry re-kicks.
      if (res && res.ok === false) {
        setSaveState("partial");
        setSaveMessage("Saved, but rendering didn't start.");
        return;
      }
      // Full success: the stack is void (no undoing into a pre-persist world),
      // the draft is spent, and the item-page hero shows the rendering state.
      draftPersistenceSuspendedRef.current = true;
      history.clear();
      clearDraft();
      setDraftDoc(null);
      setTextDirty(false);
      setSfxDirty(false);
      setOverlaysDirty(false);
      setVisualBlocksDirty(false);
      setTitleDirty(false);
      setMixDirty(false);
      setMusicDirty(false);
      setCaptionMetaDirty(false);
      setCaptionMetaPatch({});
      setSaveState("idle");
      const renderStarted = editorCommitStartedRender(res.sections);
      setSaveMessage(renderStarted ? "Saved — rendering your latest version" : "Saved");
      router.push(
        buildPlanItemEditorReturnHref(itemId, {
          variantId: variant.variant_id,
          generation: res.generation,
          priorFinishedAt: variant.render_finished_at ?? null,
          renderStarted,
        }),
      );
    } catch (err) {
      if (err instanceof EditorCommitConflictError) {
        setSaveState("conflict");
        setSaveMessage(err.message);
      } else {
        setSaveState("error");
        setSaveMessage(
          err instanceof Error ? err.message : "Couldn't save your edits.",
        );
      }
    }
  }, [
    variant,
    saveState,
    readOnly,
    itemId,
    state.bars,
    title,
    router,
    timelineDirty,
    clipDirty,
    slots,
    mixDirty,
    mixLevel,
    musicDirty,
    selectedMusicTrackId,
    musicWindowDirty,
    songWindowState,
    captionMetaDirty,
    captionMetaPatch,
    textDirty,
    lyricsDirty,
    orientationDirty,
    orientation,
    lyricOverridesDirty,
    lyricsEnabled,
    lyricLineOverrides,
    lyricsOptionalActive,
    sfxDirty,
    localSfx,
    overlaysDirty,
    localOverlays,
    visualBlocksDirty,
    localVisualBlocks,
    acceptedSuggestions,
    titleDirty,
    history,
    clearDraft,
  ]);

  // ── Draft recovery (plan §9) ────────────────────────────────────────────────
  // Mirror the working document to sessionStorage on every command push (any
  // document change while dirty). Failures degrade draft safety silently.
  const dirtyDraftIdentityRef = useRef<string | null>(null);
  useEffect(() => {
    if (draftPersistenceSuspendedRef.current) return;
    // During client-side [id] navigation React can retain this shell while the
    // next item loads. Never write the previous item's working document under
    // the next item's draft key in that gap.
    if (!variant || item?.id !== itemId) return;
    const identity = `${itemId}:${variant.variant_id}`;
    if (!dirty) {
      if (dirtyDraftIdentityRef.current === identity) {
        clearDraft();
        dirtyDraftIdentityRef.current = null;
      }
      return;
    }
    try {
      window.sessionStorage.setItem(
        draftKey(itemId, variant.variant_id),
        serializeDraft(
          itemId,
          item?.current_job_id ?? "",
          variant.variant_id,
          editorCommitBaseGeneration(variant),
          getCurrent(),
        ),
      );
      dirtyDraftIdentityRef.current = identity;
    } catch {
      /* quota full / privacy mode — editing continues, draft safety only */
    }
  }, [
    variant,
    itemId,
    item?.id,
    item?.current_job_id,
    dirty,
    state.bars,
    localSlots,
    localSfx,
    localOverlays,
    localVisualBlocks,
    captionMeta,
    captionMetaDirty,
    captionMetaPatch,
    videoMuted,
    soundMuted,
    mixLevel,
    mixDirty,
    selectedMusicTrackId,
    musicStartS,
    musicDirty,
    title,
    orientation,
    getCurrent,
    clearDraft,
  ]);

  // On open, surface a matching unsaved draft as a quiet Resume/Discard notice
  // (once per variant, after seeding so a Resume overrides the seeded bars).
  const draftCheckedRef = useRef<string | null>(null);
  useEffect(() => {
    if (!variant || item?.id !== itemId) return;
    const jobId = item.current_job_id ?? "";
    const baseGeneration = editorCommitBaseGeneration(variant);
    const identity = `${itemId}:${jobId}:${variant.variant_id}:${baseGeneration}`;
    if (draftCheckedRef.current === identity) return;
    draftCheckedRef.current = identity;
    try {
      const parsed = deserializeDraft(
        window.sessionStorage.getItem(draftKey(itemId, variant.variant_id)),
      );
      if (
        parsed &&
        parsed.planItemId === itemId &&
        parsed.jobId === jobId &&
        parsed.variantId === variant.variant_id &&
        parsed.baseGeneration === baseGeneration
      ) {
        setDraftDoc(parsed.doc);
      } else {
        setDraftDoc(null);
      }
    } catch {
      /* unreadable draft — skip the notice */
      setDraftDoc(null);
    }
  }, [item?.current_job_id, item?.id, itemId, variant]);

  const resumeDraft = useCallback(() => {
    if (!draftDoc) return;
    // Record the seeded baseline first so Resume itself is undoable, then
    // restore the draft as the working document.
    history.record();
    applyDocument(draftDoc);
    setDraftDoc(null);
  }, [draftDoc, history, applyDocument]);

  const discardDraft = useCallback(() => {
    clearDraft();
    setDraftDoc(null);
  }, [clearDraft]);

  const requestLeave = useCallback(() => {
    if (dirty) setConfirmLeave(true);
    else router.push(`/plan/items/${itemId}`);
  }, [dirty, router, itemId]);

  // ── Render ──────────────────────────────────────────────────────────────────

  if (needsAuth) {
    return (
      <Frame>
        <div className="flex flex-1 items-center justify-center">
          <p className="text-sm text-[#3f3f46]">
            Please{" "}
            <a href="/api/auth/signin" className="underline underline-offset-4">
              sign in
            </a>{" "}
            to edit this video.
          </p>
        </div>
      </Frame>
    );
  }

  if (loading) {
    return (
      <Frame>
        <div className="grid min-h-0 flex-1 grid-cols-[92px_1fr_320px_72px]">
          <div className="border-r border-zinc-200 bg-white" />
          <div className="flex items-center justify-center">
            <div className="h-[70%] w-auto rounded-xl border border-zinc-200 bg-zinc-100 motion-safe:animate-pulse" style={{ aspectRatio: "9 / 16" }} />
          </div>
          <div className="border-l border-zinc-200 bg-white" />
          <div className="border-l border-zinc-200 bg-white" />
        </div>
        <div className="h-[260px] border-t border-zinc-200 bg-white" />
      </Frame>
    );
  }

  if (loadError || !variant) {
    return (
      <Frame>
        <div className="flex flex-1 items-center justify-center p-8">
          <div className="max-w-[420px] rounded-xl border border-dashed border-zinc-300 bg-white p-6 text-center">
            <p className="text-sm text-[#3f3f46]">
              {loadError ?? "This video doesn't have an editable version yet."}
            </p>
            <div className="mt-4 flex items-center justify-center gap-3">
              {loadError && (
                <button
                  type="button"
                  onClick={() => setLoadNonce((n) => n + 1)}
                  className="min-h-11 rounded-full border border-zinc-200 px-4 text-[13px] text-[#3f3f46] hover:border-zinc-400 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500"
                >
                  Retry
                </button>
              )}
              <button
                type="button"
                onClick={() => router.push(`/plan/items/${itemId}`)}
                className="min-h-11 rounded-full bg-[#0c0c0e] px-4 text-[13px] font-semibold text-white hover:opacity-80 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500"
              >
                Back to the video
              </button>
            </div>
          </div>
        </div>
      </Frame>
    );
  }

  const isVoiceoverVariant = variant.variant_id.startsWith("voiceover");
  const backgroundMusicEditable = capabilities?.background_music === true && !readOnly;
  const musicSwapEditable = (!!variant.music_track_id || backgroundMusicEditable) && !readOnly;
  const hasSoundBed = !!effectiveMusicTrackId || isVoiceoverVariant;
  const soundBedLabel = isVoiceoverVariant
    ? effectiveMusicTrackId
      ? `Voiceover + ${effectiveMusicTitle}`
      : "Voiceover"
    : effectiveMusicTrackId
      ? effectiveMusicTitle
      : "Background";
  const soundLaneTitle = effectiveMusicTrackId
    ? "Music + effects"
    : isVoiceoverVariant
      ? "Voiceover bed"
      : "Sound effects";
  const hasUnbakedSfx = sfxDirty || localSfx.length > 0;
  const clipPreviewHint = (() => {
    if (!virtualPreviewActive) return "Clip changes preview after Save";
    const missing: string[] = [];
    if (effectiveMusicTrackId && !virtualMusicAudioUrl) missing.push("Music");
    missing.push(missing.length > 0 ? "transitions" : "Transitions");
    if (hasUnbakedSfx) missing.push("sound effects");
    return `${missing.join(", ").replace(/, ([^,]*)$/, " and $1")} preview after Save`;
  })();

  // AI suggestions inside the Overlays drawer — dual-gated (frontend flag +
  // the variant's honest capability). A false capability (e.g.
  // song_or_lyric_variant) renders NOTHING: no dead chrome in the drawer.
  const overlaySuggestionsNode = overlaySuggestionsEnabled ? (
    <OverlaySuggestions
      suggestions={overlaySuggestions}
      assets={poolAssets}
      maxAssets={maxPoolAssets}
      pending={pendingPoolUploads}
      poolUnavailable={poolUnavailable}
      poolError={poolError}
      onFiles={handlePoolFiles}
      onRemoveAsset={handleRemovePoolAsset}
      onAccept={handleAcceptSuggestion}
      onSeek={seekPlaybackTo}
    />
  ) : null;
  const showCopilotSaveNotice = sessionHasCopilotEdits && !copilotSaveNoticeDismissed;
  const lyricsToggle = {
    visible: !!variant && lyricsFeatureAvailable,
    enabled: lyricsOptionalActive ? hasLyricBars : lyricsEnabled,
    disabled: lyricsOptionalActive
      ? lyricSeedsLoading || lyricSeedsError != null || (!hasLyricBars && !lyricsCap.can_toggle_on)
      : !lyricsEnabled && !lyricsCap.can_toggle_on,
    hint: lyricsOptionalActive
      ? lyricSeedsLoading
        ? "Loading lyrics…"
        : lyricSeedsError === "no_lyrics"
          ? "This song doesn't have synced lyrics"
          : lyricSeedsError === "not_found"
            ? "Lyrics aren't available for this edit"
            : lyricsToggleHint(lyricsCap.reason)
      : lyricsToggleHint(lyricsCap.reason),
    onToggle: lyricsOptionalActive
      ? (next: boolean) => void toggleLyricsOptional(next)
      : (next: boolean) => {
          if (readOnly) return;
          if (next && !lyricsCap.can_toggle_on && !lyricsCap.enabled) {
            setToast(lyricsToggleHint(lyricsCap.reason) ?? "Lyrics can't be enabled for this edit.");
            return;
          }
          if (next === lyricsEnabled) return;
          history.record("lyrics-toggle");
          setLyricsEnabled(next);
        },
  };
  const orientationCap = capabilities?.orientation;
  const orientationToggleVisible = LANDSCAPE_UI && orientationCap != null;
  const orientationToggleDisabled = readOnly || orientationCap?.editable !== true;
  const orientationToggleHint = orientationDisabledHint(orientationCap?.reason);
  const orientationToggle = orientationToggleVisible ? (
    <OrientationToggle
      value={orientation}
      disabled={orientationToggleDisabled}
      disabledHint={orientationToggleHint}
      onChange={(next) => {
        if (orientationToggleDisabled) {
          setToast(orientationToggleHint ?? "Landscape isn't available for this edit.");
          return;
        }
        if (next === orientation) return;
        history.record("orientation");
        setOrientation(next);
      }}
    />
  ) : null;

  const editorModeProps: EditorTimelineBodyProps = {
    durationS: timelineDuration,
    renderedOutputDurationS: duration,
    currentTimeS: currentTime,
    zoom,
    fitRequestKey: timelineFitRequestKey,
    scaleResetKey: timelineVariantId,
    selection,
    onSelect: (kind, id) => {
      selectElement(kind, id);
    },
    onClear: clear,
    textBars: timelineTextBars,
    readOnly,
    onRecordTimelineEdit: recordTimelineDrag,
    onPreviewTextTiming: previewTextTiming,
    visualBlocks: localVisualBlocks.map((block) => ({
      id: block.id,
      kind: block.kind,
      start_s: block.start_s,
      end_s: block.end_s,
    })),
    showVisualBlocks:
      VISUAL_BLOCKS_UI_ENABLED && capabilities?.visual_blocks !== false,
    onPreviewVisualTiming: previewVisualTiming,
    slots,
    clipReadOnly: clipEditingLocked,
    clipDisabledReason,
    clipSourceDurations,
    onPreviewClipTiming: previewClipTiming,
    onPreviewSeek: seekPreviewToOutput,
    grid: clip.state.grid,
    clipPreviewMode: virtualPreviewActive ? "virtual" : "rendered",
    clipsLoading: clip.loadState === "loading",
    filmstripClips: clip.clips,
    sfx: localSfx.map((p) => {
      const trimStart = p.trim_start_s ?? 0;
      const trimEnd = p.trim_end_s ?? p.duration_s ?? null;
      return {
        id: p.id,
        at_s: p.at_s ?? 0,
        end_s:
          trimEnd == null
            ? null
            : (p.at_s ?? 0) + Math.max(0, trimEnd - trimStart),
        label: p.label ?? null,
      };
    }),
    onPreviewSfxTiming: previewSfxTiming,
    hasMusic: hasSoundBed,
    musicLabel: effectiveMusicTitle,
    soundLaneTitle,
    soundBedLabel,
    soundBedTitle: isVoiceoverVariant
      ? "Balance this bed against your voiceover in the inspector"
      : "The song auto-fits your cut",
    videoMuted,
    onToggleVideoMute: () => {
      if (readOnly) return;
      history.record();
      setVideoMuted((m) => !m);
    },
    soundMuted,
    onToggleSoundMute: () => {
      if (readOnly) return;
      const nextMuted = !soundMuted;
      if (capabilities?.mix !== false && mixLevel != null) {
        patchMixLevel(nextMuted ? 0 : Math.max(mixLevel, variant.mix ?? 0.2));
      } else {
        history.record();
        setSoundMuted(nextMuted);
      }
    },
    overlays: localOverlays.map((o) => ({
      id: o.id,
      start_s: o.start_s,
      end_s: o.end_s,
      label: o.kind === "video" ? "Video" : "Image",
      // Provenance until Save: accepted AI suggestions get the dashed ✦ bar.
      suggested: suggestedOverlayIds.has(o.id),
    })),
    onPreviewOverlayTiming: previewOverlayTiming,
    onOpenSounds: () => setActiveTool("sounds"),
    onScrub: seekTo,
    onScrubStart: () => {
      pausePlayback();
    },
    flashIds: flashTimelineIds,
  };

  return (
    <div
      className="fixed inset-0 z-50 grid overflow-hidden bg-[#fafaf8]"
      style={{
        gridTemplateRows:
          layoutMode === "light"
            ? "56px minmax(0, 1fr) auto"
            : "56px minmax(0, 1fr) clamp(220px, 30dvh, 260px)",
      }}
    >
      <style dangerouslySetInnerHTML={{ __html: FONT_FACES }} />

      {/* ── Top bar (plan §1) ── */}
      {layoutMode === "light" ? (
        <LightTopBar
          dirty={dirty}
          saving={saving}
          readOnly={readOnly}
          saveState={saveState}
          showCopilotNotice={showCopilotSaveNotice}
          onBack={requestLeave}
          onOpenNova={() => setActiveTool("nova")}
          onDismissCopilotNotice={() => {
            setCopilotSaveNoticeDismissed(true);
            try {
              window.localStorage.setItem(COPILOT_SAVE_NOTICE_KEY, "true");
            } catch {
              /* localStorage unavailable */
            }
          }}
          onSave={() => void handleSave()}
          orientationToggle={orientationToggle}
        />
      ) : (
        <header className="flex items-center border-b border-zinc-200 bg-white px-4">
          <div className="flex flex-1 items-center gap-3">
            <button
              type="button"
              aria-label="Back to the video page"
              onClick={requestLeave}
              className="flex h-11 w-11 items-center justify-center rounded-full border border-zinc-200 pb-0.5 text-[15px] text-[#3f3f46] hover:border-zinc-400 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500"
            >
              ‹
            </button>
            <input
              type="text"
              value={title}
              onChange={(e) => {
                if (readOnly) return;
                  // Coalesce typing bursts into one undo step.
                  history.record("title");
                  setTitleDirty(true);
                  setTitle(e.target.value);
              }}
              readOnly={readOnly}
              placeholder="add title for your video"
              aria-label="Video title"
              className="min-h-11 w-[240px] rounded-md border border-transparent bg-transparent px-2 py-1 text-[13px] text-[#0c0c0e] placeholder:text-[#a1a1aa] focus:border-lime-500 focus:bg-white focus:outline-none focus:ring-2 focus:ring-lime-500/25"
            />
          </div>

          {/* Center cluster — visually quiet; ink chip only on the active tool */}
          <div className="flex items-center gap-1.5">
            <button
              type="button"
              aria-pressed={canvasTool === "select"}
              aria-label="Select"
              title="Select"
              onClick={() => setCanvasTool("select")}
              className={`flex h-11 w-11 items-center justify-center rounded-lg text-[13px] focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500 ${
                canvasTool === "select"
                  ? "bg-[#0c0c0e] text-white"
                  : "text-[#3f3f46] hover:bg-zinc-100"
              }`}
            >
              <SelectCursorIcon />
            </button>
            <button
              type="button"
              aria-pressed={canvasTool === "pan"}
              aria-label="Pan — drag to move around the canvas when zoomed in"
              title={panEnabled ? "Pan — drag to move around the canvas when zoomed in" : "Zoom in to pan"}
              disabled={!panEnabled}
              onClick={() => setCanvasTool("pan")}
              className={`flex h-11 w-11 items-center justify-center rounded-lg text-[13px] focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500 ${
                canvasTool === "pan"
                  ? "bg-[#0c0c0e] text-white"
                  : "text-[#3f3f46] hover:bg-zinc-100 disabled:text-[#a1a1aa] disabled:hover:bg-transparent"
              }`}
            >
              <PanHandIcon />
            </button>
            {/* Undo/redo — unified document command stack (plan §7). */}
            <button
              type="button"
              aria-label="Undo"
              title="Undo (⌘Z)"
              disabled={!history.canUndo}
              onClick={history.undo}
              className="flex h-11 w-11 items-center justify-center rounded-lg text-[14px] text-[#3f3f46] hover:bg-zinc-100 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500 disabled:opacity-40 disabled:hover:bg-transparent"
            >
              ↺
            </button>
            <button
              type="button"
              aria-label="Redo"
              title="Redo (⇧⌘Z)"
              disabled={!history.canRedo}
              onClick={history.redo}
              className="flex h-11 w-11 items-center justify-center rounded-lg text-[14px] text-[#3f3f46] hover:bg-zinc-100 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500 disabled:opacity-40 disabled:hover:bg-transparent"
            >
              ↻
            </button>
            <select
              aria-label="Canvas zoom"
              value={zoomPct}
              onChange={(e) => setZoomPct(Number(e.target.value))}
              className="ml-1 h-11 rounded-lg border border-zinc-200 bg-white px-2 text-[12px] text-[#3f3f46] focus:border-lime-500 focus:outline-none focus:ring-2 focus:ring-lime-500/25"
            >
              {ZOOM_OPTIONS.map((z) => (
                <option key={z} value={z}>
                  {z}%
                </option>
              ))}
            </select>
            {orientationToggle}
          </div>

          <div className="flex flex-1 items-center justify-end gap-2">
            {showCopilotSaveNotice && (
              <div className="flex max-w-[360px] items-center gap-2 rounded-lg border border-zinc-200 bg-white px-3 py-1.5 text-[12px] text-[#3f3f46]">
                <span className="truncate">
                  The preview is a close match — the saved video is rendered exactly.
                </span>
                <button
                  type="button"
                  aria-label="Dismiss preview match note"
                  onClick={() => {
                    setCopilotSaveNoticeDismissed(true);
                    try {
                      window.localStorage.setItem(COPILOT_SAVE_NOTICE_KEY, "true");
                    } catch {
                      /* localStorage unavailable */
                    }
                  }}
                  className="min-h-8 px-1 text-[#71717a] hover:text-[#0c0c0e] focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500"
                >
                  ✕
                </button>
              </div>
            )}
            {saveState === "idle" && saveMessage && (
              <span className="max-w-[280px] truncate rounded-lg border border-zinc-200 bg-white px-3 py-1.5 text-[12px] text-[#3f3f46]">
                {saveMessage}
              </span>
            )}
            {(lyricsDirty || orientationDirty) && (
              <span className="rounded-lg border border-zinc-200 bg-white px-3 py-1.5 text-[12px] font-semibold text-[#3f3f46]">
                Re-renders on Save
              </span>
            )}
            <InkButton
              variant="ghost"
              size="compact"
              className="focus-visible:!outline-lime-500"
              onClick={requestLeave}
            >
              Cancel
            </InkButton>
            <InkButton
              size="compact"
              className="gap-2 focus-visible:!outline-lime-500"
              disabled={!dirty || saving || readOnly}
              onClick={() => void handleSave()}
            >
              {saving && <SaveSpinner />}
              {saving ? "Saving" : "Save"}
            </InkButton>
          </div>
        </header>
      )}

      {renderedMusicPreviewActive && virtualMusicAudioUrl && (
        <audio
          ref={renderedMusicAudioRef}
          data-testid="rendered-music-window-preview"
          src={virtualMusicAudioUrl}
          preload="auto"
          className="hidden"
          onError={handleVirtualMusicError}
          onLoadedMetadata={(event) => {
            const audio = event.currentTarget;
            const video = videoRef.current;
            if (!video) return;
            const target = Math.max(0, musicStartS + video.currentTime);
            audio.currentTime =
              Number.isFinite(audio.duration) && audio.duration > 0
                ? Math.min(target, Math.max(0, audio.duration - 0.01))
                : target;
            if (!video.paused) void audio.play().catch(() => {});
          }}
        />
      )}

      {/* ── Middle row: rail · drawer · canvas · inspector · edge rail ── */}
      {layoutMode === "light" ? (
        <div className="relative min-h-0">
          <EditorCanvas
            variant={variant}
            elements={previewElements}
            bars={visibleTextBars}
            visualBlocks={localVisualBlocks}
            visualAssets={poolAssets}
            mediaOverlays={localOverlays}
            overlayPreviewUrls={localOverlayPreviewUrls}
            suggestedOverlayIds={suggestedOverlayIds}
            sfxPlacements={previewSfxPlacements}
            sfxAudioUrls={localSfxAudioUrls}
            selectedTextId={selection?.kind === "text" ? selection.id : null}
            selectedOverlayId={selection?.kind === "overlay" ? selection.id : null}
            flashTextIds={flashTextIds}
            flashOverlayIds={flashOverlayIds}
            currentTime={currentTime}
            playing={playing}
            masonryDurationS={previewDuration}
            zoomPct={100}
            tool="select"
            videoRef={videoRef}
            onSelectText={selectText}
            onSelectOverlay={(id) => selectElement("overlay", id)}
            onClearSelection={() => {
              clear();
              setLightSheetOpen(false);
            }}
            onPatchBar={patchBar}
            onFocusContent={() => setLightSheetOpen(true)}
            onTimeUpdate={setCurrentTime}
            onDuration={setDuration}
            onPlayingChange={setPlaying}
            onReloadSource={() => setLoadNonce((n) => n + 1)}
            virtualPreview={virtualPreviewActive ? virtualPreview : null}
            allowManipulation={false}
            stageHeightCss="100dvh - 152px"
            canvas={activeCanvas}
          />
          {visibleTextBars.length === 0 && !readOnly && !textElementsLocked && (
            <button
              type="button"
              onClick={() => addTextAtPlayhead()}
              className="absolute bottom-4 left-1/2 min-h-11 -translate-x-1/2 rounded-full bg-white px-4 text-[13px] font-semibold text-[#0c0c0e] shadow-[0_8px_24px_rgba(12,12,14,0.18)] ring-1 ring-zinc-200 hover:bg-zinc-50 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500"
            >
              Add text
            </button>
          )}
        </div>
      ) : (
        <div
          className={[
            "relative grid min-h-0 grid-rows-[minmax(0,1fr)] overflow-hidden",
            layoutMode === "full"
              ? "grid-cols-[auto_auto_1fr_auto_auto]"
              : "grid-cols-[auto_1fr_auto_auto]",
          ].join(" ")}
        >
        <ToolRail
          activeTool={activeTool}
          disabledTools={toolDisabledReasons}
          onToggleTool={(tool) => setActiveTool((cur) => (cur === tool ? null : tool))}
        />
        {layoutMode === "full" &&
          (activeTool !== null ? (
            <ToolDrawer
              tool={activeTool}
              sampleWord={sampleWord}
              appliedPresetId={appliedPresetId}
              onAddText={() => addTextAtPlayhead()}
              lyricsToggle={lyricsToggle}
              onSplitPlaceText={splitAndPlaceText}
              splitSmartPlaceAvailable={!readOnly && !textElementsLocked}
              onSmartPlaceAll={applySmartPlacement}
              smartPlaceAllAvailable={smartPlaceAllAvailable}
              onPickPreset={pickPreset}
	              appliedStyleSetId={appliedStyleSetId}
	              onRestyleAll={onRestyleAll}
	              sfxEffects={sfxGlossaryEffects}
	              sfxLoading={sfxGlossaryLoading}
	              onAddSfx={addSfxFromGlossary}
              musicTracks={musicTracks}
              musicLoading={musicTracksLoading}
              currentMusicTrackId={selectedMusicTrackId}
              musicEditable={musicSwapEditable}
              onPickMusic={pickMusicTrack}
              onRemoveMusic={removeMusicTrack}
              musicWindow={musicWindowControl}
              overlayUploading={overlayUploading}
	              onOverlayUpload={handleOverlayUpload}
	              overlaySuggestions={overlaySuggestionsNode}
              visualBlocks={localVisualBlocks}
              visualAssets={poolAssets}
              visualTextElements={state.bars}
              visualUploading={pendingPoolUploads.length > 0}
              onVisualUpload={handlePoolFiles}
              onAddMontage={addMontageBlock}
              onAddTextCard={addTextCard}
              onAddVisualBlockText={addVisualBlockText}
              onSelectVisualBlockText={selectText}
              onPatchVisualBlock={patchVisualBlock}
              onDuplicateVisualBlock={duplicateVisualBlock}
              onDeleteVisualBlock={deleteVisualBlock}
              onRetimeVisualBlock={retimeBlock}
              layoutMode={layoutMode}
              copilot={{
                messages: copilot.messages,
                sending: copilot.sending,
                queued: copilot.queued,
                error: copilot.error,
                restoredInput: copilot.restoredInput,
                suggestions: copilot.suggestions,
                historyVersion: history.version,
                canUndo: history.canUndo,
                onSend: (text) => void copilot.send(text),
                onCancelQueued: copilot.cancelQueued,
                onEditQueued: copilot.editQueued,
                onStop: copilot.stop,
                onUndo: history.undo,
                onClearRestoredInput: copilot.clearRestoredInput,
              }}
	              onClose={() => setActiveTool(null)}
	            />
          ) : (
            <div />
          ))}
        {layoutMode === "overlay" && activeTool !== null && activeTool !== "nova" && (
          <div className="absolute bottom-0 left-[92px] top-0 z-40 shadow-[18px_0_36px_rgba(12,12,14,0.16)]">
            <ToolDrawer
              tool={activeTool}
              sampleWord={sampleWord}
              appliedPresetId={appliedPresetId}
              onAddText={() => addTextAtPlayhead()}
              lyricsToggle={lyricsToggle}
              onSplitPlaceText={splitAndPlaceText}
              splitSmartPlaceAvailable={!readOnly && !textElementsLocked}
              onSmartPlaceAll={applySmartPlacement}
              smartPlaceAllAvailable={smartPlaceAllAvailable}
              onPickPreset={pickPreset}
	              appliedStyleSetId={appliedStyleSetId}
	              onRestyleAll={onRestyleAll}
	              sfxEffects={sfxGlossaryEffects}
	              sfxLoading={sfxGlossaryLoading}
	              onAddSfx={addSfxFromGlossary}
              musicTracks={musicTracks}
              musicLoading={musicTracksLoading}
              currentMusicTrackId={selectedMusicTrackId}
              musicEditable={musicSwapEditable}
              onPickMusic={pickMusicTrack}
              onRemoveMusic={removeMusicTrack}
              musicWindow={musicWindowControl}
              overlayUploading={overlayUploading}
	              onOverlayUpload={handleOverlayUpload}
	              overlaySuggestions={overlaySuggestionsNode}
              visualBlocks={localVisualBlocks}
              visualAssets={poolAssets}
              visualTextElements={state.bars}
              visualUploading={pendingPoolUploads.length > 0}
              onVisualUpload={handlePoolFiles}
              onAddMontage={addMontageBlock}
              onAddTextCard={addTextCard}
              onAddVisualBlockText={addVisualBlockText}
              onSelectVisualBlockText={selectText}
              onPatchVisualBlock={patchVisualBlock}
              onDuplicateVisualBlock={duplicateVisualBlock}
              onDeleteVisualBlock={deleteVisualBlock}
              onRetimeVisualBlock={retimeBlock}
              layoutMode={layoutMode}
	              onClose={() => setActiveTool(null)}
	            />
          </div>
        )}
        {layoutMode === "overlay" && activeTool === "nova" && (
          <div className="absolute bottom-4 left-[108px] right-[344px] z-40">
            <ToolDrawer
              tool="nova"
              sampleWord={sampleWord}
              appliedPresetId={appliedPresetId}
              onAddText={() => addTextAtPlayhead()}
              lyricsToggle={lyricsToggle}
              onPickPreset={pickPreset}
              layoutMode={layoutMode}
              copilot={{
                messages: copilot.messages,
                sending: copilot.sending,
                queued: copilot.queued,
                error: copilot.error,
                restoredInput: copilot.restoredInput,
                suggestions: copilot.suggestions,
                historyVersion: history.version,
                canUndo: history.canUndo,
                onSend: (text) => void copilot.send(text),
                onCancelQueued: copilot.cancelQueued,
                onEditQueued: copilot.editQueued,
                onStop: copilot.stop,
                onUndo: history.undo,
                onClearRestoredInput: copilot.clearRestoredInput,
              }}
              onClose={() => setActiveTool(null)}
            />
          </div>
        )}
        <div
          data-region="canvas-cell"
          className="flex min-h-0 min-w-0 items-center justify-center overflow-hidden"
        >
          <EditorCanvas
            variant={variant}
            elements={previewElements}
            bars={visibleTextBars}
            visualBlocks={localVisualBlocks}
            visualAssets={poolAssets}
            mediaOverlays={localOverlays}
            overlayPreviewUrls={localOverlayPreviewUrls}
            suggestedOverlayIds={suggestedOverlayIds}
            sfxPlacements={previewSfxPlacements}
            sfxAudioUrls={localSfxAudioUrls}
            selectedTextId={selection?.kind === "text" ? selection.id : null}
            selectedOverlayId={selection?.kind === "overlay" ? selection.id : null}
            flashTextIds={flashTextIds}
            flashOverlayIds={flashOverlayIds}
            currentTime={currentTime}
            playing={playing}
            masonryDurationS={previewDuration}
            zoomPct={zoomPct}
            tool={canvasTool}
            videoRef={videoRef}
            onSelectText={selectText}
            onSelectOverlay={(id) => selectElement("overlay", id)}
            onClearSelection={clear}
            onPatchBar={patchBar}
            onPatchOverlay={patchOverlay}
            onFocusContent={focusContent}
            onTimeUpdate={setCurrentTime}
            onDuration={setDuration}
            onPlayingChange={setPlaying}
            onReloadSource={() => setLoadNonce((n) => n + 1)}
            virtualPreview={virtualPreviewActive ? virtualPreview : null}
            canvas={activeCanvas}
          />
        </div>
        <InspectorPanel
	          selection={selection}
	          bar={selectedBar}
	          clipTiming={selectedClip}
	          sfx={selectedSfx}
	          overlay={selectedOverlay}
	          tab={inspectorTab}
          sampleWord={sampleWord}
          appliedPresetId={appliedPresetId}
          captionsTabHref={
            // CTA only when on-video text genuinely can't be edited here — once
            // SUBTITLED_TEXT_LANE_ENABLED ships (text_elements true) the styled-text
            // lane is editable in this shell, so keep the generic empty state and
            // don't mask it. The signpost notice stays archetype-gated (captions
            // always live in the Captions tab).
            textElementsLocked && isCaptionEdit ? `/plan/items/${itemId}` : null
          }
          contentRef={contentRef}
          onEditText={(text) => {
          if (selectedBar && !readOnly) {
              // Coalesce keystrokes on one bar into a single undo step.
              history.record(`text:${selectedBar.id}`);
              if (lyricsOptionalActive || !isLyricBar(selectedBar)) setTextDirty(true);
              dispatch({ type: "EDIT_TEXT", id: selectedBar.id, text });
            }
          }}
          onPatch={(patch) => {
            if (selectedBar) patchBar(selectedBar.id, patch);
          }}
          onSetTextBoxPosition={setSelectedTextBoxPosition}
          boxPositionXFrac={selectedTextBoxScreenXFrac}
          onPatchTextTiming={patchSelectedTextTiming}
	          onPatchClipTiming={patchSelectedClipTiming}
	          onPreviewClipTiming={previewSelectedClipTiming}
	          onRecordClipTiming={recordTimelineDrag}
	          onPatchSfx={patchSfx}
	          onDeleteSfx={removeSfx}
	          onPatchOverlay={patchOverlay}
	          onPreviewOverlay={previewOverlayPatch}
	          onRecordOverlay={recordTimelineDrag}
	          onDeleteOverlay={removeOverlay}
          mixLevel={mixLevel}
          mixEditable={capabilities?.mix !== false && mixLevel != null}
          mixLabel={soundBedLabel}
          musicTracks={musicTracks}
          musicLoading={musicTracksLoading}
          currentMusicTrackId={selectedMusicTrackId}
          musicEditable={musicSwapEditable}
          onPickMusic={pickMusicTrack}
          onRemoveMusic={removeMusicTrack}
          musicWindow={musicWindowControl}
          onPatchMix={patchMixLevel}
          smartPlaceAvailable={
            !!selectedBar && !readOnly && (isMasonryVariant(variant) || !!smartPlacementCandidate)
          }
          onSmartPlace={applySelectedSmartPlacement}
	          onClose={clear}
          onPickPreset={pickPreset}
        />
        <InspectorRail
          tab={inspectorTab}
          hasSelection={selection !== null}
          onTab={setInspectorTab}
        />
      </div>
      )}

      {/* ── Timeline region (260px): TransportBar + scale-driven editor
             timeline (Text → Video → Sound → Overlays), plan §6. ── */}
      {layoutMode === "light" ? (
        <LightTransport
          playing={playing}
          currentTime={currentTime}
          duration={previewDuration}
          onPlayPause={togglePlay}
          onScrub={seekTo}
        />
      ) : (
      <div
        data-region="timeline"
        className="relative flex min-h-0 flex-col border-t border-zinc-200 bg-white"
      >
        <TransportBar
          playing={playing}
          currentTime={currentTime}
          duration={previewDuration}
          onPlayPause={togglePlay}
          canSplit={canSplit}
          splitReason={splitReason}
          onSplit={splitAtPlayhead}
          canDelete={canDelete}
          onDelete={deleteSelected}
          zoom={zoom}
          onZoom={setZoom}
          onFit={() => {
            setZoom(1);
            setTimelineFitRequestKey((key) => key + 1);
          }}
          clipTimingDirty={clipDirty}
          clipPreviewMode={virtualPreviewActive ? "virtual" : "rendered"}
          clipPreviewHint={clipPreviewHint}
        />
        <div className="min-h-0 flex-1">
          <UnifiedTimeline
            totalDurationS={timelineDuration}
            currentTimeS={currentTime}
            // Item-page-only props — unused in editor mode (UnifiedTimeline
            // early-returns on `editorMode`); passed as inert defaults so the
            // shared component's required contract stays satisfied.
            sfxPlacements={[]}
            sfxGlossaryEffects={[]}
            sfxGlossaryLoading={false}
            sfxRendering={false}
            sfxUploading={false}
            onSfxChange={() => {}}
            onSfxUploadRequest={async () => {}}
            overlayCards={localOverlays}
            overlaysEnabled={capabilities?.overlays !== false && !readOnly}
            overlayUploading={overlayUploading}
            localPreviewUrls={localOverlayPreviewUrls}
            onOverlayUploadRequest={handleOverlayUpload}
            onUpdateCard={patchOverlay}
            onRemoveCard={removeOverlay}
            onClearOverlays={() => {
              if (readOnly || capabilities?.overlays === false) return;
              history.record();
              setLocalOverlays([]);
              setLocalOverlayPreviewUrls((current) => {
                Object.values(current).forEach((url) => URL.revokeObjectURL(url));
                return {};
              });
              setOverlaysDirty(true);
              clear();
            }}
            editorMode={editorModeProps}
          />
        </div>
        {toast && (
          <div
            role="status"
            aria-live="polite"
            className="pointer-events-none absolute bottom-4 left-1/2 -translate-x-1/2 rounded-lg bg-[#0c0c0e] px-3 py-1.5 text-[12px] text-white shadow-lg"
          >
            {toast}
          </div>
        )}
      </div>
      )}

      <LightEditSheet
        open={layoutMode === "light" && lightSheetOpen && !!selectedBar}
        bar={selectedBar}
        sampleWord={sampleWord}
        appliedPresetId={appliedPresetId}
        saveState={saveState}
        saving={saving}
        dirty={dirty}
        readOnly={readOnly}
        onClose={() => setLightSheetOpen(false)}
        onEditText={(text) => {
          if (selectedBar && !readOnly) {
            history.record(`text:${selectedBar.id}`);
            if (lyricsOptionalActive || !isLyricBar(selectedBar)) setTextDirty(true);
            dispatch({ type: "EDIT_TEXT", id: selectedBar.id, text });
          }
        }}
        onPickPreset={pickPreset}
        onSave={() => void handleSave()}
      />

      {layoutMode === "light" && activeTool === "nova" && (
        <ToolDrawer
          tool="nova"
          sampleWord={sampleWord}
          appliedPresetId={appliedPresetId}
          onAddText={() => addTextAtPlayhead()}
          lyricsToggle={lyricsToggle}
          onPickPreset={pickPreset}
          layoutMode={layoutMode}
          copilot={{
            messages: copilot.messages,
            sending: copilot.sending,
            queued: copilot.queued,
            error: copilot.error,
            restoredInput: copilot.restoredInput,
            suggestions: copilot.suggestions,
            historyVersion: history.version,
            canUndo: history.canUndo,
            onSend: (text) => void copilot.send(text),
            onCancelQueued: copilot.cancelQueued,
            onEditQueued: copilot.editQueued,
            onStop: copilot.stop,
            onUndo: history.undo,
            onClearRestoredInput: copilot.clearRestoredInput,
          }}
          onClose={() => setActiveTool(null)}
        />
      )}

      {/* ── Read-only banner (ineligible variant, plan §9 / E4) ── */}
      {readOnly && (
        <div className="absolute left-1/2 top-[68px] z-[60] w-[min(560px,90vw)] -translate-x-1/2">
          <div className="rounded-lg border border-zinc-200 bg-white/95 px-4 py-2.5 text-center text-[12px] text-[#3f3f46] shadow-sm">
            This version can&apos;t be edited. {readOnlyReason}
            {(readOnlyReason === CAPTIONS_TAB_REASON || isCaptionEdit) && (
              <>
                {" "}
                <CaptionsTabLink itemId={itemId} variantId={variant.variant_id} />
              </>
            )}
          </div>
        </div>
      )}

      {/* ── Captions-tab pointer (plan 010 review round) ── Post-lift subtitled
             shells are editable (no read-only banner), but on-video text still
             lives in the Captions tab — keep the deep-link discoverable. Quiet
             notice line (DESIGN.md §2 tokens), outside the layout branches so
             both the full editor and the light layout show it. */}
      {(textElementsLocked || (!readOnly && isCaptionEdit)) && (
        <div className="absolute left-1/2 top-[68px] z-[60] w-[min(560px,90vw)] -translate-x-1/2">
          <div
            data-testid="captions-tab-notice"
            className="rounded-lg border border-zinc-200 bg-white px-4 py-2.5 text-center text-[12px] text-[#3f3f46] shadow-sm"
          >
            {!readOnly && isCaptionEdit ? (
              // Caption archetype (with base video): captions live in the Captions
              // tab regardless of text_elements, so always show the reason + link.
              <>
                {CAPTIONS_TAB_REASON}. <CaptionsTabLink itemId={itemId} variantId={variant.variant_id} />
              </>
            ) : (
              // Non-caption text lock (e.g. lyrics_sync): keep the reason-driven
              // copy, appending the link only when the reason is the caption one.
              <>
                {textElementsLockedCopy(capabilities)}.
                {textElementsLockedCopy(capabilities) === CAPTIONS_TAB_REASON && (
                  <>
                    {" "}
                    <CaptionsTabLink itemId={itemId} variantId={variant.variant_id} />
                  </>
                )}
              </>
            )}
          </div>
        </div>
      )}

      {/* ── Save micro-states (plan §9): conflict / error / partial tiles.
             All preserve working state; only Reload/Retry act. ── */}
      {(saveState === "conflict" || saveState === "error" || saveState === "partial") && (
        <div className="absolute left-1/2 top-[68px] z-[70] w-[min(520px,90vw)] -translate-x-1/2">
          <div className="flex items-center justify-between gap-3 rounded-lg border border-dashed border-zinc-300 bg-white px-4 py-3 shadow-sm">
            <p className="text-[12px] text-[#3f3f46]">
              {saveState === "conflict"
                ? "This video changed in another tab — reload to continue."
                : saveState === "partial"
                  ? "Saved, but rendering didn't start."
                  : (saveMessage ?? "Couldn't save your edits.")}
            </p>
            {saveState === "conflict" ? (
              <button
                type="button"
                onClick={() => {
                  setSaveState("idle");
                  setSaveMessage(null);
                  // Re-seed non-dirty sections from the refetch (see
                  // conflictReseedRef) and refresh the slot baseline.
                  conflictReseedRef.current = true;
                  if (!timelineDirty) {
                    slotsSeededRef.current = null;
                    reloadClipTimeline();
                  }
                  setLoadNonce((n) => n + 1);
                }}
                className="min-h-11 flex-shrink-0 rounded-full bg-[#0c0c0e] px-4 text-[12px] font-semibold text-white hover:opacity-80 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500"
              >
                Reload
              </button>
            ) : (
              <button
                type="button"
                onClick={() => void handleSave()}
                className="min-h-11 flex-shrink-0 rounded-full border border-zinc-200 px-4 text-[12px] text-[#3f3f46] hover:border-zinc-400 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500"
              >
                Retry
              </button>
            )}
          </div>
        </div>
      )}

      {/* ── Resume-draft notice (plan §9): quiet, not a modal. ── */}
      {draftDoc && saveState === "idle" && (
        <div className="absolute left-1/2 top-[68px] z-[65] w-[min(480px,90vw)] -translate-x-1/2">
          <div className="flex items-center justify-between gap-3 rounded-lg border border-zinc-200 bg-white px-4 py-2.5 shadow-sm">
            <p className="text-[12px] text-[#3f3f46]">Resume your unsaved edits?</p>
            <div className="flex flex-shrink-0 items-center gap-2">
              <button
                type="button"
                onClick={discardDraft}
                className="min-h-11 rounded-full px-3 text-[12px] text-[#71717a] hover:text-[#0c0c0e] focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500"
              >
                Discard
              </button>
              <button
                type="button"
                onClick={resumeDraft}
                className="min-h-11 rounded-full bg-[#0c0c0e] px-4 text-[12px] font-semibold text-white hover:opacity-80 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500"
              >
                Resume
              </button>
            </div>
          </div>
        </div>
      )}

      <MusicAlignmentDialog
        open={musicAlignmentPrompt}
        preserveAvailable={musicWindowCapability?.preserve_available === true}
        preserveReason={musicWindowCapability?.preserve_reason ?? null}
        onChoose={(alignment) => void handleSave(alignment)}
        onCancel={() => setMusicAlignmentPrompt(false)}
      />

      <ConfirmDialog
        open={confirmLeave}
        question="Discard your edits?"
        detail="Your changes haven't been saved. Leaving now throws them away."
        confirmLabel="Discard"
        cancelLabel="Keep editing"
        onConfirm={() => {
          setConfirmLeave(false);
          router.push(`/plan/items/${itemId}`);
        }}
        onCancel={() => setConfirmLeave(false)}
      />
    </div>
  );
}

export function MusicAlignmentDialog({
  open,
  preserveAvailable,
  preserveReason,
  onChoose,
  onCancel,
}: {
  open: boolean;
  preserveAvailable: boolean;
  preserveReason: string | null;
  onChoose: (alignment: "preserve_cuts" | "resync_beats") => void;
  onCancel: () => void;
}) {
  const cardRef = useRef<HTMLDivElement>(null);
  const preserveRef = useRef<HTMLButtonElement>(null);
  const resyncRef = useRef<HTMLButtonElement>(null);
  useFocusTrap(cardRef, open);
  useEffect(() => {
    if (!open) return;
    (preserveAvailable ? preserveRef.current : resyncRef.current)?.focus();
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopImmediatePropagation();
        onCancel();
      }
    };
    document.addEventListener("keydown", onKey, true);
    return () => document.removeEventListener("keydown", onKey, true);
  }, [onCancel, open, preserveAvailable]);
  if (!open) return null;
  return (
    <div
      className="fixed inset-0 z-[70] flex items-center justify-center bg-black/30 px-6"
      onClick={onCancel}
    >
      <div
        ref={cardRef}
        role="alertdialog"
        aria-modal="true"
        aria-label="How should the cuts follow this song section?"
        className="w-full max-w-[460px] rounded-2xl border border-zinc-200 bg-white p-6 shadow-sm"
        onClick={(event) => event.stopPropagation()}
      >
        <p className="font-display text-xl text-[#0c0c0e]">
          How should the cuts follow this song section?
        </p>
        <p className="mt-2 text-sm leading-relaxed text-[#71717a]">
          Your selected song section is saved either way.
        </p>
        <div className="mt-5 space-y-3">
          <button
            ref={preserveRef}
            type="button"
            disabled={!preserveAvailable}
            onClick={() => onChoose("preserve_cuts")}
            className="min-h-14 w-full rounded-xl border border-zinc-300 bg-white px-4 text-left text-sm font-semibold text-[#0c0c0e] hover:border-zinc-500 disabled:cursor-not-allowed disabled:bg-zinc-100 disabled:text-zinc-400 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500"
          >
            Preserve cuts
            <span className="mt-1 block text-[12px] font-normal text-[#71717a]">
              Keep the current clip order and timing.
            </span>
          </button>
          {!preserveAvailable && preserveReason === "linear_timeline_unavailable" && (
            <p className="px-1 text-[11px] text-[#71717a]">
              Preserve cuts isn’t available for this older render.
            </p>
          )}
          <button
            ref={resyncRef}
            type="button"
            onClick={() => onChoose("resync_beats")}
            className="min-h-14 w-full rounded-xl bg-[#0c0c0e] px-4 text-left text-sm font-semibold text-white hover:opacity-85 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500"
          >
            Re-sync to beats
            <span className="mt-1 block text-[12px] font-normal text-white/70">
              Rebuild the cuts around the beats in this section.
            </span>
          </button>
        </div>
        <button
          type="button"
          className="mt-5 min-h-11 w-full text-sm text-[#71717a] hover:underline focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500"
          onClick={onCancel}
        >
          Cancel
        </button>
      </div>
    </div>
  );
}

function LightTopBar({
  dirty,
  saving,
  readOnly,
  saveState,
  showCopilotNotice,
  onBack,
  onOpenNova,
  onDismissCopilotNotice,
  onSave,
  orientationToggle,
}: {
  dirty: boolean;
  saving: boolean;
  readOnly: boolean;
  saveState: "idle" | "saving" | "conflict" | "error" | "partial";
  showCopilotNotice: boolean;
  onBack: () => void;
  onOpenNova: () => void;
  onDismissCopilotNotice: () => void;
  onSave: () => void;
  orientationToggle?: React.ReactNode;
}) {
  const copilotEnabled = process.env.NEXT_PUBLIC_EDIT_COPILOT_ENABLED === "true";
  return (
    <header className="flex items-center justify-between gap-2 border-b border-zinc-200 bg-white px-3">
      <button
        type="button"
        aria-label="Back to the video page"
        onClick={onBack}
        className="flex h-11 w-11 items-center justify-center rounded-full border border-zinc-200 pb-0.5 text-[15px] text-[#3f3f46] hover:border-zinc-400 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500"
      >
        ‹
      </button>
      <div className="min-w-0 flex-1 text-center">
        {showCopilotNotice ? (
          <div className="mx-auto flex max-w-[320px] items-center justify-center gap-2 rounded-lg border border-zinc-200 bg-white px-2 py-1 text-[11px] text-[#3f3f46]">
            <span className="truncate">
              Preview is close; Save renders exactly.
            </span>
            <button
              type="button"
              aria-label="Dismiss preview match note"
              onClick={onDismissCopilotNotice}
              className="min-h-8 px-1 text-[#71717a] hover:text-[#0c0c0e] focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500"
            >
              ✕
            </button>
          </div>
        ) : orientationToggle ? (
          <div className="flex justify-center">{orientationToggle}</div>
        ) : (
          <span className="text-[13px] font-semibold text-[#3f3f46]">Edit video</span>
        )}
      </div>
      {copilotEnabled && (
        <button
          type="button"
          aria-label="Open Nova"
          disabled={readOnly}
          onClick={onOpenNova}
          className="flex h-11 w-11 items-center justify-center rounded-lg border border-zinc-200 bg-white text-[15px] text-[#0c0c0e] hover:border-zinc-400 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500 disabled:cursor-not-allowed disabled:opacity-40"
        >
          ✧
        </button>
      )}
      <button
        type="button"
        disabled={!dirty || saving || readOnly}
        onClick={onSave}
        className="min-h-11 rounded-full bg-[#0c0c0e] px-4 text-[13px] font-semibold text-white hover:opacity-80 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500 disabled:opacity-40"
      >
        {saveState === "saving" ? "Saving..." : "Save"}
      </button>
    </header>
  );
}

function LightTransport({
  playing,
  currentTime,
  duration,
  onPlayPause,
  onScrub,
}: {
  playing: boolean;
  currentTime: number;
  duration: number;
  onPlayPause: () => void;
  onScrub: (seconds: number) => void;
}) {
  const safeDuration = Math.max(0, duration);
  const safeTime = Math.min(safeDuration || currentTime, Math.max(0, currentTime));
  return (
    <div className="border-t border-zinc-200 bg-white px-4 pb-[max(16px,env(safe-area-inset-bottom))] pt-3">
      <div className="mx-auto flex max-w-[720px] items-center gap-3">
        <button
          type="button"
          aria-label={playing ? "Pause video" : "Play video"}
          aria-pressed={playing}
          onClick={onPlayPause}
          className="flex h-11 w-11 flex-none items-center justify-center rounded-full bg-[#0c0c0e] text-[13px] text-white hover:opacity-80 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500"
        >
          {playing ? "❚❚" : "▶"}
        </button>
        <input
          type="range"
          aria-label="Scrub video"
          min={0}
          max={safeDuration || 0}
          step={0.1}
          value={safeDuration > 0 ? safeTime : 0}
          disabled={safeDuration <= 0}
          onChange={(e) => onScrub(Number(e.target.value))}
          className="h-11 min-w-0 flex-1 cursor-pointer accent-lime-500 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500 disabled:cursor-not-allowed disabled:opacity-40"
        />
        <span
          aria-label="Playback position"
          className="w-[92px] flex-none text-right text-[12px] tabular-nums text-[#3f3f46]"
        >
          {formatTimecode(currentTime)}{" "}
          <span className="text-[#a1a1aa]">/ {formatTimecode(duration)}</span>
        </span>
      </div>
    </div>
  );
}

function LightEditSheet({
  open,
  bar,
  sampleWord,
  appliedPresetId,
  saveState,
  saving,
  dirty,
  readOnly,
  onClose,
  onEditText,
  onPickPreset,
  onSave,
}: {
  open: boolean;
  bar: TextElementBar | null;
  sampleWord: string | null;
  appliedPresetId: string | null;
  saveState: "idle" | "saving" | "conflict" | "error" | "partial";
  saving: boolean;
  dirty: boolean;
  readOnly: boolean;
  onClose: () => void;
  onEditText: (text: string) => void;
  onPickPreset: (preset: TextPreset) => void;
  onSave: () => void;
}) {
  const trapRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  useFocusTrap(trapRef, open);

  useEffect(() => {
    if (!open) return;
    const id = window.requestAnimationFrame(() => {
      textareaRef.current?.focus({ preventScroll: true });
      textareaRef.current?.select();
    });
    return () => window.cancelAnimationFrame(id);
  }, [open, bar?.id]);

  if (!open || !bar) return null;

  return (
    <div
      ref={trapRef}
      role="dialog"
      aria-modal="true"
      aria-labelledby="light-edit-title"
      className="fixed inset-0 z-[90] flex flex-col bg-white"
    >
      <div className="flex items-center justify-between border-b border-zinc-200 px-4 py-3">
        <div>
          <h2 id="light-edit-title" className="font-display text-[18px] text-[#0c0c0e]">
            Edit text
          </h2>
          <p className="mt-0.5 text-[12px] text-[#71717a]">Full timeline editing on desktop</p>
        </div>
        <button
          type="button"
          aria-label="Close text editor"
          onClick={onClose}
          className="flex h-11 w-11 items-center justify-center rounded-lg text-[14px] text-[#71717a] hover:bg-zinc-100 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500"
        >
          ✕
        </button>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto px-4 py-5">
        <label className="block text-[12px] font-semibold text-[#3f3f46]" htmlFor="light-edit-textarea">
          Content
        </label>
        <textarea
          id="light-edit-textarea"
          ref={textareaRef}
          value={bar.text}
          readOnly={readOnly}
          onChange={(e) => onEditText(e.target.value)}
          rows={5}
          className="mt-2 w-full resize-none rounded-lg border border-zinc-200 px-3 py-3 text-[15px] text-[#0c0c0e] outline-none focus:border-lime-500 focus:ring-2 focus:ring-lime-500/25"
        />
        <p className="mb-3 mt-6 text-[12px] font-semibold text-[#3f3f46]">Presets</p>
        <PresetGrid
          presets={TEXT_PRESETS}
          sampleWord={sampleWord}
          appliedPresetId={appliedPresetId}
          onPick={onPickPreset}
        />
      </div>
      <div className="flex items-center justify-end gap-2 border-t border-zinc-200 px-4 pb-[max(16px,env(safe-area-inset-bottom))] pt-3">
        <button
          type="button"
          onClick={onClose}
          className="min-h-11 rounded-full px-4 text-[13px] font-semibold text-[#71717a] hover:underline focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500"
        >
          Close
        </button>
        <button
          type="button"
          disabled={!dirty || saving || readOnly}
          onClick={onSave}
          className="min-h-11 rounded-full bg-[#0c0c0e] px-6 text-[13px] font-semibold text-white hover:opacity-80 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500 disabled:opacity-40"
        >
          {saveState === "saving" ? "Saving..." : "Save"}
        </button>
      </div>
    </div>
  );
}

/** Chrome-less frame for loading / error / auth states (keeps the shell's
 * grid footprint so the transition to the loaded editor doesn't jump). */
function Frame({ children }: { children: React.ReactNode }) {
  return (
    <div className="fixed inset-0 z-50 flex flex-col overflow-hidden bg-[#fafaf8]">
      <div className="h-14 flex-none border-b border-zinc-200 bg-white" />
      {children}
    </div>
  );
}
