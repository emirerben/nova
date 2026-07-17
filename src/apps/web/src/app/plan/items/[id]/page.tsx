"use client";

import Link from "next/link";
import { useParams, useRouter, useSearchParams } from "next/navigation";
import { type Dispatch, type SetStateAction, useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  attachClips,
  changePlanItemStyle,
  dismissConformance,
  editPlanItemVariant,
  expandIdea,
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
  type IdeaExpandProposal,
  type PlanItem,
  type PlanItemJobStatus,
  type PlanItemVariant,
  type MontagePreset,
  requestUploadUrls,
  retextPlanItem,
  setPlanItemIntroSize,
  swapPlanItemSong,
  uploadToGcs,
  requestOverlayUploadUrls,
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
import {
  getGenerativeStyleSets,
  type GenerativeStyleSet,
  GENERATIVE_TERMINAL_STATUSES,
  uploadVoiceover,
} from "@/lib/generative-api";
import { getMusicTracks, type MusicTrackSummary } from "@/lib/music-api";
import { FONT_FACES } from "@/lib/font-faces";
import { downloadVideo } from "@/lib/download-video";
import { sfxNeedsBake, sfxPersistDirty } from "@/lib/sfx-dirty";
import { variantFailureCopy, unplacedShotCopy } from "@/lib/variant-failure-copy";
import { stripRationalePrefix } from "@/lib/plan-text";
import { GENERATIVE_PHASE_ORDER, GENERATIVE_PHASE_LABEL } from "@/lib/job-phases";
import { ProgressTheater, ShimmerSweep } from "@/components/progress";
import { StableVideo } from "@/components/StableVideo";
import { usePolledJobStatus } from "@/hooks/usePolledJobStatus";
import { LightShell } from "@/components/ui/LightShell";
import { InkButton } from "@/components/ui/InkButton";
import { SeedProvenanceBadge } from "../../_components/ui/SeedProvenanceBadge";
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
import { InlineClipsEditor } from "../../_components/InlineClipsEditor";
import { useClipTimeline } from "../../_components/useClipTimeline";
import { getSoundEffects, type SoundEffectSummary } from "@/lib/sfx-api";
import type { TextElementBar } from "@/lib/timeline/text-timeline-reducer";
import { barsToTextElements, seedBarsFromVariant } from "./_editor/editor-bars";
import { editorReasonCopy } from "./_editor/editor-capabilities";
import FeedbackButtons from "../../../library/_components/FeedbackButtons";
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
// Server-derived per-variant capability map (routes/generative_jobs.py
// `_editor_capabilities`). Not yet in the shared PlanItemVariant type — declared
// locally here since only the Edit-entry gate reads it.
type EditorCapabilities = {
  text_elements: boolean;
  timeline: boolean;
  split_clips: boolean;
  mix: boolean;
  reason: string | null;
};

function planItemEditorDisabledReason(variant: PlanItemVariant | null): string | null {
  const capabilities = (
    variant as (PlanItemVariant & { editor_capabilities?: EditorCapabilities }) | null
  )?.editor_capabilities;
  if (
    capabilities &&
    !capabilities.text_elements &&
    !capabilities.timeline &&
    !capabilities.split_clips &&
    !capabilities.mix
  ) {
    return editorReasonCopy(capabilities.reason);
  }
  return null;
}

function canOpenPlanItemEditor(variant: PlanItemVariant | null): boolean {
  return Boolean(
    TIKTOK_EDITOR_ENABLED &&
      variant?.output_url &&
      variant.render_status !== "rendering" &&
      planItemEditorDisabledReason(variant) === null,
  );
}

const RENDER_REGISTER_ERROR = "The render didn't register — give it another go.";
type PendingEdit = {
  priorFinishedAt: string | null;
  sawRendering: boolean;
  targetGeneration?: string | null;
};

// Edit-style picker copy, keyed by `edit_format`. NOTE: "Talking to camera" here
// is a DIFFERENT namespace than persona.footage_type_bias="talking_head" (see
// the persona/onboarding footage options, which use the same
// phrase for a persona-level content preference, not an edit style). Do not
// merge these two label maps — they answer different questions.
const EDIT_FORMAT_LABELS: Record<string, { label: string; desc: string }> = {
  montage: { label: "Montage", desc: "Multiple clips cut to music" },
  narrated_planned: { label: "Voiceover", desc: "Tell the story with narration" },
  subtitled: { label: "Talking to camera", desc: "You on screen, with auto subtitles" },
  talking_head: {
    label: "Talking-head B-roll",
    desc: "Use a spoken clip as the spine, with other clips cut in",
  },
};

// Shared by the interactive Fit/Fill toggle (pre-render) and the read-only
// applied-fit display (post-render).
const LANDSCAPE_FIT_OPTIONS: { value: "fit" | "fill"; label: string; desc: string }[] = [
  { value: "fit",  label: "Fit",  desc: "Keep horizontal, black bars top & bottom" },
  { value: "fill", label: "Fill", desc: "Crop to fill the vertical frame" },
];

const MONTAGE_PRESET_OPTIONS: { value: MontagePreset; label: string; desc: string }[] = [
  { value: "classic", label: "Classic", desc: "Full-screen cuts in sequence" },
  { value: "masonry", label: "Masonry collage", desc: "Rounded clips on a white wall" },
  { value: "polaroid_wall", label: "Polaroid wall", desc: "Oversized photo cards on a wall" },
];
const COLLAGE_MONTAGE_PRESETS = new Set<MontagePreset>(["masonry", "polaroid_wall"]);

function MontagePresetPreview({ value }: { value: MontagePreset }) {
  if (value === "polaroid_wall") {
    const cards = [
      ["left-[4%] top-[9%] h-[44%] w-[24%] rotate-[-4deg]", "bg-lime-200"],
      ["left-[32%] top-[4%] h-[29%] w-[44%] rotate-[2deg]", "bg-sky-200"],
      ["left-[80%] top-[8%] h-[39%] w-[25%] rotate-[5deg]", "bg-rose-200"],
      ["left-[36%] top-[36%] h-[55%] w-[31%] rotate-[-2deg]", "bg-amber-200"],
      ["left-[4%] top-[61%] h-[27%] w-[29%] rotate-[3deg]", "bg-zinc-200"],
      ["left-[72%] top-[55%] h-[31%] w-[37%] rotate-[-3deg]", "bg-indigo-200"],
    ] as const;
    return (
      <div className="relative h-24 overflow-hidden rounded-lg border border-zinc-200 bg-white">
        <div className="absolute inset-y-0 left-0 w-[136%] motion-safe:animate-[montage-masonry-pan_2.8s_ease-in-out_infinite_alternate]">
          {cards.map(([pos, color], idx) => (
            <span
              key={idx}
              className={`absolute rounded-[9px] bg-white p-[5px] pb-[13px] shadow-sm ring-1 ring-black/5 ${pos}`}
            >
              <span className={`block h-full w-full rounded-[6px] ${color}`} />
            </span>
          ))}
        </div>
        <span className="absolute left-1/2 top-1/2 h-6 w-20 -translate-x-1/2 -translate-y-1/2 rounded-full bg-white/80 blur-sm" />
      </div>
    );
  }

  if (value === "masonry") {
    const tiles = [
      ["left-[4%] top-[9%] h-[50%] w-[24%]", "bg-lime-200"],
      ["left-[32%] top-[7%] h-[25%] w-[36%]", "bg-sky-200"],
      ["left-[72%] top-[12%] h-[48%] w-[25%]", "bg-rose-200"],
      ["left-[7%] top-[64%] h-[24%] w-[36%]", "bg-zinc-200"],
      ["left-[48%] top-[39%] h-[50%] w-[25%]", "bg-amber-200"],
      ["left-[78%] top-[67%] h-[23%] w-[35%]", "bg-indigo-200"],
    ] as const;
    return (
      <div className="relative h-24 overflow-hidden rounded-lg border border-zinc-200 bg-white">
        <div className="absolute inset-y-0 left-0 w-[132%] motion-safe:animate-[montage-masonry-pan_2.8s_ease-in-out_infinite_alternate]">
          {tiles.map(([pos, color], idx) => (
            <span
              key={idx}
              className={`absolute rounded-[10px] ${pos} ${color} shadow-sm ring-1 ring-black/5`}
            />
          ))}
        </div>
      </div>
    );
  }

  return (
    <div className="relative h-24 overflow-hidden rounded-lg border border-zinc-200 bg-[#0c0c0e]">
      <span className="absolute inset-1 rounded-md bg-[linear-gradient(135deg,#bef264,#38bdf8)] motion-safe:animate-[montage-classic-a_3.6s_steps(1,end)_infinite]" />
      <span className="absolute inset-1 rounded-md bg-[linear-gradient(135deg,#fb7185,#facc15)] motion-safe:animate-[montage-classic-b_3.6s_steps(1,end)_infinite]" />
      <span className="absolute inset-1 rounded-md bg-[linear-gradient(135deg,#a78bfa,#22c55e)] motion-safe:animate-[montage-classic-c_3.6s_steps(1,end)_infinite]" />
      <span className="absolute bottom-2 left-1/2 h-1 w-10 -translate-x-1/2 rounded-full bg-white/80" />
    </div>
  );
}

const VIDEO_UPLOAD_ACCEPT = "video/mp4,video/quicktime";
const AUDIO_UPLOAD_ACCEPT = "audio/*,.mp3,.m4a,.mp4,.wav,.webm,.ogg,.aac";
const NARRATED_READY_UPLOAD_ACCEPT = `${VIDEO_UPLOAD_ACCEPT},${AUDIO_UPLOAD_ACCEPT}`;
const MASONRY_UPLOAD_ACCEPT = `${VIDEO_UPLOAD_ACCEPT},image/jpeg,image/png,image/webp,image/heic,image/heif`;
const AUDIO_UPLOAD_EXTENSIONS = new Set([".mp3", ".m4a", ".wav", ".webm", ".ogg", ".aac"]);
const AUDIO_ONLY_PROBE_EXTENSIONS = new Set([".mp4", ".m4v", ".mov"]);

function uploadContentType(file: File): string {
  if (file.type) return file.type;
  const name = file.name.toLowerCase();
  if (name.endsWith(".jpg") || name.endsWith(".jpeg")) return "image/jpeg";
  if (name.endsWith(".png")) return "image/png";
  if (name.endsWith(".webp")) return "image/webp";
  if (name.endsWith(".heic")) return "image/heic";
  if (name.endsWith(".heif")) return "image/heif";
  if (name.endsWith(".mov")) return "video/quicktime";
  return "video/mp4";
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

function expandContextPrompt(format: PickerEditFormat): string {
  if (format === "narrated_planned") {
    return "What should your voiceover explain, reveal, or make people feel?";
  }
  if (format === "subtitled") {
    return "Who is this for, and what point are you trying to make?";
  }
  return "What should this edit make people feel or notice?";
}

function CompactPlanSummary({ item }: { item: PlanItem }) {
  const shots = item.filming_guide ?? [];
  if (shots.length === 0) return null;
  return (
    <div className="mb-4 rounded-xl border border-zinc-200 bg-white p-4">
      <p className="text-[11px] font-semibold uppercase tracking-[.15em] text-zinc-400">
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
  const checks = files.filter((file) => uploadContentType(file).startsWith("video/")).map(
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

function deriveReceiptText(job: PlanItemJobStatus): string {
  if (job.started_at && job.finished_at) {
    const ms = new Date(job.finished_at).getTime() - new Date(job.started_at).getTime();
    const secs = Math.floor(ms / 1000);
    const mins = Math.floor(secs / 60);
    const s = secs % 60;
    return `Ready in ${mins}:${String(s).padStart(2, "0")}`;
  }
  return "Your edits are ready";
}

export default function PlanItemPage() {
  const params = useParams<{ id: string }>();
  const router = useRouter();
  const searchParams = useSearchParams();
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
  const [expandProposal, setExpandProposal] = useState<IdeaExpandProposal | null>(null);
  const [expandContextOpen, setExpandContextOpen] = useState(false);
  const [expandContext, setExpandContext] = useState("");
  const [expanding, setExpanding] = useState(false);
  const [acceptingExpand, setAcceptingExpand] = useState(false);
  const [expandError, setExpandError] = useState<string | null>(null);
  const [acceptExpandError, setAcceptExpandError] = useState<string | null>(null);
  const [focusShotListAfterAccept, setFocusShotListAfterAccept] = useState(false);
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
  // Conformance polling: keep fetching for up to 3 extra cycles after clips are attached
  // so the verdict panel appears shortly after the async agent finishes (~6s window).
  const conformancePolls = useRef(0);
  // Render-start window: POST /generate dispatches a Celery task that mints the
  // Job AFTER the response — keep polling until current_job_id appears, or the
  // first click silently "does nothing" (dogfood). Time-based, not poll-count:
  // a busy worker can take >12s to pick the task up (second dogfood round: the
  // count-based window expired, showed the error, THEN the render started).
  const awaitingJobSince = useRef<number | null>(null);
  const forceFreshFetchRef = useRef(false);
  const consumedEditorReturnRef = useRef<string | null>(null);
  const autoOpenedEditorRef = useRef<string | null>(null);

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
      const anyRendering =
        job?.variants?.some((v) => v.render_status === "rendering") ?? false;
      // Plan 007 (CRITICAL-2): the zero-click autoplace chain (match → burn)
      // runs server-side AFTER variants_ready. Keep polling while any variant
      // is mid-match, so the auto-applied result (and the hydration effect)
      // is never invisible until a manual reload.
      const anyAutoMatching =
        job?.variants?.some((v) => v.overlay_suggest_status === "matching") ?? false;
      const pending = pendingEdits.current;
      // If the job-level status is already terminal (processing_failed,
      // variants_failed, etc.) treat it as done regardless of any frozen
      // per-variant render_status.  A stuck "rendering" variant after a
      // terminal job is a backend data-integrity gap — it should not keep the
      // frontend polling forever.  The failed variant renders via the existing
      // "failed" UI branch.
      const jobTerminal =
        job?.status != null && GENERATIVE_TERMINAL_STATUSES.includes(job.status);
      const baseTerminal =
        (jobTerminal || !anyRendering) &&
        !anyAutoMatching &&
        pending.size === 0 &&
        item.status !== "generating" &&
        !(item.current_job_id && item.status !== "ready" && item.status !== "failed");

      // Keep polling while a just-dispatched render hasn't minted its Job yet.
      if (item.current_job_id || item.status === "generating") {
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
    else if (pollError) setError(pollError.message);
  }, [pollError]);

  const item = data?.item ?? null;

  useEffect(() => {
    if (!focusShotListAfterAccept || (item?.filming_guide?.length ?? 0) === 0) return;
    setFocusShotListAfterAccept(false);
    window.requestAnimationFrame(() => {
      const target = document.querySelector<HTMLElement>(
        "[data-plan-shot-list-heading], [data-plan-shot-row='0']",
      );
      if (!target) return;
      const reduceMotion =
        typeof window.matchMedia === "function" &&
        window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      target.scrollIntoView({
        block: "center",
        behavior: reduceMotion ? "auto" : "smooth",
      });
      target.focus({ preventScroll: true });
    });
  }, [focusShotListAfterAccept, item?.filming_guide?.length]);

  // Sync voiceover path from item whenever it changes (after refetch / on load).
  useEffect(() => {
    if (item?.voiceover_gcs_path !== undefined) {
      setVoiceoverGcsPath(item.voiceover_gcs_path ?? null);
    }
  }, [item?.voiceover_gcs_path]);

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
    if (!variants.some((v) => v.variant_id === focusedVariantId)) {
      const firstReady = variants.find((v) => v.output_url) ?? variants[0];
      setFocusedVariantId(firstReady.variant_id);
    }
  }, [variants, focusedVariantId]);

  useEffect(() => {
    if (!TIKTOK_EDITOR_ENABLED) return;
    if (!item || item.status !== "ready") return;
    if (editorReturnSignal !== null) return;
    const readyVariants = variants.filter(
      (v) => v.render_status === "ready" && Boolean(v.output_url),
    );
    if (readyVariants.length !== 1) return;
    const variant = readyVariants[0];
    if (!canOpenPlanItemEditor(variant)) return;
    const jobId = item.current_job_id ?? "no-job";
    const markerKey = `plan-item:auto-open-editor:${item.id}:${jobId}:${variant.variant_id}`;
    if (autoOpenedEditorRef.current === markerKey) return;
    autoOpenedEditorRef.current = markerKey;
    try {
      if (window.sessionStorage.getItem(markerKey) === "1") return;
      window.sessionStorage.setItem(markerKey, "1");
    } catch {
      // Storage can be unavailable in private/embedded contexts; the navigation
      // is still useful, but the URL return guard below prevents immediate loops.
    }
    router.push(
      `/plan/items/${itemId}/edit?variant=${encodeURIComponent(variant.variant_id)}`,
    );
  }, [
    editorReturnSignal,
    item,
    itemId,
    router,
    variants,
  ]);

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
          setError(msg);
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

  // Legacy pool upload handler (uninstructed items only).
  async function handleFiles(files: FileList | null) {
    if (!files || files.length === 0 || isInstructed) return;
    setUploading(true);
    setError(null);
    conformancePolls.current = 0;
    try {
      let list = Array.from(files);
      if (isNarratedReady) {
        const { voiceoverFiles, clipFiles } = await splitNarratedReadyUploads(list);
        if (voiceoverFiles.length > 1) {
          throw new Error("Upload one voiceover audio file at a time");
        }
        if (voiceoverFiles.length === 1) {
          const uploaded = await uploadVoiceover(voiceoverFiles[0]);
          if (uploaded.kind !== "audio") {
            throw new Error("Upload an audio file for the voiceover");
          }
          const saved = await handleVoiceover(uploaded.gcs_path);
          if (!saved) return;
        }
        list = clipFiles;
        if (list.length === 0) return;
      }
      void detectLandscapeClip(list).then((found) => {
        if (found) setHasLandscapeClip(true);
      });
      const urls = await requestUploadUrls(
        itemId,
        list.map((f) => ({
          filename: f.name,
          content_type: uploadContentType(f),
          file_size_bytes: f.size,
        })),
      );
      await Promise.all(urls.map((u, i) => uploadToGcs(u.upload_url, list[i])));
      const newPaths = urls.map((u) => u.gcs_path);
      // Pass full assignments (not bare paths) so existing clips keep their
      // user_note across an append — the bare-paths legacy form resets them.
      const assignments = [
        ...(item?.clip_assignments ?? []).map((a) => ({
          gcs_path: a.gcs_path,
          shot_id: a.shot_id,
          user_note: a.user_note ?? "",
        })),
        ...newPaths.map((p) => ({ gcs_path: p, shot_id: null, user_note: "" })),
      ];
      await attachClips(
        itemId,
        assignments.map((a) => a.gcs_path),
        assignments,
      );
      refetch();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Upload failed");
    } finally {
      setUploading(false);
    }
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
    const remaining = (item?.clip_assignments ?? [])
      .filter((x) => x.gcs_path !== a.gcs_path)
      .map((x) => ({ gcs_path: x.gcs_path, shot_id: x.shot_id, user_note: x.user_note ?? "" }));
    try {
      await attachClips(
        itemId,
        remaining.map((x) => x.gcs_path),
        remaining,
      );
      refetch();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Couldn't remove that clip");
    }
  }

  // "Use in edit" (Visuals pool → clip promotion). Pool objects live under
  // users/{uid}/plan/{itemId}/pool/ — already inside attach_clips' allowed
  // prefix — so promotion is a plain re-attach with the pool path appended.
  // The asset stays in the pool (overlay suggestions still see it).
  async function promotePoolAsset(asset: PoolAsset) {
    // Pure merge (unit-tested): preserves every existing shot_id/user_note and
    // returns null on dedupe or a missing gcs_path (old-API version skew).
    const assignments = buildPromotedAssignments(item?.clip_assignments ?? [], asset.gcs_path);
    if (!assignments) return;
    conformancePolls.current = 0;
    try {
      await attachClips(
        itemId,
        assignments.map((a) => a.gcs_path),
        assignments,
      );
      refetch();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Couldn't add that visual to the edit");
    }
  }

  async function handleVoiceover(gcsPath: string | null): Promise<boolean> {
    setVoiceoverGcsPath(gcsPath);
    setVoiceoverSaving(true);
    try {
      await setItemVoiceover(itemId, gcsPath);
      refetch();
      return true;
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to save voiceover");
      return false;
    } finally {
      setVoiceoverSaving(false);
    }
  }

  async function handleExpandIdea(creatorContext: string | null) {
    if (!item) return;
    setExpanding(true);
    setExpandError(null);
    setAcceptExpandError(null);
    try {
      const proposal = await expandIdea(item.id, {
        creator_context: creatorContext,
      });
      if ((proposal.filming_guide?.length ?? 0) === 0) {
        setExpandError("Couldn't plan this idea — try again.");
        return;
      }
      setExpandProposal(proposal);
      setExpandContextOpen(false);
      setExpandError(null);
    } catch {
      setExpandError("Couldn't plan this idea — try again.");
    } finally {
      setExpanding(false);
    }
  }

  async function handleGenerate() {
    setGenerating(true);
    setError(null);
    // Arm the wait window BEFORE the POST so the release-effect can't fire
    // early while the request is still in flight.
    awaitingJobSince.current = Date.now();
    try {
      if (item && needsFormatPersist(item.edit_format)) {
        await updatePlanItem(item.id, { edit_format: resolvedFormat });
      }
      await generatePlanItem(itemId);
      refetch();
    } catch (err) {
      awaitingJobSince.current = null;
      setError(err instanceof Error ? err.message : "Failed to start generation");
      setGenerating(false);
    }
  }

  // Release the Generate lock once the render registers (or the wait window
  // expires without a job — surface that instead of silently doing nothing).
  useEffect(() => {
    const registered = !!(item?.current_job_id || item?.status === "generating");
    if (registered) {
      // A registered render moots any earlier didn't-register complaint —
      // clear it even if it was shown in a previous attempt (dogfood: the
      // banner outlived the render it was wrong about).
      setError((prev) => (prev === RENDER_REGISTER_ERROR ? null : prev));
    }
    if (!generating) return;
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
  }, [generating, item?.current_job_id, item?.status, data]);

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
  const showSetupControls = !isGenerating && variants.length === 0;
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
  // Button + hint from ONE decision so they can never disagree (plan-generate-gate).
  const gate = generateGate({
    generating,
    isGenerating,
    uploaderBusy,
    clipCount,
    isNarrated,
    hasVoiceover: !!voiceoverGcsPath,
    selfNarrationEnabled,
    isInstructed,
    shotsLeft,
  });
  // "Your narrated render became a montage" explanation (no_speech etc.) —
  // persisted by the orchestrator, surfaced here so the swap is never silent.
  const fallbackBanner = narrationFallbackBanner(
    isNarrated,
    data?.job?.archetype_fallback ?? null,
  );

  const currentPhase =
    data?.job?.current_phase ??
    (!data?.job?.started_at ? "queued" : null);
  const theaterIsTerminal = !!(item && isTerminalFn({ item, job: data?.job ?? null }));
  const theaterIsSuccess = item?.status === "ready";

  return (
    <LightShell size="wide">
      {/* @font-face for style-preview chips */}
      <style dangerouslySetInnerHTML={{ __html: FONT_FACES }} />
      <div className="motion-safe:animate-fade-up">

        {/* ── Single-column layout: back link + header + shot plan + generate + progress ── */}
        <div>

          {/* Content: back link + editorial header + uploader + generate + progress */}
          <div>
            <Link
              href="/plan"
              className="text-sm text-[#71717a] underline-offset-2 transition-colors hover:text-[#0c0c0e]"
            >
              ← back to plan
            </Link>
            {item.day_index != null && (
              <div className="mb-1 mt-4 flex items-center gap-3">
                <span className="rounded bg-zinc-100 px-2 py-0.5 text-xs text-[#71717a]">
                  Day {item.day_index}
                </span>
              </div>
            )}
            <h1 className="font-display mt-4 text-3xl text-[#0c0c0e]">
              {item.theme ?? item.idea}
            </h1>
            {item.theme && <p className="mb-2 mt-2 text-[#3f3f46]">{item.idea}</p>}
            <SeedProvenanceBadge item={item} />

            {showSetupControls && (
              <>
            {/* Notes textarea — editable, saves on blur */}
            <textarea
              defaultValue={item.notes ?? ""}
              onBlur={async (e) => {
                const val = e.currentTarget.value.trim() || null;
                if (val !== (item.notes ?? null)) {
                  await updatePlanItem(item.id, { notes: val ?? undefined }).catch(() => null);
                  refetch();
                }
              }}
              placeholder="Add notes…"
              rows={2}
              className="mb-4 mt-2 w-full resize-none rounded-lg border border-zinc-200 bg-transparent px-3 py-2 text-sm text-[#3f3f46] placeholder-zinc-400 focus:border-zinc-400 focus:outline-none"
            />

            {/* Format picker — shown when item hasn't started generating */}
            {item.status !== "generating" && item.status !== "ready" && variants.length === 0 && (
              <div className="mb-4">
                <p className="mb-2 text-xs font-medium uppercase tracking-wide text-zinc-400">
                  Edit style
                </p>
                {/* Stack on mobile (3 cards don't fit a 375px row), equal columns from sm:. */}
                <div className="grid gap-2 sm:grid-flow-col sm:auto-cols-fr">
                  {(
                    [
                      { value: "montage", ...EDIT_FORMAT_LABELS.montage },
                      ...(isTalkingHead
                        ? [{ value: "talking_head", ...EDIT_FORMAT_LABELS.talking_head }]
                        : []),
                      { value: "narrated_planned", ...EDIT_FORMAT_LABELS.narrated_planned },
                      ...(SUBTITLED_ENABLED
                        ? [{ value: "subtitled", ...EDIT_FORMAT_LABELS.subtitled }]
                        : []),
                    ] as { value: string; label: string; desc: string }[]
                  ).map(({ value, label, desc }) => {
                    const active = resolvedFormat === value;
                    return (
                      <button
                        key={label}
                        type="button"
                        onClick={async () => {
                          if (active) return;
                          await updatePlanItem(item.id, { edit_format: value }).catch(() => null);
                          refetch();
                        }}
                        className={`flex flex-1 flex-col rounded-xl border px-3 py-2.5 text-left transition-colors ${
                          active
                            ? "border-lime-400 bg-lime-50"
                            : "border-zinc-200 bg-white hover:border-zinc-300"
                        }`}
                      >
                        <span className={`text-sm font-medium ${active ? "text-lime-800" : "text-[#0c0c0e]"}`}>
                          {label}
                        </span>
                        <span className="mt-0.5 text-xs text-[#71717a]">{desc}</span>
                      </button>
                    );
                  })}
                </div>

                {/* Narrated sub-mode picker */}
                {isNarrated && (
                  <div className="mt-3 flex gap-2">
                    {(
                      [
                        { value: "narrated_planned", label: "Planning to film", desc: "Get a step guide, then film each shot" },
                        { value: "narrated_ready",   label: "I have the videos", desc: "Upload clips and we'll match them to your voice" },
                      ] as { value: string; label: string; desc: string }[]
                    ).map(({ value, label, desc }) => {
                      const active = isNarratedReady
                        ? value === "narrated_ready"
                        : value === "narrated_planned";
                      return (
                        <button
                          key={value}
                          type="button"
                          onClick={async () => {
                            if (active) return;
                            await updatePlanItem(item.id, { edit_format: value }).catch(() => null);
                            refetch();
                          }}
                          className={`flex flex-1 flex-col rounded-xl border px-3 py-2 text-left transition-colors ${
                            active
                              ? "border-zinc-900 bg-zinc-900"
                              : "border-zinc-200 bg-white hover:border-zinc-300"
                          }`}
                        >
                          <span className={`text-xs font-semibold ${active ? "text-white" : "text-[#0c0c0e]"}`}>
                            {label}
                          </span>
                          <span className={`mt-0.5 text-[11px] ${active ? "text-zinc-400" : "text-zinc-400"}`}>{desc}</span>
                        </button>
                      );
                    })}
                  </div>
                )}

                {/* Montage sub-mode picker — "Planning to film" vs "I already have footage".
                    Flips the per-item content_mode override so the user can skip shot-plan
                    generation and go straight to the pool uploader. Only shown when Montage
                    is the active style (narrated + subtitled have no content_mode sub-modes). */}
                {isMontage && (
                  <div className="mt-3 space-y-3">
                    <div className="flex gap-2">
                      {(
                        [
                          { value: "create_new",       label: "Planning to film",        desc: "Get a shot plan, film each shot" },
                          { value: "existing_footage", label: "I already have footage",  desc: "Skip the plan — just upload your footage" },
                        ] as { value: "create_new" | "existing_footage"; label: string; desc: string }[]
                      ).map(({ value, label, desc }) => {
                        // "I already have footage" is active when content_mode is explicitly
                        // existing_footage; otherwise "Planning to film" is the default.
                        const active = value === "existing_footage"
                          ? contentMode === "existing_footage"
                          : contentMode !== "existing_footage";
                        return (
                          <button
                            key={value}
                            type="button"
                            onClick={async () => {
                              if (active) return;
                              await updatePlanItem(item.id, { content_mode: value }).catch(() => null);
                              refetch();
                            }}
                            className={`flex flex-1 flex-col rounded-xl border px-3 py-2 text-left transition-colors ${
                              active
                                ? "border-zinc-900 bg-zinc-900"
                                : "border-zinc-200 bg-white hover:border-zinc-300"
                            }`}
                          >
                            <span className={`text-xs font-semibold ${active ? "text-white" : "text-[#0c0c0e]"}`}>
                              {label}
                            </span>
                            <span className={`mt-0.5 text-[11px] ${active ? "text-zinc-400" : "text-zinc-400"}`}>{desc}</span>
                          </button>
                        );
                      })}
                    </div>

                    <div>
                      <p className="mb-2 text-xs font-medium uppercase tracking-wide text-zinc-400">
                        Preset
                      </p>
                      <div className="grid gap-2 sm:grid-cols-3">
                        {MONTAGE_PRESET_OPTIONS.map(({ value, label, desc }) => {
                          const active = montagePreset === value;
                          return (
                            <button
                              key={value}
                              type="button"
                              onClick={async () => {
                                if (active) return;
                                await updatePlanItem(item.id, { montage_preset: value }).catch(() => null);
                                refetch();
                              }}
                              className={`flex flex-col rounded-xl border p-2 text-left transition-colors ${
                                active
                                  ? "border-lime-400 bg-lime-50"
                                  : "border-zinc-200 bg-white hover:border-zinc-300"
                              }`}
                            >
                              <MontagePresetPreview value={value} />
                              <span className={`mt-2 text-sm font-medium ${active ? "text-lime-800" : "text-[#0c0c0e]"}`}>
                                {label}
                              </span>
                              <span className="mt-0.5 text-xs text-zinc-400">{desc}</span>
                            </button>
                          );
                        })}
                      </div>
                    </div>
                  </div>
                )}
              </div>
            )}

            {/* Landscape-clip fit picker — only appears once a wide clip is detected on
                upload (hasLandscapeClip), so the common all-portrait case never sees
                this control. Reads as a detected notice, not a surprise setting. */}
            {item.status !== "generating" &&
              item.status !== "ready" &&
              variants.length === 0 &&
              hasLandscapeClip && (
              <div className="mb-4">
                <p className="mb-2 text-xs font-medium uppercase tracking-wide text-zinc-400">
                  Landscape clip detected
                </p>
                <div className="flex gap-2">
                  {LANDSCAPE_FIT_OPTIONS.map(({ value, label, desc }) => {
                    const active = (item.landscape_fit ?? "fit") === value;
                    return (
                      <button
                        key={value}
                        type="button"
                        onClick={async () => {
                          if (active) return;
                          await updatePlanItem(item.id, { landscape_fit: value }).catch(() => null);
                          refetch();
                        }}
                        className={`flex flex-1 flex-col rounded-xl border px-3 py-2.5 text-left transition-colors ${
                          active
                            ? "border-lime-400 bg-lime-50"
                            : "border-zinc-200 bg-white hover:border-zinc-300"
                        }`}
                      >
                        <span className={`text-sm font-medium ${active ? "text-lime-800" : "text-[#0c0c0e]"}`}>
                          {label}
                        </span>
                        <span className="mt-0.5 text-xs text-zinc-400">{desc}</span>
                      </button>
                    );
                  })}
                </div>
              </div>
            )}

            {/* AI plan proposal — available only until the item has a shot list. */}
            {totalShots === 0 && clipCount === 0 && !expandProposal && !expandContextOpen && (
              <div className="mb-4">
                <button
                  type="button"
                  disabled={expanding}
                  onClick={() => {
                    setExpandContextOpen(true);
                    setExpandError(null);
                    setAcceptExpandError(null);
                  }}
                  className="flex items-center gap-1.5 rounded-lg border border-zinc-200 bg-white px-3 py-2 text-[12px] text-[#71717a] transition-colors hover:border-lime-400 hover:text-lime-700 disabled:cursor-not-allowed disabled:opacity-50"
                >
                  <span aria-hidden>✦</span>
                  Plan this for me
                </button>
                {expandError && (
                  <p className="mt-2 text-xs text-[#71717a]">{expandError}</p>
                )}
              </div>
            )}

            {totalShots === 0 && clipCount === 0 && !expandProposal && expandContextOpen && (
              <div className="mb-4 rounded-xl border border-zinc-200 bg-white p-4">
                <p className="font-display text-lg font-medium text-[#0c0c0e]">
                  A little context helps.
                </p>
                <label className="mt-3 block text-sm text-[#3f3f46]">
                  <span>{expandContextPrompt(resolvedFormat)}</span>
                  <textarea
                    value={expandContext}
                    onChange={(e) => setExpandContext(e.currentTarget.value)}
                    maxLength={800}
                    rows={3}
                    className="mt-2 w-full resize-none rounded-lg border border-zinc-200 bg-[#fafaf8] px-3 py-2 text-base text-[#0c0c0e] placeholder-zinc-400 focus:border-lime-500/60 focus:outline-none"
                    placeholder="A rough goal or detail is enough..."
                  />
                </label>
                <div className="mt-3 flex flex-wrap gap-2">
                  <button
                    type="button"
                    disabled={expanding}
                    onClick={() => handleExpandIdea(expandContext)}
                    className="rounded-lg bg-lime-600 px-4 py-2 text-[12px] font-semibold text-white hover:bg-lime-700 disabled:cursor-not-allowed disabled:opacity-50"
                  >
                    {expanding ? "Thinking…" : "Generate plan"}
                  </button>
                  <button
                    type="button"
                    disabled={expanding}
                    onClick={() => handleExpandIdea(null)}
                    className="rounded-lg border border-zinc-200 bg-white px-4 py-2 text-[12px] text-[#71717a] hover:border-zinc-400 disabled:cursor-not-allowed disabled:opacity-50"
                  >
                    Skip and generate
                  </button>
                </div>
                {expanding && (
                  <p className="mt-2 flex items-center gap-1.5 text-xs text-[#71717a]">
                    <span
                      aria-hidden
                      className="h-1.5 w-1.5 rounded-full bg-lime-500 motion-safe:animate-ping"
                    />
                    Turning this into a plan…
                  </p>
                )}
                {expandError && (
                  <p className="mt-2 text-xs text-[#71717a]">{expandError}</p>
                )}
              </div>
            )}

            {/* AI plan proposal card */}
            {totalShots === 0 && clipCount === 0 && expandProposal && (
              <div className="mb-4">
                <div className="rounded-xl border border-lime-200 bg-lime-50 p-4">
                  <p className="text-[11px] font-semibold uppercase tracking-[.15em] text-lime-700">
                    AI SUGGESTION
                  </p>
                  <p className="mt-1 font-display text-lg font-medium text-[#0c0c0e]">
                    {expandProposal.theme}
                  </p>
                  {expandProposal.filming_suggestion && (
                    <p className="mt-1 text-sm text-[#3f3f46]">{expandProposal.filming_suggestion}</p>
                  )}
                  <ol className="mt-4 space-y-3">
                    {expandProposal.filming_guide.map((shot, index) => (
                      <li key={shot.shot_id ?? `${shot.what}-${index}`} className="flex gap-3">
                        <span className="font-display text-[17px] italic text-lime-600">
                          {index + 1}.
                        </span>
                        <div className="min-w-0 flex-1">
                          <div className="flex items-start justify-between gap-3">
                            <p className="text-[15px] font-medium text-[#0c0c0e]">{shot.what}</p>
                            <span className="shrink-0 rounded border border-zinc-200 bg-white px-1.5 py-0.5 text-[11px] text-[#3f3f46]">
                              ~{shot.duration_s}s
                            </span>
                          </div>
                          {shot.how && (
                            <p className="mt-0.5 text-[13.5px] text-[#3f3f46]">{shot.how}</p>
                          )}
                        </div>
                      </li>
                    ))}
                  </ol>
                  <div className="mt-4 flex gap-2">
                    <button
                      type="button"
                      disabled={acceptingExpand}
                      onClick={async () => {
                        setAcceptingExpand(true);
                        setAcceptExpandError(null);
                        try {
                          await updatePlanItem(item.id, {
                            theme: expandProposal.theme,
                            filming_suggestion: expandProposal.filming_suggestion,
                            filming_guide: expandProposal.filming_guide,
                          });
                          setExpandProposal(null);
                          setExpandContext("");
                          setExpandContextOpen(false);
                          setFocusShotListAfterAccept(true);
                          refetch();
                        } catch {
                          setAcceptExpandError("Couldn't save the plan — try again.");
                        } finally {
                          setAcceptingExpand(false);
                        }
                      }}
                      className="rounded-lg bg-lime-600 px-4 py-1.5 text-[12px] font-semibold text-white hover:bg-lime-700 disabled:cursor-not-allowed disabled:opacity-50"
                    >
                      {acceptingExpand ? "Saving…" : "Use this plan"}
                    </button>
                    <button
                      type="button"
                      disabled={acceptingExpand}
                      onClick={() => {
                        setExpandProposal(null);
                        setAcceptExpandError(null);
                      }}
                      className="rounded-lg border border-zinc-200 bg-white px-4 py-1.5 text-[12px] text-[#71717a] hover:border-zinc-400 disabled:cursor-not-allowed disabled:opacity-50"
                    >
                      Dismiss
                    </button>
                  </div>
                </div>
                {expandProposal.rationale && (
                  <p className="mt-2 text-xs italic text-[#71717a]">{expandProposal.rationale}</p>
                )}
                {acceptExpandError && (
                  <p className="mt-2 text-xs text-[#71717a]">{acceptExpandError}</p>
                )}
              </div>
            )}

            {hasGuide && !isInstructed && <CompactPlanSummary item={item} />}

            {/* Narrated walkthrough: sticky voice recorder bar — shown for both narrated sub-modes */}
            {isNarrated && (
              <div className="sticky top-0 z-10 -mx-6 mb-6 border-b border-zinc-100 bg-[#fafaf8] px-6 py-3">
                <p className="mb-2 text-xs font-medium uppercase tracking-wide text-zinc-400">
                  Voice recording
                </p>
                <VoiceRecorder onVoiceover={handleVoiceover} />
                {voiceoverSaving && (
                  <p className="mt-1 text-xs text-zinc-400">Saving…</p>
                )}
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
                    <span aria-hidden>✦</span>
                    Not sure what to say? Write a script with Kria
                  </Link>
                )}
              </div>
            )}

            {/* Background sound (narrated) and caption style/on-off (narrated +
                talking-to-camera) moved to the post-gen editor — see
                BackgroundSoundControl / CaptionStyleToggle in PlanVariantEditor.tsx.
                Talk-to-camera auto-generates WITH subtitles by default; both are
                tunable after generation, not before. */}
            {(isNarrated || isSubtitled) && (
              <p className="mb-4 text-xs text-[#a1a1aa]">
                {isSubtitled
                  ? "Subtitles are added automatically and editable after you generate."
                  : "Background sound and captions can be tuned after you generate."}
              </p>
            )}

            {/* Uploader — branches:
                0. subtitled: one talk-to-camera clip → pool upload (no shot plan)
                1. talking_head: speech spine + B-roll clips → pool upload
                2. narrated_ready: audio-first flow, pool upload, no step spine
                3. masonry montage → compact pool strip even when guide present
                4. isInstructed (create_new/mixed + guide present) → ShotSlotUploader
                5. isFilmThis but no guide yet → no uploader until Plan this for me is accepted
                6. existing_footage → PoolUploadCard (use footage you already have) */}
            {isSubtitled ? (
              <div>
                <p className="mb-3 text-xs font-medium uppercase tracking-wide text-zinc-400">
                  Your clip
                </p>
                <p className="mb-4 text-sm text-[#71717a]">
                  Upload one clip of you talking to camera. We&apos;ll transcribe what you
                  say and add editable captions, in Turkish or English.
                </p>
                <PoolUploadCard
                  clips={item.clip_assignments ?? []}
                  uploading={uploading}
                  onFiles={handleFiles}
                  onKeep={keepUninstructedMatch}
                  onRemove={removeUninstructedClip}
                  onNoteChange={saveUninstructedNote}
                  maxClips={1}
                  accept={itemUploadAccept}
                />
              </div>
            ) : isTalkingHead ? (
              <div>
                <p className="mb-3 text-xs font-medium uppercase tracking-wide text-zinc-400">
                  Your clips
                </p>
                <p className="mb-4 text-sm text-[#71717a]">
                  Upload the clip with the spoken audio plus any extra clips you want cut in.
                </p>
                <PoolUploadCard
                  clips={item.clip_assignments ?? []}
                  uploading={uploading}
                  onFiles={handleFiles}
                  onKeep={keepUninstructedMatch}
                  onRemove={removeUninstructedClip}
                  onNoteChange={saveUninstructedNote}
                  accept={itemUploadAccept}
                />
              </div>
            ) : isNarratedReady ? (
              <div>
                <p className="mb-3 text-xs font-medium uppercase tracking-wide text-zinc-400">
                  Your clips
                </p>
                {/* Self-narration mode keeps this line short — the gate hint under
                    Generate carries the "your video's own narration" explanation
                    (one explanation per screen, DESIGN.md §9). */}
                <p className="mb-4 text-sm text-[#71717a]">
                  {selfNarrationEnabled && !voiceoverGcsPath
                    ? "Upload all the clips you filmed."
                    : "Upload all the clips you filmed. We'll listen to your recording and match each moment to the right clip automatically."}
                </p>
                <PoolUploadCard
                  clips={item.clip_assignments ?? []}
                  uploading={uploading}
                  onFiles={handleFiles}
                  onKeep={keepUninstructedMatch}
                  onRemove={removeUninstructedClip}
                  onNoteChange={saveUninstructedNote}
                  accept={itemUploadAccept}
                />
              </div>
            ) : isCollagePreset ? (
              <div>
                <p className="mb-3 text-xs font-medium uppercase tracking-wide text-zinc-400">
                  Your clips
                </p>
                <PoolUploadCard
                  clips={item.clip_assignments ?? []}
                  uploading={uploading}
                  onFiles={handleFiles}
                  onKeep={keepUninstructedMatch}
                  onRemove={removeUninstructedClip}
                  onNoteChange={saveUninstructedNote}
                  accept={itemUploadAccept}
                />
              </div>
            ) : isInstructed ? (
              <ShotSlotUploader
                item={item}
                onAttached={(updated) => {
                  conformancePolls.current = 0;
                  refetch();
                }}
                onBusyChange={setUploaderBusy}
              />
            ) : isFilmThis ? (
              null
            ) : (
              /* existing_footage — pool upload (find the footage you already have) */
              <>
                {!hasGuide && item.filming_suggestion ? (
                  <p className="mb-4 text-sm text-[#71717a]">{item.filming_suggestion}</p>
                ) : null}
                <PoolUploadCard
                  clips={item.clip_assignments ?? []}
                  uploading={uploading}
                  onFiles={handleFiles}
                  onKeep={keepUninstructedMatch}
                  onRemove={removeUninstructedClip}
                  onNoteChange={saveUninstructedNote}
                  accept={itemUploadAccept}
                />
              </>
            )}

            {/* Visuals pool — screenshots/screen recordings that feed AI overlay
                auto-placement (plans/005 PR0). Renders nothing unless
                NEXT_PUBLIC_OVERLAY_AUTOPLACE_ENABLED=true (gate lives inside). */}
            {showVisualPools && (
              <AssetPool
                itemId={itemId}
                attachedPaths={item.clip_assignments?.map((a) => a.gcs_path) ?? []}
                onUseInEdit={promotePoolAsset}
                attachBusy={uploading || uploaderBusy}
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
                previewUrl={focused?.output_url ?? focused?.base_video_url ?? null}
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

            {/* Generate + hint caption — both from generateGate (plan-generate-gate)
                so the disabled state and its explanation can never disagree. */}
            {!isGenerating && (
              <div className="mt-4 space-y-2">
                <InkButton onClick={handleGenerate} disabled={gate.disabled}>
                  {generating
                    ? "Starting…"
                    : uploaderBusy
                      ? FINISHING_UPLOAD_HINT
                      : "Generate video"}
                </InkButton>
                {/* #71717a, not the faint #a1a1aa: this line now carries must-read
                    gating copy (why the button is off / what drives the edit) —
                    DESIGN.md §8 keeps faint ink decorative-only. */}
                <p className="text-center text-sm text-[#71717a]">{gate.hint}</p>
              </div>
            )}

            {/* Kria helper — inline, below Generate when there is an actual read. */}
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

            {/* ProgressTheater — light tone */}
            {data?.job && (
              <div className="mt-8">
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
                  receiptText={deriveReceiptText(data.job)}
                  variants={variants}
                  size="full"
                  tone="light"
                >
                  {null}
                </ProgressTheater>
              </div>
            )}
            {isGenerating && (
              <p className="mt-1 text-xs text-[#a1a1aa]">
                Usually 2–3 minutes. You can leave this page — we&apos;ll keep rendering.
              </p>
            )}
            {item.status === "failed" && variants.length === 0 && (
              <p className="mt-2 text-sm text-[#71717a]">
                Generation failed before any variant rendered. Try generating again.
              </p>
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
 *   HERO — large 9/16 video player (active variant). "Kria's pick" lime badge
 *   on variants[0]; text_mode label pill below the video.
 *
 *   RIGHT (desktop) / BELOW (mobile):
 *     Rationale blurb (1-2 sentences derived from text_mode + track_title)
 *     Alternates row — small thumbnails for the other ready variants
 *     Editor row — 4 icon+label buttons that reveal PlanVariantEditor inline
 *     Download button + feedback
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
    // Mirrors the timeline lane's remove path at the page level (the lane's
    // handler lives in FocusedVariantControls, which only mounts with the
    // Timeline tab open). The removal persists on the next Download bake,
    // which always sends the CURRENT overlayCards list.
    setOverlayCards((prev) => prev.filter((c) => c.id !== cardId));
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
  }, []);
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
  const pendingDownloadRef = useRef(false);

  // When a download-triggered burn completes (render_finished_at advances), clear the CSS
  // preview layer — the burned output_url now has the cards composited in. Only fires when
  // pendingDownloadRef is true so stale/concurrent renders (e.g. completing text edits, or
  // lingering renders from a previous session) don't wipe newly uploaded card previews.
  const prevFinishedAtRef = useRef<string | null | undefined>(undefined);
  useEffect(() => {
    const cur = variant?.render_finished_at ?? null;
    if (prevFinishedAtRef.current !== undefined && cur !== prevFinishedAtRef.current) {
      if (pendingDownloadRef.current) {
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
    if (!pendingDownloadRef.current) return;
    if (editSession.isSaving) return;
    if (variant?.render_status === "ready" && variant.output_url) {
      pendingDownloadRef.current = false;
      downloadVideo(variant.output_url, downloadName);
    } else if (variant?.render_status === "failed") {
      // A download-triggered bake failed on the backend (FFmpeg error after a
      // successful dispatch). Surface it — otherwise the button silently
      // re-enables, no file downloads, and the stale output_url keeps playing
      // as if it succeeded. The Apply/Retry button that used to surface this is
      // gone; the implicit retry is to click Download again (needsSfxBake stays
      // true after a failed bake).
      pendingDownloadRef.current = false;
      onError("Couldn't prepare your video for download. Please click Download to try again.");
    }
  }, [editSession.isSaving, variant?.render_status, variant?.output_url, downloadName, onError]);

  const baking = (instantEligible && editSession.isSaving) || pendingDownloadRef.current;

  // Inline SFX dirtiness (D5): does the download need a fresh SFX bake, and is
  // the latest set persisted? Computed from the variant + placements — no
  // sticky flag. See lib/sfx-dirty.ts.
  const needsSfxBake = sfxNeedsBake(sfxPlacements, variant);
  const sfxIsPersistDirty = sfxPersistDirty(sfxPlacements, variant);

  // Plan 009 T4: failed cards still in the working set — derived as an
  // intersection so removing a card (tile Remove or lane) unblocks instantly.
  const failedOverlayCount = overlayCards.filter((c) => failedCardIds.has(c.id)).length;

  const handleDownload = useCallback(async () => {
    if (!variant) return;

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
    if (overlayCards.length > 0) {
      // Plan 009 T4: a card whose media failed to load would bake a broken /
      // blank visual — block the overlay-bake path until it's refreshed or
      // removed (inline copy under the button explains why).
      if (failedOverlayCount > 0) return;
      pendingDownloadRef.current = true;
      try {
        await flushSfx();
        await setVariantMediaOverlays(itemId, variant.variant_id, overlayCards, { render: true });
        markVariantRendering(variant.variant_id, variant.render_finished_at ?? null);
      } catch (err) {
        pendingDownloadRef.current = false;
        onError(
          err instanceof Error
            ? err.message
            : "Couldn't add your overlays to the video. Try again.",
        );
      }
      return;
    }

    // SFX-only: bake when placements differ from what's baked into output_url.
    // Inline compare (not a sticky flag) → "nothing changed" downloads instantly.
    if (needsSfxBake) {
      pendingDownloadRef.current = true;
      try {
        await flushSfx();
        await renderVariantSfx(itemId, variant.variant_id);
        markVariantRendering(variant.variant_id, variant.render_finished_at ?? null);
      } catch (err) {
        pendingDownloadRef.current = false;
        onError(
          err instanceof Error
            ? err.message
            : "Couldn't add your sound effects to the video. Try again.",
        );
      }
      return;
    }

    if (!variant.output_url && !editSession.isDirty) return;
    if (instantEligible && editSession.isDirty) {
      pendingDownloadRef.current = true;
      void editSession.commit();
      return;
    }
    if (variant.output_url) downloadVideo(variant.output_url, downloadName);
  }, [variant, editSession, instantEligible, sfxPlacements, needsSfxBake, sfxIsPersistDirty, overlayCards, failedOverlayCount, itemId, downloadName, markVariantRendering, onError]);

  // Item pages now present one primary output. Keep the deeper inline editor
  // machinery dormant here; the full-screen editor owns post-render editing.
  const showInlineEditorControls = false;
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
      ? "Voiceover"
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
  const editorCapabilities = (
    variant as (PlanItemVariant & { editor_capabilities?: EditorCapabilities }) | null
  )?.editor_capabilities;
  const editorEntryDisabledReason =
    editorCapabilities &&
    !editorCapabilities.text_elements &&
    !editorCapabilities.timeline &&
    !editorCapabilities.split_clips &&
    !editorCapabilities.mix
      ? editorReasonCopy(editorCapabilities.reason)
      : null;

  // The editor panel reveals PlanVariantEditor filtered to the active tab.
  // We keep one PlanVariantEditor instance and use the tab to scroll/focus.
  const focusedEditable = variant && (!!variant.output_url || variant.render_status === "failed");

  return (
    <div className="mt-8">
      {/* Hero + rail: on desktop they are side-by-side */}
      <div className="flex flex-col gap-6 lg:flex-row lg:items-start">

        {/* ── HERO: large video player ── */}
        <div className="w-full shrink-0 sm:max-w-xs lg:w-[300px]">
          {/* data-variant-preview: stable DOM hook for the SuggestionRail reveal
              (row click seeks this variant's preview video — plans/005 1A). */}
          <div className="relative" data-variant-preview={variant?.variant_id}>
            {/* "Kria's pick" badge */}
            {isKriaPick && variant?.output_url && (
              <span className="absolute left-3 top-3 z-10 rounded-full border border-lime-300 bg-lime-50 px-2.5 py-0.5 text-[11px] font-semibold text-lime-800">
                Kria&apos;s pick
              </span>
            )}
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
              />
            )}
          </div>
          {/* Text-mode pill below video */}
          {modePill && !isGenerating && (
            <div className="mt-2 flex justify-center">
              <span className="rounded-full border border-zinc-200 bg-white px-3 py-0.5 text-xs text-[#71717a]">
                {modePill}
              </span>
            </div>
          )}
        </div>

        {/* ── RAIL: rationale + actions + feedback ── */}
        <div className="min-w-0 flex-1 space-y-5">

          {/* Rationale blurb */}
          {variant && !isGenerating && (
            <p className="text-sm text-[#3f3f46]">
              {deriveRationale(variant, variants.length)}
            </p>
          )}
          {isGenerating && (
            <p className="text-sm text-[#71717a]">
              Edit controls unlock as soon as a variant finishes rendering.
            </p>
          )}

          {/* ── Unplaced shots info card ── */}
          {variant && (variant.unplaced_shots?.length ?? 0) > 0 && (
            <div className="rounded-lg border border-amber-200 bg-amber-50 px-3.5 py-3">
              <p className="mb-1.5 text-[11px] font-semibold uppercase tracking-[0.12em] text-amber-700">
                Not in this take
              </p>
              <ul className="space-y-0.5">
                {variant.unplaced_shots!.map((shot) => (
                  <li key={shot.clip_id} className="text-xs text-amber-800">
                    <span className="font-medium">Shot {shot.shot_index}</span>
                    {" – "}
                    {unplacedShotCopy(shot.reason)}
                  </li>
                ))}
              </ul>
            </div>
          )}

          {/* ── Editor row: 4 icon+label buttons ── */}
          {showInlineEditorControls && focusedEditable && (
            <div>
              <div className="flex gap-2">
                {EDITOR_TABS.map((tab) => {
                  // Archetype-gated (isCaptionArchetype mirrors the backend's
                  // _is_editable_caption_variant), NOT cue-count-gated — a cue-count
                  // gate would make the tab vanish the moment subtitles are toggled
                  // off, trapping the user with no way back on (the bug the plan-item
                  // redesign's User Challenge caught).
                  const hasCaptions = !!variant && isCaptionArchetype(variant);
                  if (tab.id === "captions" && !hasCaptions) return null;
                  // Caption variants have no song to edit — only Captions + Clips.
                  if (hasCaptions && tab.id === "song") return null;
                  // Hide Song tab when no song is swappable
                  if (tab.id === "song" && (tracks.length === 0 || !variant?.music_track_id)) return null;
                  // Timeline: show when SFX is enabled or this variant has a text lane.
                  if (tab.id === "timeline" && !SOUND_EFFECTS_ENABLED && !textLaneEligible) return null;
                  const isActive = activeTab === tab.id;
                  return (
                    <button
                      key={tab.id}
                      type="button"
                      aria-pressed={isActive}
                      onClick={() => {
                        // Any manual tab interaction (open OR close) settles the
                        // caption auto-open: a user who dismisses the Captions tab
                        // must not have it force-reopened by a later "ready" poll
                        // (FocusedResults is keyed by variant_id, so this ref
                        // survives polls within the same variant).
                        autoOpenedCaptionsRef.current = true;
                        setActiveTab(isActive ? null : tab.id);
                      }}
                      className={`flex flex-col items-center gap-0.5 rounded-xl border px-3 py-2 text-center transition-colors ${
                        isActive
                          ? "border-lime-600 bg-lime-50 text-lime-800"
                          : "border-zinc-200 bg-white text-[#3f3f46] hover:border-zinc-400"
                      }`}
                    >
                      <span className="text-sm font-semibold leading-none">{tab.icon}</span>
                      <span className="text-[10px] leading-tight">{tab.label}</span>
                    </button>
                  );
                })}
              </div>

              {/* Inline editor panel — slides open below the tab row */}
              {activeTab !== null && variant && (
                <div className="mt-3">
                  {activeTab === "captions" ? (
                    variant.base_video_url && variant.caption_cues ? (
                    <div className="space-y-3" data-plan-captions-panel>
                    <CaptionEditor
                      // Re-mount (re-seed cues) whenever a server render replaces them —
                      // a language re-transcribe swaps all cues, and the editor otherwise
                      // keeps the cue state it seeded at mount (stale English after a
                      // switch to Türkçe). render_finished_at advances on every reburn/
                      // re-transcribe, so this re-seeds from the fresh server cues.
                      key={`${variant.variant_id}:${variant.render_finished_at ?? ""}`}
                      itemId={itemId}
                      variantId={variant.variant_id}
                      baseVideoUrl={variant.base_video_url}
                      initialCues={variant.caption_cues}
                      initialFont={variant.voiceover_caption_font}
                      initialCaptionStyle={variant.voiceover_caption_style ?? "sentence"}
                      initialCaptionsEnabled={variant.captions_enabled ?? true}
                      wordHint={
                        variant.resolved_archetype === "subtitled"
                          ? "Each word pops as you say it"
                          : undefined
                      }
                      rendering={variant.render_status === "rendering"}
                      // Subtitled captions are machine-transcribed from the clip's own
                      // audio — nudge review before Apply (D6). Narrated (own voiceover)
                      // doesn't need it.
                      reviewFirst={variant.resolved_archetype === "subtitled"}
                      // Preview offset mirrors the burn. A stored caption_margin_v
                      // wins; absent legacy rows keep subtitled=384/1920 (20%) and
                      // narrated=180/1920 (9.4%).
                      previewBottomCqh={
                        variant.caption_margin_v != null
                          ? (variant.caption_margin_v / 1920) * 100
                          : variant.resolved_archetype === "subtitled"
                            ? 20
                            : 9.4
                      }
                      // D5 language override — chip + re-transcribe, subtitled only.
                      captionLanguage={
                        variant.resolved_archetype === "subtitled"
                          ? (variant.caption_language ?? "en")
                          : null
                      }
                      onChangeLanguage={
                        variant.resolved_archetype === "subtitled"
                          ? async (language) => {
                              try {
                                await setPlanItemCaptionLanguage(
                                  itemId,
                                  variant.variant_id,
                                  language,
                                );
                                markVariantRendering(
                                  variant.variant_id,
                                  variant.render_finished_at ?? null,
                                );
                                refetch();
                              } catch (err) {
                                onError(
                                  err instanceof Error
                                    ? err.message
                                    : "Couldn't change the caption language.",
                                );
                              }
                            }
                          : undefined
                      }
                      onApplied={() => {
                        markVariantRendering(
                          variant.variant_id,
                          variant.render_finished_at ?? null,
                        );
                        refetch();
                      }}
                    />
                    {variant.resolved_archetype === "narrated" && (
                      <BackgroundSoundControl
                        key={`${variant.variant_id}:bed:${variant.render_finished_at ?? ""}`}
                        itemId={itemId}
                        variantId={variant.variant_id}
                        initialBedLevel={variant.voiceover_bed_level ?? null}
                        rendering={variant.render_status === "rendering"}
                        onCommitted={() => {
                          markVariantRendering(
                            variant.variant_id,
                            variant.render_finished_at ?? null,
                          );
                          refetch();
                        }}
                      />
                    )}
                    </div>
                    ) : (
                      <div
                        data-plan-captions-panel
                        data-testid="captions-unavailable"
                        className="rounded-xl border border-zinc-200 bg-white px-4 py-6 text-center text-[13px] text-[#3f3f46]"
                      >
                        {variant.render_status === "rendering" ? (
                          "Your captions are still processing — check back in a moment."
                        ) : textLaneEligible ? (
                          // Flag-on subtitled clip with no detectable speech renders
                          // ready with null cues (backend: "empty-caption state, NOT a
                          // failure"). Don't dead-end — styled text for this variant
                          // lives in the Timeline lane, so route there.
                          <>
                            No speech detected in this clip — add styled text in the
                            Timeline tab instead.
                            <button
                              type="button"
                              onClick={() => {
                                autoOpenedCaptionsRef.current = true;
                                setActiveTab("timeline");
                              }}
                              className="mt-3 inline-flex min-h-11 items-center justify-center rounded-full bg-[#0c0c0e] px-4 text-[13px] font-semibold text-white transition-opacity hover:opacity-80 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500"
                            >
                              Open the Timeline tab
                            </button>
                          </>
                        ) : (
                          // Ready render with no captions and no text lane — there is
                          // simply nothing to caption (no "yet": none are coming).
                          "No captions for this edit — there's no speech to caption."
                        )}
                      </div>
                    )
                  ) : (
                    <FocusedVariantControls
                      itemId={itemId}
                      variant={variant}
                      tracks={tracks}
                      styleSets={styleSets}
                      session={editSession}
                      instantEligible={instantEligible}
                      baking={baking}
                      activeTab={activeTab}
                      refetch={refetch}
                      markVariantRendering={markVariantRendering}
                      onSwap={onSwap}
                      onRetext={onRetext}
                      onRemoveText={onRemoveText}
                      onChangeStyle={onChangeStyle}
                      onResize={onResize}
                      onChangeLayout={onChangeLayout}
                      overlayCards={overlayCards}
                      setOverlayCards={setOverlayCards}
                      localPreviewUrls={localPreviewUrls}
                      setLocalPreviewUrls={setLocalPreviewUrls}
                      sfxPlacements={sfxPlacements}
                      setSfxPlacements={setSfxPlacements}
                      glossaryEffects={glossaryEffects}
                      glossaryLoading={glossaryLoading}
                      currentTimeS={currentTimeS}
                      onError={onError}
                      overlaySuggestions={overlaySuggestions}
                      onSuggestionEdit={onSuggestionEdit}
                      resolveAssetMeta={resolveAssetMeta}
                      externalEditCardId={requestedEditCardId}
                      onExternalEditHandled={() => setRequestedEditCardId(null)}
                    />
                  )}
                </div>
              )}
            </div>
          )}

          {/* Download button */}
          {variant && (instantEligible ? variant.base_video_url : variant.output_url) && (
            <>
              <div className="flex flex-col gap-2 sm:flex-row">
                {editorEntryEligible && (
                  editorEntryDisabledReason ? (
                    <InkButton
                      type="button"
                      disabled
                      title={editorEntryDisabledReason}
                      className="w-full sm:flex-1"
                    >
                      Edit
                    </InkButton>
                  ) : (
                    <Link
                      href={`/plan/items/${itemId}/edit?variant=${variant.variant_id}`}
                      className="w-full sm:flex-1"
                    >
                      <InkButton type="button" className="w-full">
                        Edit
                      </InkButton>
                    </Link>
                  )
                )}
                <button
                  type="button"
                  onClick={handleDownload}
                  disabled={baking}
                  className="inline-flex min-h-11 w-full items-center justify-center rounded-full border border-zinc-200 bg-white px-5 py-2 text-sm font-semibold text-[#0c0c0e] transition-colors hover:border-zinc-400 disabled:cursor-not-allowed disabled:opacity-60 sm:flex-1"
                >
                  {baking ? "Preparing your video…" : "Download"}
                </button>
              </div>
              {/* Plan 009 T4: failed card media blocks the overlay-bake path —
                  say so inline instead of silently no-oping the click. */}
              {failedOverlayCount > 0 && (
                <p className="mt-1 text-center text-xs text-[#3f3f46]">
                  {failedOverlayCount === 1
                    ? "1 visual couldn't load — refresh or remove it."
                    : `${failedOverlayCount} visuals couldn't load — refresh or remove them.`}
                </p>
              )}
              {((instantEligible && editSession.isDirty) || needsSfxBake) && !baking && (
                <p className="mt-1 text-center text-xs text-[#a1a1aa]">
                  Unsaved — downloads will include your changes
                </p>
              )}
            </>
          )}

          {/* Feedback */}
          {item.current_job_id && !isGenerating && (
            <div className="border-t border-zinc-200 pt-4">
              <p className="text-xs font-semibold uppercase tracking-wide text-[#a1a1aa]">
                How&apos;s this one?
              </p>
              <FeedbackButtons jobId={item.current_job_id} initialSignal={null} />
            </div>
          )}
        </div>
      </div>
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

  // Probe the actual variant duration so the overlay timeline shows the right length.
  const [variantDurationS, setVariantDurationS] = useState(30);
  useEffect(() => {
    const url = variant.output_url;
    if (!url) return;
    const v = document.createElement("video");
    v.preload = "metadata";
    v.onloadedmetadata = () => {
      if (isFinite(v.duration) && v.duration > 0) setVariantDurationS(v.duration);
      v.src = "";
    };
    v.src = url;
  }, [variant.output_url]);

  // Auto-save card metadata (render=false) 2.5 s after the user stops editing.
  // No FFmpeg is triggered here — rendering only happens on explicit download.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (!overlaysDirtyRef.current) return;
    const cards = overlayCardsRef.current;
    const timer = setTimeout(async () => {
      overlaysDirtyRef.current = false;
      try {
        await setVariantMediaOverlays(itemId, variant.variant_id, cards, { render: false });
        refetch();
      } catch (err) {
        // Cards are safe in local state, but the save failed (e.g. backend
        // media_overlays_enabled off → 404). Surface it so the user knows the
        // overlay positions won't persist / won't be in the render.
        onError(
          err instanceof Error
            ? err.message
            : "Couldn't save your overlays — they won't be in the render.",
        );
      }
    }, 2500);
    return () => clearTimeout(timer);
  }, [overlayCards]); // eslint-disable-line react-hooks/exhaustive-deps

  /** Upload new files, append as new overlay cards with default settings. */
  async function handleOverlayUpload(
    files: { file: File; filename: string; content_type: string; file_size_bytes: number }[],
  ) {
    setOverlayUploading(true);
    try {
      const POSITION_CYCLE: { position: "top" | "center" | "bottom"; x_frac: number; y_frac: number }[] = [
        { position: "center", x_frac: 0.5, y_frac: 0.5 },
        { position: "top", x_frac: 0.5, y_frac: 0.18 },
        { position: "bottom", x_frac: 0.5, y_frac: 0.82 },
      ];

      // Build temporary cards (src_gcs_path placeholder) and blob URLs immediately.
      const tempCards: MediaOverlay[] = files.map((f, i) => {
        const slot = POSITION_CYCLE[(overlayCards.length + i) % POSITION_CYCLE.length];
        return {
          id: crypto.randomUUID(),
          kind: f.content_type.startsWith("video/") ? "video" : "image",
          src_gcs_path: "", // filled in after GCS upload completes
          position: slot.position,
          x_frac: slot.x_frac,
          y_frac: slot.y_frac,
          scale: 0.35,
          start_s: 0,
          end_s: +Math.min(5, variantDurationS).toFixed(2),
          z: overlayCards.length + i,
        };
      });
      const blobUrls: Record<string, string> = {};
      tempCards.forEach((card, i) => {
        blobUrls[card.id] = URL.createObjectURL(files[i].file);
      });

      // Probe video durations from the local File (fast — just reads container header).
      const durationsMap: Record<string, number> = {};
      await Promise.all(
        tempCards
          .filter((card) => card.kind === "video")
          .map(
            (card) =>
              new Promise<void>((resolve) => {
                const v = document.createElement("video");
                v.preload = "metadata";
                const done = () => {
                  if (isFinite(v.duration) && v.duration > 0) {
                    durationsMap[card.id] = v.duration;
                  }
                  v.src = "";
                  resolve();
                };
                v.onloadedmetadata = done;
                v.onerror = done;
                setTimeout(done, 3000);
                v.src = blobUrls[card.id];
              }),
          ),
      );

      // Show cards immediately — trim lane is live, CSS preview is live.
      const immediateCards = tempCards.map((card) =>
        durationsMap[card.id] ? { ...card, clip_duration_s: durationsMap[card.id] } : card,
      );
      setLocalPreviewUrls((prev) => ({ ...prev, ...blobUrls }));
      setOverlayCards((prev) => [...prev, ...immediateCards]);

      // Upload to GCS in the background; update src_gcs_path when done.
      const uploadUrls = await requestOverlayUploadUrls(
        itemId,
        files.map((f) => ({
          filename: f.filename,
          content_type: f.content_type,
          file_size_bytes: f.file_size_bytes,
        })),
      );
      await Promise.all(uploadUrls.map((u, i) => uploadToGcs(u.upload_url, files[i].file)));

      // Patch the cards already in state with their real GCS paths, then mark dirty
      // so the auto-save effect persists them (with real GCS paths) after 2.5 s.
      setOverlayCards((prev) =>
        prev.map((card) => {
          const idx = immediateCards.findIndex((c) => c.id === card.id);
          if (idx === -1) return card;
          return { ...card, src_gcs_path: uploadUrls[idx].gcs_path };
        }),
      );
      overlaysDirtyRef.current = true;
    } catch (err) {
      // Upload-URL request or GCS upload failed (e.g. backend media_overlays_enabled
      // off → overlays-upload-urls 404). Surface it instead of throwing uncaught.
      onError(
        err instanceof Error
          ? err.message
          : "Couldn't upload that overlay. Try again.",
      );
    } finally {
      setOverlayUploading(false);
    }
  }

  /** Clear all overlays (restore pre-overlay clean variant). */
  async function handleClearOverlays() {
    // Clear CSS preview immediately — user explicitly removed all cards.
    setLocalPreviewUrls((prev) => {
      Object.values(prev).forEach((url) => URL.revokeObjectURL(url));
      return {};
    });
    setOverlayCards([]);
    try {
      await setVariantMediaOverlays(itemId, variant.variant_id, [], { render: false });
      refetch();
    } catch (err) {
      onError(
        err instanceof Error ? err.message : "Couldn't clear your overlays. Try again.",
      );
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
    setOverlayCards((prev) => prev.filter((c) => c.id !== id));
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
  // Optimistic render status per variantId so the UI doesn't freeze on apply
  // before the server round-trip returns (Part B: plan-item-edit-no-optimistic-state).
  const [optimisticRenderStatus, setOptimisticRenderStatus] = useState<Record<string, string>>({});
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
        at_s: Math.min(Math.max(0, currentTimeS), Math.max(0, variantDurationS - 0.05)),
        gain: 1.0,
        label: files[i].filename.replace(/\.[^.]+$/, ""),
      }));
      handleSfxChange([...sfxPlacements, ...newPlacements]);
    } catch (err) {
      // Upload-URL request or GCS upload failed (e.g. backend
      // SOUND_EFFECTS_ENABLED off → sfx-upload-urls 404). Surface it.
      onError(
        err instanceof Error ? err.message : "Couldn't upload that sound effect. Try again.",
      );
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
      } catch (err) {
        // The client-side preview still plays the effect locally, but the save
        // failed — surface it (e.g. backend SOUND_EFFECTS_ENABLED off → 404).
        onError(
          err instanceof Error
            ? err.message
            : "Couldn't save your sound effects.",
        );
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
   * Part B (plan-item-edit-no-optimistic-state learning): sets optimistic
   * "rendering" state synchronously so the UI reflects in-flight rendering
   * before the server round-trip completes.
   */
  const handleApplyTextElements = useCallback(
    async (variantId: string, elements: TextElementBar[]) => {
      // Part A: clear preview layer first.
      setLocalPreviewUrls((prev) => {
        Object.values(prev).forEach((url) => URL.revokeObjectURL(url));
        return {};
      });
      // Part B: optimistic rendering state so controls show "rendering" immediately.
      setOptimisticRenderStatus((prev) => ({ ...prev, [variantId]: "rendering" }));
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
        // Clear optimistic state on failure so controls re-enable.
        setOptimisticRenderStatus((prev) => {
          const next = { ...prev };
          delete next[variantId];
          return next;
        });
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
                  resolvedParams={resolveIntroParams(variant, styleSets, session.draft)}
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

  const introParams = resolveIntroParams(variant, styleSets, session.draft);

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
        <TextElementOverlayLayer elements={textElements} />
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
}) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const [videoTime, setVideoTime] = useState(0);

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

  if (!variant) return <SkeletonTile />;
  const failed = variant.render_status === "failed";

  // StableVideo identity: composite of variant_id + render_finished_at so it
  // adopts a new src on BOTH a re-render of the same variant (render_finished_at
  // advances) and a focus switch to a different variant (variant_id changes).
  // The old video keeps playing through a re-render; the overlay dims it gently
  // and the swap happens automatically when render_finished_at advances.
  // In live mode the identity keys on the pre-overlay GCS path instead (the
  // "live:" prefix forces the adopt when the mode flips), so re-signed poll URLs
  // never restart base playback.
  const heroIdentity = liveMode
    ? `live:${variant.variant_id}:${variant.pre_media_overlay_video_path ?? ""}`
    : `${variant.variant_id}:${variant.render_finished_at ?? ""}`;

  return (
    <div className="relative aspect-[9/16] w-full overflow-hidden rounded-xl border border-zinc-200 bg-zinc-100">
      {heroSrc ? (
        <StableVideo
          ref={videoRef}
          src={heroSrc}
          identity={heroIdentity}
          controls
          className="h-full w-full object-contain"
        />
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
      <LiveOverlayCardsLayer
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
      />
      {/* 007 Fix 2: direct-manipulation layer for kept AI overlay suggestions —
          drag to reposition, corner handle to resize; every gesture routes
          through onSuggestionEdit (implicit staging, zero network until Apply).
          Gated inside on NEXT_PUBLIC_OVERLAY_AUTOPLACE_ENABLED + non-empty. */}
      {suggestionEntries && onSuggestionEdit && (
        <HeroOverlayEditor
          entries={suggestionEntries}
          onSuggestionEdit={onSuggestionEdit}
          currentTimeS={videoTime}
          resolveCardUrl={resolveSuggestionCardUrl}
          onRequestEditCard={onRequestEditCard}
        />
      )}
      {/* While a re-render runs, keep old video playing under a gentle overlay.
          pointer-events-none ensures the video controls beneath remain usable. */}
      {rendering && variant.output_url && (
        <div className="pointer-events-none absolute inset-0" role="status" aria-label="Rendering new version">
          <div className="absolute inset-0 bg-white/25" />
          <ShimmerSweep tone="light" />
          <HeroRenderingLabel
            startedAt={variant.render_started_at ?? null}
            action={renderingAction}
          />
        </div>
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

/** Status label shown during a same-variant re-render, with a stall hint after 5 min.
 *  Shows action-specific copy when `action` is provided (e.g. the picked song name). */
function HeroRenderingLabel({
  startedAt,
  action,
}: {
  startedAt: string | null;
  action?: { type: "song" | "text" | "style" | "other"; label: string } | null;
}) {
  const STALL_HINT_MS = 300_000; // 5 min
  const [elapsed, setElapsed] = useState(() =>
    startedAt ? Date.now() - new Date(startedAt).getTime() : 0,
  );
  useEffect(() => {
    const id = setInterval(() => {
      setElapsed(startedAt ? Date.now() - new Date(startedAt).getTime() : 0);
    }, 5000);
    return () => clearInterval(id);
  }, [startedAt]);

  if (elapsed >= STALL_HINT_MS) {
    return (
      <div className="absolute inset-0 flex flex-col items-center justify-end pb-6 gap-1 text-center">
        <span className="rounded-full bg-white/80 px-3 py-1 text-xs text-[#3f3f46]">
          Taking longer than usual…
        </span>
      </div>
    );
  }

  // Song swap: full re-render takes ~1-3 min — show the song name + duration hint.
  if (action?.type === "song") {
    return (
      <div className="absolute inset-0 flex flex-col items-center justify-end pb-6 gap-1.5 text-center">
        <span className="rounded-full bg-white/90 px-3 py-1 text-[11px] font-medium text-lime-700 leading-tight max-w-[85%] truncate">
          Applying &ldquo;{action.label}&rdquo;
        </span>
        <span className="rounded-full bg-white/70 px-2.5 py-0.5 text-[10px] text-[#71717a]">
          ~1–3 min
        </span>
      </div>
    );
  }

  // Text reburn: fast path, a few seconds.
  if (action?.type === "text") {
    return (
      <div className="absolute inset-0 flex flex-col items-center justify-end pb-6 gap-1 text-center">
        <span className="rounded-full bg-white/80 px-3 py-1 text-xs text-lime-700">
          {action.label || "Updating text…"}
        </span>
        <span className="rounded-full bg-white/70 px-2.5 py-0.5 text-[10px] text-[#71717a]">
          a few seconds
        </span>
      </div>
    );
  }

  // Style / size / layout / generic re-render.
  const genericLabel = action?.label ?? "Rendering new version…";
  return (
    <div className="absolute inset-0 flex flex-col items-center justify-end pb-6 gap-1 text-center">
      <span className="rounded-full bg-white/80 px-3 py-1 text-xs text-lime-700">
        {genericLabel}
      </span>
    </div>
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
        <button
          type="button"
          onClick={onTellKria}
          className="text-xs font-medium text-lime-700 underline-offset-2 hover:underline"
        >
          Looks wrong? Tell Kria
        </button>
        <button
          type="button"
          onClick={onDismiss}
          className="text-xs text-[#71717a] underline-offset-2 hover:underline"
        >
          Hide this read
        </button>
      </div>
      <p className="mt-2 text-xs text-[#71717a]">
        You can generate anyway — this is just a read on the brief.
      </p>
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
          <button
            type="button"
            onClick={onOpen}
            className="font-medium text-lime-700 underline-offset-2 hover:underline"
          >
            Ask Kria ↗
          </button>
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
            <button
              type="button"
              onClick={onContest}
              className="text-xs font-medium text-lime-700 underline-offset-2 hover:underline"
            >
              Tell Kria
            </button>
            <button
              type="button"
              onClick={onDismissConformance}
              className="text-xs text-[#71717a] underline-offset-2 hover:underline"
            >
              Hide
            </button>
            <button
              type="button"
              onClick={onOpen}
              className="text-xs text-[#71717a] underline-offset-2 hover:underline"
            >
              Ask Kria ↗
            </button>
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

function PoolUploadCard({
  clips,
  uploading,
  onFiles,
  onKeep,
  onRemove,
  onNoteChange,
  maxClips,
  accept = VIDEO_UPLOAD_ACCEPT,
}: {
  clips: ClipAssignment[];
  uploading: boolean;
  onFiles: (files: FileList | null) => void;
  onKeep: (a: ClipAssignment) => void;
  onRemove: (a: ClipAssignment) => void;
  onNoteChange: (a: ClipAssignment, note: string) => Promise<void>;
  /** Hard cap on clip count (subtitled = 1). Undefined → unlimited (montage pool). */
  maxClips?: number;
  accept?: string;
}) {
  const atCap = maxClips != null && clips.length >= maxClips;
  return (
    <div className="mb-8 rounded-xl border border-zinc-200 bg-white p-4">
      {clips.length > 0 && (
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
                className="min-w-[190px] max-w-[220px] rounded-lg border border-zinc-200 bg-[#fafaf8] p-2"
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
                        className={`min-w-0 truncate text-xs font-medium ${
                          a.machine_matched ? "text-lime-800" : "text-[#0c0c0e]"
                        }`}
                        title={name}
                      >
                        {name}
                      </span>
                      <button
                        type="button"
                        onClick={() => onRemove(a)}
                        className="shrink-0 rounded-full px-1.5 text-sm leading-5 text-[#71717a] hover:bg-zinc-100 hover:text-[#0c0c0e]"
                        aria-label={`Remove ${name}`}
                      >
                        ×
                      </button>
                    </div>
                    {a.machine_matched ? (
                      <div className="mt-1 flex items-center gap-2">
                        <span className="rounded border border-dashed border-lime-300 bg-white px-1.5 py-0.5 text-[10px] text-lime-800">
                          Matched
                        </span>
                        <button
                          type="button"
                          onClick={() => onKeep(a)}
                          className="text-[11px] font-medium text-lime-700 underline-offset-2 hover:underline"
                        >
                          Keep
                        </button>
                      </div>
                    ) : (
                      <span className="mt-1 inline-flex rounded border border-lime-200 bg-lime-50 px-1.5 py-0.5 text-[10px] text-lime-800">
                        Added
                      </span>
                    )}
                  </div>
                </div>
                <details className="mt-2">
                  <summary className="cursor-pointer text-[11px] text-[#71717a] marker:text-zinc-300">
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
        </ul>
      )}
      {atCap ? (
        <p className="text-sm text-[#71717a]">
          {maxClips === 1
            ? "One clip added. Remove it above to swap in a different one."
            : "You've reached the clip limit. Remove one above to add another."}
        </p>
      ) : (
        <label className="block">
          <span className="sr-only">Upload video clips for this idea</span>
          <input
            type="file"
            accept={accept}
            multiple={maxClips !== 1}
            disabled={uploading}
            onChange={(e) => onFiles(e.target.files)}
            className="block w-full text-sm text-[#71717a] file:mr-3 file:rounded-full file:border-0 file:bg-[#0c0c0e] file:px-4 file:py-2 file:text-sm file:font-medium file:text-white hover:file:opacity-80"
          />
        </label>
      )}
      {uploading && <p className="mt-3 text-sm text-lime-700">Uploading…</p>}
    </div>
  );
}
