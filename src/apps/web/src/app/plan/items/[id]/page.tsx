"use client";

import Link from "next/link";
import { ArrowLeft } from "lucide-react";
import { useParams, useSearchParams } from "next/navigation";
import {
  type Dispatch,
  type ReactNode,
  type SetStateAction,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { hasRenderRegistered } from "./render-registration";
import {
  attachClips,
  changePlanItemStyle,
  dismissConformance,
  editPlanItemVariant,
  generatePlanItem,
  getPlanItem,
  getPlanItemFresh,
  getPlanItemJobStatus,
  getPlanItemJobStatusFresh,
  NotAuthenticatedError,
  setClipNote,
  setItemVoiceover,
  setPlanItemCaptionLanguage,
  updatePlanItem,
  type ClipAssignment,
  type ConformanceVerdict,
  type EditorCapabilities,
  type PlanItem,
  type PlanItemJobStatus,
  type PlanItemVariant,
  type MontagePreset,
  requestUploadUrls,
  retextPlanItem,
  setPlanItemIntroSize,
  swapPlanItemSong,
  uploadContentTypeForFile,
  uploadToGcs,
  uploadToGcsWithProgress,
  uploadMediaOverlayFiles,
  setVariantMediaOverlays,
  listPoolAssets,
  type PoolAsset,
  type MediaOverlay,
  requestSfxUploadUrls,
  setVariantSoundEffects,
  renderVariantSfx,
  getSfxAudioUrl,
  putTextElements,
  type SoundEffectPlacement,
  type TextElement,
  type CaptionCue,
  setPlanItemCaptions,
  applyPlanItemCaptions,
  setPlanItemIntroTiming,
  patchPlanItemSceneTiming,
  type SceneTimingPatch,
} from "@/lib/plan-api";
import { buildPromotedAssignments } from "@/lib/plan-clip-promotion";
import {
  FINISHING_UPLOAD_HINT,
  generateGate,
  narrationFallbackBanner,
} from "@/lib/plan-generate-gate";
import { useSfxPreview } from "../../_components/useSfxPreview";
import { resolveSfxPreviewUrls, sfxUrlKey } from "@/lib/sfx-preview-urls";
import { VoiceRecorder } from "../../../generative/VoiceRecorder";
import ShotSlotUploader, { ClipNoteControl } from "./components/ShotSlotUploader";
import AskKriaPanel from "./components/AskKriaPanel";
import { STYLE_TILES, TYPE_COPY } from "./components/SetupPicker";
import {
  getGenerativeStyleSets,
  type GenerativeStyleSet,
  isGenerativeJobSettled,
  uploadOwnedVoiceover,
} from "@/lib/generative-api";
import { getMusicTracks, type MusicTrackSummary } from "@/lib/music-api";
import { FONT_FACES } from "@/lib/font-faces";
import { downloadVideo } from "@/lib/download-video";
import { sfxNeedsBake, sfxPersistDirty } from "@/lib/sfx-dirty";
import {
  removeOverlayEffectGroup,
  type OverlayEffectState,
} from "@/lib/overlay-effect-groups";
import { variantFailureCopy, unplacedShotCopy } from "@/lib/variant-failure-copy";
import { jobFailureCopy } from "@/lib/job-failure-copy";
import { stripRationalePrefix } from "@/lib/plan-text";
import { GENERATIVE_PHASE_ORDER, GENERATIVE_PHASE_LABEL } from "@/lib/job-phases";
import { BeamLoader, ProgressTheater } from "@/components/progress";
import { deriveReceiptText, formatElapsed } from "@/components/progress/logic";
import { StableVideo } from "@/components/StableVideo";
import { usePolledJobStatus } from "@/hooks/usePolledJobStatus";
import { LightShell } from "@/components/ui/LightShell";
import { InkButton } from "@/components/ui/InkButton";
import { InfoDot } from "@/components/ui/InfoDot";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { Card, CardContent, CardFooter, CardHeader } from "@/components/ui/card";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Separator } from "@/components/ui/separator";
import { Dropzone } from "@/components/ui/dropzone";
import AssetPool from "../../_components/AssetPool";
import SuggestionRail from "../../_components/SuggestionRail";
import HeroOverlayEditor from "../../_components/HeroOverlayEditor";
import LiveOverlayCardsLayer from "../../_components/LiveOverlayCardsLayer";
import CaptionEditor from "../../_components/CaptionEditor";
import BackgroundSoundControl from "../../_components/BackgroundSoundControl";
import PlanVariantEditor from "../../_components/PlanVariantEditor";
import SignInPrompt from "../../_components/SignInPrompt";
import UnifiedTimeline from "../../_components/UnifiedTimeline";
import { computeIntroTextWindow } from "../../_components/introTextWindow";
import type { SuggestionLaneEntry } from "../../_components/UnifiedTimelineTypes";
import { useOverlaySuggestionState } from "../../_components/useOverlaySuggestions";
import { usePoolAssetUploader } from "../../_hooks/usePoolAssetUploader";
import { InlineClipsEditor } from "../../_components/InlineClipsEditor";
import { useClipTimeline } from "../../_components/useClipTimeline";
import { getSoundEffects, type SoundEffectSummary } from "@/lib/sfx-api";
import type { TextElementBar } from "@/lib/timeline/text-timeline-reducer";
import { barsToTextElements, seedBarsFromVariant } from "./_editor/editor-bars";
import { planItemEditorDisabledReason } from "./_editor/editor-capabilities";
import {
  useVariantEditSession,
  type VariantEditSession,
} from "@/lib/variant-editor/useVariantEditSession";
import {
  isCaptionArchetype,
  isInstantEditEligible,
  isTextLaneEligible,
} from "@/lib/variant-editor/eligibility";
import { IntroTextPreview } from "@/components/variant-editor/IntroTextPreview";
import { resolveIntroParams } from "@/components/variant-editor/resolve-intro-params";
import { EditToolbar } from "@/components/variant-editor/EditToolbar";
import type { EditDraft } from "@/lib/variant-editor/useVariantEditSession";
import {
  parsePlanItemEditorReturnSignal,
  stripPlanItemEditorReturnParams,
} from "@/lib/editor-return";
import {
  needsFormatPersist,
  resolvePickerFormat,
  type PickerEditFormat,
} from "@/lib/edit-format";
import TextElementOverlayLayer from "./components/TextElementOverlayLayer";
import EditProposalCard from "./components/EditProposalCard";
import PlanThreadPanel from "./components/PlanThreadPanel";
import MainCreatorAgentPanel from "./components/MainCreatorAgentPanel";
import { TikTokPublishDialog } from "@/components/TikTokPublishDialog";
import { TikTokReleaseRail } from "@/components/TikTokReleaseRail";
import {
  getTikTokConnection,
  getTikTokPublication,
  getTikTokPublicationReceipt,
  listTikTokPublications,
  shouldPollTikTokPublication,
  startTikTokOAuth,
  type TikTokConnection,
  type TikTokPublication,
} from "@/lib/tiktok-api";

// How long a dispatched render may take to register its Job before we admit
// failure. Plan-item renders are queued behind a single worker, and the Job row
// is minted when that worker picks the task up; a real render can sit queued for
// several minutes before current_job_id appears.
const RENDER_REGISTER_TIMEOUT_MS = 15 * 60_000;

// Kill-switch: overlays tab only appears when NEXT_PUBLIC_MEDIA_OVERLAYS_ENABLED=true.
// Normalise: accept "true", "True", "TRUE", "1" and trim whitespace so a
// near-miss Vercel value ("True", trailing space) doesn't silently hide the tab.
const _mediaOverlaysRaw = (process.env.NEXT_PUBLIC_MEDIA_OVERLAYS_ENABLED ?? "").trim();
const MEDIA_OVERLAYS_ENABLED =
  _mediaOverlaysRaw.toLowerCase() === "true" || _mediaOverlaysRaw === "1";
const SOUND_EFFECTS_ENABLED =
  process.env.NEXT_PUBLIC_SOUND_EFFECTS_ENABLED === "true";
// R2 (review C8): version-skew guard for the manual fullscreen-cutaway toggle.
// New web + OLD api (Vercel auto-deploys on merge; Fly is manual/CD and can lag)
// = the api's MediaOverlay model has no display_mode field and Pydantic
// extra="ignore" silently strips it → a previewed fullscreen bakes as pip. This
// is the WEB TWIN of the api's FULLSCREEN_CUTAWAYS_ENABLED. Keep it FALSE in
// Vercel until the Fly deploy carrying display_mode is confirmed live, then flip
// Vercel AFTER Fly (never before). When off, the NEW promote affordances hide;
// pip editing and EXISTING fullscreen cards from the API still work/render.
const FULLSCREEN_CUTAWAYS_ENABLED =
  process.env.NEXT_PUBLIC_FULLSCREEN_CUTAWAYS_ENABLED === "true";
// Kill-switch: the "Talking to camera" edit-style card (edit_format="subtitled")
// only appears when NEXT_PUBLIC_SUBTITLED_ENABLED=true. Keep in sync with the
// backend `subtitled_archetype_enabled` Fly secret — if the card shows but the
// backend flag is off, a subtitled job silently falls back to montage. Same
// normalize as MEDIA_OVERLAYS so a near-miss Vercel value ("True", trailing
// space) still works.
const _subtitledRaw = (process.env.NEXT_PUBLIC_SUBTITLED_ENABLED ?? "").trim();
const SUBTITLED_ENABLED = _subtitledRaw.toLowerCase() === "true" || _subtitledRaw === "1";
// Kill-switch: the item-page "Edit" entry into the full-screen TikTok-style
// editor shell (/plan/items/[id]/edit) only appears when
// NEXT_PUBLIC_TIKTOK_EDITOR_ENABLED=true. Frontend-only gate — the shell route
// and the server's editor_capabilities are unconditionally present; this flag
// only controls whether the entry point is shown.
const TIKTOK_EDITOR_ENABLED = process.env.NEXT_PUBLIC_TIKTOK_EDITOR_ENABLED === "true";
const GUIDED_EDIT_ENABLED = process.env.NEXT_PUBLIC_GUIDED_EDIT_ENABLED === "true";
const MAIN_CREATOR_AGENT_ENABLED =
  process.env.NEXT_PUBLIC_MAIN_CREATOR_AGENT_ENABLED === "true";

const RENDER_REGISTER_ERROR = "The render didn't register — give it another go.";
const TIKTOK_POLL_MAX_FAILURES = 3;
const TIKTOK_POLL_INTERVAL_MS = 5_000;
type PendingEdit = {
  priorFinishedAt: string | null;
  sawRendering: boolean;
  targetGeneration?: string | null;
};

// Edit-style picker copy lives in components/SetupPicker (TYPE_COPY). NOTE:
// "Talking to camera" there is a DIFFERENT namespace than
// persona.footage_type_bias="talking_head".

// Shared by the interactive Fit/Fill toggle (pre-render) and the read-only
// applied-fit display (post-render).
const LANDSCAPE_FIT_OPTIONS: { value: "fit" | "fill"; label: string; desc: string }[] = [
  { value: "fit",  label: "Fit",  desc: "Keep horizontal, black bars top & bottom" },
  { value: "fill", label: "Fill", desc: "Crop to fill the vertical frame" },
];

const COLLAGE_MONTAGE_PRESETS = new Set<MontagePreset>(["masonry", "polaroid_wall"]);


const VIDEO_UPLOAD_ACCEPT = "video/mp4,video/quicktime";
const MAX_CLIPS_PER_ITEM = 50;
const AUDIO_UPLOAD_ACCEPT = "audio/*,.mp3,.m4a,.mp4,.wav,.webm,.ogg,.aac";
const NARRATED_READY_UPLOAD_ACCEPT = `${VIDEO_UPLOAD_ACCEPT},${AUDIO_UPLOAD_ACCEPT}`;
const MASONRY_UPLOAD_ACCEPT = `${VIDEO_UPLOAD_ACCEPT},image/jpeg,image/png,image/webp,image/heic,image/heif`;
const AUDIO_UPLOAD_EXTENSIONS = new Set([".mp3", ".m4a", ".wav", ".webm", ".ogg", ".aac"]);
const AUDIO_ONLY_PROBE_EXTENSIONS = new Set([".mp4", ".m4v", ".mov"]);

function clipUploadErrorMessage(
  error: unknown,
  fallback = "We couldn't add this video. Try again.",
): string {
  if (error instanceof Error && error.name === "ClipAttachTimeoutError") {
    return error.message;
  }
  if (typeof error !== "object" || error === null) return fallback;
  const capacity = error as { code?: unknown; limit?: unknown; remaining?: unknown };
  if (capacity.code !== "clip_upload_limit_exceeded") return fallback;
  const limit = typeof capacity.limit === "number" ? capacity.limit : MAX_CLIPS_PER_ITEM;
  const remaining = typeof capacity.remaining === "number" ? Math.max(0, capacity.remaining) : 0;
  return remaining > 0
    ? `You can add up to ${remaining} more clip(s).`
    : `You've reached the clip limit (${limit}). Remove a clip to add more.`;
}

// Content-type resolution lives in plan-api's uploadContentTypeForFile — the
// SAME function signs the URL and sets the PUT header, so the two can never
// drift into a GCS 403 SignatureDoesNotMatch.

// One optimistic card per in-flight pool upload. Cancelled entries are removed
// from state (never kept); error entries stay until dismissed or retried.
type PendingClipUpload = {
  localId: string;
  file: File;
  filename: string;
  /** Picker ordinal (global, monotonic) — attaches commit in THIS order, not
   * network completion order: narrated-ready maps narration to clips by
   * insertion order, so completion-order attaches would scramble the story. */
  order: number;
  /** Set once the signed URL is minted; the card survives as "Saving…" until
   * the server's clip list contains this path (no maxClips re-enable gap). */
  gcsPath: string | null;
  /** 0–1, driven by real XHR byte events (state updates quantized to whole %). */
  progress: number;
  /** True while the relay fallback runs — no byte events, so no percent (D6). */
  indeterminate: boolean;
  status: "uploading" | "saving" | "error";
  error: string | null;
  abortController: AbortController;
};

// Payload shape attachClips expects; clipAssignmentsRef holds this so queued
// attach ops can rebuild the full list without the render-scope `item` closure.
type AttachAssignment = {
  gcs_path: string;
  shot_id: ClipAssignment["shot_id"];
  user_note: string;
};

let poolUploadSeq = 0;

// A committed upload must not leave the creation flow in an endless
// "Saving…" state when the attach request is stalled by a sleeping API, a
// dropped connection, or a proxy timeout. Retrying sends the complete current
// assignment set, so a late/partially committed request remains idempotent.
const CLIP_ATTACH_TIMEOUT_MS = 30_000;
const CLIP_ATTACH_TIMEOUT_MESSAGE = "Saving this clip is taking too long. Retry to continue.";

async function attachClipAssignments(
  itemId: string,
  clipGcsPaths: string[],
  assignments: AttachAssignment[],
): Promise<void> {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), CLIP_ATTACH_TIMEOUT_MS);
  try {
    await attachClips(itemId, clipGcsPaths, assignments, controller.signal);
  } catch (error) {
    if (controller.signal.aborted) {
      const timeoutError = new Error(CLIP_ATTACH_TIMEOUT_MESSAGE);
      timeoutError.name = "ClipAttachTimeoutError";
      throw timeoutError;
    }
    throw error;
  } finally {
    clearTimeout(timeout);
  }
}

// At most 3 concurrent clip PUTs — mobile bandwidth/memory gate, same pattern
// as MAX_DIRECT_UPLOADS in generative-api.ts (2 there; 3 here since plan
// uploads are fewer, larger files). Module-scoped and dumb on purpose:
// re-entrant batches (user adds more clips mid-upload) share the same slots.
const MAX_POOL_UPLOADS = 3;
let poolUploadActive = 0;
const poolUploadWaiters: Array<() => void> = [];

async function acquirePoolUploadSlot(): Promise<() => void> {
  while (poolUploadActive >= MAX_POOL_UPLOADS) {
    await new Promise<void>((resolve) => poolUploadWaiters.push(resolve));
  }
  poolUploadActive += 1;
  let released = false;
  return () => {
    if (released) return;
    released = true;
    poolUploadActive -= 1;
    poolUploadWaiters.shift()?.();
  };
}

function fileExtension(file: File): string {
  const name = file.name.toLowerCase();
  const dot = name.lastIndexOf(".");
  return dot >= 0 ? name.slice(dot) : "";
}

function isKnownAudioUpload(file: File): boolean {
  const type = (file.type || "").toLowerCase();
  if (type.startsWith("audio/")) return true;
  return AUDIO_UPLOAD_EXTENSIONS.has(fileExtension(file));
}

function canProbeForMissingVideoTrack(file: File): boolean {
  const type = (file.type || "").toLowerCase();
  return (
    AUDIO_ONLY_PROBE_EXTENSIONS.has(fileExtension(file)) ||
    type === "video/mp4" ||
    type === "video/quicktime"
  );
}

function probeHasVideoTrack(file: File): Promise<boolean | null> {
  if (typeof document === "undefined" || typeof URL === "undefined" || !URL.createObjectURL) {
    return Promise.resolve(null);
  }
  return new Promise((resolve) => {
    const video = document.createElement("video");
    const objectUrl = URL.createObjectURL(file);
    let settled = false;
    let timer: ReturnType<typeof setTimeout> | null = null;

    const finish = (value: boolean | null) => {
      if (settled) return;
      settled = true;
      if (timer) clearTimeout(timer);
      video.onloadedmetadata = null;
      video.onerror = null;
      URL.revokeObjectURL(objectUrl);
      video.removeAttribute("src");
      try {
        video.load();
      } catch {
        // Some test/browser environments do not implement load() for blob URLs.
      }
      resolve(value);
    };

    video.preload = "metadata";
    video.muted = true;
    video.onloadedmetadata = () => finish(video.videoWidth > 0 || video.videoHeight > 0);
    video.onerror = () => finish(null);
    timer = setTimeout(() => finish(null), 500);
    video.src = objectUrl;
  });
}

async function shouldRouteToNarratedVoiceover(file: File): Promise<boolean> {
  if (isKnownAudioUpload(file)) return true;
  if (!canProbeForMissingVideoTrack(file)) return false;
  const hasVideoTrack = await probeHasVideoTrack(file);
  return hasVideoTrack === false;
}

async function splitNarratedReadyUploads(files: File[]): Promise<{
  voiceoverFiles: File[];
  clipFiles: File[];
}> {
  const decisions = await Promise.all(files.map((file) => shouldRouteToNarratedVoiceover(file)));
  return files.reduce(
    (acc, file, idx) => {
      if (decisions[idx]) acc.voiceoverFiles.push(file);
      else acc.clipFiles.push(file);
      return acc;
    },
    { voiceoverFiles: [] as File[], clipFiles: [] as File[] },
  );
}


function CompactPlanSummary({ item }: { item: PlanItem }) {
  const shots = item.filming_guide ?? [];
  if (shots.length === 0) return null;
  return (
    <div className="mb-4 rounded-xl border border-zinc-200 bg-white p-4">
      <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-[#71717a]">
        Plan summary
      </p>
      {item.filming_suggestion && (
        <p className="mt-1 text-sm text-[#3f3f46]">{item.filming_suggestion}</p>
      )}
      <ol className="mt-3 space-y-2">
        {shots.map((shot, index) => (
          <li key={shot.shot_id ?? `${shot.what}-${index}`} className="flex gap-2">
            <span className="font-display text-[15px] italic text-zinc-300">
              {index + 1}.
            </span>
            <div className="min-w-0 flex-1">
              <p className="text-sm font-medium text-[#0c0c0e]">{shot.what}</p>
              {shot.how && <p className="text-xs text-[#71717a]">{shot.how}</p>}
            </div>
          </li>
        ))}
      </ol>
    </div>
  );
}

// Reads each file's video dimensions via a detached <video> element and resolves
// true iff ANY is landscape (width > height). Fails safe (resolves false, never
// rejects/throws) on metadata timeout or an unsupported codec — the caller's
// default ("fit") is already correct either way, so a missed detection just means
// the Fit/Fill picker stays hidden, not a broken render. Not covered: clips
// attached via ShotSlotUploader (per-slot uploads with no File object reaching
// this page) — only the PoolUploadCard-based flows (narrated_ready, talking-to-
// camera, existing_footage) funnel through handleFiles today.
function detectLandscapeClip(files: File[]): Promise<boolean> {
  const checks = files.filter((file) => uploadContentTypeForFile(file).startsWith("video/")).map(
    (file) =>
      new Promise<boolean>((resolve) => {
        const video = document.createElement("video");
        const url = URL.createObjectURL(file);
        const cleanup = () => URL.revokeObjectURL(url);
        const timer = setTimeout(() => {
          cleanup();
          resolve(false);
        }, 5000);
        video.preload = "metadata";
        video.onloadedmetadata = () => {
          clearTimeout(timer);
          cleanup();
          resolve(video.videoWidth > video.videoHeight);
        };
        video.onerror = () => {
          clearTimeout(timer);
          cleanup();
          resolve(false);
        };
        video.src = url;
      }),
  );
  return Promise.all(checks).then((results) => results.some(Boolean));
}


export default function PlanItemPage() {
  const params = useParams<{ id: string }>();
  const searchParams = useSearchParams();
  const requestedTab = searchParams.get("tab") === "captions" ? "captions" : null;
  const tiktokPreview = searchParams.get("tiktok_preview");
  // Keep the approved connected-account flow visible on ordinary localhost
  // item URLs. An explicit query still lets us exercise either state, while
  // production always follows the account returned by the TikTok API.
  const tiktokSimulation =
    process.env.NODE_ENV !== "production" &&
    (tiktokPreview === "connected" ||
      (process.env.NODE_ENV === "development" && tiktokPreview == null));
  const itemId = params.id;
  const editorReturnSignal = useMemo(
    () =>
      TIKTOK_EDITOR_ENABLED
        ? parsePlanItemEditorReturnSignal(searchParams)
        : null,
    [searchParams],
  );

  const [loading, setLoading] = useState(true);
  const [needsAuth, setNeedsAuth] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [uploading, setUploading] = useState(false);
  // Pool uploads in flight: one optimistic card each. Page-level because only
  // ONE PoolUploadCard renders at a time (the call sites are mutually
  // exclusive branches of a single conditional).
  const [pendingClipUploads, setPendingClipUploads] = useState<PendingClipUpload[]>([]);
  // Cancel clicked after the PUT already completed → exclude from attach.
  const cancelledUploadIds = useRef<Set<string>>(new Set());
  // Serialised attach queue: upload-attaches and deletes run through here one
  // at a time so concurrent writers compose instead of clobbering (same idea
  // as ShotSlotUploader's attachQueue).
  const attachQueue = useRef<Promise<void>>(Promise.resolve());
  const attachOpsInFlight = useRef(0);
  // Last time an attach op settled — the ref-sync effect ignores poll data for
  // a short grace window after a write, so a slow GET dispatched BEFORE the
  // POST can't clobber the ref with pre-write assignments after ops hit 0.
  const lastAttachSettledAt = useRef(0);
  // Freshest known assignments; queued attach ops read this at EXECUTION time,
  // never the render-scope `item` closure.
  const clipAssignmentsRef = useRef<AttachAssignment[]>([]);
  // Completed uploads waiting for the next attach drain. Buffering + a single
  // drain per queue turn coalesces N finished files into ONE attachClips POST
  // (one conformance analysis, one refetch) while never holding an earlier
  // success hostage to a slower sibling.
  const pendingAttachAdds = useRef<
    Array<{ order: number; localId: string; assignment: AttachAssignment }>
  >([]);
  const attachDrainScheduled = useRef(false);
  // gcs_path → picker ordinal for clips attached by THIS session; lets a drain
  // insert a slower early pick BEFORE an already-attached later pick.
  const uploadOrdinalRef = useRef<Map<string, number>>(new Map());
  // Landscape auto-detect: the Fit/Fill picker only appears once a wide clip is
  // detected on upload — hidden by default (the common case is portrait clips).
  // Sticky within the session (never resets to false) so it doesn't flicker if a
  // later upload/removal changes the pool; detection failure (metadata never
  // loads, unsupported codec) fails safe by simply not setting it — "fit" is
  // already the backend's safe default either way.
  const [hasLandscapeClip, setHasLandscapeClip] = useState(false);
  const [generating, setGenerating] = useState(false);
  // uploaderBusy: true while ShotSlotUploader has any upload/commit in flight (D6).
  const [uploaderBusy, setUploaderBusy] = useState(false);
  // Idea-centric: propose-only AI plan state.
  const [tracks, setTracks] = useState<MusicTrackSummary[]>([]);
  const [styleSets, setStyleSets] = useState<GenerativeStyleSet[]>([]);
  const [focusedVariantId, setFocusedVariantId] = useState<string | null>(null);
  // 006 T3 (005-4A lane rendering): overlay-suggestion working state, lifted
  // here so SuggestionRail (review index) and the timeline lanes (editable
  // provenance cards) share ONE envelope set. Lane edits patch the envelopes
  // and implicitly stage the row; only the rail's Apply hits the network.
  const overlaySuggestions = useOverlaySuggestionState();
  // 007 Fix 2: signed pool-asset thumbnails for the hero direct-manipulation
  // cards. Fetched once when suggestions exist (the rail/AssetPool keep their
  // own copies internal); join rows' asset_id → display_url keyed by the
  // embedded overlay's src_gcs_path so HeroOverlayEditor can resolve by overlay.
  const autoplaceEnabled = process.env.NEXT_PUBLIC_OVERLAY_AUTOPLACE_ENABLED === "true";
  const [suggestionPoolAssets, setSuggestionPoolAssets] = useState<PoolAsset[]>([]);
  // Live-mirrors AssetPool's own list (fetch/poll/register/delete/promote) —
  // lets the Generate gate see a ready pool video without a second fetch.
  // Only relevant when guided-edit auto-design is available (P2-5, 2026-08-18
  // adversarial review); see guidedEditAutoDesign below.
  const [poolAssets, setPoolAssets] = useState<PoolAsset[]>([]);
  const hasSuggestionRows = overlaySuggestions.rows.length > 0;
  useEffect(() => {
    if (!autoplaceEnabled || !hasSuggestionRows) return;
    let cancelled = false;
    listPoolAssets(itemId)
      .then((res) => {
        if (!cancelled) setSuggestionPoolAssets(res.assets);
      })
      .catch(() => {
        // Thumbnails are progressive enhancement — cards fall back to a
        // placeholder block; the gestures themselves never need the URL.
      });
    return () => {
      cancelled = true;
    };
  }, [autoplaceEnabled, hasSuggestionRows, itemId]);
  const suggestionAssetUrlBySrcPath = useMemo(() => {
    const assetById = new Map(suggestionPoolAssets.map((a) => [a.id, a]));
    const map = new Map<string, string>();
    for (const row of overlaySuggestions.rows) {
      const url = assetById.get(row.asset_id)?.display_url;
      if (url) map.set(row.overlay.src_gcs_path, url);
    }
    return map;
  }, [suggestionPoolAssets, overlaySuggestions.rows]);
  const resolveSuggestionAssetUrl = useCallback(
    (overlay: MediaOverlay): string | undefined =>
      suggestionAssetUrlBySrcPath.get(overlay.src_gcs_path) ?? overlay.preview_url ?? undefined,
    [suggestionAssetUrlBySrcPath],
  );
  // 009 T5: aspect/pixel metadata for the fullscreen crop/low-res popover
  // warnings — same asset_id → overlay.src_gcs_path join as the URL map above
  // (the pool response carries no gcs_path, so the suggestion rows are the
  // bridge). Missing assets/fields stay undefined — warnings suppress, never fake.
  const suggestionAssetMetaBySrcPath = useMemo(() => {
    const assetById = new Map(suggestionPoolAssets.map((a) => [a.id, a]));
    const map = new Map<string, { aspect?: number; width?: number; height?: number }>();
    for (const row of overlaySuggestions.rows) {
      const asset = assetById.get(row.asset_id);
      if (!asset) continue;
      map.set(row.overlay.src_gcs_path, {
        aspect: asset.aspect ?? undefined,
        width: asset.width ?? undefined,
        height: asset.height ?? undefined,
      });
    }
    return map;
  }, [suggestionPoolAssets, overlaySuggestions.rows]);
  const resolveAssetMeta = useCallback(
    (srcGcsPath: string) => suggestionAssetMetaBySrcPath.get(srcGcsPath),
    [suggestionAssetMetaBySrcPath],
  );
  // Ask Kria advisor panel: closed | opened normally | opened via "Tell Kria".
  const [askKria, setAskKria] = useState<null | "default" | "contest">(null);
  const pendingEdits = useRef<Map<string, PendingEdit>>(new Map());
  // Incremented whenever pendingEdits is mutated so the variants memo re-runs
  // immediately (useMemo only tracks reactive dependencies; the ref itself is not reactive).
  const [editGeneration, setEditGeneration] = useState(0);
  // Tracks what kind of edit is in-flight for the focused variant so the Hero
  // overlay can show a meaningful label ("Applying your new song…" vs "Updating text…").
  const renderingAction = useRef<{ type: "song" | "text" | "style" | "other"; label: string } | null>(null);
  // Transient "✓ Updated" cue: set to the variantId for 4s when render_finished_at advances.
  const [updatedVariantId, setUpdatedVariantId] = useState<string | null>(null);
  // Narrated-walkthrough: local shadow of voiceover_gcs_path — updated optimistically
  // when VoiceRecorder fires onVoiceover; reset from item on refetch.
  const [voiceoverGcsPath, setVoiceoverGcsPath] = useState<string | null>(null);
  const [voiceoverSaving, setVoiceoverSaving] = useState(false);
  // VoiceRecorder invokes its callback after the blob upload, but the plan-item
  // attachment is a second async request owned by this page. Keep that request
  // visible to Generate so a click cannot enqueue a job from the pre-voiceover
  // item snapshot (the local path is intentionally optimistic).
  const voiceoverSavePromiseRef = useRef<Promise<boolean> | null>(null);
  const voiceoverSaveQueueRef = useRef<Promise<boolean>>(Promise.resolve(true));
  const voiceoverSaveGenerationRef = useRef(0);
  // Read-only shadow of item.audio_mode — still drives post-generation variant
  // focus (original_text). The choose-audio UI was removed in the per-type
  // setup redesign; type changes go through the setup receipt only.
  const [audioPreference, setAudioPreference] = useState<"kria" | "original" | "voiceover">("kria");
  // Guided-edit conversation with Kria now lives in a Sheet (PlanThreadPanel)
  // instead of morphing the setup zone inline (DESIGN.md §12).
  const [threadOpen, setThreadOpen] = useState(false);
  // Conformance polling: keep fetching for up to 3 extra cycles after clips are attached
  // so the verdict panel appears shortly after the async agent finishes (~6s window).
  const conformancePolls = useRef(0);
  // Render-start window: POST /generate dispatches a Celery task that mints the
  // Job AFTER the response — keep polling until current_job_id appears, or the
  // first click silently "does nothing" (dogfood). Time-based, not poll-count:
  // a busy worker can take >12s to pick the task up (second dogfood round: the
  // count-based window expired, showed the error, THEN the render started).
  const awaitingJobSince = useRef<number | null>(null);
  // Snapshot of current_job_id at the moment Generate was clicked — see
  // hasRenderRegistered() above for why this is needed on a retry.
  const jobIdBeforeGenerateRef = useRef<string | null>(null);
  const forceFreshFetchRef = useRef(false);
  const consumedEditorReturnRef = useRef<string | null>(null);

  useEffect(() => {
    getMusicTracks()
      .then((r) => setTracks(r.tracks))
      .catch(() => setTracks([]));
    getGenerativeStyleSets()
      .then(setStyleSets)
      .catch(() => setStyleSets([]));
  }, []);

  const fetcher = useCallback(async () => {
    const forceFresh = forceFreshFetchRef.current;
    forceFreshFetchRef.current = false;
    const it = await (forceFresh ? getPlanItemFresh : getPlanItem)(itemId);
    const jobSt = it.current_job_id
      ? await (forceFresh ? getPlanItemJobStatusFresh : getPlanItemJobStatus)(
          it.current_job_id,
        )
      : null;
    return { item: it, job: jobSt };
  }, [itemId]);

  const isTerminalFn = useCallback(
    ({ item, job }: { item: PlanItem; job: PlanItemJobStatus | null }) => {
      // Plan 007 (CRITICAL-2): the zero-click autoplace chain (match → burn)
      // runs server-side AFTER variants_ready. Keep polling while any variant
      // is mid-match, so the auto-applied result (and the hydration effect)
      // is never invisible until a manual reload.
      const anyAutoMatching =
        job?.variants?.some((v) => v.overlay_suggest_status === "matching") ?? false;
      const pending = pendingEdits.current;
      // `isGenerativeJobSettled` owns the three-way rule (not-terminal /
      // failed-terminal wins / success-terminal yields to a genuinely live
      // variant, bounded so a dead render can't spin forever). Shared with the
      // public generative page and the onboarding EditPayoff panel — this used to
      // be hand-rolled per surface and drifted.
      //
      // The old all-terminal check made a live re-render look terminal; it only
      // kept polling because `pendingEdits` happened to be non-empty, which it is
      // NOT after a reload mid-render or when the render came from the pocket editor.
      const jobSettled = isGenerativeJobSettled(job?.status, job?.variants);
      // A manual draft is intentionally not a terminal generative Job status,
      // but an idle draft has nothing to poll. Resume polling only while its
      // first export (or a retry) is actively rendering.
      const manualDraftIdle =
        item.status === "draft" &&
        !(job?.variants?.some((variant) => variant.render_status === "rendering") ?? false);
      const baseTerminal =
        (jobSettled || manualDraftIdle) &&
        !anyAutoMatching &&
        pending.size === 0 &&
        item.status !== "generating" &&
        !(
          item.current_job_id &&
          item.status !== "ready" &&
          item.status !== "failed" &&
          item.status !== "draft"
        );

      if (
        GUIDED_EDIT_ENABLED &&
        item.guided_edit_available === true &&
        item.edit_proposal?.guidance?.state === "awaiting_direction_confirmation"
      ) {
        // A legacy automatic attempt can be paused at this checkpoint. On a
        // reload there is no task to poll yet, so let Generate resume it. Once
        // the user has clicked Generate, awaitingJobSince keeps polling until
        // the compatibility bridge moves it into active design work.
        return item.guided_edit_auto_design !== true || awaitingJobSince.current === null;
      }

      if (
        GUIDED_EDIT_ENABLED &&
        item.guided_edit_available === true &&
        (item.edit_proposal?.status === "analyzing" ||
          item.edit_proposal?.status === "drafting" ||
          (item.edit_proposal?.status === "briefing" &&
            item.guided_edit_auto_design === true) ||
          item.edit_proposal?.conversation_in_progress === true)
      ) {
        return false;
      }

      // Keep polling while a just-dispatched render hasn't minted its Job yet.
      // Uses hasRenderRegistered(), not a bare current_job_id check — on a
      // retry, current_job_id already points at the OLD (terminal) job
      // before this click, so raw truthiness would end the wait instantly.
      if (hasRenderRegistered(item, jobIdBeforeGenerateRef.current)) {
        awaitingJobSince.current = null;
      } else if (
        awaitingJobSince.current !== null &&
        Date.now() - awaitingJobSince.current < RENDER_REGISTER_TIMEOUT_MS
      ) {
        return false;
      }

      // Keep polling for up to 3 extra cycles when the item has clips but no
      // conformance verdict yet (the async task may still be running).
      const hasClips = (item.clip_gcs_paths?.length ?? 0) > 0;
      const hasFilmingGuide = (item.filming_guide?.length ?? 0) > 0;
      // Gate on the absence of a VERDICT, not the conformance object — after a
      // note edit the carry-over stub ({contested:true}, no verdict) is truthy,
      // so the old `!item.conformance` check never resumed polling and the
      // re-read never appeared (review finding).
      const awaitingConformance =
        hasClips && hasFilmingGuide && !item.conformance?.verdict && conformancePolls.current < 3;
      if (awaitingConformance) {
        conformancePolls.current += 1;
        return false;
      }
      return baseTerminal;
    },
    [],
  );

  const {
    data,
    error: pollError,
    refetch,
    applyData,
  } = usePolledJobStatus(fetcher, undefined, isTerminalFn);

  useEffect(() => {
    if (!TIKTOK_EDITOR_ENABLED || editorReturnSignal === null) return;
    if (consumedEditorReturnRef.current === editorReturnSignal.key) return;
    consumedEditorReturnRef.current = editorReturnSignal.key;
    forceFreshFetchRef.current = true;
    setFocusedVariantId(editorReturnSignal.variantId);
    setError(null);

    if (editorReturnSignal.renderStarted) {
      const existing = pendingEdits.current.get(editorReturnSignal.variantId);
      pendingEdits.current.set(editorReturnSignal.variantId, {
        priorFinishedAt: editorReturnSignal.priorFinishedAt,
        sawRendering: existing?.sawRendering ?? false,
        targetGeneration: editorReturnSignal.generation,
      });
      renderingAction.current = { type: "other", label: "Rendering your saved edits…" };
      setEditGeneration((g) => g + 1);
    }

    refetch();

    const nextSearch = stripPlanItemEditorReturnParams(window.location.search);
    window.history.replaceState(
      window.history.state,
      "",
      `${window.location.pathname}${nextSearch}${window.location.hash}`,
    );
  }, [editorReturnSignal, refetch]);

  useEffect(() => {
    if (data !== null || pollError !== null) setLoading(false);
  }, [data, pollError]);

  useEffect(() => {
    if (pollError instanceof NotAuthenticatedError) setNeedsAuth(true);
    else if (pollError) setError("We couldn't load this edit. Try again.");
  }, [pollError]);

  const item = data?.item ?? null;

  useEffect(() => {
    if (item?.audio_mode) {
      setAudioPreference(item.audio_mode);
      window.localStorage.setItem(`kria:audio-preference:${itemId}`, item.audio_mode);
      return;
    }
    const saved = window.localStorage.getItem(`kria:audio-preference:${itemId}`);
    if (saved === "kria" || saved === "original" || saved === "voiceover") {
      setAudioPreference(saved);
    }
  }, [item?.audio_mode, itemId]);


  // Closing the tab mid-transfer silently kills uploads — warn while any pool
  // upload is in flight (browsers show their own generic copy).
  const hasActivePoolUploads = pendingClipUploads.some((p) => p.status !== "error");
  useEffect(() => {
    if (!hasActivePoolUploads) return;
    const warn = (e: BeforeUnloadEvent) => {
      e.preventDefault();
      e.returnValue = "";
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [hasActivePoolUploads]);

  // Sync voiceover path from item whenever it changes (after refetch / on load).
  useEffect(() => {
    if (item?.voiceover_gcs_path !== undefined) {
      setVoiceoverGcsPath(item.voiceover_gcs_path ?? null);
    }
  }, [item?.voiceover_gcs_path]);

  // Keep the attach ref in sync with polled data — but NEVER while an attach
  // op is in flight: a stale poll response landing between our POST and its
  // refetch would clobber the ref with pre-delete data and resurrect deleted
  // clips on the next attach (see enqueueAttach).
  useEffect(() => {
    const ATTACH_SYNC_GRACE_MS = 5000;
    if (
      attachOpsInFlight.current === 0 &&
      Date.now() - lastAttachSettledAt.current > ATTACH_SYNC_GRACE_MS
    ) {
      clipAssignmentsRef.current = (item?.clip_assignments ?? []).map((a) => ({
        gcs_path: a.gcs_path,
        shot_id: a.shot_id,
        user_note: a.user_note ?? "",
      }));
    }
    // Prune "Saving…" cards whose clip the server now returns — the optimistic
    // card hands off to the real attached card with no gap (a premature clear
    // would briefly re-enable the maxClips=1 picker).
    const serverPaths = new Set((item?.clip_assignments ?? []).map((a) => a.gcs_path));
    setPendingClipUploads((prev) => {
      const next = prev.filter(
        (p) => p.status !== "saving" || p.gcsPath === null || !serverPaths.has(p.gcsPath),
      );
      return next.length === prev.length ? prev : next;
    });
  }, [item?.clip_assignments]);

  const variants = useMemo(
    () => {
      const rawVariants = data?.job?.variants ?? [];
      return rawVariants.map((v) => {
        const pending = pendingEdits.current.get(v.variant_id);
        if (!pending) return v;
        // Server confirms the re-render is running — record that we witnessed it.
        // NOTE: mutating the ref object inside useMemo is intentional. The Map
        // lives in a useRef (not reactive state) so this doesn't trigger a new
        // render, and the mutation is idempotent (false → true only), making it
        // safe even if React replays the memo under Concurrent Mode.
        if (v.render_status === "rendering") {
          pending.sawRendering = true;
          return v;
        }
        // Decide whether this "ready" / "failed" is the result of OUR edit.
        // A fresh render is detected when:
        //   (a) the editor-return generation token is now visible, OR
        //   (b) we already saw the variant pass through "rendering", OR
        //   (c) the server's render_finished_at timestamp advanced past what we
        //       captured at edit-submission time.
        // Without this guard, the first poll after submission can still return
        // the PRE-edit "ready" (the Celery task hasn't fired yet) and clear the
        // pin too early — leaving controls re-enabled while the render hasn't
        // actually run.  Mirrors the commitMarkerRef pattern in useVariantEditSession.
        const matchesTargetGeneration =
          pending.targetGeneration != null &&
          (v.render_generation_id ?? null) === pending.targetGeneration;
        const isFreshRender =
          matchesTargetGeneration ||
          pending.sawRendering ||
          (v.render_finished_at ?? null) !== pending.priorFinishedAt;
        if ((v.render_status === "ready" || v.render_status === "failed") && isFreshRender) {
          pendingEdits.current.delete(v.variant_id);
          return v;
        }
        // Pre-edit ready race window: keep forcing "rendering" so the poll
        // continues and controls stay disabled until the real render completes.
        // Safety valve: usePolledJobStatus has a 30-minute hard ceiling after
        // which the interval stops regardless of terminal state, so a stuck
        // pending entry is bounded and cannot spin the poll indefinitely.
        return { ...v, render_status: "rendering" as const };
      });
    },
    // editGeneration forces a re-run when pendingEdits is mutated (refs are not
    // reactive; without this, the optimistic pin only takes effect on the next data update).
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [data, editGeneration],
  );

  useEffect(() => {
    if (variants.length === 0) {
      if (focusedVariantId !== null) setFocusedVariantId(null);
      return;
    }
    const focusedVariant = variants.find((v) => v.variant_id === focusedVariantId);
    if (
      !focusedVariant ||
      (audioPreference === "original" && focusedVariant.variant_id !== "original_text")
    ) {
      const originalReady = variants.find(
        (v) => v.output_url && v.variant_id === "original_text",
      );
      const firstReady =
        (audioPreference === "original" ? originalReady : null) ??
        variants.find((v) => v.output_url) ??
        variants[0];
      setFocusedVariantId(firstReady.variant_id);
    }
  }, [variants, focusedVariantId, audioPreference]);

  // "✓ Updated" cue: detect when the focused variant's render_finished_at advances
  // (the exact moment StableVideo swaps in fresh bytes) and flash a transient badge.
  const prevFocusedFinishedAtRef = useRef<string | null>(undefined as unknown as null);
  useEffect(() => {
    const focused = variants.find((v) => v.variant_id === focusedVariantId);
    const cur = focused?.render_finished_at ?? null;
    const prev = prevFocusedFinishedAtRef.current;
    if (prev !== undefined && prev !== null && cur !== null && cur !== prev && focused?.render_status === "ready") {
      renderingAction.current = null; // clear the in-flight label now that it's done
      setUpdatedVariantId(focusedVariantId);
      const timer = setTimeout(() => setUpdatedVariantId(null), 4000);
      return () => clearTimeout(timer);
    }
    prevFocusedFinishedAtRef.current = cur;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [variants, focusedVariantId]);

  const markVariantRendering = useCallback(
    (variantId: string, priorFinishedAt: string | null) => {
      // Preserve sawRendering from a prior in-flight edit: if the user opens
      // the clip editor a second time while the first render is still running,
      // resetting sawRendering to false could trap the pin if the first render
      // already set it (the second edit hasn't fired yet so its "rendering"
      // poll hasn't been seen). Keep the existing flag and only update the
      // timestamp anchor.
      const existing = pendingEdits.current.get(variantId);
      pendingEdits.current.set(variantId, {
        priorFinishedAt,
        sawRendering: existing?.sawRendering ?? false,
        targetGeneration: existing?.targetGeneration ?? null,
      });
      refetch();
    },
    [refetch],
  );

  const runEdit = useCallback(
    async (
      variantId: string,
      prevFinishedAt: string | null,
      action: () => Promise<unknown>,
      actionMeta?: { type: "song" | "text" | "style" | "other"; label: string },
    ) => {
      setError(null);
      // Optimistic pin: mark rendering immediately so the variants memo (which reads
      // pendingEdits.current) fires on the SAME React tick as the click — not after
      // the HTTP round-trip + next poll. setEditGeneration triggers the parent re-render
      // that re-runs the memo; pendingEdits.current is already mutated by then.
      pendingEdits.current.set(variantId, { priorFinishedAt: prevFinishedAt, sawRendering: false });
      if (actionMeta) renderingAction.current = actionMeta;
      setEditGeneration((g) => g + 1);
      try {
        await action();
        // Re-anchor the pin now that the dispatch succeeded; keeps it alive until the
        // poll catches the variant mid-rendering or render_finished_at advances.
        markVariantRendering(variantId, prevFinishedAt);
      } catch (err) {
        // Clear the optimistic pin on any error so controls re-enable.
        pendingEdits.current.delete(variantId);
        renderingAction.current = null;
        setEditGeneration((g) => g + 1);
        const msg = err instanceof Error ? err.message : "Failed to update variant";
        // 409 = variant is being rendered by a prior edit — don't treat as a scary error.
        if (msg.toLowerCase().includes("re-rendering") || msg.includes("409")) {
          setError("Still applying your last change — wait for it to finish, then try again.");
        } else {
          setError("We couldn't apply that change. Try again.");
        }
        refetch();
      }
    },
    [markVariantRendering, refetch],
  );

  // Instructed items (WS2): create-new/mixed items with a filmed shot guide use
  // ShotSlotUploader. existing_footage items keep the legacy pool upload.
  // instruction_level no longer gates the upload UI — it only affects copy/tone.
  const contentMode = item?.content_mode ?? "create_new";
  // Narrated sub-modes:
  //   "narrated" | "narrated_planned" → step-guided flow (plan first, then film)
  //   "narrated_ready"               → have-videos flow (audio first, pool clips)
  const rawEditFormat = item?.edit_format ?? "montage";
  const resolvedFormat = resolvePickerFormat(item?.edit_format, SUBTITLED_ENABLED);
  const montagePreset = item?.montage_preset ?? "classic";
  const isMontage = resolvedFormat === "montage";
  // Lane J: "Back" returns one step into /plan/new — montage's immediate
  // previous step is the style choice, everything else is the kind choice.
  const backToFlowHref = `/plan/new?item=${itemId}&step=${isMontage ? "style" : "kind"}`;
  const isCollagePreset =
    isMontage && COLLAGE_MONTAGE_PRESETS.has(montagePreset);
  const isNarrated = resolvedFormat === "narrated_planned";
  const isNarratedReady = isNarrated && rawEditFormat === "narrated_ready";
  const itemUploadAccept = isNarratedReady
    ? NARRATED_READY_UPLOAD_ACCEPT
    : isCollagePreset
      ? MASONRY_UPLOAD_ACCEPT
      : VIDEO_UPLOAD_ACCEPT;
  // Subtitled single-clip: one talk-to-camera clip, auto-captioned. No shot plan,
  // no voiceover, no content_mode sub-modes — it uploads one clip and generates.
  const isSubtitled = resolvedFormat === "subtitled";
  // Explicit talking_head is backend-native and multi-clip: one clip supplies
  // the speech spine and the others can become B-roll. Keep it out of the
  // single-clip subtitled branch.
  const isTalkingHead = resolvedFormat === "talking_head";
  const isFilmThis = contentMode !== "existing_footage";
  const hasGuide = (item?.filming_guide?.length ?? 0) > 0;
  const isInstructed =
    isFilmThis &&
    hasGuide &&
    !isCollagePreset &&
    !isSubtitled &&
    !isTalkingHead &&
    !isNarratedReady;
  const showVisualPools = !isCollagePreset;

  // Per-type setup identity (design: Paper "V2 — Item setup per type").
  const { untitled: isUntitledTypeLabel, receipt: setupReceiptLabel } = item
    ? setupIdentityFor(item)
    : { untitled: false, receipt: "" };
  const setupTitle = isSubtitled
    ? "Add your clip."
    : isNarrated
      ? "Your voice tells the story."
      : isTalkingHead
        ? "Add your footage."
        : "Add your clips.";

  /*
   * Serialised attach pipeline. Every writer of clip_assignments goes through
   * this queue so concurrent operations compose instead of clobbering:
   *
   *   upload A done ─┐
   *   delete clip B ─┼─▶ [attachQueue] ─▶ POST full list ─▶ update ref ─▶ refetch
   *   upload C done ─┘        (payload computed from clipAssignmentsRef
   *                            at EXECUTION time, not enqueue time)
   *
   * Passes full assignments (not bare paths) so existing clips keep their
   * user_note across an append — the bare-paths legacy form resets them.
   */
  function enqueueAttach(
    mutate: (current: AttachAssignment[]) => AttachAssignment[],
  ): Promise<void> {
    const run = attachQueue.current.then(async () => {
      attachOpsInFlight.current += 1;
      try {
        const next = mutate(clipAssignmentsRef.current);
        // A mutate that returns `current` by reference is a no-op (e.g. the
        // upload was cancelled while its attach sat queued) — skip the POST
        // and the refetch entirely.
        if (next === clipAssignmentsRef.current) return;
        await attachClipAssignments(
          itemId,
          next.map((a) => a.gcs_path),
          next,
        );
        clipAssignmentsRef.current = next;
        conformancePolls.current = 0;
        refetch();
      } finally {
        attachOpsInFlight.current -= 1;
        lastAttachSettledAt.current = Date.now();
      }
    });
    // The queue itself must survive a failed op.
    attachQueue.current = run.then(
      () => undefined,
      () => undefined,
    );
    return run;
  }

  // Drain every buffered completed upload in ONE queued attach op. Inserts by
  // picker ordinal (never network completion order), skips cancelled entries,
  // and no-ops (identity return → no POST, no refetch) when nothing survives.
  function scheduleAttachDrain(): Promise<void> {
    if (attachDrainScheduled.current) {
      // A queued drain that hasn't snapshotted yet will pick up our buffered
      // add; awaiting the queue tail covers it.
      return attachQueue.current;
    }
    attachDrainScheduled.current = true;
    return enqueueAttach((current) => {
      // Snapshot synchronously at execution: adds arriving after this see
      // scheduled=false and queue the next drain.
      attachDrainScheduled.current = false;
      const adds = pendingAttachAdds.current
        .filter((a) => !cancelledUploadIds.current.has(a.localId))
        .sort((x, y) => x.order - y.order);
      pendingAttachAdds.current = [];
      if (adds.length === 0) return current;
      let next = current;
      for (const add of adds) {
        const insertAt = next.findIndex((existing) => {
          const ordinal = uploadOrdinalRef.current.get(existing.gcs_path);
          return ordinal !== undefined && ordinal > add.order;
        });
        next =
          insertAt === -1
            ? [...next, add.assignment]
            : [...next.slice(0, insertAt), add.assignment, ...next.slice(insertAt)];
      }
      return next;
    });
  }

  // Upload one pending clip end-to-end: JIT-mint its signed URL (the 15-min
  // TTL starts when the upload starts, not when the batch was picked), PUT
  // with progress, then attach through the serialised queue.
  async function runPoolUpload(local: PendingClipUpload) {
    let gcsPath: string | null = null;
    const release = await acquirePoolUploadSlot();
    try {
      if (cancelledUploadIds.current.has(local.localId)) return;
      const [url] = await requestUploadUrls(itemId, [
        {
          filename: local.file.name,
          content_type: uploadContentTypeForFile(local.file),
          file_size_bytes: local.file.size,
        },
      ]);
      if (cancelledUploadIds.current.has(local.localId)) return;
      await uploadToGcsWithProgress(
        url.upload_url,
        local.file,
        (frac, indeterminate) => {
          // Quantize to whole percents: returning `prev` unchanged lets React
          // skip the re-render (this page is very large — event-rate renders
          // are visible jank on low-end phones).
          setPendingClipUploads((prev) => {
            const idx = prev.findIndex((p) => p.localId === local.localId);
            if (idx === -1) return prev;
            const pct = Math.round(frac * 100);
            const ind = indeterminate === true;
            if (pct === Math.round(prev[idx].progress * 100) && ind === prev[idx].indeterminate) {
              return prev;
            }
            const next = [...prev];
            next[idx] = { ...next[idx], progress: frac, indeterminate: ind };
            return next;
          });
        },
        local.abortController.signal,
      );
      if (cancelledUploadIds.current.has(local.localId)) return;
      gcsPath = url.gcs_path;
    } catch (err) {
      if ((err as DOMException)?.name === "AbortError") return;
      setPendingClipUploads((prev) =>
        prev.map((p) =>
          p.localId === local.localId
            ? {
                ...p,
                status: "error" as const,
                error: clipUploadErrorMessage(err),
              }
            : p,
        ),
      );
      return;
    } finally {
      release();
    }
    if (gcsPath === null) return;
    const attachedPath = gcsPath;
    uploadOrdinalRef.current.set(attachedPath, local.order);
    pendingAttachAdds.current.push({
      order: local.order,
      localId: local.localId,
      assignment: { gcs_path: attachedPath, shot_id: null, user_note: "" },
    });
    // Card survives as "Saving…" until the server's clip list contains the
    // path (pruned by the sync effect) — no premature maxClips re-enable.
    setPendingClipUploads((prev) =>
      prev.map((p) =>
        p.localId === local.localId
          ? { ...p, status: "saving" as const, gcsPath: attachedPath, progress: 1 }
          : p,
      ),
    );
    try {
      await scheduleAttachDrain();
    } catch (err) {
      // The bytes are already in GCS — keep the card as an error whose Retry
      // re-runs ONLY the attach (never a full re-upload of a large file).
      setPendingClipUploads((prev) =>
        prev.map((p) =>
          p.localId === local.localId
            ? {
                ...p,
                status: "error" as const,
                error: clipUploadErrorMessage(err, "Couldn't save the clip"),
              }
            : p,
        ),
      );
      return;
    }
    // Cancel landed while the attach POST was in flight: the drain couldn't
    // see it, so compensate with a queued removal (keeps "cancel means it
    // won't be attached" true through the whole window).
    if (cancelledUploadIds.current.has(local.localId)) {
      try {
        await enqueueAttach((current) =>
          current.some((x) => x.gcs_path === attachedPath)
            ? current.filter((x) => x.gcs_path !== attachedPath)
            : current,
        );
      } catch {
        // Removal is best-effort; the clip is deletable from its card.
      }
      setPendingClipUploads((prev) => prev.filter((p) => p.localId !== local.localId));
    }
  }

  function cancelClipUpload(localId: string) {
    cancelledUploadIds.current.add(localId);
    pendingClipUploads.find((p) => p.localId === localId)?.abortController.abort();
    setPendingClipUploads((prev) => prev.filter((p) => p.localId !== localId));
  }

  async function retryClipUpload(localId: string) {
    const entry = pendingClipUploads.find((p) => p.localId === localId);
    if (!entry || entry.status !== "error") return;
    cancelledUploadIds.current.delete(localId);
    if (entry.gcsPath) {
      // Upload succeeded, only the attach failed — retry JUST the attach.
      const path = entry.gcsPath;
      setPendingClipUploads((prev) =>
        prev.map((p) =>
          p.localId === localId
            ? { ...p, status: "saving" as const, error: null, progress: 1 }
            : p,
        ),
      );
      pendingAttachAdds.current.push({
        order: entry.order,
        localId,
        assignment: { gcs_path: path, shot_id: null, user_note: "" },
      });
      try {
        await scheduleAttachDrain();
      } catch (err) {
        setPendingClipUploads((prev) =>
          prev.map((p) =>
            p.localId === localId
              ? {
                  ...p,
                  status: "error" as const,
                  error: clipUploadErrorMessage(err, "Couldn't save the clip"),
                }
              : p,
          ),
        );
      }
      return;
    }
    const refreshed: PendingClipUpload = {
      ...entry,
      gcsPath: null,
      progress: 0,
      indeterminate: false,
      status: "uploading",
      error: null,
      abortController: new AbortController(),
    };
    setPendingClipUploads((prev) => prev.map((p) => (p.localId === localId ? refreshed : p)));
    await runPoolUpload(refreshed);
  }

  // Legacy pool upload handler (uninstructed items only). Re-entrant: a second
  // selection while a batch is in flight just adds more cards through the same
  // 3-slot gate. `uploading` now covers only the pre-card window (narrated
  // voiceover routing); per-file state lives on the cards.
  async function handleFiles(files: FileList | null) {
    if (!files || files.length === 0 || isInstructed) return;
    setUploading(true);
    setError(null);
    conformancePolls.current = 0;
    let list: File[];
    try {
      list = Array.from(files);
      if (isNarratedReady) {
        const { voiceoverFiles, clipFiles } = await splitNarratedReadyUploads(list);
        if (voiceoverFiles.length > 1) {
          throw new Error("Upload one narration file at a time.");
        }
        if (voiceoverFiles.length === 1) {
          const uploaded = await uploadOwnedVoiceover(voiceoverFiles[0]);
          if (uploaded.kind !== "audio") {
            throw new Error("Upload an audio file for narration.");
          }
          const saved = await handleVoiceover(uploaded.gcs_path);
          if (!saved) return;
        }
        list = clipFiles;
        if (list.length === 0) return;
      }
    } catch {
      setError("We couldn't add those videos. Try again.");
      return;
    } finally {
      setUploading(false);
    }
    void detectLandscapeClip(list).then((found) => {
      if (found) setHasLandscapeClip(true);
    });
    const locals: PendingClipUpload[] = list.map((f) => {
      const seq = ++poolUploadSeq;
      return {
        localId: `clip-${seq}-${f.name}`,
        file: f,
        filename: f.name,
        order: seq,
        gcsPath: null,
        progress: 0,
        indeterminate: false,
        status: "uploading" as const,
        error: null,
        abortController: new AbortController(),
      };
    });
    setPendingClipUploads((prev) => [...prev, ...locals]);
    await Promise.allSettled(locals.map((local) => runPoolUpload(local)));
  }

  // ── Uninstructed clip actions (no-shot-list items: feedback #3 + pool Keep) ──

  async function saveUninstructedNote(a: ClipAssignment, note: string) {
    await setClipNote(itemId, a.gcs_path, note);
    conformancePolls.current = 0;
    refetch();
  }

  async function keepUninstructedMatch(a: ClipAssignment) {
    try {
      await saveUninstructedNote(a, a.user_note ?? "");
    } catch {
      setError("Couldn't keep that clip — try again.");
    }
  }

  async function removeUninstructedClip(a: ClipAssignment) {
    try {
      // Queued: composes with in-flight upload attaches (delete during an
      // upload's settle must not resurrect, and vice versa).
      await enqueueAttach((current) => current.filter((x) => x.gcs_path !== a.gcs_path));
    } catch {
      setError("We couldn't remove that video. Try again.");
    }
  }

  // "Use in edit" (Visuals pool → clip promotion). Pool objects live under
  // users/{uid}/plan/{itemId}/pool/ — already inside attach_clips' allowed
  // prefix — so promotion is a plain re-attach with the pool path appended.
  // The asset stays in the pool (overlay suggestions still see it).
  async function promotePoolAsset(asset: PoolAsset) {
    try {
      // Through the serialised queue like every other clip_assignments writer —
      // a direct attachClips from the render-scope `item` would race queued
      // deletes (resurrection) and leave the ref without the promoted asset
      // (a later upload's drain would silently detach it).
      await enqueueAttach((current) => {
        // Pure merge (unit-tested): preserves every existing shot_id/user_note;
        // null on dedupe or missing gcs_path (old-API version skew) → identity
        // return, so no POST fires.
        const assignments = buildPromotedAssignments(current, asset.gcs_path);
        return assignments ?? current;
      });
    } catch {
      setError("We couldn't add that visual to the edit. Try again.");
    }
  }

  async function handleVoiceover(gcsPath: string | null): Promise<boolean> {
    const generation = ++voiceoverSaveGenerationRef.current;
    setVoiceoverGcsPath(gcsPath);
    setVoiceoverSaving(true);
    // Serialize replacement/removal saves. VoiceRecorder lets a user remove a
    // take while its upload is still settling; concurrent PATCHes could commit
    // out of order and resurrect the old take after the user cleared it.
    const savePromise = persistVoiceover(gcsPath, generation);
    voiceoverSaveQueueRef.current = savePromise;
    voiceoverSavePromiseRef.current = savePromise;
    try {
      return await savePromise;
    } finally {
      if (voiceoverSavePromiseRef.current === savePromise) {
        voiceoverSavePromiseRef.current = null;
      }
    }
  }

  async function persistVoiceover(gcsPath: string | null, generation: number): Promise<boolean> {
    await voiceoverSaveQueueRef.current.catch(() => false);
    try {
      const saved = await setItemVoiceover(itemId, gcsPath);
      if (generation === voiceoverSaveGenerationRef.current) {
        // Keep the client shadow aligned with the server response. This avoids
        // a stale poll clearing a successfully persisted attachment while the
        // next Generate click is being prepared.
        setVoiceoverGcsPath(saved.voiceover_gcs_path ?? gcsPath);
        refetch();
      }
      return true;
    } catch {
      if (generation === voiceoverSaveGenerationRef.current) {
        setVoiceoverGcsPath(null);
        setError("We couldn't save your narration. Try again.");
      }
      return false;
    } finally {
      if (generation === voiceoverSaveGenerationRef.current) {
        setVoiceoverSaving(false);
      }
    }
  }



  async function handleGenerate() {
    setGenerating(true);
    setError(null);
    // Arm the wait window BEFORE the POST so the release-effect can't fire
    // early while the request is still in flight. Snapshot the job id that
    // was current BEFORE this click too — on a retry, current_job_id
    // already points at the old (terminal) job, so hasRenderRegistered()
    // needs this baseline to tell "the old job is still there" apart from
    // "a new job has registered".
    jobIdBeforeGenerateRef.current = item?.current_job_id ?? null;
    awaitingJobSince.current = Date.now();
    try {
      // The local voiceover path is optimistic while PATCH /voiceover is in
      // flight. Await that persistence boundary even if a stale caller invokes
      // Generate before the button re-renders disabled.
      const pendingVoiceoverSave = voiceoverSavePromiseRef.current;
      if (pendingVoiceoverSave && !(await pendingVoiceoverSave)) {
        throw new Error("Your narration couldn't be saved. Try again before creating the video.");
      }
      if (item && needsFormatPersist(item.edit_format)) {
        await updatePlanItem(item.id, { edit_format: resolvedFormat });
      }
      const generated = await generatePlanItem(itemId);
      // A legacy API may still return the persisted direction checkpoint. Keep
      // the local creation lock held; the next poll observes the server-side
      // resume instead of turning this into a dead-end review step.
      if (generated.edit_proposal?.guidance?.state === "awaiting_direction_confirmation") {
        awaitingJobSince.current = Date.now();
      }
      refetch();
    } catch {
      awaitingJobSince.current = null;
      setError("We couldn't create your video. Try again.");
      setGenerating(false);
    }
  }

  // Release the Generate lock once the render registers (or the wait window
  // expires without a job — surface that instead of silently doing nothing).
  useEffect(() => {
    const directionPending =
      item?.edit_proposal?.guidance?.state === "awaiting_direction_confirmation";
    const autoDesigning =
      item?.guided_edit_auto_design === true &&
      (item?.edit_proposal?.status === "analyzing" ||
        item?.edit_proposal?.status === "drafting");
    if (directionPending && !autoDesigning && !generating) {
      awaitingJobSince.current = null;
      setGenerating(false);
      setError((prev) => (prev === RENDER_REGISTER_ERROR ? null : prev));
      return;
    }
    const registered = item != null && hasRenderRegistered(item, jobIdBeforeGenerateRef.current);
    if (registered) {
      // A registered render moots any earlier didn't-register complaint —
      // clear it even if it was shown in a previous attempt (dogfood: the
      // banner outlived the render it was wrong about).
      setError((prev) => (prev === RENDER_REGISTER_ERROR ? null : prev));
    }
    if (!generating) return;
    // Auto-design's design phase (analyzing/drafting) can legitimately run
    // past RENDER_REGISTER_TIMEOUT_MS under transient-analysis retries — no
    // render Job even exists to register yet. Keep re-arming the wait window
    // while designing so the watchdog only starts counting once design
    // settles and a render is actually expected to register (P3, 2026-08-18
    // adversarial review).
    if (autoDesigning) {
      awaitingJobSince.current = Date.now();
      return;
    }
    if (registered) {
      awaitingJobSince.current = null;
      setGenerating(false);
    } else if (
      awaitingJobSince.current !== null &&
      Date.now() - awaitingJobSince.current >= RENDER_REGISTER_TIMEOUT_MS &&
      data !== null
    ) {
      awaitingJobSince.current = null;
      setGenerating(false);
      setError(RENDER_REGISTER_ERROR);
    }
  }, [generating, item, data]);

  if (needsAuth) {
    return (
      <LightShell size="narrow">
        <SignInPrompt
          callbackUrl={`/plan/items/${itemId}`}
          title="Sign in to continue"
          subtitle="We use your Google account to save your clips and renders."
        />
      </LightShell>
    );
  }

  if (loading) {
    return (
      <LightShell size="narrow">
        <p className="py-24 text-center text-[#71717a]">Loading…</p>
      </LightShell>
    );
  }

  if (item === null) {
    return (
      <LightShell size="narrow">
        <div className="motion-safe:animate-fade-up py-24 text-center">
          <p className="mb-6 text-[#71717a]">We couldn&apos;t find that idea.</p>
          <Link href="/plan">
            <InkButton>Back to your plan</InkButton>
          </Link>
        </div>
      </LightShell>
    );
  }

  const clipCount = item.clip_gcs_paths.length;
  const isGenerating = item.status === "generating";
  const showResults = isGenerating || variants.length > 0;
  const showReleaseDesk = !isGenerating && variants.length > 0;
  const showSetupControls = !isGenerating && variants.length === 0;
  // A variant can exist and still be a dead end: the first render created one
  // variant object before failing, so showSetupControls (and its Generate
  // button) never comes back — FocusedResults renders instead, but per-variant
  // edit controls (song swap, captions) all require data that's only written
  // on a SUCCESSFUL render (e.g. base_video_url), so none of them apply either.
  // This drives the ProgressTheater "Try again" button below.
  const allVariantsFailed =
    variants.length > 0 && variants.every((v) => v.render_status === "failed");
  const zeroVariantFailure = jobFailureCopy(data?.job?.failure_reason);
  // Conformance in-flight: clips attached + guide present + verdict pending,
  // bounded by the poll window — resolves to the tile, the on-track line, or
  // (when guards skipped the run) silently vanishes. Never hangs.
  const conformanceChecking =
    clipCount > 0 &&
    (item.filming_guide?.length ?? 0) > 0 &&
    item.instruction_level !== "none" &&
    !item.conformance?.verdict &&
    conformancePolls.current < 3;
  const showKriaHelper =
    askKria !== null ||
    conformanceChecking ||
    Boolean(
      item.conformance?.verdict &&
        !item.conformance.dismissed &&
        !item.conformance.suppressed &&
        (item.conformance.confidence ?? 0) >= 0.6,
    );
  const focused = variants.find((v) => v.variant_id === focusedVariantId) ?? null;
  const focusedEditable =
    focused && (!!focused.output_url || focused.render_status === "failed");

  // "N shots left" caption under the Generate button.
  const totalShots = item.filming_guide?.length ?? 0;
  const filledShots = item.clip_assignments?.filter((a) => a.shot_id !== null).length ?? 0;
  const shotsLeft = Math.max(0, totalShots - filledShots);

  // Self-narration (dual-flag with NARRATED_SELF_NARRATION_ENABLED on Fly — flip
  // Fly first, then Vercel): narrated items may generate without a recorded
  // voiceover; the footage's own audio drives the edit.
  const selfNarrationEnabled =
    process.env.NEXT_PUBLIC_NARRATED_SELF_NARRATION_ENABLED === "true";
  const guidedEditActive = GUIDED_EDIT_ENABLED && item.guided_edit_available === true;
  const guidedEditApproved = item.edit_proposal?.status === "approved";
  // GUIDED_AUTO_DESIGN_ENABLED: absent/false on an old API keeps today's
  // strict-gate behavior (deploy-skew safe) — see PlanItem.guided_edit_auto_design.
  const guidedEditAutoDesign = item.guided_edit_auto_design ?? false;
  const hasApprovedGuidedMedia = Boolean(
    guidedEditActive &&
      guidedEditApproved &&
      item.edit_proposal?.last_approved?.snapshot.story_beats.some(
        (beat) => beat.media_ids.length > 0,
      ),
  );
  // Matches the backend's own eligibility check (_maybe_auto_design_generate:
  // PlanItemAsset.status == "ready", no kind filter) so a pool-only item is
  // reachable from Generate exactly when the server would actually design
  // from it. Gated on guidedEditAutoDesign so a pool-only item with
  // auto-design off/undefined keeps today's exact behavior (P2-5).
  const hasReadyPoolMedia =
    guidedEditAutoDesign && poolAssets.some((asset) => asset.status === "ready");
  const guidedDesigning =
    guidedEditActive &&
    guidedEditAutoDesign &&
    (item.edit_proposal?.status === "analyzing" ||
      item.edit_proposal?.status === "drafting" ||
      item.edit_proposal?.conversation_in_progress === true);
  const designingHint =
    item.edit_proposal?.status === "drafting" ? "Building your edit…" : "Analyzing your clips…";
  // Existing upload/format rules come from one decision; guided-edit approval
  // composes a second explicit gate immediately below.
  const gate = generateGate({
    generating,
    designing: guidedDesigning,
    designingHint,
    isGenerating,
    // Pool uploads keep the trigger enabled (per-file cards), so Generate must
    // gate on them explicitly — the old page-global `uploading` no longer
    // spans the whole transfer. Error cards deliberately do NOT gate:
    // "Finishing upload…" would be a lie for an upload that already failed.
    uploaderBusy: uploaderBusy || uploading || hasActivePoolUploads || voiceoverSaving,
    clipCount,
    hasApprovedGuidedMedia,
    hasReadyPoolMedia,
    isNarrated,
    hasVoiceover: !!voiceoverGcsPath,
    selfNarrationEnabled,
    isInstructed,
    shotsLeft,
  });
  const guidedEditHint =
    item.edit_proposal?.guidance?.state === "awaiting_direction_confirmation"
      ? "Kria is analyzing your clips…"
      : item.edit_proposal?.status === "stale"
      ? "Your media changed — plan the edit again."
      : item.edit_proposal?.status === "analyzing" || item.edit_proposal?.status === "drafting"
        ? guidedEditAutoDesign
          ? item.edit_proposal?.status === "drafting"
            ? "Kria is building your edit…"
            : "Kria is analyzing your clips…"
          : "Kria is still planning this edit."
        : item.edit_proposal?.status === "draft"
          ? "Review and approve the edit plan first."
          : item.edit_proposal?.status === "failed"
            ? guidedEditAutoDesign
              ? "Kria couldn't finish planning this edit — it'll retry when you hit Generate."
              : "Kria couldn't finish planning this edit — open the planner to try again."
            : guidedEditAutoDesign
              ? "Kria will analyze your clips and build the edit automatically."
              : "Plan this edit before generating.";
  // Compact status row under Tell Kria (PlanThreadPanel trigger) — replaces
  // the inline EditProposalCard morph (DESIGN.md §12). Badge label/variant +
  // one sentence + button label, all keyed off item.edit_proposal?.status.
  const guidedEditStatusRow = (() => {
    const status = item.edit_proposal?.status;
    if (item.edit_proposal?.guidance?.state === "awaiting_direction_confirmation") {
      return {
        badgeLabel: "AI planning",
        badgeVariant: "zinc" as const,
        sentence: "Kria is analyzing your clips…",
        buttonLabel: "Plan with Kria",
      };
    }
    if (status === "draft") {
      return {
        badgeLabel: "Draft ready",
        badgeVariant: "lime-soft" as const,
        sentence: "Your draft is ready to review.",
        buttonLabel: "Review Kria's plan",
      };
    }
    if (status === "approved") {
      return {
        badgeLabel: "Approved",
        badgeVariant: "lime-soft" as const,
        sentence: "This edit plan is locked in.",
        buttonLabel: "Change plan",
      };
    }
    if (status === "failed" || status === "stale") {
      return {
        badgeLabel: "Needs a look",
        badgeVariant: "zinc" as const,
        sentence:
          status === "stale"
            ? "Your media changed — the plan needs another look."
            : "Kria couldn't finish the plan — take a look.",
        buttonLabel: "Plan with Kria",
      };
    }
    return {
      badgeLabel: "AI planning",
      badgeVariant: "zinc" as const,
      sentence:
        status === "analyzing" || status === "drafting"
          ? status === "drafting"
            ? "Kria is building your edit…"
            : "Kria is analyzing your clips…"
          : "Kria will analyze your clips and build the edit automatically.",
      buttonLabel: "Plan with Kria",
    };
  })();
  // "Your narrated render became a montage" explanation (no_speech etc.) —
  // persisted by the orchestrator, surfaced here so the swap is never silent.
  const fallbackBanner = narrationFallbackBanner(
    isNarrated,
    data?.job?.archetype_fallback ?? null,
  );

  const currentPhase =
    data?.job?.current_phase ??
    (!data?.job?.started_at ? "queued" : null);
  // Variant re-renders deliberately leave the parent job/item ready while the
  // selected output is rendering. Treat that as live progress so text/song/
  // style edits get the same compact theater alongside the existing preview.
  const variantRenderInProgress = variants.some(
    (candidate) => candidate.render_status === "rendering",
  );
  const theaterIsTerminal =
    !variantRenderInProgress && !!(item && isTerminalFn({ item, job: data?.job ?? null }));
  const theaterIsSuccess = !variantRenderInProgress && item?.status === "ready";
  const renderProgress = data?.job && (item.status !== "ready" || variantRenderInProgress) ? (
    <ProgressTheater
      phases={GENERATIVE_PHASE_ORDER}
      phaseLabels={GENERATIVE_PHASE_LABEL}
      currentPhase={currentPhase}
      expectedPhaseMs={data.job.expected_phase_durations ?? null}
      phaseLog={data.job.phase_log ?? null}
      startedAt={data.job.started_at ?? null}
      jobCreatedAt={data.job.created_at ?? new Date().toISOString()}
      isTerminal={theaterIsTerminal}
      isSuccess={theaterIsSuccess}
      receiptText={deriveReceiptText(data.job.started_at, data.job.finished_at)}
      variants={variants}
      retrying={data.job.retrying ?? false}
      steps={data.job.steps ?? null}
      stepsPresentation="disclosure"
      size="full"
      tone="light"
      onRetry={allVariantsFailed && !generating ? handleGenerate : undefined}
    />
  ) : null;

  // ── Lane G: one-card setup surface (Clips/Visuals tabs + Tell Kria + footer
  // CTA) — derived JSX kept out of the return so the Card/Tabs/CardFooter
  // markup below stays legible. clipCount (item.clip_gcs_paths.length) is
  // already declared above for the generate gate — reused here for the tab
  // label so both stay in sync. ─────────────────────────────────────────
  const visualsCount = poolAssets.length;

  const landscapeFitNotice =
    item.status !== "generating" &&
    item.status !== "ready" &&
    variants.length === 0 &&
    hasLandscapeClip ? (
      <div>
        <p className="mb-2 text-[11px] font-semibold uppercase tracking-[0.18em] text-muted-foreground">
          Landscape clip detected
        </p>
        <div className="flex gap-2">
          {LANDSCAPE_FIT_OPTIONS.map(({ value, label, desc }) => {
            const active = (item.landscape_fit ?? "fit") === value;
            return (
              <Button
                key={value}
                type="button"
                variant="ghost"
                onClick={async () => {
                  if (active) return;
                  await updatePlanItem(item.id, { landscape_fit: value }).catch(() => null);
                  refetch();
                }}
                className={`h-auto flex-1 flex-col items-start justify-start rounded-xl border px-3 py-2.5 text-left font-normal transition-colors ${
                  active
                    ? "border-lime-200 bg-lime-50 hover:bg-lime-50"
                    : "border-zinc-200 bg-white hover:border-zinc-300 hover:bg-white"
                }`}
              >
                <span className={`text-sm font-medium ${active ? "text-lime-800" : "text-[#0c0c0e]"}`}>
                  {label}
                </span>
                <span className="mt-0.5 text-xs text-zinc-400">{desc}</span>
              </Button>
            );
          })}
        </div>
      </div>
    ) : null;

  // Uploader — branches:
  //   0. subtitled: one talk-to-camera clip → pool upload (no shot plan)
  //   1. talking_head: speech spine + B-roll clips → pool upload
  //   2. narrated_ready: audio-first flow, pool upload, no step spine
  //   3. masonry montage → compact pool strip even when guide present
  //   4. isInstructed (create_new/mixed + guide present) → ShotSlotUploader
  //   5. isFilmThis, no guide → pool upload (Plan-this-for-me offered above)
  //   6. existing_footage → PoolUploadCard (use footage you already have)
  const uploaderNode = isSubtitled ? (
    <PoolUploadCard
      clips={item.clip_assignments ?? []}
      pending={pendingClipUploads}
      uploading={uploading}
      onFiles={handleFiles}
      onCancelUpload={cancelClipUpload}
      onRetryUpload={retryClipUpload}
      onKeep={keepUninstructedMatch}
      onRemove={removeUninstructedClip}
      onNoteChange={saveUninstructedNote}
      maxClips={1}
      accept={itemUploadAccept}
      subline="One clip of you talking"
    />
  ) : isTalkingHead ? (
    <PoolUploadCard
      clips={item.clip_assignments ?? []}
      pending={pendingClipUploads}
      uploading={uploading}
      onFiles={handleFiles}
      onCancelUpload={cancelClipUpload}
      onRetryUpload={retryClipUpload}
      onKeep={keepUninstructedMatch}
      onRemove={removeUninstructedClip}
      onNoteChange={saveUninstructedNote}
      accept={itemUploadAccept}
      subline="First clip: you talking. Then extra footage to cut in."
    />
  ) : isNarratedReady ? (
    <PoolUploadCard
      clips={item.clip_assignments ?? []}
      pending={pendingClipUploads}
      uploading={uploading}
      onFiles={handleFiles}
      onCancelUpload={cancelClipUpload}
      onRetryUpload={retryClipUpload}
      onKeep={keepUninstructedMatch}
      onRemove={removeUninstructedClip}
      onNoteChange={saveUninstructedNote}
      accept={itemUploadAccept}
      // Self-narration mode keeps this line short — the gate hint under
      // Generate carries the "your video's own narration" explanation
      // (one explanation per screen, DESIGN.md §9).
      subline={
        selfNarrationEnabled && !voiceoverGcsPath
          ? "Upload all the clips you filmed."
          : "Upload all the clips you filmed. We'll listen to your recording and match each moment to the right clip automatically."
      }
    />
  ) : isCollagePreset ? (
    <PoolUploadCard
      clips={item.clip_assignments ?? []}
      pending={pendingClipUploads}
      uploading={uploading}
      onFiles={handleFiles}
      onCancelUpload={cancelClipUpload}
      onRetryUpload={retryClipUpload}
      onKeep={keepUninstructedMatch}
      onRemove={removeUninstructedClip}
      onNoteChange={saveUninstructedNote}
      accept={itemUploadAccept}
      subline="Photos and videos both work in this style."
    />
  ) : isInstructed ? (
    <ShotSlotUploader
      item={item}
      onAttached={(updated) => {
        conformancePolls.current = 0;
        refetch();
      }}
      onBusyChange={setUploaderBusy}
    />
  ) : (
    // isFilmThis (no guide yet) OR existing_footage — both fall through to
    // pool upload (find/upload the footage you already have).
    <PoolUploadCard
      clips={item.clip_assignments ?? []}
      pending={pendingClipUploads}
      uploading={uploading}
      onFiles={handleFiles}
      onCancelUpload={cancelClipUpload}
      onRetryUpload={retryClipUpload}
      onKeep={keepUninstructedMatch}
      onRemove={removeUninstructedClip}
      onNoteChange={saveUninstructedNote}
      accept={itemUploadAccept}
      subline={
        !hasGuide && item.filming_suggestion
          ? item.filming_suggestion
          : isMontage
            ? "3 or more clips work best. Kria cuts them to the beat of a matched song."
            : undefined
      }
    />
  );

  const clipsTabBody = (
    <div className="space-y-4">
      {hasGuide && !isInstructed && <CompactPlanSummary item={item} />}
      {landscapeFitNotice}
      <section aria-labelledby="main-footage-heading">
        <h2 id="main-footage-heading" className="sr-only">
          {isSubtitled ? "Your clip" : "Your clips"}
        </h2>
        {uploaderNode}
      </section>
    </div>
  );

  // Narrated voiceover — a second Separator'd section between the dropzone
  // and Tell Kria, not its own bordered block (Lane G).
  const narratedVoiceoverSection = isNarrated ? (
    <>
      <Separator />
      <div>
        <Label className="mb-2 block">Your narration</Label>
        <p className="mb-3 text-xs text-muted-foreground">
          This recording becomes the soundtrack. It is separate from your note to Kria.
        </p>
        <VoiceRecorder onVoiceover={handleVoiceover} upload={uploadOwnedVoiceover} />
        {voiceoverSaving && <p className="mt-1 text-xs text-muted-foreground">Saving…</p>}
        {voiceoverGcsPath && !voiceoverSaving && (
          <p className="mt-1 text-xs text-lime-700">
            Voice recorded — clips will be timed to match your narration.
          </p>
        )}
        {/* First-class entry point (moved from a buried inline link during the
            plan-item redesign) — narrated only. Talking-to-camera does not get
            this: TeleprompterRecorder/ReviewStep write to voiceover_gcs_path, a
            field _render_subtitled_variant never reads (see the plan's "Plan
            correction" section). */}
        {process.env.NEXT_PUBLIC_TRANSCRIPT_HELPER_ENABLED === "true" && (
          <Link
            href={`/plan/items/${item.id}/transcript`}
            className="mt-3 inline-flex items-center gap-1.5 rounded-lg border border-zinc-200 bg-white px-3 py-2 text-[12px] text-[#71717a] transition-colors hover:border-lime-400 hover:text-lime-700"
          >
            Need narration? Write a script with Kria
          </Link>
        )}
      </div>
    </>
  ) : null;

  const tellKriaSection = (
    <section>
      <Label htmlFor="tell-kria" className="mb-2 block">
        Tell Kria <span className="font-normal text-muted-foreground">(optional)</span>
      </Label>
      <Textarea
        id="tell-kria"
        aria-label="Tell Kria"
        key={item.notes ?? "empty"}
        defaultValue={item.notes ?? ""}
        onBlur={async (e) => {
          const val = e.currentTarget.value.trim() || null;
          if (val !== (item.notes ?? null)) {
            await updatePlanItem(item.id, { notes: val ?? undefined }).catch(() => null);
            refetch();
          }
        }}
        placeholder="For example: start fast and keep the candid moments."
        rows={2}
      />
    </section>
  );

  const guidedStatusRowNode = guidedEditActive ? (
    <div className="flex items-center gap-3 text-sm">
      <Badge variant={guidedEditStatusRow.badgeVariant}>{guidedEditStatusRow.badgeLabel}</Badge>
      <p className="flex-1 text-muted-foreground">{guidedEditStatusRow.sentence}</p>
      <Button type="button" variant="outline" size="sm" onClick={() => setThreadOpen(true)}>
        {guidedEditStatusRow.buttonLabel}
      </Button>
    </div>
  ) : null;

  const cardBody = (
    <>
      <TabsContent value="clips" className="mt-0 space-y-6">
        {clipsTabBody}
      </TabsContent>
      {showVisualPools && (
        <TabsContent value="visuals" className="mt-0">
          <AssetPool
            embedded
            itemId={itemId}
            attachedPaths={item.clip_assignments?.map((a) => a.gcs_path) ?? []}
            onUseInEdit={promotePoolAsset}
            attachBusy={uploading || uploaderBusy || hasActivePoolUploads}
            onAssetsChanged={setPoolAssets}
            onMutated={() => {
              forceFreshFetchRef.current = true;
              refetch();
            }}
            onAssetContextUpdated={(updated) => {
              overlaySuggestions.setRows([]);
              overlaySuggestions.setKeptIds(new Set());
              setSuggestionPoolAssets((prev) =>
                prev.map((asset) => (asset.id === updated.id ? updated : asset)),
              );
            }}
          />
        </TabsContent>
      )}
      {MAIN_CREATOR_AGENT_ENABLED && <MainCreatorAgentPanel itemId={itemId} />}
      <Separator />
      {narratedVoiceoverSection}
      {tellKriaSection}
      {guidedStatusRowNode}
    </>
  );

  const generateGated =
    gate.disabled || (guidedEditActive && !guidedEditApproved && !guidedEditAutoDesign);
  const generateLabel = generating
    ? "Creating…"
    : guidedDesigning
      ? "Creating…"
    : uploaderBusy
      ? FINISHING_UPLOAD_HINT
      : "Create video";
  // #71717a-equivalent (text-muted-foreground), not the faint token: this line
  // carries must-read gating copy (why the button is off / what drives the
  // edit) — DESIGN.md §8 keeps faint ink decorative-only.
  const generateHint =
    gate.hint ??
    (guidedEditActive && !guidedEditApproved ? guidedEditHint : null);

  return (
    <LightShell size="wide">
      {/* @font-face for style-preview chips */}
      <style dangerouslySetInnerHTML={{ __html: FONT_FACES }} />
      <div className="motion-safe:animate-fade-up">

        {/* ── Single-column layout: back link + header + shot plan + generate + progress ── */}
        <div>

          {/* Content: back link + editorial header + uploader + generate + progress */}
          <div>
            {!showReleaseDesk && (
              <>
                {/* Lane J: "Back" returns one step into the creation flow
                    (/plan/new) instead of home to /plan — montage items land
                    on the style step since that's the immediate previous
                    choice, everything else lands on the kind step. The old
                    "your videos" destination stays reachable via the header
                    logo/avatar menu. */}
                <Button
                  type="button"
                  variant="link"
                  size="sm"
                  asChild
                  className="h-auto p-0 text-sm text-[#71717a] hover:text-[#0c0c0e]"
                >
                  <Link href={backToFlowHref}>
                    <ArrowLeft className="h-4 w-4" aria-hidden="true" />
                    Back
                  </Link>
                </Button>
                {/* Setup receipt: type (+ montage style). Editing now happens by
                    going Back into the /plan/new chooser (Lane J) — the inline
                    "Change" toggle + poster picker are gone. */}
                <div className="mt-5 flex items-baseline justify-between gap-3">
                  <Badge variant="lime">{setupReceiptLabel}</Badge>
                </div>
                <h1 className="font-display mt-1 text-3xl text-[#0c0c0e]">
                  {isUntitledTypeLabel ? setupTitle : item.theme ?? item.idea}
                </h1>
                {!isUntitledTypeLabel && item.theme && (
                  <p className="mb-2 mt-2 text-[#3f3f46]">{item.idea}</p>
                )}
              </>
            )}

            {showSetupControls && (
              <>
              {/* One-card setup surface (Lane G): Clips/Visuals tabs, Tell Kria,
                  and the Generate CTA all live in a single shadcn Card instead of
                  the old stack of bordered blocks. */}
              <Card>
                {showVisualPools ? (
                  <Tabs defaultValue="clips">
                    <CardHeader className="pb-0">
                      <TabsList>
                        <TabsTrigger value="clips">
                          Clips{clipCount > 0 ? ` (${clipCount})` : ""}
                        </TabsTrigger>
                        <TabsTrigger value="visuals">
                          Visuals{visualsCount > 0 ? ` (${visualsCount})` : ""}
                        </TabsTrigger>
                      </TabsList>
                    </CardHeader>
                    <CardContent className="space-y-6 pt-6">{cardBody}</CardContent>
                  </Tabs>
                ) : (
                  <CardContent className="space-y-6 pt-6">
                    <div>{clipsTabBody}</div>
                    <Separator />
                    {narratedVoiceoverSection}
                    {tellKriaSection}
                    {guidedStatusRowNode}
                  </CardContent>
                )}
                {!isGenerating && (
                  <CardFooter
                    className={`items-center gap-4 border-t pt-6 ${
                      generateHint ? "flex justify-between" : "hidden justify-end sm:flex"
                    }`}
                  >
                    {generateHint && (
                      <p className="text-sm text-muted-foreground">{generateHint}</p>
                    )}
                    <Button
                      onClick={handleGenerate}
                      disabled={generateGated}
                      className="hidden sm:flex"
                    >
                      {generateLabel}
                    </Button>
                  </CardFooter>
                )}
              </Card>

              {guidedEditActive && (
                <PlanThreadPanel
                  open={threadOpen}
                  onOpenChange={setThreadOpen}
                  item={item}
                  // P1-2: mirrors the backend's own media gate for a conversation
                  // turn (routes/plan_items.py) — clip_assignments OR any pool
                  // asset still finishing analysis (queued/analyzing) or ready.
                  // Unrelated to guidedEditAutoDesign (hasReadyPoolMedia above),
                  // which only gates the Generate button.
                  hasPoolMedia={poolAssets.some((asset) =>
                    ["queued", "analyzing", "ready"].includes(asset.status),
                  )}
                  onRefresh={refetch}
                  onChanged={(updated) => {
                    // Apply the authoritative response immediately (G3) — the
                    // conversation POST/PATCH already returned the fresh item,
                    // so don't make the creator wait a poll tick to see it.
                    // Still force-fetch + refetch right after: this keeps the
                    // job-status half of `data` in sync and re-arms polling
                    // (e.g. for conversation_in_progress / analyzing states).
                    applyData((prev) => (prev ? { ...prev, item: updated } : prev));
                    forceFreshFetchRef.current = true;
                    refetch();
                  }}
                />
              )}

              {/* Suggestion rail — AI overlay auto-placement review for the
                  focused variant (plans/005 PR2). Same flag gate as AssetPool;
                  renders nothing until a variant exists, and nothing when the
                  variant's editor_capabilities report suggestions=false (plan
                  010 OV-5 — caption archetypes, song/lyric variants). */}
              {showVisualPools && (
                <SuggestionRail
                  itemId={itemId}
                  variantId={focused?.variant_id ?? null}
                  suggestionsCapability={focused?.editor_capabilities?.suggestions ?? null}
                  previewUrl={
                    // Frozen-frame veil: a stale mini-preview mid-render reads as
                    // "already updated" — withhold it so the rail shows its own
                    // shimmer placeholder until the re-render lands.
                    focused?.render_status === "rendering"
                      ? null
                      : (focused?.output_url ?? focused?.base_video_url ?? null)
                  }
                  rows={overlaySuggestions.rows}
                  onRowsChange={overlaySuggestions.setRows}
                  keptIds={overlaySuggestions.keptIds}
                  onKeptIdsChange={overlaySuggestions.setKeptIds}
                  onSuggestionEdit={overlaySuggestions.onSuggestionEdit}
                  applyReceipt={focused?.overlay_apply_receipt ?? null}
                  onApplied={() => {
                    if (focused) {
                      markVariantRendering(focused.variant_id, focused.render_finished_at ?? null);
                    }
                    refetch();
                  }}
                />
              )}

              {/* Mobile-only sticky Generate bar — the CardFooter button hides on
                  mobile (Lane G) so Generate never appears twice. The hint
                  itself is NOT repeated here — the CardFooter's copy sits
                  directly above this bar on every breakpoint. */}
              {!isGenerating && (
                <div className="sticky bottom-0 z-20 -mx-5 mt-4 border-t border-zinc-200 bg-[#ffffff] px-5 pb-[max(16px,env(safe-area-inset-bottom))] pt-4 sm:hidden md:mx-0 md:px-0">
                  <InkButton onClick={handleGenerate} disabled={generateGated}>
                    {generateLabel}
                  </InkButton>
                </div>
              )}

              {/* Optional planning/conformance conversation stays after Generate. */}
              {showKriaHelper && (
                <div className="mt-4">
                <KriaHelper
                  item={item}
                  conformanceChecking={conformanceChecking}
                  askKria={askKria}
                  onOpen={() => setAskKria("default")}
                  onContest={() => setAskKria("contest")}
                  onClose={() => setAskKria(null)}
                  onDismissConformance={async () => {
                    try {
                      await dismissConformance(itemId);
                    } finally {
                      refetch();
                    }
                  }}
                  onItemChanged={() => {
                    conformancePolls.current = 0;
                    refetch();
                  }}
                />
                </div>
              )}
              </>
            )}


            {/* Error banner — outside the fork so it shows on both item types */}
            {error && (
              <div className="mb-6 rounded border border-zinc-200 bg-white px-4 py-3 text-sm text-[#3f3f46]">
                {error}
              </div>
            )}

            {/* With no preview, setup remains the progress/failure fallback. */}
            {!showResults && renderProgress && <div className="mt-8">{renderProgress}</div>}
            {item.status === "failed" && variants.length === 0 && (
              <div className="mt-3 rounded-xl border border-zinc-200 bg-white px-4 py-3 text-sm text-[#3f3f46]">
                <p className="font-medium text-[#0c0c0e]">{zeroVariantFailure.title}</p>
                <p className="mt-1 text-[#71717a]">{zeroVariantFailure.detail}</p>
                {item.current_job_id && zeroVariantFailure.action === "contact_support" && (
                  <p className="mt-2 text-xs text-[#71717a]">
                    Support reference:{" "}
                    <code className="select-all rounded bg-zinc-100 px-1.5 py-0.5 font-mono text-[11px] text-[#3f3f46]">
                      {item.current_job_id}
                    </code>
                  </p>
                )}
              </div>
            )}
            {/* Style-downgrade explanation: the narrated render fell back to
                montage (no speech found / unreadable clip / flag-skew window).
                Quiet zinc notice — informative, recoverable, never red (DESIGN.md).
                Gated on a finished render WITH variants: the reason persists at
                render START, so without the gate the past-tense "we made a montage"
                would show mid-render, and after a hard failure it would claim a
                montage exists right under "Generation failed". */}
            {fallbackBanner && !isGenerating && variants.length > 0 && (
              <p className="mt-2 rounded-lg border border-zinc-200 bg-white px-3 py-2 text-sm text-[#3f3f46]">
                {fallbackBanner}
              </p>
            )}
          </div>
        </div>

        {/* ── Results: Hero + rail layout ── */}
        {/* FocusedResults owns the edit session and renders the hero+rail layout.
            The hero shows the active variant; the rail shows alternates + rationale
            + editor row. Keyed by variant_id so switching the focused variant
            remounts → fresh session (no stale draft over the new video). */}
        {showResults && (
          <FocusedResults
            key={focused?.variant_id ?? "pending"}
            itemId={itemId}
            item={item}
            variant={focused}
            variants={variants}
            tracks={tracks}
            styleSets={styleSets}
            isGenerating={isGenerating}
            renderProgress={renderProgress}
            refetch={refetch}
            markVariantRendering={markVariantRendering}
            onError={setError}
            onSwap={
              focused
                ? (trackId) => {
                    const trackName = tracks.find((t) => t.id === trackId)?.title ?? "new song";
                    return runEdit(
                      focused.variant_id,
                      focused.render_finished_at ?? null,
                      () => swapPlanItemSong(itemId, focused.variant_id, trackId),
                      { type: "song", label: trackName },
                    );
                  }
                : async () => {}
            }
            onRetext={
              focused
                ? (text) =>
                    runEdit(
                      focused.variant_id,
                      focused.render_finished_at ?? null,
                      () => retextPlanItem(itemId, focused.variant_id, { text }),
                      { type: "text", label: "Updating text…" },
                    )
                : async () => {}
            }
            onRemoveText={
              focused
                ? () =>
                    runEdit(
                      focused.variant_id,
                      focused.render_finished_at ?? null,
                      () => retextPlanItem(itemId, focused.variant_id, { remove: true }),
                      { type: "text", label: "Removing text…" },
                    )
                : async () => {}
            }
            onChangeStyle={
              focused
                ? (styleSetId) =>
                    runEdit(
                      focused.variant_id,
                      focused.render_finished_at ?? null,
                      () => changePlanItemStyle(itemId, focused.variant_id, styleSetId),
                      { type: "style", label: "Applying style…" },
                    )
                : async () => {}
            }
            onResize={
              focused
                ? (px) =>
                    runEdit(
                      focused.variant_id,
                      focused.render_finished_at ?? null,
                      () => setPlanItemIntroSize(itemId, focused.variant_id, px),
                      { type: "style", label: "Updating text size…" },
                    )
                : async () => {}
            }
            onChangeLayout={
              focused
                ? (layout) =>
                    runEdit(
                      focused.variant_id,
                      focused.render_finished_at ?? null,
                      () => editPlanItemVariant(itemId, focused.variant_id, { intro_layout: layout }),
                      { type: "style", label: "Updating layout…" },
                    )
                : async () => {}
            }
            renderingAction={renderingAction.current}
            updatedVariantId={updatedVariantId}
            requestedTab={requestedTab}
            tiktokSimulation={tiktokSimulation}
            onVariantSelect={setFocusedVariantId}
            overlaySuggestions={overlaySuggestions.laneEntries}
            onSuggestionEdit={overlaySuggestions.onSuggestionEdit}
            resolveSuggestionAssetUrl={resolveSuggestionAssetUrl}
            resolveAssetMeta={resolveAssetMeta}
          />
        )}
      </div>
    </LightShell>
  );
}

// ── Variant rationale (client-only, no LLM) ─────────────────────────────────
// Maps text_mode + track_title to a 1-2 sentence blurb shown below the hero.
/** Items minted by /plan/new carry the bare type label as their idea and no
    theme — treat those as untitled and lead with the type (+ montage style)
    eyebrow instead of an h1 that literally reads "Montage". */
function setupIdentityFor(item: PlanItem): { untitled: boolean; receipt: string } {
  // Label from the item's TRUE type family (subtitledEnabled: true), not the
  // flag-folded picker value — a flag-skewed render context (preview deploy,
  // rollback) must never relabel a talking-to-camera item as MONTAGE.
  const resolved = resolvePickerFormat(item.edit_format, true);
  const untitled =
    !item.theme && Object.values(TYPE_COPY).some((copy) => copy.label === item.idea);
  const styleLabel =
    STYLE_TILES.find((tile) => tile.value === (item.montage_preset ?? "classic"))?.label ??
    "Classic";
  const receipt = `${TYPE_COPY[resolved].label}${
    resolved === "montage" ? ` · ${styleLabel}` : ""
  }`.toUpperCase();
  return { untitled, receipt };
}

function deriveRationale(variant: PlanItemVariant, totalVariants: number): string {
  const track = variant.track_title ?? null;
  if (variant.text_mode === "lyrics" && track) return `Beat-synced to ${track}.`;
  if (variant.text_mode === "lyrics") return "Beat-synced lyrics overlay.";
  if (variant.text_mode === "agent_text" && track) return `Styled text over ${track}.`;
  if (variant.text_mode === "agent_text") return "Kria-written intro, your original audio.";
  if (variant.text_mode === "none") return "Your original audio, kept.";
  return `Kria generated ${totalVariants} edit${totalVariants !== 1 ? "s" : ""}.`;
}

// ── Editor panel tabs ────────────────────────────────────────────────────────
// Clips tab removed in PR-5: editing moved inline to the Timeline Clips lane.
// Text + Font tabs removed in PR-4: editing moved inline to the Timeline Text lane.
// Overlays tab removed in PR-3: editing moved inline to the Timeline Overlays lane.
type EditorTab = "song" | "captions" | "timeline";

const EDITOR_TABS: { id: EditorTab; icon: string; label: string }[] = [
  { id: "captions", icon: "CC", label: "Captions" },
  { id: "song", icon: "♫", label: "Song" },
  { id: "timeline", icon: "▭", label: "Timeline" },
];

/**
 * Owns the focused variant's edit session and renders the Hero + rail layout.
 *
 * Layout:
 *   MOBILE — identity, render progress (when active), 9/16 video preview with
 *   variant picker, then the TikTok release desk.
 *
 *   DESKTOP — identity in column one, video preview in column two, and an
 *   independent column-three stack with render progress above the release desk.
 *   Without active render progress, the release desk starts at the top.
 *
 * DEFERRED-BURN model: for an instant-edit-eligible variant the session is the
 * draft store. Caption / Text size / Layout / Style controls mutate that draft
 * with ZERO network; the hero is the text-free base video + a live
 * IntroTextPreview overlay. The single FFmpeg bake fires only on Download.
 *
 * INELIGIBLE variants keep the legacy behavior: burned output_url in the hero +
 * PlanVariantEditor controls that re-render server-side per field.
 *
 * Keyed by variant_id in the parent so the edit session resets when the user
 * focuses a different variant — never showing variant A's draft over variant B.
 */
function FocusedResults({
  itemId,
  item,
  variant,
  variants,
  tracks,
  styleSets,
  isGenerating,
  renderProgress,
  refetch,
  markVariantRendering,
  onError,
  onSwap,
  onRetext,
  onRemoveText,
  onChangeStyle,
  onResize,
  onChangeLayout,
  renderingAction,
  updatedVariantId,
  requestedTab,
  tiktokSimulation,
  onVariantSelect,
  overlaySuggestions,
  onSuggestionEdit,
  resolveSuggestionAssetUrl,
  resolveAssetMeta,
}: {
  itemId: string;
  item: PlanItem;
  variant: PlanItemVariant | null;
  variants: PlanItemVariant[];
  tracks: MusicTrackSummary[];
  styleSets: GenerativeStyleSet[];
  isGenerating: boolean;
  renderProgress?: ReactNode;
  refetch: () => void;
  markVariantRendering: (variantId: string, priorFinishedAt: string | null) => void;
  /** Surface a user-facing error in the page-level banner (e.g. SFX save/render failures). */
  onError: (msg: string) => void;
  onSwap: (trackId: string) => Promise<void>;
  onRetext: (text: string) => Promise<void>;
  onRemoveText: () => Promise<void>;
  onChangeStyle: (styleSetId: string) => Promise<void>;
  onResize: (textSizePx: number) => Promise<void>;
  onChangeLayout: (layout: "linear" | "cluster") => Promise<void>;
  renderingAction: { type: "song" | "text" | "style" | "other"; label: string } | null;
  updatedVariantId: string | null;
  requestedTab: EditorTab | null;
  tiktokSimulation: boolean;
  onVariantSelect: (variantId: string) => void;
  /** 006 T3: pending AI suggestions for the timeline lanes (from the page's
   *  useOverlaySuggestionState — same envelopes SuggestionRail reviews). */
  overlaySuggestions?: SuggestionLaneEntry[];
  onSuggestionEdit?: (suggestionId: string, patch: Partial<MediaOverlay>) => void;
  /** 007 Fix 2: overlay → signed pool display_url for hero suggestion cards. */
  resolveSuggestionAssetUrl?: (overlay: MediaOverlay) => string | undefined;
  /** 009 T5: src_gcs_path → {aspect,width,height} for the fullscreen popover
   *  crop/low-res warnings (page-built join over the suggestion pool assets). */
  resolveAssetMeta?: (
    srcGcsPath: string,
  ) => { aspect?: number; width?: number; height?: number } | undefined;
}) {
  const [activeTab, setActiveTab] = useState<EditorTab | null>(null);
  // T5: textLaneOpen is derived (not state) — true when the timeline tab is open and the variant
  // has text. Text controls are now always visible below the timeline (not in a collapsible panel),
  // so we show LiveEditPreview whenever the user can interact with them.
  // Previously this was state set via onTextPanelChange from UnifiedTimeline; that callback was
  // removed in T5 when the expandable textPanel slot was replaced by the interactive bar lane.
  const textLaneOpen = activeTab === "timeline" && !!variant && variant.text_mode !== "none";
  const requestedTabAppliedRef = useRef<string | null>(null);

  // Frozen-frame veil visibility — lifted from Hero (the only surface that
  // knows whether the stale video errored) so this component, the single
  // owner of both Hero and the ProgressTheater (`renderProgress`) mount, can
  // enforce "the veil is the sole rendering voice while it's
  // visible": the theater stays out of the result grid in the exact window
  // the veil covers the hero. See the `veilVisible` computation further down,
  // once `instantEligible` (LiveEditPreview vs. Hero) is known.
  const [playbackFailed, setPlaybackFailed] = useState(false);

  // ── Overlay-card state (lifted here so Hero can render the instant preview) ─
  const [overlayCards, setOverlayCards] = useState<MediaOverlay[]>(
    variant?.media_overlays ?? [],
  );
  // Seed from preview_url on load so existing applied cards show in the CSS overlay
  // immediately without re-uploading (preview_url is a fresh-signed read URL from the API).
  // localPreviewUrls: blob: URLs from freshly-uploaded card files. NOT initialised from
  // preview_url — the burned output_url already shows those cards, so using preview_url
  // here would double the overlay on page load. Cleared when a burn completes (render_finished_at
  // effect below), so the burned output takes over without doubling.
  const [localPreviewUrls, setLocalPreviewUrls] = useState<Record<string, string>>({});
  // Plan 009 T4: card ids whose preview media failed to load (routine — signed
  // URLs expire in 24h). While any CURRENT card is failed, the Download
  // overlay-bake path is blocked with inline copy; lifted from
  // LiveOverlayCardsLayer via onCardMediaError.
  const [failedCardIds, setFailedCardIds] = useState<Set<string>>(new Set());
  // Plan 009 T4 click-to-edit: card id whose timeline popover was requested by
  // clicking a fullscreen card's frame in the hero. Consumed by UnifiedTimeline
  // (externalEditCardId / onExternalEditHandled, T3 props).
  const [requestedEditCardId, setRequestedEditCardId] = useState<string | null>(null);
  // SFX placements — lifted alongside overlayCards so both stay in sync with the active variant.
  const [sfxPlacements, setSfxPlacements] = useState<SoundEffectPlacement[]>(
    variant?.sound_effects ?? [],
  );
  // sfxAudioUrls: map from src_gcs_path → playable URL (signed GCS or blob URL) for instant preview.
  const [sfxAudioUrls, setSfxAudioUrls] = useState<Record<string, string>>({});
  // SFX glossary — owned HERE (not in FocusedVariantControls) so APPLIED
  // placements loaded from the variant get playable URLs even when no editor
  // tab is open: the hero's useSfxPreview needs sfxAudioUrls populated to make
  // saved effects audible, not just freshly-picked ones.
  const [glossaryEffects, setGlossaryEffects] = useState<SoundEffectSummary[]>([]);
  const [glossaryLoading, setGlossaryLoading] = useState(false);
  // Load the glossary when the Timeline tab first opens (picker needs the list)
  // OR as soon as an applied glossary placement lacks a preview URL (hero
  // playback needs preview_audio_url from the glossary payload).
  const needsGlossaryForApplied = sfxPlacements.some(
    (p) => p.sound_effect_id && !sfxAudioUrls[sfxUrlKey(p)],
  );
  useEffect(() => {
    if (!SOUND_EFFECTS_ENABLED) return;
    if (glossaryEffects.length > 0) return;
    if (activeTab !== "timeline" && !needsGlossaryForApplied) return;
    setGlossaryLoading(true);
    getSoundEffects()
      .then(setGlossaryEffects)
      .catch(() => {/* glossary is best-effort */})
      .finally(() => setGlossaryLoading(false));
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeTab, needsGlossaryForApplied]);
  // Fetch signed playback URLs for SFX placements that don't have one yet.
  // Key: use src_gcs_path when available, fall back to placement.id so glossary
  // effects (src_gcs_path="" until server resolves it) get a URL immediately.
  useEffect(() => {
    if (!SOUND_EFFECTS_ENABLED) return;
    const { glossaryUrls, userUploadPaths } = resolveSfxPreviewUrls(
      sfxPlacements,
      glossaryEffects,
      sfxAudioUrls,
    );

    if (Object.keys(glossaryUrls).length > 0) {
      setSfxAudioUrls((prev) => ({ ...prev, ...glossaryUrls }));
    }

    for (const p of userUploadPaths) {
      getSfxAudioUrl(itemId, p.src_gcs_path)
        .then((url) => setSfxAudioUrls((prev) => ({ ...prev, [p.src_gcs_path]: url })))
        .catch(() => {});
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sfxPlacements, glossaryEffects, sfxAudioUrls, itemId]);
  // Current video time lifted from the hero player so "Add at playhead" works.
  const [currentTimeS, setCurrentTimeS] = useState(0);
  // 007 Fix 2: thumbnail lookup for the hero direct-manipulation cards —
  // local blob previews first (freshly uploaded), then the page-built signed
  // pool display_url join.
  const resolveSuggestionCardUrl = useCallback(
    (overlay: MediaOverlay): string | undefined =>
      localPreviewUrls[overlay.id] ?? resolveSuggestionAssetUrl?.(overlay),
    [localPreviewUrls, resolveSuggestionAssetUrl],
  );
  // Plan 009 T4: failed-media lift + failed-tile Remove + fullscreen
  // click-to-edit. These serve BOTH hero surfaces (Hero and LiveEditPreview —
  // both mount LiveOverlayCardsLayer + HeroOverlayEditor).
  const handleCardMediaError = useCallback((cardId: string) => {
    setFailedCardIds((prev) => {
      if (prev.has(cardId)) return prev;
      const next = new Set(prev);
      next.add(cardId);
      return next;
    });
  }, []);
  const handleRemoveFailedCard = useCallback((cardId: string) => {
    if (!variant) return;
    const next = removeOverlayEffectGroup(
      { overlays: overlayCards, soundEffects: sfxPlacements, cameraEffects: [] },
      cardId,
    );
    setOverlayCards(next.overlays);
    setSfxPlacements(next.soundEffects);
    // Failed cards live above PlanVariantEditor's timeline state, so they do
    // not pass through its overlaysDirtyRef. Persist the desired empty/list
    // state here; the server marks it render-dirty and cascades grouped camera
    // effects as well. Without this, deleting the last failed card could leave
    // the old overlay baked forever because Download saw a clean local state.
    void setVariantMediaOverlays(itemId, variant.variant_id, next.overlays, {
      render: false,
    })
      .then(() => refetch())
      .catch((err) => {
        onError("We couldn't remove that overlay. Try again.");
      });
    setLocalPreviewUrls((prev) => {
      if (!prev[cardId]) return prev;
      URL.revokeObjectURL(prev[cardId]);
      const next = { ...prev };
      delete next[cardId];
      return next;
    });
    setFailedCardIds((prev) => {
      if (!prev.has(cardId)) return prev;
      const next = new Set(prev);
      next.delete(cardId);
      return next;
    });
  }, [itemId, onError, overlayCards, refetch, sfxPlacements, variant]);
  const handleRequestEditCard = useCallback((cardId: string) => {
    // The popover lives in the timeline lanes — make sure they are mounted.
    setActiveTab("timeline");
    setRequestedEditCardId(cardId);
  }, []);
  useEffect(() => {
    const nextCards = variant?.media_overlays ?? [];
    setOverlayCards(nextCards);
    setSfxPlacements(variant?.sound_effects ?? []);
    setSfxAudioUrls({});
    setFailedCardIds(new Set());
    setRequestedEditCardId(null);
    // Revoke any blob URLs from the previous variant and reset to empty.
    // Do NOT repopulate from preview_url — the burned output_url already shows the cards.
    setLocalPreviewUrls((prev) => {
      Object.values(prev).forEach((url) => URL.revokeObjectURL(url));
      return {};
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [variant?.variant_id]);
  // Plan 007 Fix 3 (decision D4-A): the effect above keys on variant_id only, so
  // server-side card mutations on the SAME variant (Apply burn, zero-click
  // auto-apply) never reached the lanes until a full page reload — the timeline
  // showed empty OVERLAYS/SFX on a variant with baked visuals. Re-sync from the
  // refetched variant when the burn-completion signal advances. Keyed to
  // render_finished_at: no edit session exists at burn completion, so this can
  // never clobber in-flight local edits.
  useEffect(() => {
    if (!variant?.render_finished_at) return;
    setOverlayCards(variant?.media_overlays ?? []);
    setSfxPlacements(variant?.sound_effects ?? []);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [variant?.render_finished_at]);
  // Declared here (before the render_finished_at effect) so the effect can read it.
  // The full definition lives further down alongside handleDownload.
  const pendingExportRef = useRef<"download" | "publish" | null>(null);
  const [exportPending, setExportPending] = useState(false);
  const [publishOpen, setPublishOpen] = useState(false);
  const [tiktokConnection, setTikTokConnection] = useState<TikTokConnection | null>(null);
  const [allTikTokPublications, setAllTikTokPublications] = useState<TikTokPublication[]>([]);
  const [tiktokPublications, setTikTokPublications] = useState<TikTokPublication[]>([]);
  const [tiktokReceiptState, setTikTokReceiptState] = useState<"loading" | "ready" | "error">("loading");
  const [tiktokReceiptRefresh, setTikTokReceiptRefresh] = useState(0);
  const [tiktokPollStalled, setTikTokPollStalled] = useState(false);
  const [tiktokComparisonAvailable, setTikTokComparisonAvailable] = useState(true);
  // Newest-first by submission time. A slow status poll for an OLDER publication
  // can resolve after a newer one was created; prepending blindly would make the
  // settled old row the "latest", which unlocks republish while the new delivery
  // is still submitting.
  const mergeTikTokPublication = (
    current: TikTokPublication[],
    publication: TikTokPublication,
  ) => [publication, ...current.filter((value) => value.id !== publication.id)]
    .sort((a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime());

  const upsertTikTokPublication = useCallback((publication: TikTokPublication) => {
    setTikTokPublications((current) => mergeTikTokPublication(current, publication));
    setAllTikTokPublications((current) => mergeTikTokPublication(current, publication));
  }, []);

  // Bumped ONLY when a publish creates a row the server fetch may not know about
  // yet. Status polls deliberately do NOT bump: they refresh rows the fetch also
  // returns, and counting them would make the effect below discard legitimate
  // variant-switch results and strand the new variant on stale publications.
  const tiktokPublishWrite = useRef(0);
  const onTikTokPublished = useCallback((publication: TikTokPublication) => {
    tiktokPublishWrite.current += 1;
    upsertTikTokPublication(publication);
  }, [upsertTikTokPublication]);

  useEffect(() => {
    let cancelled = false;
    const focusedVariantId = variant?.variant_id ?? null;
    const publishWriteAtStart = tiktokPublishWrite.current;
    setTikTokReceiptState("loading");
    setTikTokPollStalled(false);
    setTikTokComparisonAvailable(true);
    void getTikTokConnection()
      .then((connection) => {
        if (!cancelled) setTikTokConnection(connection);
      })
      .catch(() => {
        if (!cancelled) setTikTokConnection(null);
      });
    void Promise.all([
      item.current_job_id
        ? getTikTokPublicationReceipt(item.current_job_id, focusedVariantId ?? undefined)
        : Promise.resolve(null),
      item.current_job_id
        ? listTikTokPublications({ jobId: item.current_job_id, variantId: focusedVariantId ?? undefined })
            .then((publications) => publications.filter(
              (publication) =>
                publication.job_id === item.current_job_id &&
                (!focusedVariantId || publication.variant_id === focusedVariantId),
            ))
            .catch(() => [])
        : Promise.resolve([] as TikTokPublication[]),
      listTikTokPublications()
        .then((publications) => ({ publications, available: true }))
        .catch(() => ({ publications: [] as TikTokPublication[], available: false })),
    ])
      .then(([itemPublication, itemHistory, comparisonResult]) => {
        if (cancelled) return;
        // The receipt endpoint only filters by variant when the param is sent,
        // so a variant-less call returns the job's latest publication across ALL
        // variants. Trust it as this variant's receipt only when it matches;
        // otherwise fall back to the already variant-scoped history, so one
        // variant's publication never becomes another's receipt.
        const matchedPublication =
          itemPublication && itemPublication.variant_id === focusedVariantId
            ? itemPublication
            : null;
        // A publish landed while this request was in flight — its result is
        // already staler than local state. Keep the receipt, drop the response.
        if (tiktokPublishWrite.current !== publishWriteAtStart) {
          setTikTokReceiptState("ready");
          return;
        }
        setTikTokPublications(matchedPublication
          ? [matchedPublication, ...itemHistory.filter((publication) => publication.id !== matchedPublication.id)]
          : itemHistory);
        setAllTikTokPublications(comparisonResult.publications);
        setTikTokComparisonAvailable(comparisonResult.available);
        setTikTokReceiptState("ready");
      })
      .catch(() => {
        if (!cancelled) setTikTokReceiptState("error");
      });
    return () => { cancelled = true; };
    // render_finished_at: an edit reburns the SAME variant_id, so without it the
    // receipt goes stale after the creator edits a published video.
  }, [item.current_job_id, variant?.variant_id, variant?.render_finished_at, tiktokReceiptRefresh]);
  const latestTikTokPublication = tiktokPublications[0] ?? null;
  useEffect(() => {
    if (!latestTikTokPublication || !shouldPollTikTokPublication(latestTikTokPublication)) return;
    let consecutiveFailures = 0;
    const timer = window.setInterval(() => {
      void getTikTokPublication(latestTikTokPublication.id)
        .then((publication) => {
          consecutiveFailures = 0;
          setTikTokPollStalled(false);
          upsertTikTokPublication(publication);
        })
        .catch(() => {
          consecutiveFailures += 1;
          if (consecutiveFailures >= TIKTOK_POLL_MAX_FAILURES) {
            window.clearInterval(timer);
            setTikTokPollStalled(true);
          }
        });
    }, TIKTOK_POLL_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [latestTikTokPublication, upsertTikTokPublication]);

  // When an export-triggered burn completes (render_finished_at advances), clear the CSS
  // preview layer — the burned output_url now has the cards composited in. Only fires when
  // an export is pending so stale/concurrent renders (e.g. completing text edits, or
  // lingering renders from a previous session) don't wipe newly uploaded card previews.
  const prevFinishedAtRef = useRef<string | null | undefined>(undefined);
  useEffect(() => {
    const cur = variant?.render_finished_at ?? null;
    if (prevFinishedAtRef.current !== undefined && cur !== prevFinishedAtRef.current) {
      if (pendingExportRef.current) {
        setLocalPreviewUrls((prev) => {
          Object.values(prev).forEach((url) => URL.revokeObjectURL(url));
          return {};
        });
      }
    }
    prevFinishedAtRef.current = cur;
  }, [variant?.render_finished_at]);

  // Revoke all blob URLs when the component unmounts (FocusedResults is re-keyed
  // on variant switch, so unmount fires when the user focuses a different variant).
  useEffect(() => {
    return () => {
      setLocalPreviewUrls((prev) => {
        Object.values(prev).forEach((url) => URL.revokeObjectURL(url));
        return {};
      });
    };
  }, []);

  // ── Deferred-burn session — eligible variants only ──────────────────────────
  // Use a stable no-op variant when nothing is focused yet (pre-first-render).
  const stableVariant: PlanItemVariant = variant ?? {
    variant_id: "__pending__",
    output_url: null,
    render_status: null,
    text_mode: "none",
    style_set_id: null,
    intro_text_size_px: null,
  };

  const editSession = useVariantEditSession(stableVariant, async (payload) => {
    if (!variant) return;
    await editPlanItemVariant(itemId, variant.variant_id, payload);
    refetch();
  });
  const instantEligible = variant ? isInstantEditEligible(variant) : false;
  const textLaneEligible = variant ? isTextLaneEligible(variant) : false;

  // Mirrors the ternary below that picks LiveEditPreview vs. Hero — only Hero
  // ever mounts the frozen-frame veil, so the theater must stay untouched
  // whenever LiveEditPreview (no veil at all) is the active preview surface.
  const usingLiveEditPreview =
    instantEligible && !!variant && (activeTab !== "timeline" || textLaneOpen);
  // Single source of truth for "the veil is covering the hero right now" —
  // matches the veil's own render gate in Hero (`rendering && output_url &&
  // !playbackFailed`). ProgressTheater (`renderProgress`, in the result grid)
  // is hidden exactly when this is true, per the "one rendering voice at a
  // time" contract: elsewhere (no output yet, a non-focused variant
  // rendering, or the stale video failing to play) the theater is the only
  // indicator.
  const veilVisible =
    !usingLiveEditPreview &&
    !!variant &&
    variant.render_status === "rendering" &&
    !!variant.output_url &&
    !playbackFailed;

  // ── Auto-open the Captions tab for caption archetypes (caption-edit
  // discoverability fix) ────────────────────────────────────────────────────
  // Talking-to-camera / voiceover edits edit their captions in the Captions
  // tab, not the timeline shell — landing with every tab collapsed reads as
  // "there's nowhere to edit my captions". Open it for them once the render is
  // ready. Precedence: a save/return render flow owns the screen
  // (renderingAction != null) and the user's own tab choice wins, so we only
  // auto-open on a clean, ready load with cues present, exactly once per mount.
  const autoOpenedCaptionsRef = useRef(false);
  const pendingCaptionScrollRef = useRef(false);
  useEffect(() => {
    if (requestedTab !== "captions" || !variant) return;
    const key = `${variant.variant_id}:captions`;
    if (requestedTabAppliedRef.current === key) return;
    requestedTabAppliedRef.current = key;
    pendingCaptionScrollRef.current = true;
    autoOpenedCaptionsRef.current = true;
    setActiveTab("captions");
  }, [requestedTab, variant]);
  useEffect(() => {
    if (autoOpenedCaptionsRef.current) return;
    if (activeTab !== null) return; // user already picked a tab — don't override
    if (renderingAction !== null) return; // a render/return flow is in progress
    if (!variant || !isCaptionArchetype(variant)) return;
    if (variant.render_status !== "ready" || !variant.caption_cues) return;
    autoOpenedCaptionsRef.current = true;
    pendingCaptionScrollRef.current = true;
    setActiveTab("captions");
  }, [variant, activeTab, renderingAction]);

  // Bring the auto-opened Captions panel into view: it sits far down a long
  // page, so silently opening it still reads as "nothing happened" for a user
  // who arrived from the editor shell's Captions link. Mirrors the
  // focusShotListAfterAccept scroll pattern (reduce-motion aware; block:"nearest"
  // so an already-visible panel doesn't jump).
  useEffect(() => {
    if (activeTab !== "captions" || !pendingCaptionScrollRef.current) return;
    pendingCaptionScrollRef.current = false;
    window.requestAnimationFrame(() => {
      const el = document.querySelector<HTMLElement>("[data-plan-captions-panel]");
      if (!el) return;
      const reduceMotion =
        typeof window.matchMedia === "function" &&
        window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      el.scrollIntoView({ block: "nearest", behavior: reduceMotion ? "auto" : "smooth" });
    });
  }, [activeTab]);

  useEffect(() => {
    if (instantEligible && !editSession.isEditing) editSession.enterEdit();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [instantEligible]);

  useEffect(() => {
    if (!editSession.isSaving) return;
    const t = setInterval(refetch, 2000);
    return () => clearInterval(t);
  }, [editSession.isSaving, refetch]);

  const downloadName = `kria-${slugify(item.theme ?? "") || itemId.slice(0, 8)}.mp4`;

  useEffect(() => {
    if (!pendingExportRef.current) return;
    if (editSession.commitError) {
      // commit() intentionally catches HTTP failures so the inline editor can
      // reopen with its draft intact. An export must still treat that settled
      // promise as a failure; otherwise the unchanged pre-edit ready URL would
      // be downloaded or published as though it contained the user's edits.
      pendingExportRef.current = null;
      setExportPending(false);
      onError(editSession.commitError);
      return;
    }
    if (editSession.isSaving) return;
    if (variant?.render_status === "ready" && variant.output_url) {
      const action = pendingExportRef.current;
      pendingExportRef.current = null;
      setExportPending(false);
      if (action === "download")
        downloadVideo(
          variant.download_url ?? variant.output_url,
          downloadName,
          !variant.download_url,
        );
      else setPublishOpen(true);
    } else if (variant?.render_status === "failed") {
      // An export-triggered bake failed on the backend (FFmpeg error after a
      // successful dispatch). Surface it — otherwise the button silently
      // re-enables, no file downloads, and the stale output_url keeps playing
      // as if it succeeded. The Apply/Retry button that used to surface this is
      // gone; the implicit retry is to click Download again (needsSfxBake stays
      // true after a failed bake).
      pendingExportRef.current = null;
      setExportPending(false);
      onError("Couldn't prepare your video. Please try again.");
    }
  }, [
    editSession.commitError,
    editSession.isSaving,
    variant?.render_status,
    variant?.output_url,
    variant?.download_url,
    downloadName,
    onError,
  ]);

  const baking = (instantEligible && editSession.isSaving) || exportPending;

  // Inline SFX dirtiness (D5): does the download need a fresh SFX bake, and is
  // the latest set persisted? Computed from the variant + placements — no
  // sticky flag. See lib/sfx-dirty.ts.
  const needsSfxBake = sfxNeedsBake(sfxPlacements, variant);
  const sfxIsPersistDirty = sfxPersistDirty(sfxPlacements, variant);

  // Plan 009 T4: failed cards still in the working set — derived as an
  // intersection so removing a card (tile Remove or lane) unblocks instantly.
  const failedOverlayCount = overlayCards.filter((c) => failedCardIds.has(c.id)).length;
  // render=false autosave only updates desired metadata. Keep the overlay
  // branch active after the last card is removed until a token-winning render
  // clears this persisted dirty bit. The local-vs-persisted comparison closes
  // the window before the render:false autosave/refetch lands (especially a
  // deletion-to-zero, where card-count alone cannot express pending work).
  const overlaysDifferFromPersisted =
    JSON.stringify(overlayCards) !==
    JSON.stringify(variant?.media_overlays ?? []);
  const needsOverlayBake =
    overlayCards.length > 0 ||
    overlaysDifferFromPersisted ||
    Boolean(variant?.media_overlays_render_dirty);

  const prepareExactExport = useCallback(async (action: "download" | "publish") => {
    if (!variant) return;
    if (pendingExportRef.current) return;
    if (variant.render_status !== "ready" || !variant.output_url) {
      onError("This video is still rendering. Try again when it is ready.");
      return;
    }

    // Flush the latest SFX placements before any bake. SFX edits save on a
    // 600ms debounce; without this, a fast Download (or the post-overlay SFX
    // reapply, which reads persisted placements) would mix a STALE set while
    // the live preview shows the current one.
    const flushSfx = async () => {
      if (sfxIsPersistDirty) {
        await setVariantSoundEffects(itemId, variant.variant_id, sfxPlacements);
      }
    };

    // Overlay-first: an overlay bake re-applies persisted SFX on top (backend),
    // composing BOTH lanes in one pass, and stays "rendering" until the SFX
    // remix finishes (two-pass observability). Must run before the SFX-only
    // branch so a co-edit isn't split across two Download clicks.
    if (needsOverlayBake) {
      // Plan 009 T4: a card whose media failed to load would bake a broken /
      // blank visual — block the overlay-bake path until it's refreshed or
      // removed (inline copy under the button explains why).
      if (failedOverlayCount > 0) return;
      pendingExportRef.current = action;
      setExportPending(true);
      try {
        await flushSfx();
        await setVariantMediaOverlays(itemId, variant.variant_id, overlayCards, { render: true });
        markVariantRendering(variant.variant_id, variant.render_finished_at ?? null);
      } catch (err) {
        pendingExportRef.current = null;
        setExportPending(false);
        onError("We couldn't add your overlays to the video. Try again.");
      }
      return;
    }

    // SFX-only: bake when placements differ from what's baked into output_url.
    // Inline compare (not a sticky flag) → "nothing changed" downloads instantly.
    if (needsSfxBake) {
      pendingExportRef.current = action;
      setExportPending(true);
      try {
        await flushSfx();
        await renderVariantSfx(itemId, variant.variant_id);
        markVariantRendering(variant.variant_id, variant.render_finished_at ?? null);
      } catch (err) {
        pendingExportRef.current = null;
        setExportPending(false);
        onError("We couldn't add your sound effects to the video. Try again.");
      }
      return;
    }

    if (instantEligible && editSession.isDirty) {
      pendingExportRef.current = action;
      setExportPending(true);
      void editSession.commit();
      return;
    }
    if (variant.output_url) {
      if (action === "download")
        downloadVideo(
          variant.download_url ?? variant.output_url,
          downloadName,
          !variant.download_url,
        );
      else setPublishOpen(true);
    }
  }, [variant, editSession, instantEligible, sfxPlacements, needsSfxBake, sfxIsPersistDirty, overlayCards, needsOverlayBake, failedOverlayCount, itemId, downloadName, markVariantRendering, onError]);

  const handleDownload = useCallback(() => {
    void prepareExactExport("download");
  }, [prepareExactExport]);

  const handlePublish = useCallback(() => {
    if (tiktokSimulation) {
      setPublishOpen(true);
      return;
    }
    void prepareExactExport("publish");
  }, [prepareExactExport, tiktokSimulation]);

  // "Kria's pick" is always the first variant (index 0 in the variants array)
  const isKriaPick = variant != null && variants.length > 0 && variants[0].variant_id === variant.variant_id;

  // Text-mode label for the pill below the hero. Narrated variants carry the
  // creator's recorded voiceover (not the clips' original audio), so they get
  // their own label regardless of text_mode ("none").
  const TEXT_MODE_PILL: Record<string, string> = {
    lyrics: "With lyrics",
    agent_text: "Original audio",
    none: "Original audio",
  };
  const modePill = variant
    ? variant.resolved_archetype === "narrated"
      ? "Narration"
      : variant.track_title || variant.music_track_id
        ? variant.text_mode === "lyrics"
          ? "With lyrics"
          : "Music"
      : (TEXT_MODE_PILL[variant.text_mode] ?? "Original audio")
    : null;

  // Flag-gated Edit entry into the full-screen TikTok-style editor shell.
  // Eligible = rendered (output_url present) and not mid-render. If the
  // server's editor_capabilities map is present and every capability is
  // false, the button still shows but disabled, with the server's reason
  // as the tooltip (kills FE 404-probing on a genuinely ineligible variant).
  const editorEntryEligible =
    TIKTOK_EDITOR_ENABLED &&
    !!variant &&
    !!variant.output_url &&
    variant.render_status !== "rendering";
  const editorEntryDisabledReason = planItemEditorDisabledReason(variant);
  const editorHref = editorEntryEligible && !editorEntryDisabledReason && variant
    ? `/plan/items/${itemId}/edit?variant=${variant.variant_id}`
    : null;
  const releaseVariantLabel = [isKriaPick ? "Kria's pick" : null, modePill]
    .filter(Boolean)
    .join(" · ") || "Original";
  const releaseTikTokConnection: TikTokConnection | null = tiktokSimulation
    ? {
        available: true,
        connected: true,
        status: "connected",
        account: tiktokConnection?.account ?? { display_name: "Emir" },
        granted_scopes: ["video.publish", "video.upload"],
        can_publish: true,
        can_upload_draft: true,
        can_analyze: true,
        audited: true,
        beta: false,
        last_synced_at: null,
        learned_post_count: 0,
      }
    : tiktokConnection;
  // Keep one live ProgressTheater instance while moving it responsively. All
  // result regions remain direct children of this grid at every breakpoint,
  // so a resize cannot remount the progress feed or release desk and reset
  // their disclosure state. Mobile source/focus order is tracker, preview,
  // release desk; desktop grid coordinates put tracker + desk in column three.
  // A second breakpoint-hidden instance would duplicate timers, live-region
  // announcements, and IDs.
  const showRenderProgress = Boolean(renderProgress) && !veilVisible;
  const progressRegion = showRenderProgress ? (
    <section
      key="render-progress"
      aria-label="Render timing"
      data-testid="result-render-progress"
      className="order-2 min-w-0 lg:col-start-3 lg:row-start-1 lg:order-none lg:pt-3"
    >
      {renderProgress}
    </section>
  ) : null;
  const releaseDesk = (
    <div
      key="release-desk"
      data-testid="result-release-column"
      className={`order-4 min-w-0 lg:col-start-3 lg:order-none ${
        showRenderProgress
          ? "lg:row-start-2"
          : "lg:row-start-1 lg:row-end-3 lg:pt-3"
      }`}
    >
      <TikTokReleaseRail
        connection={releaseTikTokConnection}
        publication={latestTikTokPublication}
        publications={tiktokPublications}
        comparisonPublications={allTikTokPublications}
        receiptState={tiktokSimulation ? "ready" : tiktokReceiptState}
        pollingStalled={tiktokPollStalled}
        videoReady={Boolean(variant?.render_status === "ready" && variant.output_url)}
        comparisonAvailable={tiktokComparisonAvailable}
        canPublish={Boolean(
          (tiktokSimulation || tiktokReceiptState === "ready") &&
          (releaseTikTokConnection?.can_publish || releaseTikTokConnection?.can_upload_draft) &&
          item.current_job_id &&
          variant?.render_status === "ready" &&
          variant.output_url,
        )}
        baking={baking}
        editHref={editorHref}
        durationSeconds={variant?.duration_s ?? null}
        renderFinishedAt={variant?.render_finished_at ?? null}
        variantLabel={releaseVariantLabel}
        captionPreview={item.idea}
        onPublish={handlePublish}
        onDownload={handleDownload}
        onConnect={() => void startTikTokOAuth(`${window.location.pathname}${window.location.search}`)}
        onReceiptRetry={() => setTikTokReceiptRefresh((value) => value + 1)}
        simulation={tiktokSimulation}
      />

      {failedOverlayCount > 0 && (
        <p className="mt-4 text-sm text-[#3f3f46]">
          {failedOverlayCount === 1
            ? "One visual couldn't load. Refresh or remove it before exporting."
            : `${failedOverlayCount} visuals couldn't load. Refresh or remove them before exporting.`}
        </p>
      )}
      {((instantEligible && editSession.isDirty) || needsSfxBake) && !baking && (
        <p className="mt-3 text-xs text-[#71717a]">Your next export will include these edits.</p>
      )}
    </div>
  );

  return (
    <div className="mt-2 lg:-mt-4">
      <div
        className="grid grid-cols-1 gap-y-6 lg:grid-cols-[minmax(210px,0.75fr)_minmax(320px,430px)_minmax(300px,0.95fr)] lg:grid-rows-[auto_1fr] lg:items-start lg:gap-x-8 xl:gap-x-12"
      >
        <section
          key="identity"
          className="order-1 lg:col-start-1 lg:row-start-1 lg:row-end-3 lg:pt-3"
          aria-labelledby="release-item-title"
        >
          <Button variant="link" size="sm" asChild className="h-auto p-0 text-muted-foreground hover:text-foreground">
            <Link href="/plan">
              <ArrowLeft className="h-4 w-4" aria-hidden="true" />
              Back to plan
            </Link>
          </Button>
          {(() => {
            const { untitled, receipt } = setupIdentityFor(item);
            return untitled ? (
            <p
              id="release-item-title"
              className="mt-4 text-[11px] font-semibold uppercase tracking-[0.18em] text-lime-700"
            >
              {receipt}
            </p>
          ) : (
            <h1
              id="release-item-title"
              className="mt-3 line-clamp-2 text-3xl font-semibold tracking-tight text-foreground lg:line-clamp-none"
            >
              {item.theme ?? item.idea}
            </h1>
          );
          })()}
          {!setupIdentityFor(item).untitled && item.theme && (
            <p className="mt-4 hidden text-sm text-muted-foreground lg:block">{item.idea}</p>
          )}
          {variant && !isGenerating && (
            <p className="mt-6 hidden border-l-2 border-border pl-3 text-sm text-muted-foreground lg:block">
              {deriveRationale(variant, variants.length)}
            </p>
          )}
        </section>

        {progressRegion}

        <div
          key="preview"
          data-testid="result-preview-column"
          className="order-3 w-full lg:col-start-2 lg:row-start-1 lg:row-end-3 lg:mx-auto lg:max-w-[430px]"
        >
          <div className="relative" data-variant-preview={variant?.variant_id}>
            {instantEligible && variant && (activeTab !== "timeline" || textLaneOpen) ? (
              <LiveEditPreview
                variant={variant}
                styleSets={styleSets}
                session={editSession}
                playToken={editSession.playToken}
                textElements={variant.text_elements ?? undefined}
                sfxPlacements={sfxPlacements}
                sfxAudioUrls={sfxAudioUrls}
                overlayCards={overlayCards}
                localPreviewUrls={localPreviewUrls}
                suggestionEntries={overlaySuggestions}
                onSuggestionEdit={onSuggestionEdit}
                resolveSuggestionCardUrl={resolveSuggestionCardUrl}
                onCardMediaError={handleCardMediaError}
                onRemoveCard={handleRemoveFailedCard}
                onRequestEditCard={handleRequestEditCard}
              />
            ) : (
              <Hero
                variant={variant}
                generating={isGenerating}
                overlayCards={overlayCards}
                localPreviewUrls={localPreviewUrls}
                sfxPlacements={sfxPlacements}
                sfxAudioUrls={sfxAudioUrls}
                renderingAction={renderingAction}
                showUpdatedCue={updatedVariantId === variant?.variant_id}
                suggestionEntries={overlaySuggestions}
                onSuggestionEdit={onSuggestionEdit}
                resolveSuggestionCardUrl={resolveSuggestionCardUrl}
                onCardMediaError={handleCardMediaError}
                onRemoveCard={handleRemoveFailedCard}
                onRequestEditCard={handleRequestEditCard}
                onDownload={handleDownload}
                playbackFailed={playbackFailed}
                onPlaybackFailedChange={setPlaybackFailed}
              />
            )}
          </div>
          {variants.filter((value) => value.output_url).length > 1 && (
            <VariantReleasePicker
              variants={variants}
              selectedVariantId={variant?.variant_id ?? null}
              onSelect={onVariantSelect}
            />
          )}
        </div>

        {releaseDesk}
      </div>

      {item.current_job_id && variant && (
        <TikTokPublishDialog
          open={publishOpen}
          jobId={item.current_job_id}
          variantId={variant.variant_id}
          videoTitle={item.theme ?? item.idea}
          variantLabel={releaseVariantLabel}
          accountAvatarUrl={releaseTikTokConnection?.account?.avatar_url ?? null}
          simulation={tiktokSimulation && variant.output_url
            ? {
                creatorNickname: releaseTikTokConnection?.account?.display_name ?? "Emir",
                previewUrl: variant.output_url,
                durationSeconds: variant.duration_s ?? null,
              }
            : null}
          onClose={() => setPublishOpen(false)}
          onPublished={onTikTokPublished}
        />
      )}
    </div>
  );
}

/**
 * Controls-only column for the focused variant. Receives the edit session as a
 * prop (the parent owns it, keyed by variant_id) — it does NOT create one.
 *
 * `activeTab` controls which section of PlanVariantEditor is surfaced. The
 * "song" tab shows the song-swap picker; "clips" opens the timeline editor sheet.
 * Text/font editing is now inline in the UnifiedTimeline Text lane (PR-4).
 *
 * For an ELIGIBLE variant the Caption / Text size / Layout / Style controls are
 * re-pointed at the session draft (no render). Song + Clips keep their server
 * paths. An INELIGIBLE variant gets the original server handlers (per-field
 * re-render, legacy behavior).
 */
function FocusedVariantControls({
  itemId,
  variant,
  tracks,
  styleSets,
  session,
  instantEligible,
  baking,
  activeTab,
  refetch,
  markVariantRendering,
  onSwap,
  onRetext,
  onRemoveText,
  onChangeStyle,
  onResize,
  onChangeLayout,
  overlayCards,
  setOverlayCards,
  localPreviewUrls,
  setLocalPreviewUrls,
  sfxPlacements,
  setSfxPlacements,
  glossaryEffects,
  glossaryLoading,
  currentTimeS,
  onError,
  overlaySuggestions,
  onSuggestionEdit,
  resolveAssetMeta,
  externalEditCardId,
  onExternalEditHandled,
}: {
  itemId: string;
  variant: PlanItemVariant;
  tracks: MusicTrackSummary[];
  styleSets: GenerativeStyleSet[];
  session: VariantEditSession;
  instantEligible: boolean;
  baking: boolean;
  activeTab: EditorTab;
  refetch: () => void;
  markVariantRendering: (variantId: string, priorFinishedAt: string | null) => void;
  onSwap: (trackId: string) => Promise<void>;
  onRetext: (text: string) => Promise<void>;
  onRemoveText: () => Promise<void>;
  onChangeStyle: (styleSetId: string) => Promise<void>;
  onResize: (textSizePx: number) => Promise<void>;
  onChangeLayout: (layout: "linear" | "cluster") => Promise<void>;
  overlayCards: MediaOverlay[];
  setOverlayCards: Dispatch<SetStateAction<MediaOverlay[]>>;
  localPreviewUrls: Record<string, string>;
  setLocalPreviewUrls: Dispatch<SetStateAction<Record<string, string>>>;
  sfxPlacements: SoundEffectPlacement[];
  setSfxPlacements: Dispatch<SetStateAction<SoundEffectPlacement[]>>;
  /** SFX glossary — owned by FocusedResults (hero preview needs it too). */
  glossaryEffects: SoundEffectSummary[];
  glossaryLoading: boolean;
  currentTimeS: number;
  /** Surface a user-facing error in the page-level banner (e.g. SFX save failures). */
  onError: (msg: string) => void;
  /** 006 T3: pending AI suggestions rendered in the timeline lanes. */
  overlaySuggestions?: SuggestionLaneEntry[];
  onSuggestionEdit?: (suggestionId: string, patch: Partial<MediaOverlay>) => void;
  /** 009 T5: src_gcs_path → asset dims for the fullscreen popover warnings. */
  resolveAssetMeta?: (
    srcGcsPath: string,
  ) => { aspect?: number; width?: number; height?: number } | undefined;
  /** Plan 009 T4: hero fullscreen click-to-edit → open this card's timeline
   *  popover (forwarded to UnifiedTimeline's T3 props). */
  externalEditCardId?: string | null;
  onExternalEditHandled?: () => void;
}) {
  const [overlayUploading, setOverlayUploading] = useState(false);
  // True when cards have been modified and need metadata persistence.
  const overlaysDirtyRef = useRef(false);
  // Latest overlayCards value for setTimeout closures.
  const overlayCardsRef = useRef(overlayCards);
  overlayCardsRef.current = overlayCards;
  const localPreviewUrlsRef = useRef<Record<string, string>>({});
  localPreviewUrlsRef.current = localPreviewUrls;
  const inlineOverlayUploader = usePoolAssetUploader({
    itemId,
    // AssetPool remains the source of the full server count; the backend is
    // the final quota fence for this independent inline tree.
    assetCount: 0,
    maxAssets: 20,
    onRegistered: (asset, _file, intent, context) => {
      if (intent !== "inline-overlay") return;
      const card = context as MediaOverlay;
      const finalize = async () => {
        let current = asset;
        for (let attempt = 0; attempt < 60; attempt += 1) {
          if (
            (current.media_status
              ? current.media_status === "ready"
              : current.status === "ready") &&
            (!current.preview_status ||
              current.preview_status === "ready" ||
              current.preview_status === "not_needed")
          ) {
            setOverlayCards((prev) =>
              prev.map((row) =>
                row.id === card.id
                  ? {
                      ...row,
                      src_gcs_path: current.gcs_path,
                      preview_url: current.preview_url ?? null,
                    }
                  : row,
              ),
            );
            overlaysDirtyRef.current = true;
            return;
          }
          if (
            current.media_status === "failed" ||
            current.media_status === "unreadable" ||
            current.preview_status === "failed"
          ) {
            throw new Error("Kria couldn't read that visual. Try another file.");
          }
          await new Promise((resolve) => window.setTimeout(resolve, 1000));
          const refreshed = await listPoolAssets(itemId);
          current = refreshed.assets.find((row) => row.id === asset.id) ?? current;
        }
        throw new Error("That visual is taking longer than expected. Try again shortly.");
      };
      void finalize()
        .catch((err) => {
          setOverlayCards((prev) => prev.filter((row) => row.id !== card.id));
          const local = localPreviewUrlsRef.current[card.id];
          if (local) {
            URL.revokeObjectURL(local);
            setLocalPreviewUrls((prev) => {
              const next = { ...prev };
              delete next[card.id];
              return next;
            });
          }
          onError("We couldn't add that overlay. Try again.");
        })
        .finally(() => setOverlayUploading(false));
    },
    onFailed: (_file, intent, _context) => {
      if (intent === "inline-overlay") {
        // Keep the card and its local preview in place so the inline surface
        // can offer Retry/Remove. Removing it here made a transient network
        // failure irreversible and left the uploader's failed row orphaned.
        setOverlayUploading(false);
      }
    },
    onUnavailable: () => onError("Visuals aren't available right now."),
  });

  // Shared clip-timeline data: owned here so ClipsLane header bars and the
  // InlineClipsEditor expanded panel read/write one draft (no double fetch).
  const clipTimeline = useClipTimeline(itemId, variant.variant_id, "plan-item");
  const clipTimelineEditable = variant.editor_capabilities
    ? variant.editor_capabilities.timeline !== false &&
      variant.editor_capabilities.split_clips !== false
    : true;
  const textLaneEligible = isTextLaneEligible(variant);

  // 009 T5: intro-text keep-out window for the Overlays lane (hatched band +
  // "Covers your intro text" warning) — derived from the variant's persisted
  // text_elements by the single unit-tested helper. Null when no text layer.
  const introTextWindow = useMemo(
    () => computeIntroTextWindow(variant.text_elements),
    [variant.text_elements],
  );

  // 009 D5/E9: fullscreen cutaways are structurally self-defeating on lyric
  // edits (the burned lyric layer would be covered) — the server 422s them;
  // this disables the promote affordances with honest copy.
  const fullscreenDisabledReason =
    variant.text_mode === "lyrics" || variant.variant_id === "song_lyrics"
      ? "Full-screen cutaways aren't available on lyric edits."
      : null;
  const latestOutputUrlRef = useRef<string | null>(variant.output_url ?? null);
  latestOutputUrlRef.current = variant.output_url ?? null;

  // Probe the actual variant duration so the overlay timeline shows the right length.
  const [variantDurationS, setVariantDurationS] = useState(30);
  useEffect(() => {
    const url = latestOutputUrlRef.current;
    if (!url) return;
    const v = document.createElement("video");
    v.preload = "metadata";
    v.onloadedmetadata = () => {
      if (isFinite(v.duration) && v.duration > 0) setVariantDurationS(v.duration);
      v.src = "";
    };
    v.src = url;
    return () => {
      v.onloadedmetadata = null;
      v.removeAttribute("src");
      v.load();
    };
  }, [variant.variant_id, variant.render_finished_at]);

  // Auto-save card metadata (render=false) 2.5 s after the user stops editing.
  // No FFmpeg is triggered here — rendering only happens on explicit download.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (!overlaysDirtyRef.current) return;
    const cards = overlayCardsRef.current;
    // A card is staged immediately for local preview, but the server must not
    // receive it until pool registration has supplied its immutable path.
    if (cards.some((card) => !card.src_gcs_path.trim())) return;
    const timer = setTimeout(async () => {
      overlaysDirtyRef.current = false;
      try {
        await setVariantMediaOverlays(itemId, variant.variant_id, cards, { render: false });
        refetch();
      } catch (err) {
        // Cards are safe in local state, but the save failed (e.g. backend
        // media_overlays_enabled off → 404). Surface it so the user knows the
        // overlay positions won't persist / won't be in the render.
        onError("We couldn't save your overlays, so they won't be in the render. Try again.");
      }
    }, 2500);
    return () => clearTimeout(timer);
  }, [overlayCards]); // eslint-disable-line react-hooks/exhaustive-deps

  /** Upload new files, append as new overlay cards with default settings. */
  async function handleOverlayUpload(
    files: { file: File; filename: string; content_type: string; file_size_bytes: number }[],
  ) {
    setOverlayUploading(true);
    const positionCycle: {
      position: "top" | "center" | "bottom";
      x_frac: number;
      y_frac: number;
    }[] = [
      { position: "center", x_frac: 0.5, y_frac: 0.5 },
      { position: "top", x_frac: 0.5, y_frac: 0.18 },
      { position: "bottom", x_frac: 0.5, y_frac: 0.82 },
    ];
    const cards = files.map<MediaOverlay>((entry, index) => {
      const slot = positionCycle[(overlayCards.length + index) % positionCycle.length];
      return {
        id: crypto.randomUUID(),
        kind: entry.content_type.startsWith("video/") ? "video" : "image",
        src_gcs_path: "",
        position: slot.position,
        x_frac: slot.x_frac,
        y_frac: slot.y_frac,
        scale: 0.35,
        start_s: 0,
        end_s: +Math.min(5, variantDurationS).toFixed(2),
        z: overlayCards.length + index,
      };
    });
    if ((variant.editor_capabilities?.overlay_upload_mode ?? "legacy") === "legacy") {
      try {
        const confirmed = await uploadMediaOverlayFiles(itemId, files);
        const confirmedCards = cards.map((card, index) => ({
          ...card,
          src_gcs_path: confirmed[index].gcs_path,
          preview_gcs_path: confirmed[index].preview_gcs_path ?? null,
          preview_url: confirmed[index].preview_url ?? null,
        }));
        setOverlayCards((prev) => [...prev, ...confirmedCards]);
        overlaysDirtyRef.current = true;
      } catch {
        onError("We couldn't upload that overlay. Try again.");
      } finally {
        setOverlayUploading(false);
      }
      return;
    }
    const blobUrls = Object.fromEntries(
      cards.map((card, index) => [card.id, URL.createObjectURL(files[index].file)]),
    );
    setLocalPreviewUrls((prev) => ({ ...prev, ...blobUrls }));
    setOverlayCards((prev) => [...prev, ...cards]);
    const accepted = inlineOverlayUploader.addFiles(
      files.map((entry) => entry.file),
      {
        intent: "inline-overlay",
        context: (_file, index) => cards[index],
      },
    );
    if (accepted === 0) {
      Object.values(blobUrls).forEach((url) => URL.revokeObjectURL(url));
      setLocalPreviewUrls((prev) => {
        const next = { ...prev };
        cards.forEach((card) => delete next[card.id]);
        return next;
      });
      setOverlayCards((prev) => prev.filter((row) => !cards.some((card) => card.id === row.id)));
      setOverlayUploading(false);
    }
  }

  const removeInlineUpload = useCallback(
    (localId: string, context: unknown) => {
      const card = context as MediaOverlay | undefined;
      inlineOverlayUploader.remove(localId);
      if (!card) return;
      setOverlayCards((prev) => prev.filter((row) => row.id !== card.id));
      setLocalPreviewUrls((prev) => {
        const local = prev[card.id];
        if (local) URL.revokeObjectURL(local);
        if (!local) return prev;
        const next = { ...prev };
        delete next[card.id];
        return next;
      });
    },
    [inlineOverlayUploader, setLocalPreviewUrls, setOverlayCards],
  );

  /** Clear all overlays (restore pre-overlay clean variant). */
  async function handleClearOverlays() {
    // Clear CSS preview immediately — user explicitly removed all cards.
    setLocalPreviewUrls((prev) => {
      Object.values(prev).forEach((url) => URL.revokeObjectURL(url));
      return {};
    });
    const next = overlayCards.reduce<OverlayEffectState>(
      (state, overlay) => removeOverlayEffectGroup(state, overlay.id),
      {
        overlays: overlayCards,
        soundEffects: sfxPlacements,
        cameraEffects: [],
      } satisfies OverlayEffectState,
    );
    setOverlayCards([]);
    setSfxPlacements(next.soundEffects);
    try {
      await setVariantMediaOverlays(itemId, variant.variant_id, [], { render: false });
      refetch();
    } catch {
      onError("We couldn't clear your overlays. Try again.");
    }
  }

  function handleUpdateCard(id: string, patch: Partial<MediaOverlay>) {
    // Resolve position presets to fracs so the CSS preview updates immediately.
    const resolved: Partial<MediaOverlay> = { ...patch };
    if (patch.position === "top") { resolved.x_frac = 0.5; resolved.y_frac = 0.18; }
    else if (patch.position === "center") { resolved.x_frac = 0.5; resolved.y_frac = 0.5; }
    else if (patch.position === "bottom") { resolved.x_frac = 0.5; resolved.y_frac = 0.82; }
    overlaysDirtyRef.current = true;
    setOverlayCards((prev) => prev.map((c) => (c.id === id ? { ...c, ...resolved } : c)));
  }

  function handleRemoveCard(id: string) {
    overlaysDirtyRef.current = true;
    const next = removeOverlayEffectGroup(
      { overlays: overlayCards, soundEffects: sfxPlacements, cameraEffects: [] },
      id,
    );
    setOverlayCards(next.overlays);
    setSfxPlacements(next.soundEffects);
    setLocalPreviewUrls((prev) => {
      if (!prev[id]) return prev;
      URL.revokeObjectURL(prev[id]);
      const next = { ...prev };
      delete next[id];
      return next;
    });
  }

  // For an eligible variant, re-point the text/size/layout/style handlers at the
  // session draft (synchronous → resolved promise so PlanVariantEditor's `run()`
  // busy-wrapper completes immediately). Song + Clips stay on the server paths.
  const editorVariant =
    instantEligible && session.isEditing ? variantWithDraft(variant, session.draft) : variant;
  const draftHandlers = instantEligible
    ? {
        onRetext: async (text: string) => {
          session.setText(text);
        },
        onRemoveText: async () => {
          session.setRemoved(true);
        },
        onChangeStyle: async (styleSetId: string) => {
          session.setStyle(styleSetId);
        },
        onResize: async (px: number) => {
          session.setSize(px);
        },
        onChangeLayout: async (layout: "linear" | "cluster") => {
          session.setLayout(layout);
        },
      }
    : { onRetext, onRemoveText, onChangeStyle, onResize, onChangeLayout };

  // ── SFX state + handlers ──────────────────────────────────────────────────
  // (glossaryEffects / glossaryLoading and the sfxAudioUrls signing effect were
  // hoisted to FocusedResults so applied placements preview on the hero even
  // when no editor tab is open.)
  const [sfxUploading, setSfxUploading] = useState(false);

  // ── Text-elements state (T10 + T6) ────────────────────────────────────────
  // Transient error/retry banner shown after a save conflict (409) or failed save.
  const [textApplyError, setTextApplyError] = useState<string | null>(null);
  // Brief note after a TRIM_START clamp (e.g. "Minimum 0.1s") — auto-clears after 2 s.
  const [textElementNote, setTextElementNote] = useState<string | null>(null);
  // State 3 note: selected-bar tracking is managed internally by TextLane (onBarSelect).
  // UnifiedTimeline's textExpandedBarId is cleared when the selected bar is deleted.
  // Local mirror of textElements bars — seeded from:
  //   • variant.caption_cues (narrated variants, PR-B) — teal "narrated_caption" bars
  //   • variant.text_elements (generative variants, T6) — amber bars
  // Updated on every reducer mutation; used to derive State 5 (text too long) warning.
  const [textElements, setTextElements] = useState<TextElementBar[]>(() => {
    return seedBarsFromVariant(variant);
  });
  // Re-sync from API data when a render completes (render_finished_at advances).
  useEffect(() => {
    setTextElements(seedBarsFromVariant(variant));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [variant.render_finished_at]);
  // Debounce timer ref for the auto-apply after text-element edits.
  const textApplyTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  async function handleSfxUpload(
    files: { file: File; filename: string; content_type: string; file_size_bytes: number }[],
  ) {
    setSfxUploading(true);
    try {
      const urls = await requestSfxUploadUrls(
        itemId,
        files.map((f) => ({ filename: f.filename, content_type: f.content_type, file_size_bytes: f.file_size_bytes })),
      );
      await Promise.all(urls.map((u, i) => uploadToGcs(u.upload_url, files[i].file)));
      const newPlacements: SoundEffectPlacement[] = urls.map((u, i) => ({
        id: crypto.randomUUID(),
        src_gcs_path: u.gcs_path,
        source: "user",
        at_s: Math.min(Math.max(0, currentTimeS), Math.max(0, variantDurationS - 0.05)),
        gain: 1.0,
        label: files[i].filename.replace(/\.[^.]+$/, ""),
      }));
      handleSfxChange([...sfxPlacements, ...newPlacements]);
    } catch {
      // Upload-URL request or GCS upload failed (e.g. backend
      // SOUND_EFFECTS_ENABLED off → sfx-upload-urls 404). Surface it.
      onError("We couldn't upload that sound effect. Try again.");
    } finally {
      setSfxUploading(false);
    }
  }

  // Edits PERSIST (debounced) but do NOT render. The effects play live in the
  // preview (useSfxPreview); the FFmpeg bake happens only on Download
  // (handleDownload in the parent), which flushes this pending save first and
  // computes dirtiness inline (sfxPlacements vs the baked set) — there is no
  // sfxDirty flag and no "Apply" button.
  const sfxSaveTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  function handleSfxChange(newPlacements: SoundEffectPlacement[]) {
    setSfxPlacements(newPlacements);
    if (sfxSaveTimer.current) clearTimeout(sfxSaveTimer.current);
    sfxSaveTimer.current = setTimeout(async () => {
      try {
        await setVariantSoundEffects(itemId, variant.variant_id, newPlacements);
      } catch {
        // The client-side preview still plays the effect locally, but the save
        // failed — surface it (e.g. backend SOUND_EFFECTS_ENABLED off → 404).
        onError("We couldn't save your sound effects. Try again.");
      }
    }, 600);
  }

  // ── Text-element handlers (T6) ────────────────────────────────────────────

  /**
   * Apply text-element bars to the variant via PUT text-elements (T6 wiring).
   *
   * Part A (apply-clears-preview-layer learning): clears localPreviewUrls
   * BEFORE triggering the render pass so the burned output takes over without
   * double-compositing previously-uploaded overlay blob URLs.
   *
   * The actual optimistic "rendering" pin the UI reads is markVariantRendering
   * below (the pendingEdits fingerprint), not a separate local status map.
   */
  const handleApplyTextElements = useCallback(
    async (variantId: string, elements: TextElementBar[]) => {
      // Part A: clear preview layer first.
      setLocalPreviewUrls((prev) => {
        Object.values(prev).forEach((url) => URL.revokeObjectURL(url));
        return {};
      });
      setTextApplyError(null);
      try {
        // Convert TextElementBar → TextElement for the API. Existing API
        // elements are the merge base so renderer-only fields survive.
        // narrated_caption bars are handled by setPlanItemCaptions — filter them out here.
        const apiElements: TextElement[] = barsToTextElements(
          elements,
          new Map((variant.text_elements ?? []).map((el) => [el.id, el])),
        );
        await putTextElements(itemId, variantId, apiElements);
        markVariantRendering(variantId, variant.render_finished_at ?? null);
      } catch (err) {
        const msg = err instanceof Error ? err.message : "";
        if (msg.includes("409") || msg.toLowerCase().includes("conflict")) {
          // State 1: save conflict — refresh to get latest server state.
          setTextApplyError("Text updated elsewhere — refreshing");
          refetch();
        } else {
          // State 2: undo after failed save — inform the user; caller should revert reducer.
          setTextApplyError("Couldn't save text — retrying");
        }
      }
    },
    [
      setLocalPreviewUrls,
      markVariantRendering,
      variant.render_finished_at,
      variant.text_elements,
      refetch,
      itemId,
    ],
  );

  /**
   * Handle text-element changes from the reducer: update local mirror + debounce-apply.
   * Waits 1 s after the last edit before persisting so rapid drag/trim gestures
   * don't flood the API.
   *
   * PR-B: for narrated_caption bars, persists via setPlanItemCaptions (no re-render —
   * the player overlays them instantly).  Generative bars use the existing
   * handleApplyTextElements path (triggers a full reburn).
   */
  const handleTextElementsChange = useCallback(
    (bars: TextElementBar[]) => {
      setTextElements(bars);
      if (textApplyTimer.current) clearTimeout(textApplyTimer.current);
      if (bars[0]?.role === "narrated_caption") {
        textApplyTimer.current = setTimeout(() => {
          const cues: CaptionCue[] = bars.map((b) => ({
            text: b.text,
            start_s: b.start_s,
            end_s: b.end_s,
          }));
          void setPlanItemCaptions(itemId, variant.variant_id, cues);
        }, 1000);
      } else if (bars[0]?.role === "generative_sequence" && variant.scene_timings?.length) {
        // PR-E: sequence bars — persist via patchPlanItemSceneTiming (no re-render).
        textApplyTimer.current = setTimeout(() => {
          const overrides: SceneTimingPatch[] = bars.map((b, i) => ({
            scene_index: i,
            start_s: b.start_s,
            end_s: b.end_s,
          }));
          void patchPlanItemSceneTiming(itemId, variant.variant_id, overrides);
        }, 1000);
      } else if (bars[0]?.role === "generative_intro" && variant.intro_start_s != null) {
        // PR-E: intro timing bar — persist via setPlanItemIntroTiming (no re-render).
        textApplyTimer.current = setTimeout(() => {
          const bar = bars[0];
          void setPlanItemIntroTiming(itemId, variant.variant_id, bar.start_s, bar.end_s);
        }, 1000);
      } else {
        textApplyTimer.current = setTimeout(() => {
          void handleApplyTextElements(variant.variant_id, bars);
        }, 1000);
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [variant.variant_id, variant.scene_timings, variant.intro_start_s, handleApplyTextElements, itemId],
  );

  /** State 4: called by UnifiedTimeline when a trim drag is clamped to MIN_DUR_S. */
  const handleTextTrimClamped = useCallback(() => {
    setTextElementNote("Minimum 0.1s");
    const t = setTimeout(() => setTextElementNote(null), 2000);
    return () => clearTimeout(t);
  }, []);

  const showSongSection = activeTab === "song";
  const showTimelineSection = activeTab === "timeline" && (SOUND_EFFECTS_ENABLED || textLaneEligible);

  return (
    <>
      {/* Song tab: song picker only — a standalone SongPicker section */}
      {showSongSection && (
        <PlanVariantEditor
          variant={baking ? { ...editorVariant, render_status: "rendering" } : editorVariant}
          tracks={tracks}
          styleSets={[]}
          onSwap={onSwap}
          onRetext={async () => {}}
          onRemoveText={async () => {}}
          onChangeStyle={async () => {}}
          onResize={undefined}
          onChangeLayout={undefined}
          onEditClips={undefined}
          showClipEditor={false}
          clipSlotCount={null}
          hasClipEdits={false}
          hideSections={["caption", "size", "layout", "style", "clips"]}
        />
      )}

      {/* Timeline tab: unified multi-lane timeline (SFX + Overlays + Text + Clips inline) */}
      {showTimelineSection && (
        <div className="space-y-1.5">
          {/* State 6: no base_video_path = fast reburn unavailable — inform the user. */}
          {!variant.base_video_path && (
            <p className="text-[11px] text-zinc-500">
              Full re-render needed (may take a moment)
            </p>
          )}
          {/* States 1+2: save conflict or failed save — amber banner. */}
          {textApplyError && (
            <p className="rounded bg-amber-900/30 px-2 py-1 text-[11px] text-amber-400">
              {textApplyError}
            </p>
          )}
          {/* State 4: minimum-duration clamp note — auto-clears after 2 s. */}
          {textElementNote && (
            <p className="px-1 text-[11px] text-zinc-500">{textElementNote}</p>
          )}
          {/* State 5: text too long — inline character count warning. */}
          {textElements.some((b) => b.text.length > 500) && (
            <p className="px-1 text-[11px] text-amber-400">
              Text block exceeds 500 chars — may be truncated on render
            </p>
          )}
          {inlineOverlayUploader.uploads
            .filter((upload) => upload.intent === "inline-overlay")
            .map((upload) => (
              <div
                key={upload.localId}
                className="flex items-center justify-between gap-2 rounded border border-dashed border-amber-400/40 px-2 py-1 text-[11px] text-zinc-300"
              >
                <span className="truncate">
                  {upload.filename}: {upload.stage === "failed" ? upload.message : "Uploading…"}
                </span>
                <span className="flex shrink-0 gap-2">
                  {upload.stage === "failed" && upload.retryable && (
                    <Button
                      type="button"
                      variant="link"
                      className="h-auto p-0 text-[11px] text-zinc-300 underline underline-offset-2"
                      onClick={() => inlineOverlayUploader.retry(upload.localId)}
                    >
                      Retry
                    </Button>
                  )}
                  {upload.stage === "failed" && (
                    <Button
                      type="button"
                      variant="link"
                      className="h-auto p-0 text-[11px] text-zinc-300 underline underline-offset-2"
                      onClick={() => removeInlineUpload(upload.localId, upload.context)}
                    >
                      Remove
                    </Button>
                  )}
                </span>
              </div>
            ))}
          <div className="rounded-xl bg-[#0c0c0e] border border-white/10 p-3">
            <UnifiedTimeline
              totalDurationS={variantDurationS}
              externalEditCardId={externalEditCardId}
              onExternalEditHandled={onExternalEditHandled}
              currentTimeS={currentTimeS}
              sfxPlacements={sfxPlacements}
              sfxGlossaryEffects={glossaryEffects}
              sfxGlossaryLoading={glossaryLoading}
              sfxRendering={variant.render_status === "rendering"}
              sfxUploading={sfxUploading}
              onSfxChange={handleSfxChange}
              onSfxUploadRequest={handleSfxUpload}
              overlayCards={overlayCards}
              overlaysEnabled={MEDIA_OVERLAYS_ENABLED}
              overlayUploading={overlayUploading}
              localPreviewUrls={localPreviewUrls}
              onOverlayUploadRequest={handleOverlayUpload}
              onUpdateCard={handleUpdateCard}
              onRemoveCard={handleRemoveCard}
              onClearOverlays={handleClearOverlays}
              overlaySuggestions={overlaySuggestions}
              onSuggestionEdit={onSuggestionEdit}
              introTextWindow={introTextWindow}
              resolveAssetMeta={resolveAssetMeta}
              fullscreenDisabledReason={fullscreenDisabledReason}
              fullscreenPromoteEnabled={FULLSCREEN_CUTAWAYS_ENABLED}
              showTextLane={textLaneEligible}
              textElements={textElements}
              textVariant={variant}
              onTextElementsChange={handleTextElementsChange}
              onTextApply={(bars) => {
                if (bars[0]?.role === "narrated_caption") {
                  // Narrated captions: persist + trigger reburn via Apply endpoint.
                  const cues: CaptionCue[] = bars.map((b) => ({
                    text: b.text,
                    start_s: b.start_s,
                    end_s: b.end_s,
                  }));
                  void setPlanItemCaptions(itemId, variant.variant_id, cues).then(() =>
                    applyPlanItemCaptions(itemId, variant.variant_id),
                  );
                } else if (bars[0]?.role === "generative_sequence" && variant.scene_timings?.length) {
                  // PR-E: sequence bars — flush timing patch then re-render.
                  const overrides: SceneTimingPatch[] = bars.map((b, i) => ({
                    scene_index: i,
                    start_s: b.start_s,
                    end_s: b.end_s,
                  }));
                  void patchPlanItemSceneTiming(itemId, variant.variant_id, overrides).then(() =>
                    handleApplyTextElements(variant.variant_id, bars),
                  );
                } else if (bars[0]?.role === "generative_intro" && variant.intro_start_s != null) {
                  // PR-E: intro timing bar — flush timing patch then re-render.
                  const bar = bars[0];
                  void setPlanItemIntroTiming(itemId, variant.variant_id, bar.start_s, bar.end_s).then(() =>
                    handleApplyTextElements(variant.variant_id, bars),
                  );
                } else {
                  void handleApplyTextElements(variant.variant_id, bars);
                }
              }}
              onTextTrimClamped={handleTextTrimClamped}
              isFirstSequenceEdit={
                variant.intro_mode === "sequence" && !variant.text_elements_user_edited
              }
              showClipsLane={clipTimelineEditable}
              clipTimelineHandle={clipTimelineEditable ? clipTimeline : undefined}
              clipsPanel={
                clipTimelineEditable ? (
                  <InlineClipsEditor
                    ownerId={itemId}
                    variantId={variant.variant_id}
                    base="plan-item"
                    onRenderEnqueued={() => {
                      markVariantRendering(variant.variant_id, variant.render_finished_at ?? null);
                      refetch();
                    }}
                    externalState={clipTimeline.state}
                    externalDispatch={clipTimeline.dispatch}
                    externalClips={clipTimeline.clips}
                    externalGuidedTokens={
                      clipTimeline.revisionNumber != null && clipTimeline.baseGeneration != null
                        ? {
                            revision_number: clipTimeline.revisionNumber,
                            base_generation: clipTimeline.baseGeneration,
                          }
                        : null
                    }
                    onReload={clipTimeline.reload}
                  />
                ) : null
              }
            />
          </div>
          {/* Text editing controls — rendered below the timeline for text-mode variants. */}
          {variant.text_mode !== "none" && (
            <div className="mt-2 space-y-3">
              <PlanVariantEditor
                variant={baking ? { ...editorVariant, render_status: "rendering" } : editorVariant}
                tracks={[]}
                styleSets={instantEligible ? [] : styleSets}
                onSwap={onSwap}
                onRetext={draftHandlers.onRetext}
                onRemoveText={draftHandlers.onRemoveText}
                onChangeStyle={draftHandlers.onChangeStyle}
                onResize={instantEligible ? undefined : draftHandlers.onResize}
                onChangeLayout={draftHandlers.onChangeLayout}
                onEditClips={undefined}
                showClipEditor={false}
                clipSlotCount={null}
                hasClipEdits={false}
              />
              {instantEligible && (
                <EditToolbar
                  session={session}
                  styleSets={[]}
                  fallbackSizePx={variant.intro_text_size_px}
                  resolvedParams={resolveIntroParams(variant, styleSets, session.draft, session.isDirty)}
                />
              )}
            </div>
          )}
        </div>
      )}
    </>
  );
}

/**
 * Overlay the live edit draft onto the variant so PlanVariantEditor's controls
 * reflect the in-progress selection (the user's chosen caption / size / layout /
 * style) rather than the last-baked server values. Only the fields the editor
 * reads are touched; everything else (song, clips, render_status) passes through.
 */
function variantWithDraft(variant: PlanItemVariant, draft: EditDraft): PlanItemVariant {
  return {
    ...variant,
    intro_text: draft.removed ? "" : draft.text,
    text_mode: draft.removed ? "none" : variant.text_mode === "none" ? "agent_text" : variant.text_mode,
    style_set_id: draft.styleSetId ?? variant.style_set_id,
    intro_text_size_px: draft.sizePx ?? variant.intro_text_size_px,
    // A user-driven size shows as the explicit value (no "· auto" suffix).
    intro_size_source: draft.sizePx != null ? "user" : variant.intro_size_source,
    intro_layout: draft.layout ?? variant.intro_layout,
  };
}

/**
 * The LEFT-hero live preview for an eligible plan-item variant: the text-free
 * base video plays under a live DOM intro overlay; every control change (from
 * the RIGHT column) updates this preview at 0 network via the session draft.
 * Occupies the exact hero frame the burned-output Hero does. Light editorial
 * canvas (lime accent, cream/white tiles — never amber). The overlay is
 * non-editable: the user edits the caption via the RIGHT Caption control, not by
 * typing on the video.
 */
function LiveEditPreview({
  variant,
  styleSets,
  session,
  playToken,
  textElements,
  sfxPlacements = [],
  sfxAudioUrls = {},
  overlayCards = [],
  localPreviewUrls = {},
  suggestionEntries,
  onSuggestionEdit,
  resolveSuggestionCardUrl,
  onCardMediaError,
  onRemoveCard,
  onRequestEditCard,
}: {
  variant: PlanItemVariant;
  styleSets: GenerativeStyleSet[];
  session: VariantEditSession;
  playToken?: number;
  /**
   * T6: Full TextElement array from the variant (API data). When non-empty,
   * the preview renders ALL elements as CSS overlays instead of the single
   * IntroTextPreview (which models the legacy linear/cluster intro path).
   */
  textElements?: TextElement[];
  /**
   * Live SFX preview: instant-eligible variants (agent_text intro, etc.) render
   * THROUGH this component on the Timeline tab, NOT Hero — so the sound-effect
   * <audio> sync must live here too, or glossary effects are silent in the
   * preview even though the Download bake includes them. Mirrors Hero's wiring.
   */
  sfxPlacements?: SoundEffectPlacement[];
  sfxAudioUrls?: Record<string, string>;
  /**
   * Plan 008 gap-close: instant-eligible variants render THROUGH this component
   * (not Hero), so the live overlay-card layer must exist here too — otherwise
   * timeline edits (scale / position / trim) never reach the preview for
   * agent_text variants. Mirrors Hero's live-edit wiring exactly.
   */
  overlayCards?: MediaOverlay[];
  localPreviewUrls?: Record<string, string>;
  suggestionEntries?: SuggestionLaneEntry[];
  onSuggestionEdit?: (suggestionId: string, patch: Partial<MediaOverlay>) => void;
  resolveSuggestionCardUrl?: (overlay: MediaOverlay) => string | undefined;
  /** Plan 009 T4: failed-media lift / failed-tile Remove / fullscreen
   *  click-to-edit — mirrors Hero's wiring (both surfaces mount the same
   *  LiveOverlayCardsLayer + HeroOverlayEditor). */
  onCardMediaError?: (cardId: string) => void;
  onRemoveCard?: (cardId: string) => void;
  onRequestEditCard?: (cardId: string) => void;
}) {
  const sfxVideoRef = useRef<HTMLVideoElement>(null);
  // Sync SFX audio elements to whichever preview video is active (burned output
  // or text-free base). Both StableVideos below carry sfxVideoRef; only one
  // mounts at a time, so the ref always points at the visible player.
  useSfxPreview(sfxVideoRef, sfxPlacements, sfxAudioUrls);

  const introParams = resolveIntroParams(variant, styleSets, session.draft, session.isDirty);

  // Live layout follows the draft (so toggling Classic/Editorial re-lays the
  // overlay instantly), falling back to the variant's persisted layout.
  const previewLayout =
    (session.draft.layout ?? variant.intro_layout) === "cluster" ? "cluster" : "linear";

  // ── Live overlay-card mode (mirrors Hero) ───────────────────────────────────
  // ACTIVE when the variant carries the overlay-clean base AND cards exist.
  // Same two latches as Hero: frozen while a re-burn is in flight, and sticky
  // through "Clear all" so the un-carded base previews a cleared download.
  const overlayRendering = variant.render_status === "rendering";
  const hasPreOverlayBase = !!variant.pre_overlay_video_url;
  const prevLiveModeRef = useRef(false);
  const liveOverlayMode =
    hasPreOverlayBase &&
    (overlayRendering
      ? prevLiveModeRef.current
      : overlayCards.length > 0 || prevLiveModeRef.current);
  useEffect(() => {
    prevLiveModeRef.current = liveOverlayMode;
  });

  // Playhead time for the time-gated overlay layers (cards + suggestion editor).
  const [videoTime, setVideoTime] = useState(0);

  // When the draft is clean (no uncommitted edits, not saving), show the burned
  // output_url — byte-identical to what the download button serves. Switch to
  // the WYSIWYG DOM overlay only while the user is actively editing or a reburn
  // is in flight, giving 0-latency live preview during edits while ensuring
  // what they see at rest IS what they get.
  // (fireCommit already calls setBaseline(toCommit) so isDirty resets to false
  // as soon as a commit fires; it goes true again only on the next keystroke.)
  // In live overlay mode the clean source is the PRE-OVERLAY base (text baked,
  // cards NOT) so the CSS card layer is the single source of card pixels.
  const burnedSrc: string | null =
    !session.isDirty && !session.isSaving
      ? liveOverlayMode
        ? (variant.pre_overlay_video_url ?? null)
        : (variant.output_url ?? null)
      : null;
  // Live mode keys the identity on the pre-overlay GCS path (re-signed poll
  // URLs never restart playback; the "live:" prefix forces adopt on mode flip).
  const burnedIdentity = liveOverlayMode
    ? `live:${variant.variant_id}:${variant.pre_media_overlay_video_path ?? ""}`
    : `${variant.variant_id}:${variant.render_finished_at ?? ""}`;

  // Track the playhead of whichever preview video is mounted. Keyed on which
  // source kind is active — NOT the URL string, which is re-signed every poll.
  const mountedSrcKind = burnedSrc
    ? `clean:${liveOverlayMode}`
    : variant.base_video_url
      ? "base"
      : "none";
  useEffect(() => {
    const el = sfxVideoRef.current;
    if (!el) return;
    const onTimeUpdate = () => setVideoTime(el.currentTime);
    el.addEventListener("timeupdate", onTimeUpdate);
    return () => el.removeEventListener("timeupdate", onTimeUpdate);
  }, [mountedSrcKind]);

  const hasTextElements = !burnedSrc && Boolean(textElements && textElements.length > 0);

  return (
    <div className="relative aspect-[9/16] w-full overflow-hidden rounded-xl border border-zinc-200 bg-zinc-100">
      {burnedSrc ? (
        <StableVideo
          ref={sfxVideoRef}
          src={burnedSrc}
          identity={burnedIdentity}
          controls
          loop
          autoPlay
          muted
          playsInline
          className="h-full w-full object-contain"
        />
      ) : variant.base_video_url ? (
        // StableVideo holds the base src across re-signed polls (same base_video_path
        // identity → no reload) and only swaps when a new base video is rendered
        // (clip timeline edit changes base_video_path → identity changes → swap).
        <StableVideo
          ref={sfxVideoRef}
          src={variant.base_video_url}
          identity={variant.base_video_path ?? undefined}
          controls
          loop
          autoPlay
          muted
          playsInline
          className="h-full w-full object-contain"
        />
      ) : (
        <div className="flex h-full items-center justify-center text-sm text-[#71717a]">
          No preview
        </div>
      )}
      {/* N-element text overlay (T6): shows all text_elements from the API. */}
      {hasTextElements && textElements ? (
        <TextElementOverlayLayer elements={textElements} currentTime={videoTime} />
      ) : (
        // Legacy single-element preview: driven by the instant-editor draft.
        !burnedSrc && (
          <IntroTextPreview params={introParams} editable={false} layout={previewLayout} playToken={playToken} />
        )
      )}
      {/* CSS overlay-card layer — rendered ABOVE the text layers to match the
          bake order (text burns first, cards composite on top).
          LIVE mode / base playback: no cards are baked into the playing video,
          so ALL cards render here and lane edits reflect in real time.
          Burned output playback: only fresh blob-URL uploads render (baked
          pixels are never doubled). */}
      <LiveOverlayCardsLayer
        cards={overlayCards}
        resolveCardSrc={(card) =>
          liveOverlayMode || !burnedSrc
            ? (card.preview_url ?? localPreviewUrls[card.id])
            : localPreviewUrls[card.id]
        }
        videoTimeS={videoTime}
        timeGate={mountedSrcKind !== "none"}
        mainVideoRef={sfxVideoRef}
        onCardMediaError={onCardMediaError}
        onRemoveCard={onRemoveCard}
      />
      {/* Direct-manipulation layer for kept AI overlay suggestions (007 Fix 2)
          — instant-eligible variants render through THIS component, so the
          drag/resize layer must mount here too, not just in Hero. */}
      {suggestionEntries && onSuggestionEdit && (
        <HeroOverlayEditor
          entries={suggestionEntries}
          onSuggestionEdit={onSuggestionEdit}
          currentTimeS={videoTime}
          resolveCardUrl={resolveSuggestionCardUrl}
          onRequestEditCard={onRequestEditCard}
        />
      )}
    </div>
  );
}

function VariantReleasePicker({
  variants,
  selectedVariantId,
  onSelect,
}: {
  variants: PlanItemVariant[];
  selectedVariantId: string | null;
  onSelect: (variantId: string) => void;
}) {
  const readyVariants = variants.filter((value) => value.output_url);
  if (readyVariants.length < 2) return null;
  return (
    <div className="mt-3" aria-label="Visual variants">
      <p className="sr-only">Choose the version to publish</p>
      <div className="flex gap-2 overflow-x-auto pb-1">
        {readyVariants.map((value, index) => {
          const selected = value.variant_id === selectedVariantId;
          return (
            <Button
              key={value.variant_id}
              type="button"
              variant="ghost"
              aria-pressed={selected}
              aria-label={`Publish version ${index + 1}`}
              onClick={() => onSelect(value.variant_id)}
              className={`h-auto min-h-11 shrink-0 items-center gap-2 rounded-lg border px-2 text-xs font-medium focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-600 ${
                selected
                  ? "border-lime-600 bg-lime-50 text-lime-800 hover:bg-lime-50"
                  : "border-zinc-200 bg-white text-[#3f3f46] hover:bg-white"
              }`}
            >
              {value.render_status === "rendering" ? (
                // A re-render is in flight for this thumbnail's variant — the
                // stale <video> would read as "already updated"; show a
                // shimmer placeholder instead (frozen-frame veil, secondary
                // surface).
                <div
                  aria-hidden="true"
                  className="aspect-[9/16] h-8 motion-safe:animate-shimmer rounded bg-[length:200%_100%] bg-gradient-to-r from-zinc-100 via-zinc-200 to-zinc-100"
                />
              ) : (
                <video
                  src={value.output_url ?? undefined}
                  muted
                  playsInline
                  preload="metadata"
                  aria-hidden="true"
                  className="aspect-[9/16] h-8 rounded bg-zinc-100 object-cover"
                />
              )}
              Version {index + 1}
            </Button>
          );
        })}
      </div>
    </div>
  );
}

/** Large hero player for the focused variant. */
function Hero({
  variant,
  generating,
  overlayCards = [],
  localPreviewUrls = {},
  sfxPlacements = [],
  sfxAudioUrls = {},
  renderingAction = null,
  showUpdatedCue = false,
  suggestionEntries,
  onSuggestionEdit,
  resolveSuggestionCardUrl,
  onCardMediaError,
  onRemoveCard,
  onRequestEditCard,
  onDownload,
  playbackFailed,
  onPlaybackFailedChange,
}: {
  variant: PlanItemVariant | null;
  generating: boolean;
  overlayCards?: MediaOverlay[];
  localPreviewUrls?: Record<string, string>;
  sfxPlacements?: SoundEffectPlacement[];
  sfxAudioUrls?: Record<string, string>;
  /** Describes what edit is in-flight so the overlay can show a meaningful label. */
  renderingAction?: { type: "song" | "text" | "style" | "other"; label: string } | null;
  /** Show the "✓ Updated" confirmation cue for 4 s after render_finished_at advances. */
  showUpdatedCue?: boolean;
  /** 007 Fix 2: kept AI overlay suggestions rendered as direct-manipulation
   *  cards over the video (HeroOverlayEditor gates on flag + non-empty). */
  suggestionEntries?: SuggestionLaneEntry[];
  onSuggestionEdit?: (suggestionId: string, patch: Partial<MediaOverlay>) => void;
  resolveSuggestionCardUrl?: (overlay: MediaOverlay) => string | undefined;
  /** Plan 009 T4: failed-media lift / failed-tile Remove / fullscreen
   *  click-to-edit — mirrored in LiveEditPreview (both surfaces mount the
   *  same LiveOverlayCardsLayer + HeroOverlayEditor). */
  onCardMediaError?: (cardId: string) => void;
  onRemoveCard?: (cardId: string) => void;
  onRequestEditCard?: (cardId: string) => void;
  onDownload?: () => void;
  /** Lifted to FocusedResults (owner of the theater-vs-veil dedup decision):
   *  the veil is the sole rendering voice while it's visible, and visibility
   *  depends on this stale-playback-error flag, which only Hero can observe. */
  playbackFailed: boolean;
  onPlaybackFailedChange: (failed: boolean) => void;
}) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const [videoTime, setVideoTime] = useState(0);
  const [playbackRetry, setPlaybackRetry] = useState(0);

  // Sync SFX audio elements to the video playhead for instant preview.
  useSfxPreview(videoRef, sfxPlacements, sfxAudioUrls);

  // ── Live-edit mode ──────────────────────────────────────────────────────────
  // ACTIVE when the variant carries a signed pre-overlay base (captured before
  // the first card burn) AND there are overlay cards. The hero then plays the
  // overlay-CLEAN base and ALL cards render as a live CSS layer on top, so
  // every timeline edit (scale / position / window drag / clip trim / remove)
  // reflects instantly — the FFmpeg bake still only fires on Download.
  //
  // Two latches, both scoped to this variant (Hero remounts on a focus switch —
  // FocusedResults is keyed by variant_id):
  //  • While a re-burn is in-flight (render_status "rendering") the mode is
  //    FROZEN at its pre-burn value so the video source never flips mid-burn
  //    (the shimmer/lock overlay below keeps today's behavior).
  //  • Once ON, the mode survives overlayCards going empty ("Clear all"): the
  //    un-carded base IS the correct preview of a cleared download, while the
  //    burned output_url still has the old cards baked in.
  const rendering = variant?.render_status === "rendering";
  const hasPreOverlayBase = !!variant?.pre_overlay_video_url;
  const prevLiveModeRef = useRef(false);
  const liveMode =
    hasPreOverlayBase &&
    (rendering
      ? prevLiveModeRef.current
      : overlayCards.length > 0 || prevLiveModeRef.current);
  useEffect(() => {
    prevLiveModeRef.current = liveMode;
  });

  // In live mode the hero plays the clean base; otherwise the burned output.
  const heroSrc = liveMode
    ? (variant?.pre_overlay_video_url ?? null)
    : (variant?.output_url ?? null);
  const heroSrcPresent = !!heroSrc;

  // Re-attach when the hero video mounts (a src becomes available) or the source
  // mode flips. Keyed on presence + mode — NOT the URL string, which is re-signed
  // on every status poll.
  useEffect(() => {
    const el = videoRef.current;
    if (!el) return;
    const onTimeUpdate = () => setVideoTime(el.currentTime);
    el.addEventListener("timeupdate", onTimeUpdate);
    return () => el.removeEventListener("timeupdate", onTimeUpdate);
  }, [heroSrcPresent, liveMode]);

  // StableVideo identity: composite of variant_id + render_finished_at so it
  // adopts a new src on BOTH a re-render of the same variant (render_finished_at
  // advances) and a focus switch to a different variant (variant_id changes).
  // The old video keeps playing through a re-render; the overlay dims it gently
  // and the swap happens automatically when render_finished_at advances.
  // In live mode the identity keys on the pre-overlay GCS path instead (the
  // "live:" prefix forces the adopt when the mode flips), so re-signed poll URLs
  // never restart base playback.
  const heroIdentity = !variant
    ? "pending"
    : liveMode
      ? `live:${variant.variant_id}:${variant.pre_media_overlay_video_path ?? ""}`
      : `${variant.variant_id}:${variant.render_finished_at ?? ""}`;

  useEffect(() => {
    onPlaybackFailedChange(false);
    setPlaybackRetry(0);
  }, [heroIdentity, onPlaybackFailedChange]);

  // Frozen-frame veil (V2): pause the old video the instant a re-render
  // starts so it reads as a still frame, not live playback, under the blur.
  useEffect(() => {
    if (rendering) {
      videoRef.current?.pause();
    }
  }, [rendering]);

  if (!variant) return <SkeletonTile />;
  const failed = variant.render_status === "failed";

  return (
    <div className="relative aspect-[9/16] w-full overflow-hidden rounded-xl border border-zinc-200 bg-zinc-100">
      {heroSrc && !playbackFailed ? (
        // Frozen-frame veil (V2): the video itself keeps its native `controls`
        // (a live regression test locates it via `video[controls]` mid-render —
        // see plan-item-live-preview.test.tsx "re-burn in-flight"), but this
        // wrapper carries `aria-hidden` + the blur/scale veil classes, and the
        // (non-pointer-events-none) veil painted below intercepts every click,
        // so the paused/blurred controls underneath are functionally unreachable.
        <div
          aria-hidden={rendering || undefined}
          data-rendering={rendering ? "true" : undefined}
          className="hero-veil-frame h-full w-full"
        >
          <StableVideo
            key={`${heroIdentity}:${playbackRetry}`}
            ref={videoRef}
            src={heroSrc}
            identity={heroIdentity}
            controls
            // Keep the `controls` attribute (plan-item-live-preview.test.tsx
            // locates the hero via `video[controls]` mid-render), but pull it
            // out of tab order while veiled — otherwise a keyboard user can
            // Tab onto the visually-hidden video and hit Space to resume
            // audible playback of the stale take (axe aria-hidden-focus).
            tabIndex={rendering ? -1 : undefined}
            playsInline
            preload="metadata"
            onLoadedData={() => onPlaybackFailedChange(false)}
            onError={() => onPlaybackFailedChange(true)}
            className="h-full w-full object-contain"
          />
        </div>
      ) : heroSrc && playbackFailed ? (
        <div className="flex h-full flex-col items-center justify-center px-5 text-center">
          <p className="max-w-xs text-xs leading-relaxed text-[#3f3f46] lg:text-sm">
            The preview couldn&apos;t load, but your finished video is safe. Try the preview again or
            download the video.
          </p>
          <div className="mt-3 flex w-full flex-col justify-center gap-2 px-2 lg:mt-5 lg:w-auto lg:flex-row lg:flex-wrap lg:gap-3 lg:px-0">
            <Button
              type="button"
              aria-label="Try preview again"
              onClick={() => {
                onPlaybackFailedChange(false);
                setPlaybackRetry((value) => value + 1);
              }}
              className="h-auto min-h-10 rounded-full px-3 text-xs font-semibold focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-lime-600 lg:min-h-11 lg:px-5 lg:text-sm"
            >
              Try preview again
            </Button>
            {onDownload && (
              <Button
                type="button"
                variant="outline"
                aria-label="Download video"
                onClick={onDownload}
                className="h-auto min-h-10 rounded-full px-3 text-xs font-semibold focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-lime-600 lg:min-h-11 lg:px-5 lg:text-sm"
              >
                <span className="lg:hidden">Download</span>
                <span className="hidden lg:inline">Download video</span>
              </Button>
            )}
          </div>
        </div>
      ) : failed ? (
        <div className="flex h-full items-center justify-center px-4 text-center text-sm text-[#3f3f46]">
          {variantFailureCopy(variant.error_class)}
        </div>
      ) : (
        <div className="flex h-full items-center justify-center text-sm text-[#71717a]">
          {generating ? "Rendering…" : "No preview yet"}
        </div>
      )}
      {/* CSS overlay-card layer.
          LIVE mode: the hero above plays the overlay-clean base, so ALL cards
          render here (signed preview_url for applied cards, blob URL for fresh
          uploads) and reflect lane edits in real time.
          LEGACY mode: only freshly-uploaded cards (blob URL, not yet burned)
          render, so pixels already baked into output_url are never doubled;
          in configuration-only mode (no video yet) they show un-gated. */}
      {!playbackFailed && <LiveOverlayCardsLayer
        cards={overlayCards}
        resolveCardSrc={(card) =>
          liveMode
            ? (card.preview_url ?? localPreviewUrls[card.id])
            : localPreviewUrls[card.id]
        }
        videoTimeS={videoTime}
        timeGate={liveMode || !!variant.output_url}
        mainVideoRef={videoRef}
        onCardMediaError={onCardMediaError}
        onRemoveCard={onRemoveCard}
      />}
      {/* 007 Fix 2: direct-manipulation layer for kept AI overlay suggestions —
          drag to reposition, corner handle to resize; every gesture routes
          through onSuggestionEdit (implicit staging, zero network until Apply).
          Gated inside on NEXT_PUBLIC_OVERLAY_AUTOPLACE_ENABLED + non-empty. */}
      {!playbackFailed && suggestionEntries && onSuggestionEdit && (
        <HeroOverlayEditor
          entries={suggestionEntries}
          onSuggestionEdit={onSuggestionEdit}
          currentTimeS={videoTime}
          resolveCardUrl={resolveSuggestionCardUrl}
          onRequestEditCard={onRequestEditCard}
        />
      )}
      {/* Frozen-frame veil (V2): while a re-render runs, the old video stays
          mounted (paused + blurred, above) but this wash makes it unmistakable
          that it is NOT the new result. Deliberately NOT pointer-events-none —
          it must swallow clicks so the paused/blurred controls underneath
          can't be used mid-render. Gated on !playbackFailed (matching
          LiveOverlayCardsLayer / HeroOverlayEditor above): if the stale video
          errors out mid-render (e.g. signed-URL expiry), Hero falls back to
          the "Preview unavailable / Try again / Download" recovery branch —
          the veil must not paint over it and swallow those clicks for the
          rest of the render. */}
      {rendering && variant.output_url && !playbackFailed && (
        <BeamLoader
          tone="light"
          mode="frame"
          strength="medium"
          ariaLabel="Rendering new version"
          className="absolute inset-0 rounded-xl"
        >
          <div className="hero-veil-wash absolute inset-0 flex flex-col items-center justify-center gap-2.5 bg-white/[0.62] px-6 text-center">
            <RenderingVeilStatus
              startedAt={variant.render_started_at ?? null}
              action={renderingAction}
            />
          </div>
        </BeamLoader>
      )}
      {/* "✓ Updated" confirmation — flashes for 4 s when the new video swaps in. */}
      {showUpdatedCue && !rendering && variant.output_url && (
        <div className="pointer-events-none absolute inset-0 flex items-end justify-center pb-5">
          <span className="rounded-full bg-lime-600/90 px-3.5 py-1.5 text-xs font-semibold text-white shadow-sm">
            ✓ Updated
          </span>
        </div>
      )}
    </div>
  );
}

/**
 * Pure copy logic for the rendering veil: headline + optional ETA subtext,
 * keyed off elapsed time and the in-flight edit action. Same branch priority
 * as the pre-veil label this replaces — stall hint first, then action type —
 * so extracting it here changes nothing about which copy shows when.
 */
function renderingCopyFor(
  action: { type: "song" | "text" | "style" | "other"; label: string } | null | undefined,
  elapsedMs: number,
): { headline: string; etaText: string | null } {
  const STALL_HINT_MS = 300_000; // 5 min
  if (elapsedMs >= STALL_HINT_MS) {
    return { headline: "Taking longer than usual…", etaText: null };
  }
  // Song swap: full re-render takes ~1-3 min — show the song name + duration hint.
  if (action?.type === "song") {
    return { headline: `Applying “${action.label}”`, etaText: "~1–3 min" };
  }
  // Text reburn: fast path, a few seconds.
  if (action?.type === "text") {
    return { headline: action.label || "Updating text…", etaText: "a few seconds" };
  }
  // Style / size / layout / generic re-render.
  return { headline: action?.label ?? "Rendering new version…", etaText: null };
}

/** Centered status column inside the frozen-frame veil: an italic Fraunces
 *  headline (action-aware copy from renderingCopyFor) over a small elapsed
 *  clock — no fabricated progress, just real time since render_started_at. */
function RenderingVeilStatus({
  startedAt,
  action,
}: {
  startedAt: string | null;
  action?: { type: "song" | "text" | "style" | "other"; label: string } | null;
}) {
  const [elapsed, setElapsed] = useState(() =>
    startedAt ? Date.now() - new Date(startedAt).getTime() : 0,
  );
  useEffect(() => {
    setElapsed(startedAt ? Date.now() - new Date(startedAt).getTime() : 0);
    const id = setInterval(() => {
      setElapsed(startedAt ? Date.now() - new Date(startedAt).getTime() : 0);
    }, 1000);
    return () => clearInterval(id);
  }, [startedAt]);

  const { headline, etaText } = renderingCopyFor(action, elapsed);

  return (
    <>
      <p className="font-display italic text-xl text-[#0c0c0e]">{headline}</p>
      <p className="text-xs font-medium text-[#3f3f46]">
        {etaText ? `${formatElapsed(elapsed)} · ${etaText}` : formatElapsed(elapsed)}
      </p>
    </>
  );
}

function SkeletonTile() {
  return (
    <div className="aspect-[9/16] w-full motion-safe:animate-shimmer rounded-xl border border-zinc-200 bg-[length:200%_100%] bg-gradient-to-r from-zinc-100 via-zinc-200 to-zinc-100" />
  );
}

function slugify(s: string): string {
  return s
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 40);
}

// ── Conformance verdict tile ────────────────────────────────────────────────────
// Display-only: never disables or blocks Generate. Redesigned per DESIGN.md §7-D10
// after the wrong-brief incident: dashed zinc (no red walls), a READ AGAINST
// evidence line so the user can SEE what was judged, advice voice, and real
// recourse ("Tell Kria" re-reads the clip; "Hide this read" dismisses).

const VERDICT_LABEL: Record<"minor_drift" | "off_brief", string> = {
  minor_drift: "Close — one tweak",
  off_brief: "Different from the brief",
};

function ConformanceVerdictPanel({
  conformance,
  onTellKria,
  onDismiss,
}: {
  conformance: ConformanceVerdict;
  onTellKria: () => void;
  onDismiss: () => void;
}) {
  // Render gates: dismissed/suppressed verdicts and low-confidence reads show
  // nothing — silence beats a read the user can't trust.
  if (conformance.dismissed || conformance.suppressed) return null;
  if ((conformance.confidence ?? 0) < 0.6) return null;

  if (conformance.verdict === "on_track") {
    return (
      <p
        className="mb-4 text-sm text-[#3f3f46]"
        role="status"
        aria-live="polite"
        data-testid="conformance-verdict-panel"
      >
        <span className="text-lime-700">✓</span> Looks on-brief.
      </p>
    );
  }

  const label = VERDICT_LABEL[conformance.verdict] ?? VERDICT_LABEL.off_brief;
  // Label promises "one tweak" for minor drift — the advice keeps that promise.
  const adviceCap = conformance.verdict === "minor_drift" ? 1 : 2;
  const advice = (conformance.suggestions ?? []).slice(0, adviceCap);

  return (
    <div
      className="mb-6 rounded-xl border border-dashed border-zinc-300 bg-white p-4"
      role="status"
      aria-live="polite"
      data-testid="conformance-verdict-panel"
    >
      {conformance.evaluated_theme && (
        <p className="mb-1 text-[11px] font-semibold uppercase tracking-[0.18em] text-[#71717a]">
          Read against: &ldquo;{conformance.evaluated_theme}&rdquo;
        </p>
      )}
      <p className="mb-1 text-[11px] font-semibold uppercase tracking-[0.18em] text-[#52525b]">
        {label}
      </p>
      <p className="text-sm text-[#0c0c0e]">{conformance.summary}</p>
      {advice.length > 0 && (
        <ul className="mt-1 space-y-0.5">
          {advice.map((s, i) => (
            <li key={i} className="text-sm text-[#3f3f46]">
              {s}
            </li>
          ))}
        </ul>
      )}
      <div className="mt-3 flex gap-4">
        <Button
          type="button"
          variant="link"
          onClick={onTellKria}
          className="h-auto p-0 text-xs font-medium text-lime-700 underline-offset-2 hover:underline"
        >
          Looks wrong? Tell Kria
        </Button>
        <Button
          type="button"
          variant="link"
          onClick={onDismiss}
          className="h-auto p-0 text-xs text-[#71717a] underline-offset-2 hover:underline"
        >
          Hide this read
        </Button>
      </div>
    </div>
  );
}

// ── Kria helper ─────────────────────────────────────────────────────────────────
// One quiet line in the right action panel. Collapses the two pre-generate AI
// surfaces (conformance critic + Ask Kria) into a single lime-dot row.
// States: checking (pulse) → on-track → off-brief one-liner → default prompt.
// Expanding → AskKriaPanel (full advisor chat) replaces this row entirely.

function KriaHelper({
  item,
  conformanceChecking,
  askKria,
  onOpen,
  onContest,
  onClose,
  onDismissConformance,
  onItemChanged,
}: {
  item: PlanItem;
  conformanceChecking: boolean;
  askKria: null | "default" | "contest";
  onOpen: () => void;
  onContest: () => void;
  onClose: () => void;
  onDismissConformance: () => void;
  onItemChanged: () => void;
}) {
  // AskKriaPanel is the full-expanded state — it takes over the row entirely.
  if (askKria !== null) {
    return (
      <AskKriaPanel
        item={item}
        mode={askKria}
        onClose={onClose}
        onItemChanged={onItemChanged}
      />
    );
  }

  const c = item.conformance;
  // Reuse the same render gates as ConformanceVerdictPanel: dismissed,
  // suppressed, and low-confidence reads are silent.
  const hasVerdict =
    !!c?.verdict &&
    !c.dismissed &&
    !c.suppressed &&
    (c.confidence ?? 0) >= 0.6;

  return (
    <div role="status" aria-live="polite" className="space-y-1.5" data-testid="kria-helper">
      {conformanceChecking ? (
        <p className="flex items-start gap-2 text-sm text-[#71717a] motion-safe:animate-pulse">
          <span
            className="mt-1.5 inline-block h-1.5 w-1.5 shrink-0 rounded-full bg-lime-600"
            aria-hidden="true"
          />
          Reading your clips against the brief…
        </p>
      ) : hasVerdict && c!.verdict === "on_track" ? (
        <p className="flex items-start gap-2 text-sm text-[#3f3f46]">
          <span
            className="mt-1.5 inline-block h-1.5 w-1.5 shrink-0 rounded-full bg-lime-600"
            aria-hidden="true"
          />
          Looks on-brief.{" "}
          <Button
            type="button"
            variant="link"
            onClick={onOpen}
            className="h-auto p-0 font-medium text-lime-700 underline-offset-2 hover:underline"
          >
            Ask Kria ↗
          </Button>
        </p>
      ) : hasVerdict ? (
        <div className="space-y-1">
          <p className="flex items-start gap-2 text-sm text-[#3f3f46]">
            <span
              className="mt-1.5 inline-block h-1.5 w-1.5 shrink-0 rounded-full bg-lime-600"
              aria-hidden="true"
            />
            <span>{c!.summary}</span>
          </p>
          <div className="flex gap-3 pl-3.5">
            <Button
              type="button"
              variant="link"
              onClick={onContest}
              className="h-auto p-0 text-xs font-medium text-lime-700 underline-offset-2 hover:underline"
            >
              Tell Kria
            </Button>
            <Button
              type="button"
              variant="link"
              onClick={onDismissConformance}
              className="h-auto p-0 text-xs text-[#71717a] underline-offset-2 hover:underline"
            >
              Hide
            </Button>
            <Button
              type="button"
              variant="link"
              onClick={onOpen}
              className="h-auto p-0 text-xs text-[#71717a] underline-offset-2 hover:underline"
            >
              Ask Kria ↗
            </Button>
          </div>
        </div>
      ) : null}
    </div>
  );
}

// ── Pool upload card (uninstructed items) ────────────────────────────────────────
// Replaces the legacy inline <section> for items without a filming guide.
// Visually matches the shot-slot card: rounded-2xl, border-zinc-200, bg-white.
// Logic is identical to the old section — only the markup has been trimmed.

// 44px touch target at the base tier (DESIGN.md §8), compact at sm:. Negative
// margins keep the card from inflating. Shared by the attached-card Remove ×
// and the pending-card Cancel ×.
const POOL_CARD_DISMISS_CLASS =
  "-my-2 -mr-2 flex h-11 w-11 shrink-0 items-center justify-center rounded-full text-sm leading-5 text-[#71717a] hover:bg-zinc-100 hover:text-[#0c0c0e] sm:my-0 sm:mr-0 sm:h-5 sm:w-5";

function PoolUploadCard({
  clips,
  pending,
  uploading,
  onFiles,
  onCancelUpload,
  onRetryUpload,
  onKeep,
  onRemove,
  onNoteChange,
  maxClips,
  accept = VIDEO_UPLOAD_ACCEPT,
  subline,
}: {
  clips: ClipAssignment[];
  pending: PendingClipUpload[];
  uploading: boolean;
  onFiles: (files: FileList | null) => void;
  onCancelUpload: (localId: string) => void;
  onRetryUpload: (localId: string) => void;
  onKeep: (a: ClipAssignment) => void;
  onRemove: (a: ClipAssignment) => void;
  onNoteChange: (a: ClipAssignment, note: string) => Promise<void>;
  /** Hard cap on clip count (subtitled = 1); montage pools use the shared 100 cap. */
  maxClips?: number;
  accept?: string;
  /** Per-format helper copy shown as the dropzone's subline while empty
   *  (e.g. "3 or more clips work best..."). Omit for no subline. */
  subline?: ReactNode;
}) {
  const clipLimit = maxClips ?? MAX_CLIPS_PER_ITEM;
  // In-flight cards count toward the cap so maxClips=1 can't double-pick.
  const remaining = Math.max(0, clipLimit - clips.length - pending.length);
  const atCap = remaining === 0;
  const hasAny = clips.length > 0 || pending.length > 0;
  const dropzoneLabel =
    clipLimit === 1
      ? "Drop a video here or choose a file"
      : hasAny
        ? "Add more videos"
        : "Drop videos here or choose files";
  return (
    <div>
      {hasAny && (
        <ul
          className="mb-4 flex gap-3 overflow-x-auto pb-2"
          aria-label="Uploaded clips"
          data-testid="uploaded-clip-filmstrip"
        >
          {clips.map((a) => {
            const raw = a.gcs_path.split("/").pop() ?? a.gcs_path;
            const name = raw.includes("-") ? raw.slice(raw.indexOf("-") + 1) : raw;
            const kind = /\.(jpe?g|png|webp|heic|heif)$/i.test(name) ? "IMG" : "VID";
            return (
              <li
                key={a.gcs_path}
                className="min-w-[190px] max-w-[220px] rounded-md border border-border bg-card p-2"
              >
                <div className="flex gap-2">
                  <span
                    className="flex h-12 w-10 shrink-0 items-center justify-center rounded-md bg-zinc-900 text-[10px] font-semibold tracking-wide text-white"
                    aria-hidden="true"
                  >
                    {kind}
                  </span>
                  <div className="min-w-0 flex-1">
                    <div className="flex min-w-0 items-start justify-between gap-2">
                      <span
                        className="min-w-0 truncate text-xs font-medium text-foreground"
                        title={name}
                      >
                        {name}
                      </span>
                      <Button
                        type="button"
                        variant="ghost"
                        size="icon-sm"
                        onClick={() => onRemove(a)}
                        className={POOL_CARD_DISMISS_CLASS}
                        aria-label={`Remove ${name}`}
                      >
                        ×
                      </Button>
                    </div>
                    {a.machine_matched ? (
                      <div className="mt-1 flex items-center gap-2">
                        <Badge variant="secondary" className="rounded border-dashed font-normal normal-case tracking-normal">
                          Matched
                        </Badge>
                        <Button
                          type="button"
                          variant="link"
                          onClick={() => onKeep(a)}
                          className="h-auto p-0 text-[11px] font-medium text-lime-700 underline-offset-2 hover:underline"
                        >
                          Keep
                        </Button>
                      </div>
                    ) : (
                      <Badge variant="secondary" className="mt-1 font-normal normal-case tracking-normal">
                        Added
                      </Badge>
                    )}
                  </div>
                </div>
                <details className="mt-2">
                  <summary className="cursor-pointer text-[11px] text-muted-foreground marker:text-zinc-300">
                    Notes
                    {a.user_note ? (
                      <span className="ml-1 text-lime-700">saved</span>
                    ) : null}
                  </summary>
                  <div className="mt-2">
                    <ClipNoteControl
                      note={a.user_note ?? ""}
                      onSave={(note) => onNoteChange(a, note)}
                    />
                  </div>
                </details>
              </li>
            );
          })}
          {pending.map((p) => (
            <li
              key={p.localId}
              data-testid="pending-clip-card"
              className="min-w-[190px] max-w-[220px] rounded-md border border-border bg-card p-2"
            >
              <div className="flex min-w-0 items-start justify-between gap-2">
                <span
                  className="min-w-0 truncate text-xs font-medium text-foreground"
                  title={p.filename}
                >
                  {p.filename}
                </span>
                {/* No cancel while "Saving…" — the attach is committing; the
                    clip is deletable from its card the moment it lands. */}
                {p.status !== "saving" && (
                  <Button
                    type="button"
                    variant="ghost"
                    size="icon-sm"
                    onClick={() => onCancelUpload(p.localId)}
                    aria-label={
                      p.status === "uploading"
                        ? `Cancel upload of ${p.filename}`
                        : `Dismiss ${p.filename}`
                    }
                    className={POOL_CARD_DISMISS_CLASS}
                  >
                    ×
                  </Button>
                )}
              </div>
              {p.status === "saving" ? (
                <>
                  <div
                    className="mt-2 h-1 w-full overflow-hidden rounded-full bg-zinc-100"
                    role="progressbar"
                    aria-valuemin={0}
                    aria-valuemax={100}
                    aria-valuenow={100}
                    aria-label={`Upload progress for ${p.filename}`}
                  >
                    <div className="h-full w-full rounded-full bg-lime-600" />
                  </div>
                  <p className="mt-1 text-[11px] text-lime-700">Saving…</p>
                </>
              ) : p.status === "uploading" ? (
                <>
                  {/* Fill is real XHR byte progress (quantized to whole %) —
                      never an invented number; the relay phase has no byte
                      events, so it renders as shimmer (DESIGN.md D6). scaleX
                      keeps the animation compositor-only. */}
                  <div
                    className="mt-2 h-1 w-full overflow-hidden rounded-full bg-zinc-100"
                    role="progressbar"
                    aria-valuemin={0}
                    aria-valuemax={100}
                    aria-valuenow={p.indeterminate ? undefined : Math.round(p.progress * 100)}
                    aria-label={`Upload progress for ${p.filename}`}
                  >
                    {p.indeterminate ? (
                      <div className="h-full w-full rounded-full bg-lime-200 motion-safe:animate-pulse" />
                    ) : (
                      <div
                        className="h-full w-full origin-left rounded-full bg-lime-600 transition-transform"
                        style={{ transform: `scaleX(${Math.round(p.progress * 100) / 100})` }}
                      />
                    )}
                  </div>
                  <p className="mt-1 text-[11px] text-lime-700">
                    {p.indeterminate ? "Uploading…" : `Uploading… ${Math.round(p.progress * 100)}%`}
                  </p>
                </>
              ) : (
                <>
                  <p className="mt-1 text-[11px] text-[#3f3f46]">
                    {p.error ?? "We couldn't add this video. Try again."}
                  </p>
                  <Button
                    type="button"
                    variant="link"
                    onClick={() => onRetryUpload(p.localId)}
                    className="-mb-2 -ml-2 h-11 items-center px-2 py-0 text-[11px] font-medium text-lime-700 underline-offset-2 hover:underline sm:mb-0 sm:ml-0 sm:h-auto sm:px-0"
                  >
                    Retry
                  </Button>
                </>
              )}
            </li>
          ))}
        </ul>
      )}
      {atCap ? (
        pending.length === 0 && (
          <p className="text-sm text-muted-foreground">
            You&apos;ve reached the clip limit ({clipLimit}). Remove a clip to add more.
          </p>
        )
      ) : (
        // Disabled ONLY during narrated pre-processing (voiceover split +
        // save) — a concurrent handleFiles there would double-save the
        // voiceover. Clip TRANSFERS clear `uploading` first, so adding
        // more clips mid-batch stays possible.
        <>
          <p className="mb-2 text-sm text-muted-foreground">
            You can add up to {remaining} more clip(s).
          </p>
          <Dropzone
            onFiles={onFiles}
            accept={accept}
            multiple={clipLimit !== 1}
            disabled={uploading}
            compact={hasAny}
            title={hasAny ? "Add more videos" : "Drop videos here or choose files"}
            subline={
              hasAny
                ? undefined
                : subline ?? "iCloud videos may take a moment to prepare before they appear here."
            }
            ariaLabel={dropzoneLabel}
            inputAriaLabel="Drop videos here or choose files"
          />
        </>
      )}
      {uploading && pending.length === 0 && (
        <p className="mt-3 text-sm text-lime-700">Uploading…</p>
      )}
    </div>
  );
}
