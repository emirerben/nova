"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { useSession, signOut } from "next-auth/react";
import { useRouter, useSearchParams } from "next/navigation";
import {
  Check, Download, Film, Menu, MoreHorizontal, PanelLeftClose,
  PanelLeftOpen, Pencil, Play, Plus, RefreshCw, Sparkles, Trash2, UserRound, WifiOff,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import {
  CREATOR_MEMORY_ENABLED,
  MemoryApiError,
  undoCreatorMemoryOperation,
} from "@/lib/memory-api";
import { Button } from "@/components/ui/button";
import { Dropzone } from "@/components/ui/dropzone";
import {
  AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent,
  AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import {
  DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuLabel,
  DropdownMenuSeparator, DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Input } from "@/components/ui/input";
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from "@/components/ui/sheet";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { AgentApprovalCard } from "@/components/chat/AgentApprovalCard";
import { AgentComposer } from "@/components/chat/AgentComposer";
import { ChatMessage } from "@/components/chat/ChatMessage";
import { ChatThinking } from "@/components/chat/ChatThinking";
import { ChatArtifactCard } from "@/components/chat/ChatArtifactCard";
import WorkspaceEditorActivity from "./WorkspaceEditorActivity";
import { useWorkspaceEditorChat } from "@/lib/editor-chat/useWorkspaceEditorChat";
import { useEditorConversation } from "@/lib/editor-chat/useEditorConversation";
import type { EditorChatAction } from "@/lib/editor-chat/protocol";
import { openEditorCreationThread, recordEditorConversation, type EditorConversationBatch, type CreationThreadMessage } from "@/lib/creation-thread-api";
import { BeamLoader } from "@/components/progress";
import { VoiceRecorder } from "@/app/generative/VoiceRecorder";
import {
  applyCreationAction, cancelKriaTurn, creationFormat, creationFormatLabel, creationJobFailed,
  creationJobPartial, creationJobReady, creationJobSettled, creationPlanningFailed, creationThreadMediaCount, createCreationThread,
  CreationThreadError, decideKriaApproval, deleteCreationThread, getCreationCapabilities,
  getKriaApproval, getKriaDelta, getKriaDraft, listCreationThreads, refreshCreationThread,
  creationClipLimit, sendCreationMessage, threadMessages, uploadCreationMedia,
  creationSpeechCleanupActionId, creationThreadInProgress, creationThreadNeedsPolling,
  creationThreadPreparing, creationThreadProgressKey,
  creationGenerationArtifactKey,
  isCreationSpeechCleanupStaleConflict,
  isCreationThreadRevisionConflict,
  creationVariantPlayable, latestCreationDirection,
  renameCreationThread, sendKriaTurn, undoKriaDraft,
  type CreationFormat, type CreationSpeechCleanupChoice, type CreationThread,
  type CreationThreadEvent,
} from "@/lib/creation-thread-api";
import { cn } from "@/lib/cn";
import CreatorDirectionReceipt from "@/app/plan/_components/CreatorDirectionReceipt";
import { listMyJobs, type LibraryJob, type LibraryRetentionSummary, type LibraryRetentionWarning } from "@/lib/me-api";
import { getPlanItemFresh, type PlanItem } from "@/lib/plan-api";
import LibraryTile from "@/components/library/LibraryTile";
import KriaWordmark from "@/components/KriaWordmark";
import AssetPool from "@/app/plan/_components/AssetPool";
import { useLibraryPosterRecovery } from "@/hooks/useLibraryPosterRecovery";
import {
  SpeechCleanupDecisionCard,
  SpeechCleanupReceipt,
  speechCleanupAnnouncement,
  type SpeechCleanupPendingAction,
} from "./SpeechCleanupDecisionCard";

const FORMATS: Array<{ value: CreationFormat; label: string; description: string }> = [
  { value: "montage", label: "Montage", description: "Music-led cuts from your strongest moments." },
  { value: "narrated_planned", label: "Narrated", description: "Let your voice guide the story." },
  { value: "subtitled", label: "Talking to camera", description: "A clean, captioned edit from your delivery." },
];
const FORMAT_GUIDANCE: Record<CreationFormat, { title: string; description: string }> = {
  montage: {
    title: "Add clips",
    description: "Three or more clips work best.",
  },
  narrated_planned: {
    title: "Add clips and voiceover",
    description: "Choose the footage for your story.",
  },
  subtitled: {
    title: "Add your clip",
    description: "Use one clip of you talking.",
  },
};

function formatFromThread(thread: CreationThread | null): CreationFormat | null {
  return creationFormat(thread?.state.edit_format);
}

function projectTitle(thread: CreationThread): string {
  for (const candidate of [
    thread.title,
  ]) {
    if (typeof candidate === "string" && candidate.trim()) return candidate.trim();
  }
  return "Untitled video";
}

function projectSidebarTitle(thread: CreationThread): string {
  const title = projectTitle(thread);
  if (title !== "Untitled video") return title;
  const intent = thread.state.intent;
  if (typeof intent === "string" && intent.trim()) return intent.trim();
  const creatorAgent = thread.state.creator_agent;
  if (creatorAgent && typeof creatorAgent === "object" && "summary" in creatorAgent) {
    const summary = creatorAgent.summary;
    if (typeof summary === "string" && summary.trim()) return summary.trim();
  }
  return title;
}

function projectStatusLabel(thread: CreationThread): string {
  if (thread.status === "archived") return "Archived";
  if (thread.speech_cleanup?.outcome?.status === "failed") return "Needs attention";
  if (creationJobFailed(thread) || creationPlanningFailed(thread)) return "Needs attention";
  if (thread.job?.status === "variants_ready_partial") return "Partially ready";
  if (thread.job && ["done", "variants_ready"].includes(thread.job.status)) return "Ready";
  if (creationJobReady(thread)) return creationJobPartial(thread) ? "Partially ready" : "Ready";
  if (creationThreadPreparing(thread)) return "Preparing";
  if (creationThreadInProgress(thread)) return "Rendering";
  if (creationThreadMediaCount(thread) > 0) return "Shaping direction";
  return "New project";
}

function projectDeletionBlocked(thread: CreationThread): boolean {
  const stateStatus = [thread.state.job_status, thread.state.render_status]
    .map((value) => typeof value === "string" ? value.toLowerCase() : "")
    .find(Boolean);
  const agentStatus = thread.creator_agent?.status?.toLowerCase();
  return Boolean(
    (thread.job && !creationJobSettled(thread))
    || (agentStatus && [
      "briefing", "planning", "awaiting_confirmation", "executing", "rendering",
      "reviewing", "awaiting_feedback", "revising",
    ].includes(agentStatus))
    || (stateStatus && ["queued", "processing", "generating", "rendering"].includes(stateStatus)),
  );
}

interface AttachedMedia {
  media_id: string;
  filename: string;
  kind: "video" | "image" | "audio";
}

function attachedMedia(thread: CreationThread | null): AttachedMedia[] {
  if (!Array.isArray(thread?.state.media)) return [];
  return thread.state.media.flatMap((entry) => {
    if (!entry || typeof entry !== "object") return [];
    const item = entry as Record<string, unknown>;
    if (typeof item.media_id !== "string" || !["video", "image", "audio"].includes(String(item.kind))) return [];
    return [{
      media_id: item.media_id,
      filename: typeof item.filename === "string" && item.filename ? item.filename : `${item.kind} file`,
      kind: item.kind as AttachedMedia["kind"],
    }];
  });
}

function readyVariants(thread: CreationThread | null) {
  return thread?.job?.variants.filter(creationVariantPlayable) ?? [];
}

function readyVariant(thread: CreationThread | null) {
  const variants = readyVariants(thread);
  const selectedId = typeof thread?.state.selected_variant_id === "string" ? thread.state.selected_variant_id : null;
  return variants.find((variant) => variant.variant_id === selectedId) ?? variants[0] ?? null;
}

/** Existing cuts and manual drafts both open in the shared editor workspace. */
export function workspaceEditorVariant(thread: CreationThread | null) {
  const variants = thread?.job?.variants ?? [];
  // A selected variant remains the target while rendering or recovering. Never
  // silently send its next request to a different ready variant or creation.
  const selected = variants.find((row) => row.variant_id === thread?.state.selected_variant_id);
  if (selected) return selected;
  return variants.find((row) => row.output_url || row.base_video_url || row.render_status === "ready"
    || (row.render_status === "draft" && row.manual_draft === true)) ?? null;
}

function failedVariant(thread: CreationThread | null) {
  return thread?.job?.variants.find((variant) =>
    ["failed", "error", "render_failed"].includes(String(variant.render_status)) && variant.variant_id,
  ) ?? null;
}

function variantLabel(variantId: string | undefined): string {
  return (variantId ?? "Cut").replaceAll("_", " ").replace(/\b\w/g, (character) => character.toUpperCase());
}

interface AutomaticMemoryReceipt {
  operationId: string;
  memoryRevision: number;
  undoExpiresAt: string;
  undone: boolean;
}

function automaticMemoryReceipt(event: CreationThreadEvent | undefined): AutomaticMemoryReceipt | null {
  if (!event || !["memory_updated", "creator_memory_receipt"].includes(event.event_type) || !event.payload) return null;
  const operationId = event.payload.operation_id;
  const memoryRevision = Number(event.payload.memory_revision);
  const undoExpiresAt = event.payload.undo_expires_at;
  if (
    typeof operationId !== "string"
    || !operationId
    || !Number.isInteger(memoryRevision)
    || memoryRevision < 0
    || typeof undoExpiresAt !== "string"
    || !undoExpiresAt
  ) return null;
  return { operationId, memoryRevision, undoExpiresAt, undone: event.payload.undone === true };
}

function AutomaticMemoryReceiptCard({
  receipt,
  busy,
  undone,
  error,
  onUndo,
}: {
  receipt: AutomaticMemoryReceipt;
  busy: boolean;
  undone: boolean;
  error?: string;
  onUndo: () => void;
}) {
  const expiresAt = Date.parse(receipt.undoExpiresAt);
  const [expired, setExpired] = useState(() => !Number.isFinite(expiresAt) || expiresAt <= Date.now());

  useEffect(() => {
    if (expired || !Number.isFinite(expiresAt)) return undefined;
    const timer = window.setTimeout(() => setExpired(true), Math.max(0, expiresAt - Date.now()));
    return () => window.clearTimeout(timer);
  }, [expired, expiresAt]);

  return (
    <ChatArtifactCard
      badge={<Badge variant="secondary">Personalization</Badge>}
      title={undone ? "Automatic update undone" : "Preference updated"}
      description={undone ? "That automatic update is no longer applied." : "You can undo this automatic update for 10 minutes."}
    >
      {!undone && !expired ? (
        <Button type="button" variant="outline" className="min-h-11 w-full" disabled={busy} onClick={onUndo}>
          {busy ? "Undoing…" : "Undo (10 minutes)"}
        </Button>
      ) : null}
      {!undone && expired ? <p className="text-sm text-muted-foreground">The 10-minute Undo window has expired.</p> : null}
      {error ? <p className="mt-2 text-sm text-destructive" role="alert">{error}</p> : null}
    </ChatArtifactCard>
  );
}

const PRODUCTION_LIBRARY_THREAD_PREFIX = "production-library:";

function isProductionLibraryThread(thread: CreationThread | null): boolean {
  return Boolean(thread?.id.startsWith(PRODUCTION_LIBRARY_THREAD_PREFIX));
}

function inferredProductionTitle(thread: CreationThread): string {
  if (typeof thread.title === "string" && thread.title.trim()) return thread.title.trim();
  if (typeof thread.state.intent === "string" && thread.state.intent.trim()) {
    return thread.state.intent.trim().slice(0, 120);
  }
  const firstDirection = [...thread.events]
    .sort((left, right) => left.sequence - right.sequence)
    .find((event) => event.role === "user" && event.content?.trim())?.content?.trim();
  return firstDirection?.slice(0, 120) || "Untitled video";
}

function productionLibraryTitle(job: LibraryJob, planItem?: PlanItem): string {
  if (planItem?.idea?.trim()) return planItem.idea.trim().slice(0, 120);
  const date = new Date(job.created_at);
  const dateLabel = Number.isNaN(date.getTime())
    ? "Production video"
    : new Intl.DateTimeFormat("en", { day: "numeric", month: "short", year: "numeric" }).format(date);
  const mode = job.mode.replaceAll("_", " ").replace(/\b\w/g, (character) => character.toUpperCase());
  return `${mode || "Kria"} · ${dateLabel}`;
}

function productionLibraryThread(job: LibraryJob, planItem?: PlanItem): CreationThread {
  const isReady = job.status === "ready" && Boolean(job.output_url);
  const isFailed = job.status === "failed";
  const eventType = isReady ? "generation_ready" : isFailed ? "generation_failed" : "generation_started";
  const content = isReady
    ? "This finished cut is from your production video library."
    : isFailed
      ? "This production render needs attention."
      : "This production render is still being prepared.";
  const editFormat = creationFormat(planItem?.edit_format) ?? "montage";
  return {
    id: `${PRODUCTION_LIBRARY_THREAD_PREFIX}${job.id}`,
    title: productionLibraryTitle(job, planItem),
    status: isFailed ? "failed" : "active",
    revision: 0,
    state: {
      edit_format: editFormat,
      intent: planItem?.idea ?? undefined,
      media: [],
      media_count: 0,
      production_mode: job.mode,
      selected_variant_id: job.output_variant_id ?? "production_cut",
    },
    content_plan_id: null,
    active_plan_item_id: job.content_plan_item_id,
    active_creator_agent_session_id: null,
    active_job_id: job.status === "generating" ? job.id : null,
    events: [{
      id: `${job.id}:${eventType}`,
      sequence: 0,
      revision: 0,
      role: "assistant",
      event_type: eventType,
      content,
      payload: { source: "production_library", status: job.status },
      created_at: job.created_at,
    }],
    job: {
      id: job.id,
      status: isReady ? "done" : isFailed ? "failed" : job.raw_status,
      current_phase: job.status === "generating" ? "rendering" : null,
      failure_reason: job.failure_reason ?? null,
      variants: isReady
        ? [{
            variant_id: job.output_variant_id ?? "production_cut",
            render_status: "ready",
            output_url: job.output_url,
            poster_url: job.poster_url ?? null,
          }]
        : isFailed
          ? [{ variant_id: job.output_variant_id ?? "production_cut", render_status: "failed" }]
          : [{ variant_id: job.output_variant_id ?? "production_cut", render_status: "rendering" }],
    },
    created_at: job.created_at,
    updated_at: job.created_at,
  };
}

function ProductionPreviewVideoCard({ job, title }: { job: LibraryJob; title: string }) {
  const playable = job.status === "ready" && Boolean(job.output_url);
  return (
    <article className="w-full" data-testid={`production-video-${job.id}`}>
      <div className="relative aspect-[9/16] overflow-hidden rounded-2xl border border-zinc-200 bg-zinc-950">
        {/* Signed production posters use dynamic hosts that cannot be allowlisted for next/image. */}
        {/* eslint-disable-next-line @next/next/no-img-element */}
        {job.poster_url ? <img src={job.poster_url} alt="" className="size-full object-cover" /> : null}
        {!job.poster_url ? <div className="flex size-full items-center justify-center px-4 text-center text-xs text-zinc-400">{job.status === "generating" ? "Rendering…" : job.status === "failed" ? "Render failed" : "Video ready"}</div> : null}
        {playable ? <Button type="button" size="icon" className="absolute inset-0 m-auto size-12 rounded-full" aria-label={`Play ${title}`} onClick={() => window.open(job.output_url ?? "", "_blank", "noopener,noreferrer")}><Play /></Button> : null}
        <span className="absolute bottom-3 left-3 rounded-full bg-white/90 px-2 py-1 text-[11px] font-semibold leading-[15px] text-[#30352C]">{job.status === "generating" ? "Rendering…" : job.status === "failed" ? "Needs attention" : "Ready"}</span>
      </div>
      <p className="mt-2 truncate text-sm font-semibold leading-5 text-[#30352C]">{title}</p>
    </article>
  );
}

export function renderPhaseLabel(phase?: string | null): string {
  const normalized = phase?.trim().toLowerCase();
  const labels: Record<string, string> = {
    queued: "Your edit is queued…",
    analyze_clips: "Finding the story in your footage…",
    match_song: "Choosing the right sound…",
    render_variants: "Rendering the edit variations…",
    finalize: "Polishing the final cut…",
    analyzing: "Finding the story in your footage…",
    matching: "Choosing the strongest moments…",
    assembling: "Assembling your footage and sound…",
    rendering: "Rendering the edit…",
    finalizing: "Polishing the final cut…",
  };
  return (normalized && labels[normalized]) || "Building your cut…";
}

function RenderStatusCard({ thread }: { thread: CreationThread }) {
  const phase = renderPhaseLabel(thread.job?.current_phase);
  const playable = readyVariants(thread);
  const total = thread.job?.variants.length ?? 0;
  const count = playable.length > 0 && total > 0 ? `${playable.length} of ${total} ready` : null;
  return (
    <ChatArtifactCard badge={<Badge variant="secondary">Rendering</Badge>} title={count ?? "Kria is building your cut…"} description={phase}>
      <BeamLoader tone="light" mode="line" strength="medium" ariaLabel={`${phase}${count ? ` ${count}.` : ""}`} className="rounded-lg">
        <div className="flex items-center gap-2 px-3 py-2 text-sm text-muted-foreground">
          <span className="size-1.5 motion-safe:animate-ping rounded-full bg-lime-600" aria-hidden="true" />
          <span>{phase}</span>
        </div>
      </BeamLoader>
      {playable.length > 0 ? (
        <div className="mt-3 flex flex-wrap gap-2" aria-label="Playable cuts">
          {playable.map((variant) => (
            <Button
              key={variant.variant_id}
              type="button"
              variant="outline"
              onClick={() => window.open(variant.output_url ?? "", "_blank", "noopener,noreferrer")}
            >
              <Play /> Play {variantLabel(variant.variant_id)}
            </Button>
          ))}
        </div>
      ) : null}
    </ChatArtifactCard>
  );
}

function FailureStatusCard({
  thread,
  busy,
  readOnly = false,
  planningFailure = false,
  onRetry,
  onAdjust,
}: {
  thread: CreationThread;
  busy: boolean;
  readOnly?: boolean;
  planningFailure?: boolean;
  onRetry?: () => void;
  onAdjust: () => void;
}) {
  return (
    <ChatArtifactCard badge={<Badge variant="outline">{planningFailure ? "Direction needs attention" : "Render needs attention"}</Badge>} title={planningFailure ? "Let’s refine the direction" : "Your project is safe"} description={planningFailure ? "I couldn’t start this edit yet, but your direction, footage, and voiceover are still here." : "The render did not finish, but your direction and footage are still here."}>
      <div className="flex flex-wrap gap-2">{onRetry ? <Button type="button" onClick={onRetry} disabled={busy || readOnly}><RefreshCw /> Retry render</Button> : null}<Button type="button" variant="outline" onClick={onAdjust} disabled={readOnly}>Edit direction and try again</Button></div>
    </ChatArtifactCard>
  );
}

function ReadyStatusCard({
  thread,
  isPartial,
  selectedReadyVariant,
  selectedFailedVariant,
  busy,
  readOnly = false,
  onSelectVariant,
  onOpenEditor,
  onRetryVariant,
}: {
  thread: CreationThread;
  isPartial: boolean;
  selectedReadyVariant: ReturnType<typeof readyVariant>;
  selectedFailedVariant: ReturnType<typeof failedVariant>;
  busy: boolean;
  readOnly?: boolean;
  onSelectVariant: (id: string) => void;
  onOpenEditor: () => void;
  onRetryVariant: (id: string) => void;
}) {
  return (
    <ChatArtifactCard badge={<Badge variant="secondary"><Check /> {isPartial ? "Partially ready" : "Ready"}</Badge>} title={isPartial ? "Your cut is ready; one variant needs another pass" : "Your cut is ready"} description={isPartial ? "The playable cut is available now. Retry the failed variant whenever you’re ready." : "Preview or download your cut. Keep chatting to edit the draft, then Save when you’re ready."}>
      <SpeechCleanupReceipt outcome={thread.speech_cleanup?.outcome} />
      {readyVariants(thread).length > 1 ? <div className="mb-3 mt-3 flex flex-wrap gap-2" role="group" aria-label="Available cuts">{readyVariants(thread).map((variant) => <Button key={variant.variant_id} type="button" variant={selectedReadyVariant?.variant_id === variant.variant_id ? "secondary" : "outline"} aria-pressed={selectedReadyVariant?.variant_id === variant.variant_id} disabled={busy || readOnly} onClick={() => onSelectVariant(variant.variant_id ?? "")}>{variantLabel(variant.variant_id)}</Button>)}</div> : null}
      <div className="mt-3 flex flex-wrap gap-2">{selectedReadyVariant?.output_url ? <><Button type="button" onClick={() => window.open(selectedReadyVariant.output_url ?? "", "_blank", "noopener,noreferrer")}><Play /> Play</Button><Button type="button" variant="outline" asChild><a href={selectedReadyVariant.output_url ?? ""} download><Download /> Download</a></Button></> : null}<Button type="button" variant="outline" onClick={onOpenEditor} disabled={!thread.active_plan_item_id && !readOnly}><Pencil /> {readOnly ? "View video" : "Open editor"}</Button>{isPartial && selectedFailedVariant?.variant_id ? <Button type="button" variant="ghost" onClick={() => onRetryVariant(selectedFailedVariant.variant_id ?? "")} disabled={busy || readOnly}><RefreshCw /> Retry failed variant</Button> : null}</div>
    </ChatArtifactCard>
  );
}

export interface ChatCreationWorkspaceProps {
  /** When present, hydrate this exact project instead of choosing the latest. */
  initialThreadId?: string;
  /** Preview-only mode: hydrate the signed-in account's production data, while
   * keeping every server mutation disabled. Rename/delete remain local demos. */
  productionPreview?: boolean;
}

export default function ChatCreationWorkspace({
  initialThreadId,
  productionPreview = false,
}: ChatCreationWorkspaceProps) {
  const { data: session } = useSession();
  const router = useRouter();
  const searchParams = useSearchParams();
  const requestedEditorItem = searchParams.get("editor_item");
  const requestedEditorVariant = searchParams.get("variant");
  const [thread, setThread] = useState<CreationThread | null>(null);
  const [projects, setProjects] = useState<CreationThread[]>([]);
  const [input, setInput] = useState("");
  const [pendingFiles, setPendingFiles] = useState<File[]>([]);
  const [busy, setBusy] = useState(false);
  const [thinking, setThinking] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [offline, setOffline] = useState(false);
  const [pollReconnecting, setPollReconnecting] = useState(false);
  const [projectsOpen, setProjectsOpen] = useState(false);
  const [sidebarHidden, setSidebarHidden] = useState(false);
  const [mobileTab, setMobileTab] = useState<"chat" | "editor">("chat");
  const [editorOpen, setEditorOpen] = useState(true);
  const [galleryOpen, setGalleryOpen] = useState(() => searchParams.get("view") === "gallery");
  const [threadUnavailable, setThreadUnavailable] = useState(false);
  const [renameTarget, setRenameTarget] = useState<CreationThread | null>(null);
  const [renameValue, setRenameValue] = useState("");
  const renameInputRef = useRef<HTMLInputElement>(null);
  const renameInFlightRef = useRef(false);
  const renameCancelledRef = useRef(false);
  const [deleteTarget, setDeleteTarget] = useState<CreationThread | null>(null);
  const [projectActionBusy, setProjectActionBusy] = useState(false);
  const [projectActionError, setProjectActionError] = useState<string | null>(null);
  const [memoryUndoBusy, setMemoryUndoBusy] = useState<string | null>(null);
  const [undoneMemoryOperations, setUndoneMemoryOperations] = useState<Record<string, boolean>>({});
  const [memoryUndoErrors, setMemoryUndoErrors] = useState<Record<string, string>>({});
  const [galleryJobs, setGalleryJobs] = useState<LibraryJob[]>([]);
  const [galleryRetentionWarnings, setGalleryRetentionWarnings] = useState<LibraryRetentionWarning[]>([]);
  const [galleryRetentionSummary, setGalleryRetentionSummary] = useState<LibraryRetentionSummary | null>(null);
  const [galleryCursor, setGalleryCursor] = useState<string | null>(null);
  const [galleryLoading, setGalleryLoading] = useState(false);
  const [galleryLoadError, setGalleryLoadError] = useState<string | null>(null);
  const [galleryRetryCursor, setGalleryRetryCursor] = useState<string | null>(null);
  const [formatPickerOpen, setFormatPickerOpen] = useState(false);
  const [hasNewUpdate, setHasNewUpdate] = useState(false);
  const [announcement, setAnnouncement] = useState("");
  const [initialLoading, setInitialLoading] = useState(true);
  const [speechCleanupPendingAction, setSpeechCleanupPendingAction] = useState<SpeechCleanupPendingAction>(null);
  const [availableFormats, setAvailableFormats] = useState<CreationFormat[]>(["montage", "narrated_planned", "subtitled"]);
  const [capabilities, setCapabilities] = useState<Awaited<ReturnType<typeof getCreationCapabilities>>>(() => ({ formats: [] }));
  const posterRecovery = useLibraryPosterRecovery({
    enabled: galleryOpen && !productionPreview,
    jobs: galleryJobs,
    setJobs: setGalleryJobs,
  });
  const visualsEnabled = process.env.NEXT_PUBLIC_OVERLAY_AUTOPLACE_ENABLED === "true"
    || process.env.NEXT_PUBLIC_GUIDED_EDIT_ENABLED === "true";
  const editorFrameRef = useRef<HTMLIFrameElement>(null);
  const editorVariant = workspaceEditorVariant(thread);
  const { state: editorChatState, command: sendEditorCommand, waitForReady: waitForEditorReady } = useWorkspaceEditorChat({
    frameRef: editorFrameRef, threadId: thread?.id ?? null,
    itemId: thread?.active_plan_item_id ?? null, variantId: editorVariant?.variant_id ?? null,
    enabled: !productionPreview && thread?.status === "active" && Boolean(editorVariant) && editorOpen && !galleryOpen,
  });
  const desktopSidebarRef = useRef<HTMLDivElement>(null);
  const transcriptRef = useRef<HTMLDivElement>(null);
  const latestMessageRef = useRef<HTMLDivElement>(null);
  const composerRef = useRef<HTMLTextAreaElement>(null);
  const atLiveEdgeRef = useRef(true);
  const lastRenderedMessageRef = useRef<string | null>(null);
  const lastRenderedThreadRef = useRef<string | null>(null);
  const queuedMessageRef = useRef<{ threadId: string; message: string } | null>(null);
  const preparedRevisionRef = useRef<string | null>(null);
  const activeThreadIdRef = useRef<string | null>(null);
  const expectedEditorRenderRef = useRef<{
    threadId: string;
    variantId: string;
    generation: string;
  } | null>(null);
  const threadRequestSequenceRef = useRef(0);
  const latestAcceptedThreadSequenceRef = useRef(0);
  const loadStartedRef = useRef(false);
  const loadInFlightRef = useRef<Promise<void> | null>(null);
  const loadSequenceRef = useRef(0);
  const productionGalleryLoadedRef = useRef(false);
  const detailRequestRef = useRef<{ threadId: string; controller: AbortController } | null>(null);
  const speechCleanupActionInFlightRef = useRef(false);

  useEffect(() => {
    const sidebarNode = desktopSidebarRef.current;
    if (!sidebarNode) return;
    if (sidebarHidden) sidebarNode.setAttribute("inert", "");
    else sidebarNode.removeAttribute("inert");
  }, [sidebarHidden]);

  const activateThread = useCallback((next: CreationThread) => {
    if (expectedEditorRenderRef.current?.threadId !== next.id) {
      expectedEditorRenderRef.current = null;
    }
    activeThreadIdRef.current = next.id;
    setThreadUnavailable(false);
    setSpeechCleanupPendingAction(null);
    setThread(next);
  }, []);

  const acceptThreadResponse = useCallback((
    expectedId: string,
    next: CreationThread,
    requestSequence?: number,
  ) => {
    if (activeThreadIdRef.current !== expectedId) return false;
    if (
      requestSequence !== undefined
      && requestSequence < latestAcceptedThreadSequenceRef.current
    ) return false;
    const expectedRender = expectedEditorRenderRef.current;
    if (expectedRender?.threadId === expectedId) {
      const target = next.job?.variants.find(
        (variant) => variant.variant_id === expectedRender.variantId,
      );
      // The editor Save response is authoritative for this attempt. Ignore a
      // cached or out-of-order pre-Save projection until the exact generation
      // is observable; otherwise an old ready response stops polling.
      if (target?.render_generation_id !== expectedRender.generation) return false;
      if (!["queued", "rendering"].includes(String(target.render_status))) {
        expectedEditorRenderRef.current = null;
      }
    }
    if (requestSequence !== undefined) {
      latestAcceptedThreadSequenceRef.current = requestSequence;
    }
    setThread(next);
    return true;
  }, []);

  const refreshThreadProjection = useCallback(async (threadId: string) => {
    detailRequestRef.current?.controller.abort();
    const controller = new AbortController();
    detailRequestRef.current = { threadId, controller };
    const requestSequence = ++threadRequestSequenceRef.current;
    try {
      const next = await refreshCreationThread(threadId, controller.signal);
      return { next, requestSequence };
    } finally {
      if (detailRequestRef.current?.controller === controller) detailRequestRef.current = null;
    }
  }, []);

  const waitForKriaTurn = useCallback(async (
    sourceThread: CreationThread,
    turnId: string,
  ) => {
    let cursor = sourceThread.events.reduce(
      (latest, event) => Math.max(latest, event.sequence),
      -1,
    );
    const terminalEvents = new Set([
      "assistant_response",
      "assistant_question",
      "assistant_error",
      "draft_applied",
      "approval_requested",
    ]);
    for (let attempt = 0; attempt < 13; attempt += 1) {
      const delta = await getKriaDelta(sourceThread.id, cursor, 100);
      cursor = delta.next_after_sequence;
      const settled = delta.events.some((event) =>
        terminalEvents.has(event.event_type)
        && event.payload?.turn_id === turnId,
      );
      if (settled) {
        const { next, requestSequence } = await refreshThreadProjection(sourceThread.id);
        acceptThreadResponse(sourceThread.id, next, requestSequence);
        return true;
      }
      if (activeThreadIdRef.current !== sourceThread.id) return false;
      const delayMs = Math.min(2_500, 1_000 + (attempt * 500));
      await new Promise((resolve) => window.setTimeout(resolve, delayMs));
    }
    return false;
  }, [acceptThreadResponse, refreshThreadProjection]);

  // Every async mutation gets a sequence at request start. A poll or refresh
  // that began later must be allowed to win even if an older send/action is
  // delayed by the network. This keeps a late mutation response from
  // resurrecting a stale rendering projection.
  const requestThreadResponse = useCallback(async (
    expectedId: string,
    request: (requestSequence: number) => Promise<CreationThread>,
  ) => {
    const requestSequence = ++threadRequestSequenceRef.current;
    const next = await request(requestSequence);
    acceptThreadResponse(expectedId, next, requestSequence);
    return next;
  }, [acceptThreadResponse]);

  async function undoAutomaticMemory(receipt: AutomaticMemoryReceipt) {
    if (productionPreview || !thread || memoryUndoBusy) return;
    const threadId = thread.id;
    setMemoryUndoBusy(receipt.operationId);
    setMemoryUndoErrors((current) => {
      const next = { ...current };
      delete next[receipt.operationId];
      return next;
    });
    try {
      await undoCreatorMemoryOperation(receipt.operationId, receipt.memoryRevision);
      setUndoneMemoryOperations((current) => ({ ...current, [receipt.operationId]: true }));
      await requestThreadResponse(threadId, () => refreshCreationThread(threadId));
    } catch (cause) {
      const apiError = cause instanceof MemoryApiError ? cause : null;
      const message = apiError?.code === "undo_expired" || apiError?.status === 410
        ? "This Undo window has expired."
        : apiError?.code === "stale_revision"
          ? "This preference changed after the receipt, so Undo is no longer available."
          : "I couldn’t undo this preference. Try refreshing the chat.";
      setMemoryUndoErrors((current) => ({ ...current, [receipt.operationId]: message }));
    } finally {
      setMemoryUndoBusy(null);
    }
  }

  const persistEditorConversation = useCallback((threadId: string, batch: EditorConversationBatch) =>
    requestThreadResponse(threadId, () => recordEditorConversation(threadId, batch)), [requestThreadResponse]);
  const editorConversation = useEditorConversation(thread, editorChatState, persistEditorConversation);
  const runEditorAction = useCallback((action: EditorChatAction) => {
    setError(null);
    if (action.kind === "visuals-reveal" || action.kind === "reveal") setMobileTab("editor");
    void sendEditorCommand(action).catch((cause) => setError(
      cause instanceof Error ? cause.message : "Kria couldn’t complete the editor action.",
    ));
  }, [sendEditorCommand]);
  const editorRenderWasActive = useRef(false);
  useEffect(() => {
    const active = Boolean(editorChatState?.renderActive || editorChatState?.director?.serverRendering);
    if (editorRenderWasActive.current !== active && thread?.id) {
      editorRenderWasActive.current = active;
      const id = thread.id;
      void refreshThreadProjection(id).then(({ next, requestSequence }) => acceptThreadResponse(id, next, requestSequence)).catch(() => setError("The render status could not refresh. Your editor is still open."));
    }
  }, [acceptThreadResponse, editorChatState?.renderActive, editorChatState?.director?.serverRendering, refreshThreadProjection, thread?.id]);

  const load = useCallback(async () => {
    if (loadInFlightRef.current) return loadInFlightRef.current;
    const loadSequence = ++loadSequenceRef.current;
    const isCurrentLoad = () => loadSequence === loadSequenceRef.current;
    setInitialLoading(true);
    let loadPromise: Promise<void>;
    loadPromise = (async () => {
      setError(null);
      try {
        if (productionPreview) {
          const [threadsResult, capabilitiesResult, library] = await Promise.all([
            listCreationThreads().catch((cause) => {
              if (cause instanceof CreationThreadError && cause.status === 404) return [];
              throw cause;
            }),
            getCreationCapabilities().catch((cause) => {
              if (cause instanceof CreationThreadError && cause.status === 404) return { formats: [] };
              throw cause;
            }),
            listMyJobs({ limit: 24 }),
          ]);
          const planItemIds = [...new Set(library.jobs.flatMap((job) => job.content_plan_item_id ? [job.content_plan_item_id] : []))];
          const planItems = await Promise.all(planItemIds.map(async (itemId) => {
            try {
              return [itemId, await getPlanItemFresh(itemId)] as const;
            } catch {
              return [itemId, undefined] as const;
            }
          }));
          const planItemById = new Map(planItems);
          const liveThreads = threadsResult.map((item) => ({ ...item, title: inferredProductionTitle(item) }));
          const linkedJobIds = new Set(liveThreads.flatMap((item) => item.active_job_id ? [item.active_job_id] : []));
          const libraryThreads = library.jobs
            .filter((job) => !linkedJobIds.has(job.id))
            .map((job) => productionLibraryThread(
              job,
              job.content_plan_item_id ? planItemById.get(job.content_plan_item_id) : undefined,
            ));
          const listed = [...liveThreads, ...libraryThreads]
            .sort((left, right) => Date.parse(right.updated_at) - Date.parse(left.updated_at));
          if (!isCurrentLoad()) return;
          productionGalleryLoadedRef.current = true;
          setGalleryJobs(library.jobs);
          setGalleryRetentionWarnings(library.retention_warnings ?? []);
          setGalleryRetentionSummary(library.retention_summary ?? null);
          setAvailableFormats(capabilitiesResult.formats.map((item) => item.edit_format));
          setCapabilities(capabilitiesResult);
          setProjects(listed);
          const requestedId = initialThreadId?.trim() || null;
          const summary = requestedId
            ? listed.find((item) => item.id === requestedId)
            : listed[0];
          if (!summary) {
            activeThreadIdRef.current = null;
            setThread(null);
            setThreadUnavailable(Boolean(requestedId));
            return;
          }
          const hydrated = isProductionLibraryThread(summary)
            ? summary
            : await refreshCreationThread(summary.id);
          const next = { ...hydrated, title: inferredProductionTitle(hydrated) };
          if (!isCurrentLoad()) return;
          activateThread(next);
          return;
        }
        const [listed, capabilities] = await Promise.all([listCreationThreads(), getCreationCapabilities()]);
        if (!isCurrentLoad()) return;
        setAvailableFormats(capabilities.formats.map((item) => item.edit_format));
        setCapabilities(capabilities);
        setProjects(listed);
        const requestedId = initialThreadId?.trim() || null;
        const current = activeThreadIdRef.current
          ? listed.find((item) => item.id === activeThreadIdRef.current)
          : null;
        const summary = requestedId
          ? listed.find((item) => item.id === requestedId)
          : current ?? listed.find((item) => item.status === "active");
        const requestSequence = ++threadRequestSequenceRef.current;
        const next = requestedEditorItem
          ? await openEditorCreationThread(requestedEditorItem, requestedEditorVariant)
          : requestedId ? await refreshCreationThread(requestedId)
          : summary ? await refreshCreationThread(summary.id) : await createCreationThread();
        if (!isCurrentLoad()) return;
        latestAcceptedThreadSequenceRef.current = requestSequence;
        activateThread(next);
        if (!requestedId) {
          const destination = galleryOpen
            ? `/plan/${next.id}?view=gallery`
            : `/plan/${next.id}`;
          router.replace(destination, { scroll: false });
        }
        if (!current && !listed.some((item) => item.id === next.id)) setProjects((items) => [next, ...items]);
      } catch (cause) {
        if (!isCurrentLoad()) return;
        if (initialThreadId && cause instanceof CreationThreadError && cause.status === 404) {
          activeThreadIdRef.current = null;
          setThread(null);
          setThreadUnavailable(true);
          return;
        }
        setError("I couldn’t open this creation chat. Check your connection and try again.");
      }
    })().finally(() => {
      if (isCurrentLoad()) setInitialLoading(false);
      if (loadInFlightRef.current === loadPromise) loadInFlightRef.current = null;
    });
    loadInFlightRef.current = loadPromise;
    return loadPromise;
  }, [activateThread, galleryOpen, initialThreadId, productionPreview, requestedEditorItem, requestedEditorVariant, router]);

  useEffect(() => {
    // React Strict Mode replays effects in local development. Keep the initial
    // empty-project mutation one-shot so it cannot mint duplicate projects.
    if (loadStartedRef.current) return;
    loadStartedRef.current = true;
    void load(); /* load once; refresh is driven by the render poll */ // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    const update = () => setOffline(typeof navigator !== "undefined" && !navigator.onLine);
    update();
    window.addEventListener("online", update);
    window.addEventListener("offline", update);
    return () => { window.removeEventListener("online", update); window.removeEventListener("offline", update); };
  }, []);

  useEffect(() => {
    const onEditorMessage = (event: MessageEvent) => {
      if (event.origin !== window.location.origin || event.source !== editorFrameRef.current?.contentWindow) return;
      if (event.data?.type !== "nova:embedded-editor-leave") return;
      setEditorOpen(false);
      setMobileTab("chat");
      const currentId = activeThreadIdRef.current;
      if (event.data?.refresh === true && currentId) {
        const variantId = typeof event.data?.variant_id === "string"
          ? event.data.variant_id
          : null;
        const generation = typeof event.data?.render_generation_id === "string"
          ? event.data.render_generation_id
          : null;
        if (variantId && generation) {
          expectedEditorRenderRef.current = { threadId: currentId, variantId, generation };
          setThread((current) => {
            if (current?.id !== currentId || !current.job) return current;
            return {
              ...current,
              job: {
                ...current.job,
                variants: current.job.variants.map((variant) =>
                  variant.variant_id === variantId
                    ? {
                      ...variant,
                      render_generation_id: generation,
                      render_status: "rendering",
                    }
                    : variant,
                ),
              },
            };
          });
        }
        void refreshThreadProjection(currentId)
          .then(({ next, requestSequence }) => {
            acceptThreadResponse(currentId, next, requestSequence);
          })
          .catch(() => setError("Your editor save started, but I couldn’t refresh the render yet. Reconnecting…"));
      }
    };
    window.addEventListener("message", onEditorMessage);
    return () => window.removeEventListener("message", onEditorMessage);
  }, [acceptThreadResponse, refreshThreadProjection]);

  const threadProgressKey = thread ? creationThreadProgressKey(thread) : null;
  const progressThreadId = thread?.id ?? null;
  const threadNeedsPolling = Boolean(thread && creationThreadNeedsPolling(thread));

  useEffect(() => {
    if (!progressThreadId || !threadNeedsPolling) return;
    let cancelled = false;
    let timer: number | undefined;
    const poll = async () => {
      try {
        const { next, requestSequence } = await refreshThreadProjection(progressThreadId);
        if (cancelled || activeThreadIdRef.current !== progressThreadId) return;
        setPollReconnecting(false);
        const accepted = acceptThreadResponse(progressThreadId, next, requestSequence);
        const awaitingEditorGeneration = expectedEditorRenderRef.current?.threadId
          === progressThreadId;
        if ((accepted && creationThreadNeedsPolling(next)) || awaitingEditorGeneration) {
          timer = window.setTimeout(() => void poll(), 2500);
        }
      } catch (cause) {
        if (cause instanceof Error && cause.name === "AbortError") return;
        if (!cancelled && activeThreadIdRef.current === progressThreadId) {
          setPollReconnecting(true);
          timer = window.setTimeout(() => void poll(), 5000);
        }
      }
    };
    void poll();
    return () => {
      cancelled = true;
      if (detailRequestRef.current?.threadId === progressThreadId) {
        detailRequestRef.current.controller.abort();
        detailRequestRef.current = null;
      }
      if (timer) window.clearTimeout(timer);
    };
  }, [acceptThreadResponse, progressThreadId, refreshThreadProjection, threadNeedsPolling, threadProgressKey]);

  useEffect(() => {
    if (!galleryOpen) return;
    if (productionPreview && productionGalleryLoadedRef.current) return;
    let cancelled = false;
    setGalleryLoading(true);
    setGalleryLoadError(null);
    void listMyJobs({ limit: productionPreview ? 24 : undefined })
      .then((page) => {
        if (cancelled) return;
        if (productionPreview) productionGalleryLoadedRef.current = true;
        setGalleryJobs(page.jobs);
        setGalleryRetentionWarnings(page.retention_warnings ?? []);
        setGalleryRetentionSummary(page.retention_summary ?? null);
        setGalleryCursor(productionPreview ? null : page.next_cursor);
        setGalleryRetryCursor(null);
      })
      .catch(() => {
        if (cancelled) return;
        setGalleryRetryCursor(null);
        setGalleryLoadError("I couldn’t load your Gallery. Your saved videos are still safe.");
      })
      .finally(() => {
        if (!cancelled) setGalleryLoading(false);
      });
    return () => { cancelled = true; };
  }, [galleryOpen, productionPreview]);

  async function loadMoreGallery() {
    if (productionPreview || !galleryCursor || galleryLoading) return;
    const cursor = galleryCursor;
    setGalleryLoading(true);
    setGalleryLoadError(null);
    try {
      const page = await listMyJobs({ cursor });
      setGalleryJobs((current) => {
        const jobsById = new Map(current.map((job) => [job.id, job]));
        page.jobs.forEach((job) => jobsById.set(job.id, job));
        return [...jobsById.values()];
      });
      setGalleryCursor(page.next_cursor);
      setGalleryRetentionWarnings((current) => {
        const byJob = new Map(current.map((warning) => [warning.job_id, warning]));
        (page.retention_warnings ?? []).forEach((warning) => byJob.set(warning.job_id, warning));
        return [...byJob.values()].sort((left, right) => Date.parse(left.delete_at) - Date.parse(right.delete_at));
      });
      if (page.retention_summary !== undefined) {
        setGalleryRetentionSummary(page.retention_summary ?? null);
      }
      setGalleryRetryCursor(null);
    } catch {
      setGalleryRetryCursor(cursor);
      setGalleryLoadError("I couldn’t load more videos. Your saved videos are still safe.");
    } finally {
      setGalleryLoading(false);
    }
  }

  async function retryGalleryLoad() {
    if (galleryLoading) return;
    const cursor = galleryRetryCursor;
    setGalleryLoading(true);
    setGalleryLoadError(null);
    try {
      const page = await listMyJobs(cursor
        ? { cursor }
        : { limit: productionPreview ? 24 : undefined });
      if (cursor) {
        setGalleryJobs((current) => {
          const jobsById = new Map(current.map((job) => [job.id, job]));
          page.jobs.forEach((job) => jobsById.set(job.id, job));
          return [...jobsById.values()];
        });
      } else {
        setGalleryJobs(page.jobs);
      }
      if (cursor) {
        setGalleryRetentionWarnings((current) => {
          const byJob = new Map(current.map((warning) => [warning.job_id, warning]));
          (page.retention_warnings ?? []).forEach((warning) => byJob.set(warning.job_id, warning));
          return [...byJob.values()].sort((left, right) => Date.parse(left.delete_at) - Date.parse(right.delete_at));
        });
      } else {
        setGalleryRetentionWarnings(page.retention_warnings ?? []);
      }
      if (page.retention_summary !== undefined) {
        setGalleryRetentionSummary(page.retention_summary ?? null);
      }
      setGalleryCursor(productionPreview ? null : page.next_cursor);
      setGalleryRetryCursor(null);
      if (productionPreview) productionGalleryLoadedRef.current = true;
    } catch {
      setGalleryLoadError(cursor
        ? "I couldn’t load more videos. Your saved videos are still safe."
        : "I couldn’t load your Gallery. Your saved videos are still safe.");
    } finally {
      setGalleryLoading(false);
    }
  }

  async function handleGalleryJobDeleted(jobId: string) {
    setGalleryJobs((current) => current.filter((item) => item.id !== jobId));
    setGalleryRetentionWarnings((current) => current.filter((item) => item.job_id !== jobId));
    try {
      const page = await listMyJobs();
      setGalleryRetentionWarnings(page.retention_warnings ?? []);
      setGalleryRetentionSummary(page.retention_summary ?? null);
    } catch {
      // The deleted tile remains gone. Clear the aggregate rather than showing
      // a known-stale warning; reopening Gallery retries the authoritative read.
      setGalleryRetentionSummary(null);
    }
  }

  useEffect(() => {
    if (!thread) return;
    setProjects((items) => {
      const exists = items.some((item) => item.id === thread.id);
      return exists
        ? items.map((item) => item.id === thread.id ? thread : item)
        : [thread, ...items];
    });
  }, [thread]);

  useEffect(() => {
    if (productionPreview) return;
    const intent = thread?.state.pending_revision_intent;
    const jobId = thread?.job?.id;
    if (!thread || !jobId || !creationJobReady(thread) || typeof intent !== "string" || !intent.trim()) return;
    const key = `${thread.id}:${jobId}`;
    if (preparedRevisionRef.current === key) return;
    preparedRevisionRef.current = key;
    void requestThreadResponse(thread.id, () =>
      applyCreationAction(thread, "revise", { intent }, `revision-${thread.id}-${jobId}`))
      .catch(() => {
        preparedRevisionRef.current = null;
        setError("Your revision is ready to review, but I couldn’t prepare it yet. Try again.");
      });
  }, [productionPreview, requestThreadResponse, thread]);

  const format = formatFromThread(thread);
  const clipLimit = creationClipLimit(capabilities.formats, format);
  const mediaCount = creationThreadMediaCount(thread);
  const speechCleanup = thread?.speech_cleanup ?? null;
  const speechCleanupOutcomeFailed = speechCleanup?.outcome?.status === "failed";
  const hasReady = Boolean(thread && creationJobReady(thread) && !speechCleanupOutcomeFailed);
  const hasEditor = Boolean(editorVariant) && !speechCleanupOutcomeFailed;
  const isPartial = Boolean(thread && creationJobPartial(thread));
  const eventMessages = useMemo<CreationThreadMessage[]>(() => [
    ...(thread ? threadMessages(thread) : []),
    ...editorConversation.unsaved.map((message) => ({
      id: `editor:${message.id}`, role: message.role, content: message.text,
      eventType: `editor_${message.role}_message`,
      payload: { editor_message_id: message.id, changes: message.applied ?? [] },
    })),
  ], [thread, editorConversation.unsaved]);
  const pendingRuntimeApprovalIds = useMemo(() => {
    const pending = new Set<string>();
    for (const event of [...(thread?.events ?? [])].sort((left, right) => left.sequence - right.sequence)) {
      const approvalId = typeof event.payload?.approval_id === "string"
        ? event.payload.approval_id
        : null;
      if (!approvalId) continue;
      if (event.event_type === "approval_requested") pending.add(approvalId);
      if (["approval_approved", "approval_denied", "approval_cancelled", "approval_expired"].includes(event.event_type)) {
        pending.delete(approvalId);
      }
    }
    return pending;
  }, [thread?.events]);
  const queuedRuntimeTurn = useMemo(() => {
    const queued = new Map<string, string>();
    for (const event of [...(thread?.events ?? [])].sort((left, right) => left.sequence - right.sequence)) {
      const turnId = typeof event.payload?.turn_id === "string" ? event.payload.turn_id : null;
      if (!turnId) continue;
      if (
        event.role === "user"
        && event.event_type === "user_message"
        && event.payload?.turn_status === "queued"
        && event.content
      ) queued.set(turnId, event.content);
      if ([
        "assistant_response", "assistant_question", "assistant_error", "draft_applied",
        "approval_requested", "turn_cancelled",
      ].includes(event.event_type)) queued.delete(turnId);
    }
    const latest = [...queued.entries()].at(-1);
    return latest ? { turnId: latest[0], message: latest[1] } : null;
  }, [thread?.events]);
  const latestRuntimeDraftId = useMemo(() => [...eventMessages]
    .reverse()
    .find((message) => message.artifact === "draft")?.id ?? null, [eventMessages]);
  const eventSequenceById = useMemo(
    () => new Map((thread?.events ?? []).map((event) => [event.id, event.sequence])),
    [thread?.events],
  );
  const latestGenerationSequence = useMemo(
    () => (thread?.events ?? []).reduce((latest, event) => (
      ["action_generate", "action_confirm_generation", "generation_started", "agent_user_confirmation", "agent_assistant_execution"].includes(event.event_type)
        ? Math.max(latest, event.sequence)
        : latest
    ), -1),
    [thread?.events],
  );
  const hasPendingConfirmation = eventMessages.some((message) =>
    (message.artifact === "confirmation" || (message.artifact === "revision" && !hasReady))
    && (eventSequenceById.get(message.id) ?? -1) > latestGenerationSequence,
  );
  const planningFailed = Boolean(thread && creationPlanningFailed(thread));
  const canConfirmDirection = thread !== null
    && !planningFailed
    && (!creationThreadInProgress(thread) || creationJobFailed(thread))
    && (!thread.active_job_id || creationJobFailed(thread));
  const media = useMemo(() => attachedMedia(thread), [thread]);
  const variantStillRendering = Boolean(thread?.job?.variants.some((variant) => variant.render_status === "rendering"));
  const messages = useMemo(() => {
    if (!thread) return eventMessages;
    const lifecycleMessageId = [...eventMessages].reverse().find((message) =>
      ["progress", "result", "failure"].includes(String(message.artifact)),
    )?.id ?? null;
    const rows = eventMessages.flatMap((message) => {
      if (!["progress", "result", "failure"].includes(String(message.artifact))) return message;
      return message.id === lifecycleMessageId ? { ...message, artifact: undefined } : [];
    });
    const synthetic = (artifact: NonNullable<(typeof eventMessages)[number]["artifact"]>, suffix: string) => ({
      id: ["progress", "result", "failure"].includes(artifact)
        ? `${thread.id}:generation:${creationGenerationArtifactKey(thread)}`
        : `${thread.id}:${suffix}`,
      role: "assistant" as const,
      content: "",
      eventType: `state_${suffix}`,
      artifact,
    });
    const insertAfter = (predicate: (row: (typeof rows)[number]) => boolean, row: (typeof rows)[number]) => {
      const index = rows.reduce((found, current, currentIndex) => predicate(current) ? currentIndex : found, -1);
      rows.splice(index + 1, 0, row);
    };
    // State-only/recovered threads still receive their actionable cards at a
    // deterministic point in the transcript, never in a trailing side rail.
    if ((!format || formatPickerOpen) && !eventMessages.some((message) => message.artifact === "format")) {
      rows.unshift(synthetic("format", "format"));
    }
    if (format && !thread.active_job_id && (!productionPreview || !hasReady) && !eventMessages.some((message) => message.artifact === "upload" || message.artifact === "voiceover")) {
      insertAfter((row) => row.artifact === "format", synthetic("upload", "upload"));
    }
    const lifecycleAnchor = (row: (typeof rows)[number]) =>
      ["confirmation", "revision", "progress"].includes(String(row.artifact))
      || ["action_generate", "action_confirm_generation", "agent_user_confirmation", "agent_assistant_execution"].includes(row.eventType);
    const lifecycleArtifact = speechCleanupOutcomeFailed
      || ((creationJobFailed(thread) || planningFailed)
        && (!hasPendingConfirmation || planningFailed))
      ? "failure"
      : creationJobFailed(thread) && !hasPendingConfirmation
        ? "failure"
      : thread.active_job_id && (!creationJobSettled(thread) || variantStillRendering)
        ? "progress"
        : hasReady ? "result" : null;
    if (lifecycleArtifact) {
      const lifecycleIndex = lifecycleMessageId
        ? rows.findIndex((message) => message.id === lifecycleMessageId)
        : -1;
      if (lifecycleIndex >= 0) {
        rows[lifecycleIndex] = {
          ...rows[lifecycleIndex],
          id: `${thread.id}:generation:${creationGenerationArtifactKey(thread)}`,
          content: "",
          artifact: lifecycleArtifact,
        };
      } else {
        insertAfter(lifecycleAnchor, synthetic(lifecycleArtifact, "lifecycle"));
      }
    }
    return rows;
  }, [eventMessages, format, formatPickerOpen, hasPendingConfirmation, hasReady, planningFailed, productionPreview, speechCleanupOutcomeFailed, thread, variantStillRendering]);
  const lastMessageId = messages[messages.length - 1]?.id;
  const latestAudio = [...media].reverse().find((item) => item.kind === "audio") ?? null;
  const clipMedia = media.filter((item) => item.kind === "video");
  const clipCount = clipMedia.length || (media.length === 0 ? mediaCount : 0);
  const accountName = session?.user?.name ?? session?.user?.email ?? "Account";

  const scrollToLiveEdge = useCallback(() => {
    const transcript = transcriptRef.current;
    if (!transcript) return;
    latestMessageRef.current?.scrollIntoView?.({ block: "end" });
    transcript.scrollTop = transcript.scrollHeight;
    atLiveEdgeRef.current = true;
    setHasNewUpdate(false);
  }, []);

  const focusComposerAfterAction = useCallback(() => {
    window.requestAnimationFrame(() => composerRef.current?.focus());
  }, []);

  useEffect(() => {
    const changed = lastRenderedMessageRef.current !== lastMessageId;
    const switchedThread = lastRenderedThreadRef.current !== (thread?.id ?? null);
    if (atLiveEdgeRef.current || switchedThread) scrollToLiveEdge();
    else if (changed) setHasNewUpdate(true);
    if (changed && lastMessageId) {
      const latest = messages.at(-1);
      setAnnouncement(latest?.artifact === "result"
        ? "Your cut is ready."
        : latest?.artifact === "progress" ? "Render progress updated."
          : latest?.artifact === "approval" ? "Approval required."
            : latest?.role === "user" ? "Your message was added."
              : "Kria replied.");
    }
    lastRenderedMessageRef.current = lastMessageId ?? null;
    lastRenderedThreadRef.current = thread?.id ?? null;
  }, [hasReady, lastMessageId, messages, scrollToLiveEdge, thread?.id, thinking, variantStillRendering]);

  const onTranscriptScroll = useCallback(() => {
    const transcript = transcriptRef.current;
    if (!transcript) return;
    const atEdge = transcript.scrollHeight - transcript.scrollTop - transcript.clientHeight <= 80;
    atLiveEdgeRef.current = atEdge;
    if (atEdge) setHasNewUpdate(false);
  }, []);

  async function selectFormat(value: CreationFormat) {
    if (productionPreview || !thread || busy || (format && !formatPickerOpen) || !availableFormats.includes(value)) return;
    const threadId = thread.id;
    setBusy(true); setError(null);
    const paperFormat = value === "narrated_planned" ? "narrated" : value === "subtitled" ? "talking_to_camera" : "montage";
    try {
      const next = await requestThreadResponse(threadId, () =>
        applyCreationAction(thread, "select_format", { format: paperFormat }));
      if (activeThreadIdRef.current === threadId) setFormatPickerOpen(false);
    }
    catch { setError("I couldn’t set that format. Try again in a moment."); }
    finally { setBusy(false); }
  }

  async function selectVariant(variantId: string) {
    if (productionPreview || !thread || busy) return;
    const threadId = thread.id;
    setBusy(true); setError(null);
    try { await requestThreadResponse(threadId, () => applyCreationAction(thread, "select_variant", { variant_id: variantId })); }
    catch { setError("I couldn’t switch to that cut. Try again in a moment."); }
    finally { setBusy(false); }
  }

  async function attach(files: FileList | null) {
    if (productionPreview || !thread || !files?.length) return;
    const sourceThread = thread;
    setError(null);
    const incoming = Array.from(files);
    const clipPolicy = thread.media_capabilities?.clips ?? capabilities.media?.clips;
    const acceptedContentTypes = new Set(clipPolicy?.content_types ?? ["video/mp4", "video/quicktime"]);
    const maxFileBytes = clipPolicy?.max_file_bytes;
    const inferredContentType = (file: File) => {
      const contentType = file.type.trim().toLowerCase();
      return !contentType && /\.mp4$/i.test(file.name)
        ? "video/mp4"
        : !contentType && /\.mov$/i.test(file.name) ? "video/quicktime" : contentType;
    };
    const videoFiles = incoming.filter((file) => acceptedContentTypes.has(inferredContentType(file))
      && (typeof maxFileBytes !== "number" || file.size <= maxFileBytes));
    const rejectedVisuals = incoming.filter((file) => file.type.toLowerCase().startsWith("image/")).length;
    const oversizedVideos = incoming.filter((file) => acceptedContentTypes.has(inferredContentType(file))
      && typeof maxFileBytes === "number" && file.size > maxFileBytes).length;
    const unsupportedFiles = incoming.length - videoFiles.length - rejectedVisuals - oversizedVideos;
    const remaining = Math.max(0, clipLimit - clipCount - pendingFiles.length);
    const chosen = videoFiles.slice(0, remaining);
    if (rejectedVisuals > 0) {
      setError("Photos and screenshots belong in Visuals. Add them from the supporting visuals card below.");
    } else if (oversizedVideos > 0) {
      setError("That video is larger than the PlanItem upload limit.");
    } else if (unsupportedFiles > 0) {
      setError("That video type isn’t supported. Choose an MP4 or MOV file.");
    }
    if (chosen.length < videoFiles.length) {
      setError(`You can add up to ${clipLimit} primary clips for this PlanItem.`);
    }
    if (!chosen.length) return;
    setPendingFiles((items) => [...items, ...chosen].slice(0, clipLimit));
    setUploading(true);
    try {
      const next = await requestThreadResponse(sourceThread.id, (requestSequence) => uploadCreationMedia(sourceThread, chosen, (progress, file) => {
        acceptThreadResponse(sourceThread.id, progress, requestSequence);
        if (activeThreadIdRef.current === sourceThread.id) {
          setPendingFiles((items) => items.filter((item) => item !== file));
        }
      }));
    } catch (cause) {
      if (cause instanceof CreationThreadError && cause.status === 409 && activeThreadIdRef.current === sourceThread.id) {
        void refreshThreadProjection(sourceThread.id).then(({ next: latest, requestSequence }) =>
          acceptThreadResponse(sourceThread.id, latest, requestSequence));
      }
      setError("That upload didn’t finish. Retry the file or remove it and choose another.");
    }
    finally { setUploading(false); }
  }

  async function retryFile(file: File) {
    if (productionPreview || !thread || uploading) return;
    const sourceThread = thread;
    setUploading(true); setError(null);
    try {
      await requestThreadResponse(sourceThread.id, () => uploadCreationMedia(sourceThread, [file]));
      if (activeThreadIdRef.current === sourceThread.id) {
        setPendingFiles((items) => items.filter((item) => item !== file));
      }
    } catch {
      setError(`We couldn’t upload ${file.name}. Retry it or remove it.`);
    } finally { setUploading(false); }
  }

  async function uploadRecordedVoice(file: File | Blob, filename = "voiceover.webm") {
    if (productionPreview) throw new Error("Production previews are read-only.");
    if (!thread) throw new Error("Start a creation project before recording.");
    const sourceThread = thread;
    const recording = file instanceof File ? file : new File([file], filename, { type: file.type || "audio/webm" });
    await requestThreadResponse(sourceThread.id, () => uploadCreationMedia(sourceThread, [recording]));
    return { gcs_path: "creation-thread-media", kind: "audio" };
  }

  async function removeMedia(mediaId: string) {
    if (productionPreview || !thread || busy || uploading) return;
    const sourceThread = thread;
    setBusy(true); setError(null);
    try {
      await requestThreadResponse(sourceThread.id, () => applyCreationAction(sourceThread, "remove_media", { media_id: mediaId }));
    } catch {
      setError("I couldn’t remove that file. Refresh the project and try again.");
    } finally { setBusy(false); }
  }

  const submitMessage = useCallback(async (sourceThread: CreationThread, message: string) => {
    setInput(""); setThinking(true); setError(null);
    try {
      if (workspaceEditorVariant(sourceThread)) {
        if (sourceThread.status !== "active") throw new Error("This project is archived.");
        setEditorOpen(true);
        const targetVariant = workspaceEditorVariant(sourceThread)!;
        const live = await waitForEditorReady(sourceThread.id, sourceThread.active_plan_item_id!, targetVariant.variant_id!);
        const persistedIds = new Set(sourceThread.events.map((event) => event.payload?.editor_message_id));
        const conversation = [
          ...threadMessages(sourceThread),
          ...live.messages.filter((row) => !persistedIds.has(row.id)).map((row) => ({
            role: row.role, content: row.text,
            payload: { clarification_context: row.clarification_context, pending_actions: row.pending_actions },
          })),
        ];
        const history = conversation.filter((row) => row.content).slice(-12).map((row) => ({
          role: row.role, content: row.content.slice(0, 2000),
          ...(row.payload?.clarification_context ? { clarification_context: row.payload.clarification_context as Record<string, unknown> } : {}),
          ...(Array.isArray(row.payload?.pending_actions) ? { pending_actions: row.payload.pending_actions as Array<Record<string, unknown>> } : {}),
        }));
        await sendEditorCommand({ kind: "send", text: message, turns: history });
      } else if (sourceThread.runtime_version === 2) {
        const accepted = await sendKriaTurn(sourceThread, message);
        if (accepted.status === "queued") {
          const { next, requestSequence } = await refreshThreadProjection(sourceThread.id);
          acceptThreadResponse(sourceThread.id, next, requestSequence);
          setError("I queued that behind the current edit. You can keep using this project while it finishes.");
          return;
        }
        const settled = await waitForKriaTurn(sourceThread, accepted.turn_id);
        if (!settled && activeThreadIdRef.current === sourceThread.id) {
          setError("Kria is still working on that. Your request is saved and this chat will keep its place.");
        }
      } else {
        await requestThreadResponse(
          sourceThread.id,
          () => sendCreationMessage(sourceThread, message),
        );
      }
    }
    catch (cause) {
      setInput(message);
      if (isCreationThreadRevisionConflict(cause)) {
        try {
          const { next: latest, requestSequence } = await refreshThreadProjection(sourceThread.id);
          acceptThreadResponse(sourceThread.id, latest, requestSequence);
          setError("This chat changed in another window. Your draft is still here; review the latest direction and send again.");
        } catch {
          setError("This chat changed in another window. Refresh the project, then send again.");
        }
      } else {
        setError(workspaceEditorVariant(sourceThread) && cause instanceof Error
          ? cause.message
          : cause instanceof CreationThreadError && cause.status === 409
            ? `${cause.message}. Your draft is still here.`
            : "I couldn’t send that message. Your draft is still here; try again.");
      }
    }
    finally { setThinking(false); }
  }, [acceptThreadResponse, sendEditorCommand, waitForEditorReady, refreshThreadProjection, requestThreadResponse, waitForKriaTurn]);

  async function send() {
    const message = input.trim();
    if (productionPreview || !message || !thread || thinking) return;
    const sourceThread = thread;
    if (offline) {
      queuedMessageRef.current = { threadId: sourceThread.id, message };
      setError("You’re offline. Your message is saved here; send it when you reconnect.");
      return;
    }
    // An explicit newer send supersedes an older offline draft for this same
    // project; never let that hidden draft send after the newer message.
    if (queuedMessageRef.current?.threadId === sourceThread.id && queuedMessageRef.current.message !== message) {
      queuedMessageRef.current = null;
    }
    await submitMessage(sourceThread, message);
  }

  useEffect(() => {
    if (offline || !queuedMessageRef.current || !thread || thinking) return;
    const queued = queuedMessageRef.current;
    if (queued.threadId !== thread.id) return;
    // Do not replace a newer draft the user typed while reconnecting. They can
    // explicitly send it; the older queued message remains isolated to this
    // project until the composer is cleared or the project is reopened.
    if (input.trim() && input.trim() !== queued.message) return;
    queuedMessageRef.current = null;
    void submitMessage(thread, queued.message);
  }, [offline, input, submitMessage, thread, thinking]);

  async function confirm(action: "generate" | "retry" | "revise", payload: Record<string, unknown> = {}) {
    if (productionPreview || !thread || busy) return;
    if (editorChatState?.dirty || editorChatState?.sending || editorChatState?.saving) {
      setError("Save or undo your editor draft before starting another render.");
      return;
    }
    const sourceThread = thread;
    setBusy(true); setError(null);
    try { await requestThreadResponse(sourceThread.id, () => applyCreationAction(sourceThread, action, payload)); }
    catch { setError("I couldn’t start that render. Your project is safe—adjust the direction or try again."); }
    finally { setBusy(false); }
  }

  async function decideRuntimeApproval(
    approvalId: string,
    decision: "approve" | "deny",
  ) {
    if (productionPreview || !thread || thread.runtime_version !== 2 || busy) return;
    if (decision === "approve" && (editorChatState?.dirty || editorChatState?.sending || editorChatState?.saving)) {
      setError("Save or undo your editor draft before approving a different render.");
      return;
    }
    const sourceThread = thread;
    setBusy(true); setError(null);
    try {
      const approval = await getKriaApproval(sourceThread.id, approvalId);
      await decideKriaApproval(
        sourceThread.id,
        approval,
        decision,
        sourceThread.revision,
      );
      const { next, requestSequence } = await refreshThreadProjection(sourceThread.id);
      acceptThreadResponse(sourceThread.id, next, requestSequence);
    } catch (cause) {
      if (cause instanceof CreationThreadError && cause.problem?.recovery === "refresh_replan") {
        const { next, requestSequence } = await refreshThreadProjection(sourceThread.id);
        acceptThreadResponse(sourceThread.id, next, requestSequence);
      }
      setError(cause instanceof Error
        ? cause.message
        : "I couldn’t record that decision. Your draft is still safe.");
    } finally {
      setBusy(false);
      focusComposerAfterAction();
    }
  }

  async function undoRuntimeDraft() {
    if (productionPreview || !thread || thread.runtime_version !== 2 || busy) return;
    const sourceThread = thread;
    setBusy(true); setError(null);
    try {
      const draft = await getKriaDraft(sourceThread.id);
      if (!draft.can_undo) {
        setError("There isn’t an earlier draft to restore yet.");
        return;
      }
      await undoKriaDraft(sourceThread.id, draft.draft_revision);
      const { next, requestSequence } = await refreshThreadProjection(sourceThread.id);
      acceptThreadResponse(sourceThread.id, next, requestSequence);
    } catch (cause) {
      setError(cause instanceof Error
        ? cause.message
        : "I couldn’t undo that draft. Refresh the project and try again.");
    } finally {
      setBusy(false);
      focusComposerAfterAction();
    }
  }

  async function cancelQueuedRuntimeTurn(restore: boolean) {
    if (!thread || thread.runtime_version !== 2 || !queuedRuntimeTurn || busy) return;
    const sourceThread = thread;
    setBusy(true); setError(null);
    try {
      await cancelKriaTurn(sourceThread.id, queuedRuntimeTurn.turnId, sourceThread.revision);
      const { next, requestSequence } = await refreshThreadProjection(sourceThread.id);
      acceptThreadResponse(sourceThread.id, next, requestSequence);
      if (restore) {
        setInput(queuedRuntimeTurn.message);
      }
    } catch (cause) {
      setError(cause instanceof Error
        ? cause.message
        : "I couldn’t change the queued request. Refresh and try again.");
    } finally {
      setBusy(false);
      focusComposerAfterAction();
    }
  }

  function beginRename(project: CreationThread) {
    renameCancelledRef.current = false;
    setRenameTarget(project);
    setRenameValue(projectTitle(project));
    setProjectActionError(null);
  }

  async function renameProject() {
    const target = renameTarget;
    const name = renameValue.trim();
    if (!target || projectActionBusy || renameInFlightRef.current || renameCancelledRef.current) return;
    if (!name || name === projectTitle(target)) {
      setRenameTarget(null);
      setProjectActionError(null);
      return;
    }
    renameInFlightRef.current = true;
    setProjectActionBusy(true);
    setProjectActionError(null);
    setError(null);
    if (productionPreview) {
      const next = { ...target, title: name };
      setProjects((items) => items.map((item) => item.id === target.id ? next : item));
      setThread((current) => current?.id === target.id ? next : current);
      setRenameTarget(null);
      renameInFlightRef.current = false;
      setProjectActionBusy(false);
      return;
    }
    try {
      const next = await renameCreationThread(target, name);
      setProjects((items) => items.map((item) => item.id === next.id ? next : item));
      setThread((current) => current?.id === target.id ? next : current);
      setRenameTarget(null);
    } catch {
      setProjectActionError("I couldn’t rename that project. Try again in a moment.");
    } finally {
      renameInFlightRef.current = false;
      setProjectActionBusy(false);
    }
  }

  async function deleteProject() {
    const target = deleteTarget;
    if (!target || projectActionBusy) return;
    setProjectActionBusy(true);
    setProjectActionError(null);
    setError(null);
    if (productionPreview) {
      const remaining = projects.filter((item) => item.id !== target.id);
      setProjects(remaining);
      setDeleteTarget(null);
      if (activeThreadIdRef.current === target.id) {
        const nextProject = remaining[0] ?? null;
        activeThreadIdRef.current = nextProject?.id ?? null;
        setThread(nextProject);
        if (nextProject) {
          router.replace(`/dev-qa/chat-first-creation?live=1&project=${encodeURIComponent(nextProject.id)}`, { scroll: false });
        }
      }
      setProjectActionBusy(false);
      return;
    }
    try {
      await deleteCreationThread(target);
      const remaining = projects.filter((item) => item.id !== target.id);
      setProjects(remaining);
      setDeleteTarget(null);
      if (activeThreadIdRef.current === target.id) {
        const nextProject = remaining[0];
        if (nextProject) {
          activeThreadIdRef.current = nextProject.id;
          setThread(nextProject);
          router.replace(`/plan/${nextProject.id}`, { scroll: false });
          void refreshCreationThread(nextProject.id)
            .then((next) => acceptThreadResponse(nextProject.id, next))
            .catch(() => setError("I deleted that project, but couldn’t refresh the next one yet."));
        } else {
          activeThreadIdRef.current = null;
          setThread(null);
          router.replace("/plan", { scroll: false });
        }
      }
    } catch {
      setProjectActionError("I couldn’t delete that project. It may have changed elsewhere.");
      setError("I couldn’t delete that project. It may have changed elsewhere.");
    } finally {
      setProjectActionBusy(false);
    }
  }

  async function runSpeechCleanupAction(
    action: "generate" | "retry" | "retry_speech_cleanup" | "create_without_cleanup",
    pendingAction: Exclude<SpeechCleanupPendingAction, null>,
    payload: Record<string, unknown>,
    stableAction: CreationSpeechCleanupChoice | "no_findings" | "retry_analysis" | "retry_render" | "bypass",
  ) {
    const analysisId = speechCleanup?.analysis?.id;
    const actionNeedsVideo = action !== "retry_speech_cleanup";
    if (
      productionPreview
      || !thread
      || !analysisId
      || busy
      || speechCleanupActionInFlightRef.current
      || (actionNeedsVideo && speechCleanup?.render_blocker === "video_required")
    ) return;
    const sourceThread = thread;
    // React state disables the controls on the next render. The ref closes the
    // smaller same-tick window so a rapid clean/keep double action can never
    // dispatch two mutations before that render lands.
    speechCleanupActionInFlightRef.current = true;
    setBusy(true);
    setSpeechCleanupPendingAction(pendingAction);
    setError(null);
    try {
      const actionPayload = { ...payload, speech_cleanup_analysis_id: analysisId };
      await requestThreadResponse(sourceThread.id, () => applyCreationAction(
        sourceThread,
        action,
        actionPayload,
        creationSpeechCleanupActionId(analysisId, stableAction, sourceThread.revision),
      ));
    } catch (cause) {
      if (isCreationSpeechCleanupStaleConflict(cause)) {
        try {
          const { next, requestSequence } = await refreshThreadProjection(sourceThread.id);
          acceptThreadResponse(sourceThread.id, next, requestSequence);
          setError("The speech check changed with your media. Review the latest result and choose again.");
        } catch {
          setError("The speech check changed with your media. Refresh the project and choose again.");
        }
      } else {
        setError(action === "retry_speech_cleanup"
          ? "I couldn’t restart the speech check. Try again in a moment."
          : "I couldn’t start that video. Your project and speech choice are safe—try again.");
      }
    } finally {
      speechCleanupActionInFlightRef.current = false;
      setBusy(false);
      setSpeechCleanupPendingAction(null);
    }
  }

  function generateWithSpeechCleanup(choice?: CreationSpeechCleanupChoice) {
    void runSpeechCleanupAction(
      "generate",
      choice ?? "no_findings",
      choice ? { speech_cleanup_choice: choice } : {},
      choice ?? "no_findings",
    );
  }

  function retrySpeechCleanupAnalysis() {
    void runSpeechCleanupAction("retry_speech_cleanup", "retry_analysis", {}, "retry_analysis");
  }

  function createWithoutSpeechCleanup() {
    void runSpeechCleanupAction("create_without_cleanup", "bypass", {}, "bypass");
  }

  function retrySpeechCleanupRender() {
    void runSpeechCleanupAction("retry", "retry_render", {}, "retry_render");
  }

  async function startNew() {
    if (productionPreview || (initialLoading && !thread) || busy || thinking || uploading) return;
    const previousThreadId = activeThreadIdRef.current;
    setBusy(true);
    setError(null);
    try {
      const next = await createCreationThread();
      activateThread(next);
      setProjects((items) => [next, ...items]);
      setPendingFiles([]);
      setInput("");
      setProjectsOpen(false);
      setGalleryOpen(false);
      router.replace(`/plan/${next.id}`, { scroll: false });
    } catch {
      // A failed create must not strand the currently open project. Keep its
      // identity authoritative so refresh/actions still target that thread.
      activeThreadIdRef.current = previousThreadId;
      setError("I couldn’t start a new project. Try again in a moment.");
    }
    finally { setBusy(false); }
  }

  async function openProject(project: CreationThread) {
    if (busy || thinking || uploading) return;
    if (expectedEditorRenderRef.current?.threadId !== project.id) {
      expectedEditorRenderRef.current = null;
    }
    activeThreadIdRef.current = project.id;
    setThread(project);
    setPendingFiles([]);
    setInput("");
    setProjectsOpen(false);
    setGalleryOpen(false);
    router.replace(
      productionPreview
        ? `/dev-qa/chat-first-creation?live=1&project=${encodeURIComponent(project.id)}`
        : `/plan/${project.id}`,
      { scroll: false },
    );
    if (productionPreview && isProductionLibraryThread(project)) return;
    try {
      const { next, requestSequence } = await refreshThreadProjection(project.id);
      acceptThreadResponse(project.id, productionPreview
        ? { ...next, title: inferredProductionTitle(next) }
        : next, requestSequence);
    }
    catch { if (activeThreadIdRef.current === project.id) setError("I couldn’t open that project."); }
  }

  function openGallery() {
    const activeThreadId = activeThreadIdRef.current;
    setProjectsOpen(false);
    setGalleryOpen(true);
    router.replace(
      productionPreview
        ? "/dev-qa/chat-first-creation?live=1&view=gallery"
        : `${activeThreadId ? `/plan/${activeThreadId}` : "/plan"}?view=gallery`,
      { scroll: false },
    );
  }

  function closeGallery() {
    const activeThreadId = activeThreadIdRef.current;
    setGalleryOpen(false);
    router.replace(
      productionPreview
        ? `/dev-qa/chat-first-creation?live=1${thread ? `&project=${encodeURIComponent(thread.id)}` : ""}`
        : activeThreadId ? `/plan/${activeThreadId}` : "/plan",
      { scroll: false },
    );
  }

  const selectedReadyVariant = readyVariant(thread);
  const selectedFailedVariant = failedVariant(thread);
  const editorUrl = !productionPreview && thread?.active_plan_item_id
    ? `/plan/items/${thread.active_plan_item_id}/edit?embedded=1${editorVariant?.variant_id ? `&variant=${encodeURIComponent(editorVariant.variant_id)}` : ""}`
    : null;
  const directionDescription = "Kria will use the proposed direction to make a new cut. Rendering starts only after you approve.";
  const cleanupCard = speechCleanup?.applicable ? (
    <SpeechCleanupDecisionCard
      cleanup={speechCleanup}
      formatLabel={creationFormatLabel(format)}
      direction={directionDescription}
      busy={busy || productionPreview}
      pendingAction={speechCleanupPendingAction}
      onGenerate={generateWithSpeechCleanup}
      onRetryAnalysis={retrySpeechCleanupAnalysis}
      onRetryRender={retrySpeechCleanupRender}
      onCreateWithoutCleanup={createWithoutSpeechCleanup}
    />
  ) : null;
  const defaultConfirmationCard = (
    <AgentApprovalCard
      badge={<Badge variant="secondary">Creative direction</Badge>}
      title={`${creationFormatLabel(format)} is ready to make`}
      description={directionDescription}
      actions={<Button type="button" className="min-h-11 w-full" disabled={productionPreview || busy || clipCount === 0} onClick={() => void confirm("generate")}>
        <Sparkles />{busy ? "Starting…" : "Create this video"}
      </Button>}
    />
  );
  const liveAnnouncement = speechCleanupAnnouncement(speechCleanup, {
    rendering: Boolean(thread && creationThreadInProgress(thread)),
    ready: hasReady,
  });

  if (threadUnavailable) {
    return (
      <div className="flex h-dvh items-center justify-center bg-background px-6 text-center text-foreground">
        <div className="max-w-md space-y-4">
          <h1 className="font-display text-3xl font-medium">Project unavailable</h1>
          <p className="text-sm text-muted-foreground">This project may have been deleted or you may no longer have access to it.</p>
          <Button type="button" asChild><Link href="/plan">Back to projects</Link></Button>
        </div>
      </div>
    );
  }

  const sidebar = (
    <aside className="flex h-full w-[260px] shrink-0 flex-col gap-6 border-r border-border bg-background px-[14px] pb-8 pt-6" aria-label="Projects">
      <div className="flex h-11 shrink-0 items-center justify-between px-3">
        <span className="flex items-center justify-start gap-2 text-[45px] text-[#9BCAFF]" role="img" aria-label="Kria"><KriaWordmark className="h-[54px] w-[143px]" /></span>
        <Button type="button" variant="ghost" size="icon" className="size-11 md:size-9" aria-label="Hide project sidebar" onClick={() => setSidebarHidden(true)}><PanelLeftClose /></Button>
      </div>
      <Button
        type="button"
        variant="ghost"
        className={cn(
          "h-12 w-full shrink-0 justify-start gap-3 rounded-2xl px-3 text-left text-[15px] font-semibold",
          galleryOpen
            ? "bg-[#EBF3FF] text-[#30352C] hover:bg-[#EBF3FF]"
            : "bg-white text-[#30352C] hover:bg-[#F7F7F8]",
        )}
        disabled={busy || thinking || uploading}
        onClick={openGallery}
      >
        <Film className="size-6" aria-hidden="true" />
        Gallery
      </Button>
      <div className="flex min-h-0 flex-1 flex-col gap-1" data-testid="recent-chats-section">
        <div className="flex h-8 shrink-0 items-center px-3">
          <p className="text-[13px] font-semibold text-muted-foreground">Recent chats</p>
        </div>
      <nav className="min-h-0 space-y-1 overflow-y-auto" aria-label="Recent projects">
        {projects.slice(0, 10).map((project) => {
          const title = projectTitle(project);
          const sidebarTitle = projectSidebarTitle(project);
          return (
            <div key={project.id} className="flex min-w-0 items-center gap-1">
              {renameTarget?.id === project.id ? (
                <form
                  className={cn("min-h-11 min-w-0 flex-1 rounded-md px-4 py-2 text-sm font-medium", project.id === thread?.id && "bg-[#EBF3FF] text-[#245E9B]")}
                  aria-label={`Rename ${title}`}
                  onSubmit={(event) => { event.preventDefault(); void renameProject(); }}
                >
                  <Input
                    aria-label="Project name"
                    className="block h-5 w-full min-w-0 appearance-none rounded-none border-0 bg-transparent p-0 text-sm font-medium leading-5 text-inherit shadow-none focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#30352c]"
                    value={renameValue}
                    maxLength={120}
                    readOnly={projectActionBusy}
                    onChange={(event) => setRenameValue(event.target.value)}
                    ref={renameInputRef}
                    onFocus={(event) => event.currentTarget.select()}
                    onBlur={() => { void renameProject(); }}
                    onKeyDown={(event) => {
                      if (event.key === "Enter" && event.nativeEvent.isComposing) event.preventDefault();
                      if (event.key === "Escape" && !projectActionBusy) {
                        event.preventDefault();
                        event.stopPropagation();
                        renameCancelledRef.current = true;
                        setRenameTarget(null);
                        setProjectActionError(null);
                      }
                    }}
                    aria-invalid={Boolean(projectActionError)}
                    aria-describedby={projectActionError ? `rename-error-${project.id}` : undefined}
                  />
                  <span className="block truncate text-[11px] font-normal text-muted-foreground">{projectStatusLabel(project)}</span>
                  {projectActionError ? <p id={`rename-error-${project.id}`} className="mt-1 text-xs text-destructive" role="alert">{projectActionError}</p> : null}
                </form>
              ) : (
              <Button type="button" variant="ghost" className={cn("h-auto min-h-11 min-w-0 flex-1 justify-start text-left", project.id === thread?.id ? "bg-[#EBF3FF] text-[#245E9B] hover:bg-[#EBF3FF] hover:text-[#245E9B]" : "hover:bg-[#F7F7F8] hover:text-foreground")} disabled={busy || thinking || uploading} onClick={() => void openProject(project)}><span className="min-w-0"><span className="block truncate">{sidebarTitle}</span><span className="block truncate text-[11px] font-normal text-muted-foreground">{projectStatusLabel(project)}</span></span></Button>
              )}
              <DropdownMenu>
              <DropdownMenuTrigger asChild><Button type="button" variant="ghost" size="icon" className="size-11 shrink-0 md:size-9" aria-label={`Project actions for ${title}`} disabled={busy || thinking || uploading}><MoreHorizontal /></Button></DropdownMenuTrigger>
                <DropdownMenuContent align="end" onCloseAutoFocus={(event) => { if (renameInputRef.current) { event.preventDefault(); requestAnimationFrame(() => renameInputRef.current?.focus()); } }}>
                  <DropdownMenuItem onSelect={() => beginRename(project)}>Rename project{productionPreview ? " (preview)" : ""}</DropdownMenuItem>
                  <DropdownMenuSeparator />
                  <DropdownMenuItem className="text-destructive focus:text-destructive" disabled={projectDeletionBlocked(project)} title={projectDeletionBlocked(project) ? "Finish the active render before deleting this project." : undefined} onSelect={() => setDeleteTarget(project)}>{projectDeletionBlocked(project) ? "Delete after rendering" : `Delete project${productionPreview ? " (preview)" : ""}`}</DropdownMenuItem>
                </DropdownMenuContent>
              </DropdownMenu>
            </div>
          );
        })}
      </nav>
      </div>
      <div className="mt-auto flex shrink-0 items-center justify-between gap-2 border-t pt-4">
        <Button
          type="button"
          className="h-12 w-[132px] shrink-0 justify-center gap-2 rounded-full bg-[#FFF0A6] px-4 text-base font-bold text-[#30352C] hover:bg-[#FFE98A] hover:text-[#30352C]"
          disabled={productionPreview || (initialLoading && !thread) || busy || thinking || uploading}
          title={productionPreview ? "Production data is read-only in this preview." : undefined}
          onClick={() => void startNew()}
        >
          <Pencil className="size-[22px]" aria-hidden="true" />
          New chat
        </Button>
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button type="button" variant="ghost" size="icon" className="size-12 shrink-0 rounded-full" aria-label="Account menu">
              <UserRound aria-hidden="true" />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent side="top" align="start" className="w-44">
            <DropdownMenuLabel className="truncate text-[11px] font-normal text-muted-foreground">{accountName}</DropdownMenuLabel>
            <DropdownMenuItem onSelect={openGallery}>My videos</DropdownMenuItem>
            {CREATOR_MEMORY_ENABLED ? <DropdownMenuItem asChild><Link href="/plan/profile">Personalization</Link></DropdownMenuItem> : null}
            <DropdownMenuSeparator />
            <DropdownMenuItem onSelect={() => void signOut({ callbackUrl: "/" })}>Sign out</DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
    </aside>
  );

  const formatArtifact = (
    <ChatArtifactCard title="What are you making?" description="Pick a starting point. You can shape the creative direction together in chat.">
      <div className={cn(
        "gap-3",
        hasEditor && editorOpen
          ? "grid grid-cols-1"
          : "grid grid-flow-col auto-cols-[minmax(220px,85%)] snap-x overflow-x-auto scrollbar-none sm:grid-flow-row sm:auto-cols-auto sm:grid-cols-3 sm:overflow-visible",
      )}>
        {FORMATS.map((item) => <Button key={item.value} type="button" variant="outline" disabled={productionPreview || busy || (Boolean(format) && !formatPickerOpen) || !availableFormats.includes(item.value)} className={cn("h-auto min-h-[96px] snap-start flex-col items-start justify-start whitespace-normal p-4 text-left", format === item.value && "border-primary ring-1 ring-primary", !availableFormats.includes(item.value) && "opacity-60")} onClick={() => void selectFormat(item.value)}><span className="font-medium">{item.label}</span><span className="mt-1 text-xs font-normal text-muted-foreground">{availableFormats.includes(item.value) ? item.description : "Temporarily unavailable — choose another format."}</span></Button>)}
      </div>
    </ChatArtifactCard>
  );

  const uploadArtifact = (
    <ChatArtifactCard title={format ? FORMAT_GUIDANCE[format].title : "Add clips"} description={format ? FORMAT_GUIDANCE[format].description : "Choose the footage for your story."}>
      {!productionPreview && format === "narrated_planned" && !latestAudio ? <VoiceRecorder upload={uploadRecordedVoice} onVoiceover={() => undefined} /> : null}
      {format ? <Button type="button" variant="ghost" className="min-h-11 px-2 text-xs text-muted-foreground md:h-8 md:min-h-8" disabled={productionPreview} onClick={() => setFormatPickerOpen(true)}>Change format</Button> : null}
      <Dropzone compact accept="video/*" multiple={format !== "subtitled"} disabled={productionPreview || uploading || clipCount >= clipLimit} title={productionPreview ? "Uploads are disabled in this read-only preview" : uploading ? "Uploading…" : clipCount >= clipLimit ? "Clip limit reached" : format === "subtitled" ? "Choose a clip or drop it here" : "Choose clips or drop them here"} subline={format === "subtitled" ? undefined : `Up to ${clipLimit} clips`} ariaLabel="Add primary video clips" inputAriaLabel="Upload primary video clips" onFiles={(files) => void attach(files)} />
      {pendingFiles.length > 0 ? <div className="mt-2 space-y-1">{pendingFiles.map((file) => <div key={`${file.name}-${file.size}`} className="flex items-center justify-between gap-2 rounded-md bg-muted px-2 py-1 text-xs"><span className="truncate">{file.name}</span><div className="flex shrink-0 items-center gap-1"><Button type="button" variant="ghost" size="sm" className="min-h-11 px-3 md:h-7 md:min-h-7" disabled={uploading} onClick={() => void retryFile(file)}>Retry</Button><Button type="button" variant="ghost" size="icon" className="size-11 md:size-7" aria-label={`Remove ${file.name}`} onClick={() => setPendingFiles((items) => items.filter((item) => item !== file))}><Trash2 className="size-3" /></Button></div></div>)}</div> : null}
      {media.length > 0 && !thread?.active_job_id ? <div className="mt-2 space-y-1" role="list">{media.map((item) => <div key={item.media_id} className="flex items-center justify-between gap-2 rounded-md bg-muted px-3 py-2 text-sm" role="listitem"><span className="min-w-0 truncate">{item.filename}{item.kind === "audio" ? <span className="ml-2 text-xs text-muted-foreground">Voiceover</span> : null}</span><Button type="button" variant="ghost" size="icon" className="size-11 shrink-0 md:size-8" disabled={productionPreview || busy || uploading} aria-label={`Remove attached ${item.filename}`} onClick={() => void removeMedia(item.media_id)}><Trash2 className="size-4" /></Button></div>)}</div> : null}
    </ChatArtifactCard>
  );

  const visualsArtifact = !productionPreview && visualsEnabled && thread?.active_plan_item_id && format
    && (!thread.active_job_id || creationJobFailed(thread)) ? (
    <ChatArtifactCard
      title="Add visuals (optional)"
      description="Photos, screenshots, or short supporting videos."
      data-testid="creation-visuals-artifact"
    >
      <AssetPool itemId={thread.active_plan_item_id} embedded concise />
    </ChatArtifactCard>
  ) : null;

  const projectDialogs = (
    <>
      <AlertDialog open={Boolean(deleteTarget)} onOpenChange={(open) => { if (!open && !projectActionBusy) setDeleteTarget(null); }}>
        <AlertDialogContent>
          <AlertDialogHeader><AlertDialogTitle>{productionPreview ? "Preview the deleted state?" : "Delete project?"}</AlertDialogTitle><AlertDialogDescription>{productionPreview ? `“${deleteTarget ? projectTitle(deleteTarget) : "This project"}” will disappear only from this browser preview and return on reload. No production data will be changed.` : `“${deleteTarget ? projectTitle(deleteTarget) : "This project"}” will permanently delete this chat, its uploads, edit data, and completed Kria videos. This cannot be recovered. Published TikTok posts remain on TikTok.`}</AlertDialogDescription></AlertDialogHeader>
          {projectActionError ? <p className="text-sm text-destructive" role="alert">{projectActionError}</p> : null}<AlertDialogFooter><AlertDialogCancel disabled={projectActionBusy}>Cancel</AlertDialogCancel><AlertDialogAction className="bg-destructive text-destructive-foreground hover:bg-destructive/90" disabled={projectActionBusy} onClick={(event) => { event.preventDefault(); void deleteProject(); }}>{projectActionBusy ? "Deleting…" : productionPreview ? "Hide in preview" : "Delete project"}</AlertDialogAction></AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  );

  const chat = (
    <>
    <section className="flex min-h-0 flex-1 flex-col" aria-label="Kria creation chat">
      {productionPreview ? <div className="flex shrink-0 items-center justify-center gap-2 border-b border-lime-300 bg-lime-50 px-4 py-2 text-center text-xs text-lime-950" role="status" data-testid="production-preview-banner"><span className="size-2 rounded-full bg-lime-600" aria-hidden="true" /><strong>Live production data</strong><span>Read-only. Rename and delete are local previews that reset on reload.</span></div> : null}
      {CREATOR_MEMORY_ENABLED && thread?.direction_receipt ? <div className="px-4 sm:px-6"><CreatorDirectionReceipt receipt={thread.direction_receipt} projectId={thread.id} expectedRevision={thread.direction_receipt.memory_revision} /></div> : null}
      <p className="sr-only" aria-live="polite" aria-atomic="true" data-testid="creation-live-announcer">{announcement}</p>
      <p className="sr-only" role="status" aria-atomic="true" data-testid="speech-cleanup-live-announcer">{liveAnnouncement}</p>
      <div ref={transcriptRef} role="log" aria-label="Conversation history" aria-live="off" tabIndex={0} onScroll={onTranscriptScroll} className="min-h-0 flex-1 touch-pan-y overflow-y-auto overscroll-y-contain [scrollbar-gutter:stable]"><div className="mx-auto flex w-full max-w-2xl flex-col gap-4 px-4 py-6 sm:px-8">
        {!thread && !error ? <div className="space-y-3" role="status"><div className="h-5 w-40 motion-safe:animate-pulse rounded bg-muted" /><div className="h-20 w-full motion-safe:animate-pulse rounded bg-muted" /></div> : null}
        {!thread && error ? <ChatArtifactCard title="Creation chat couldn’t load" description="Your projects are safe. Check your connection, then try again."><Button type="button" variant="outline" disabled={initialLoading} onClick={() => void load()}><RefreshCw /> {initialLoading ? "Retrying…" : "Retry"}</Button></ChatArtifactCard> : null}
        {messages.map((message, index) => {
          const approvalId = typeof message.payload?.approval_id === "string"
            ? message.payload.approval_id
            : null;
          const changes = Array.isArray(message.payload?.changes)
            ? message.payload.changes.filter((value): value is string => typeof value === "string")
            : [];
          return <div key={message.id} ref={index === messages.length - 1 ? latestMessageRef : undefined} className="space-y-3">
            {message.content ? <ChatMessage role={message.role} animate={index === messages.length - 1}>{message.content}</ChatMessage> : null}
            {queuedRuntimeTurn && message.payload?.turn_id === queuedRuntimeTurn.turnId ? <ChatArtifactCard badge={<Badge variant="outline">After this render</Badge>} title="One follow-up is queued" description="Kria saved this request and will pick it up when the current work settles."><div className="flex flex-col-reverse gap-2 sm:flex-row sm:justify-end"><Button type="button" variant="outline" className="min-h-11" disabled={productionPreview || busy} onClick={() => void cancelQueuedRuntimeTurn(false)}>Cancel request</Button><Button type="button" variant="outline" className="min-h-11" disabled={productionPreview || busy} onClick={() => void cancelQueuedRuntimeTurn(true)}>Change request</Button></div></ChatArtifactCard> : null}
            {message.artifact === "draft" && message.id === latestRuntimeDraftId ? <ChatArtifactCard badge={<Badge variant="secondary">Draft saved</Badge>} title="Your edit is ready to review" description={changes.length > 0 ? changes.join(" · ") : "Kria applied the direction as a reversible draft."}><Button type="button" variant="outline" className="min-h-11 w-full" disabled={productionPreview || busy} onClick={() => void undoRuntimeDraft()}><RefreshCw /> Undo draft</Button></ChatArtifactCard> : null}
            {message.artifact === "approval" && approvalId && pendingRuntimeApprovalIds.has(approvalId) ? <AgentApprovalCard badge={<Badge variant="secondary">Approval required</Badge>} title="Start this render?" description={`${String(message.payload?.consequence_summary ?? "Render the saved draft.")}${message.payload?.cost_summary ? ` ${String(message.payload.cost_summary)}.` : ""}`} actions={<div className="flex flex-col-reverse gap-2 sm:flex-row sm:justify-end"><Button type="button" variant="outline" className="min-h-11" disabled={productionPreview || busy} onClick={() => void decideRuntimeApproval(approvalId, "deny")}>Not yet</Button><Button type="button" className="min-h-11" disabled={productionPreview || busy} onClick={() => void decideRuntimeApproval(approvalId, "approve")}><Sparkles />{busy ? "Recording approval…" : "Approve and render"}</Button></div>} /> : null}
            {message.artifact === "format" && (!format || formatPickerOpen) ? formatArtifact : null}
            {message.artifact === "upload" && !thread?.active_job_id ? <>{uploadArtifact}{visualsArtifact}</> : null}
            {message.artifact === "voiceover" && !thread?.active_job_id ? uploadArtifact : null}
            {(message.artifact === "confirmation" || (message.artifact === "revision" && !hasReady)) && canConfirmDirection && !speechCleanupOutcomeFailed ? (cleanupCard ?? defaultConfirmationCard) : null}
            {message.artifact === "revision" && hasReady ? <AgentApprovalCard badge={<Badge variant="secondary">Revision ready</Badge>} title="Apply this direction?" description="This creates a new generation from the finished cut." actions={<Button type="button" className="min-h-11 w-full" disabled={productionPreview || busy} onClick={() => void confirm("generate", { base_generation: thread?.job?.id })}><RefreshCw /> Create revision</Button>} /> : null}
            {message.artifact === "progress" && thread?.active_job_id && !creationJobFailed(thread) && !speechCleanupOutcomeFailed ? <RenderStatusCard thread={thread} /> : null}
            {message.artifact === "failure" && thread && (speechCleanupOutcomeFailed || ((creationJobFailed(thread) || planningFailed) && (!hasPendingConfirmation || planningFailed))) ? (speechCleanupOutcomeFailed ? cleanupCard : <FailureStatusCard thread={thread} busy={busy} readOnly={productionPreview} planningFailure={planningFailed} onRetry={creationJobFailed(thread) && thread.runtime_version !== 2 ? () => void confirm("retry") : undefined} onAdjust={() => setInput(latestCreationDirection(thread) || "Try a different opening and keep the pacing quick.")} />) : null}
            {message.artifact === "result" && thread && hasReady ? <ReadyStatusCard thread={thread} isPartial={isPartial} selectedReadyVariant={selectedReadyVariant} selectedFailedVariant={selectedFailedVariant} busy={busy} readOnly={productionPreview} onSelectVariant={(id) => void selectVariant(id)} onOpenEditor={() => { setEditorOpen(true); setMobileTab("editor"); }} onRetryVariant={(id) => void confirm("retry", { variant_id: id })} /> : null}
            {(() => {
              const receipt = automaticMemoryReceipt(thread?.events.find((event) => event.id === message.id));
              return receipt ? (
                <AutomaticMemoryReceiptCard
                  receipt={receipt}
                  busy={memoryUndoBusy === receipt.operationId}
                  undone={receipt.undone || Boolean(undoneMemoryOperations[receipt.operationId])}
                  error={memoryUndoErrors[receipt.operationId]}
                  onUndo={() => void undoAutomaticMemory(receipt)}
                />
              ) : null;
            })()}
          </div>;
        })}
        {hasEditor && !productionPreview ? <WorkspaceEditorActivity state={editorChatState} onCommand={runEditorAction} /> : null}
        {editorConversation.error ? <div role="alert" className="space-y-2 text-sm text-destructive">{editorConversation.error}<Button type="button" variant="outline" onClick={() => void editorConversation.retry()}>Retry saving conversation</Button></div> : null}
        {thinking ? <ChatThinking /> : null}
      </div></div>
      {hasNewUpdate ? <div className="flex shrink-0 justify-center border-t bg-background/95 px-3 py-2"><Button type="button" variant="secondary" className="min-h-11" onClick={scrollToLiveEdge}>New update</Button></div> : null}
      <div className={cn("shrink-0 bg-background p-3 pb-[max(0.75rem,env(safe-area-inset-bottom))] sm:p-4", !hasNewUpdate && "border-t")}><AgentComposer ref={composerRef} className="mx-auto max-w-2xl" value={input} onValueChange={setInput} onSubmit={() => void send()} disabled={productionPreview || thread?.status === "archived"} submitDisabled={thinking || !thread || Boolean(editorChatState?.sending)} placeholder={productionPreview ? "Read-only production preview" : "Tell Kria what you’re imagining…"} inputLabel="Message Kria" submitLabel="Send message" leadingAction={<><Button type="button" variant="ghost" size="icon" className="size-11 shrink-0 md:hidden" aria-label="Open projects" onClick={() => setProjectsOpen(true)}><Menu /></Button><Button type="button" variant="ghost" size="icon" className="size-11 shrink-0 rounded-full" aria-label="Attach primary video clips" disabled={productionPreview || !thread || uploading || Boolean(thread?.active_job_id) || clipCount >= clipLimit} onClick={() => document.getElementById("creation-file-picker")?.click()}><Plus /></Button><input id="creation-file-picker" type="file" className="sr-only" accept="video/*" multiple={format !== "subtitled"} disabled={productionPreview} onChange={(event) => { void attach(event.target.files); event.target.value = ""; }} /></>} status={offline || pollReconnecting || error ? <>{offline ? <p className="flex items-center gap-1 text-xs text-muted-foreground" role="status"><WifiOff className="size-3" /> Offline — messages stay in the composer until you reconnect.</p> : null}{pollReconnecting ? <p className="flex items-center gap-1 text-xs text-muted-foreground" role="status"><RefreshCw className="size-3 motion-safe:animate-spin" /> Reconnecting…</p> : null}{error ? <p className="text-sm text-destructive" role="alert">{error}</p> : null}</> : undefined} /></div>
    </section>
    {projectDialogs}
    </>
  );

  const editor = <section className="flex min-w-0 flex-1 flex-col overflow-hidden border-l bg-muted/10" aria-label={productionPreview ? "Production video preview" : "Video editor"}>{productionPreview && selectedReadyVariant?.output_url ? <div className="flex min-h-0 flex-1 items-center justify-center bg-zinc-950 p-4"><video key={selectedReadyVariant.output_url} controls playsInline preload="metadata" poster={selectedReadyVariant.poster_url ?? undefined} src={selectedReadyVariant.output_url} className="max-h-full max-w-full rounded-lg shadow-2xl" data-testid="production-video-player">Your browser cannot play this video.</video></div> : editorUrl ? <iframe ref={editorFrameRef} src={editorUrl} title="Full video editor" className="min-h-0 flex-1 border-0 bg-background" /> : <div className="flex flex-1 items-center justify-center p-6 text-center text-sm text-muted-foreground">The editor will appear when your first cut is ready.</div>}</section>;

  const retentionNotice = galleryRetentionSummary ?? (galleryRetentionWarnings.length > 0 ? {
    affected_video_count: galleryRetentionWarnings.length,
    source_count: galleryRetentionWarnings.reduce((total, warning) => total + warning.source_count, 0),
    earliest_delete_at: galleryRetentionWarnings[0].delete_at,
    final_retention_days: galleryRetentionWarnings[0].final_retention_days,
  } : null);

  const sidebarShell = (
    <div
      ref={desktopSidebarRef}
      className={cn(
        "hidden h-full shrink-0 overflow-hidden md:block",
        "motion-safe:transition-[width] motion-safe:duration-[var(--t-accordion-dur)] motion-safe:ease-[var(--t-accordion-ease)]",
        sidebarHidden ? "pointer-events-none md:w-0" : "md:w-[260px]",
      )}
      data-state={sidebarHidden ? "closed" : "open"}
      data-testid="project-sidebar-shell"
      aria-hidden={sidebarHidden || undefined}
    >
      <div
        className={cn(
          "h-full w-[260px] motion-safe:transition-[transform,opacity] motion-safe:duration-[var(--t-accordion-dur)] motion-safe:ease-[var(--t-accordion-ease)]",
          sidebarHidden ? "md:-translate-x-full md:opacity-0" : "md:translate-x-0 md:opacity-100",
        )}
        data-testid="project-sidebar-panel"
      >
        {sidebar}
      </div>
    </div>
  );
  const collapsedProjectRail = sidebarHidden ? (
    <nav aria-label="Project navigation" className="hidden w-16 shrink-0 flex-col items-center border-r border-border bg-background pt-6 md:flex">
      <Button
        type="button"
        variant="ghost"
        size="icon"
        className="size-11 shrink-0"
        aria-label="Show project sidebar"
        title="Show projects"
        onClick={() => setSidebarHidden(false)}
      >
        <PanelLeftOpen aria-hidden="true" />
      </Button>
    </nav>
  ) : null;
  const projectSheet = (
    <Sheet open={projectsOpen} onOpenChange={setProjectsOpen}>
      <SheetContent side="left" className="w-[260px] p-0 sm:max-w-[260px]">
        <SheetHeader className="sr-only">
          <SheetTitle>Projects</SheetTitle>
          <SheetDescription>Move between creation projects and your gallery.</SheetDescription>
        </SheetHeader>
        {sidebar}
      </SheetContent>
    </Sheet>
  );

  if (galleryOpen) return (
    <div className="relative flex h-dvh min-h-0 overflow-hidden bg-background text-foreground">
      {sidebarShell}
      {collapsedProjectRail}
      {projectSheet}
      {projectDialogs}
      <section className="flex min-w-0 flex-1 flex-col gap-6 overflow-hidden px-4 py-6 sm:gap-8 sm:px-8 sm:py-10 lg:px-12 lg:py-14">
        {productionPreview ? <div className="flex shrink-0 items-center justify-center gap-2 border-b border-lime-300 bg-lime-50 px-4 py-2 text-center text-xs text-lime-950"><strong>Live production data</strong><span>Read-only playback</span></div> : null}
        <header className="flex shrink-0 flex-wrap items-end justify-between gap-4 sm:gap-6">
          <div className="flex min-w-0 items-end gap-3">
            <Button type="button" variant="ghost" size="icon" className="mb-1 size-11 shrink-0 md:hidden" aria-label="Open projects" onClick={() => setProjectsOpen(true)}><Menu /></Button>
            <div className="flex min-w-0 flex-col gap-2">
              <h1 className="font-display text-[40px] font-medium leading-[48px] text-[#30352C]">Gallery</h1>
              <p className="text-sm leading-[21px] text-muted-foreground">Finished videos and works in progress.</p>
              {productionPreview ? <p className="text-xs text-muted-foreground">{accountName} · {galleryJobs.length} recent videos</p> : null}
            </div>
          </div>
          <div className="flex shrink-0 items-center gap-2">
            <Button type="button" variant="ghost" className="min-h-11 px-3 text-sm" onClick={closeGallery}>Back to chat</Button>
            <Button
              type="button"
              className="h-11 gap-2 rounded-lg bg-[#FFF0A6] px-[18px] text-sm font-semibold text-[#30352C] hover:bg-[#FFE98A] hover:text-[#30352C]"
              disabled={productionPreview || (initialLoading && !thread) || busy || thinking || uploading}
              title={productionPreview ? "Production data is read-only in this preview." : undefined}
              onClick={() => void startNew()}
            >
              <Plus className="size-4" aria-hidden="true" />
              New video
            </Button>
          </div>
        </header>
        <main className="min-h-0 flex-1 overflow-y-auto">
          {retentionNotice ? <div className="mb-4 max-w-[1022px] rounded-lg border border-zinc-200 bg-zinc-50 px-4 py-3 text-sm text-zinc-800" role="status"><strong>Source-file retention notice.</strong> Editable source files for {retentionNotice.affected_video_count} inactive {retentionNotice.affected_video_count === 1 ? "video" : "videos"} are scheduled for removal as early as {new Date(retentionNotice.earliest_delete_at).toLocaleDateString()}. Your latest final video and poster remain under the {retentionNotice.final_retention_days}-day retention policy.</div> : null}
          {galleryLoading && galleryJobs.length === 0 ? <div className="py-16 text-center text-sm text-muted-foreground" role="status">Loading your videos…</div> : null}
          {galleryLoadError ? <div className="mb-4 flex max-w-md items-center justify-between gap-3 rounded-lg border border-border bg-muted/40 px-4 py-3 text-sm" role="alert"><span>{galleryLoadError}</span><Button type="button" variant="outline" className="min-h-11 shrink-0" onClick={() => void retryGalleryLoad()}>Retry</Button></div> : null}
          <ul className="flex max-w-[1022px] flex-wrap gap-[18px]">
            {galleryJobs.map((job) => {
              if (!productionPreview) return <li key={job.id} className="w-full sm:w-[calc((100%-18px)/2)] lg:w-[242px]"><LibraryTile job={job} title={productionLibraryTitle(job)} onDeleted={(jobId) => void handleGalleryJobDeleted(jobId)} onPosterLoadError={posterRecovery.onPosterLoadError} onPosterLoadSuccess={posterRecovery.onPosterLoadSuccess} posterRecoveryExhausted={posterRecovery.exhaustedJobIds.has(job.id)} posterRefreshUnavailable={posterRecovery.refreshUnavailableJobIds.has(job.id)} /></li>;
              const matchingProject = projects.find((project) => project.active_job_id === job.id || project.id === `${PRODUCTION_LIBRARY_THREAD_PREFIX}${job.id}`);
              return <li key={job.id} className="w-full sm:w-[calc((100%-18px)/2)] lg:w-[242px]"><ProductionPreviewVideoCard job={job} title={matchingProject ? projectTitle(matchingProject) : productionLibraryTitle(job)} /></li>;
            })}
          </ul>
          {galleryJobs.length === 0 && !galleryLoading && !galleryLoadError ? <p className="max-w-md py-16 text-center text-sm text-muted-foreground">Your finished cuts will appear here.</p> : null}
          {galleryCursor && !galleryLoadError ? <div className="flex justify-center py-8"><Button type="button" variant="outline" className="min-h-11" disabled={galleryLoading} onClick={() => void loadMoreGallery()}>{galleryLoading ? "Loading more videos…" : "Load more videos"}</Button></div> : null}
        </main>
      </section>
    </div>
  );

  return (
    <div className="relative flex h-dvh min-h-0 overflow-hidden bg-background text-foreground">
      {sidebarShell}
      {collapsedProjectRail}
      {projectSheet}
      <div className="flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden">
        {hasEditor && editorOpen ? (
          <div className="shrink-0 border-b p-2 lg:hidden">
            <Tabs value={mobileTab} onValueChange={(value) => setMobileTab(value as "chat" | "editor")}>
              <TabsList className="grid h-11 w-full grid-cols-2">
                <TabsTrigger value="chat">Chat</TabsTrigger>
                <TabsTrigger value="editor">Editor</TabsTrigger>
              </TabsList>
            </Tabs>
          </div>
        ) : null}
        <div className="flex min-h-0 flex-1 overflow-hidden">
          <div
            className={cn(
              "min-h-0 min-w-0 flex-1 flex-col overflow-hidden",
              hasEditor && editorOpen && "lg:flex-none lg:w-[420px]",
              hasEditor && mobileTab === "editor" ? "hidden lg:flex" : "flex",
            )}
          >
            {chat}
          </div>
          {hasEditor && editorOpen ? (
            <div className={cn("min-h-0 min-w-0 flex-1 overflow-hidden", mobileTab === "chat" ? "hidden lg:flex" : "flex")}>
              {editor}
            </div>
          ) : null}
        </div>
      </div>
    </div>
  );
}
