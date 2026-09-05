"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { useSession, signOut } from "next-auth/react";
import { useRouter, useSearchParams } from "next/navigation";
import {
  ArrowUp, Check, Download, Film, FolderOpen, Menu, MoreHorizontal, PanelLeftClose,
  PanelLeftOpen, Pencil, Play, Plus, RefreshCw, Sparkles, Trash2, WifiOff,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Dropzone } from "@/components/ui/dropzone";
import {
  AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent,
  AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import {
  Dialog, DialogClose, DialogContent, DialogDescription, DialogFooter,
  DialogHeader, DialogTitle,
} from "@/components/ui/dialog";
import {
  DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Input } from "@/components/ui/input";
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from "@/components/ui/sheet";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Textarea } from "@/components/ui/textarea";
import { ChatBubble } from "@/components/chat/ChatBubble";
import { ChatThinking } from "@/components/chat/ChatThinking";
import { ChatArtifactCard } from "@/components/chat/ChatArtifactCard";
import { BeamLoader } from "@/components/progress";
import { VoiceRecorder } from "@/app/generative/VoiceRecorder";
import {
  applyCreationAction, creationFormat, creationFormatLabel, creationJobFailed,
  creationJobPartial, creationJobReady, creationJobSettled, creationThreadMediaCount, createCreationThread,
  CreationThreadError, deleteCreationThread, getCreationCapabilities, listCreationThreads, refreshCreationThread,
  creationClipLimit, renameCreationThread, sendCreationMessage, threadMessages, uploadCreationMedia,
  type CreationFormat, type CreationThread,
} from "@/lib/creation-thread-api";
import { setChatFirstFallback } from "@/lib/chat-first";
import { cn } from "@/lib/cn";
import { listMyJobs, type LibraryJob } from "@/lib/me-api";
import { getPlanItemFresh, type PlanItem } from "@/lib/plan-api";
import LibraryTile from "@/components/library/LibraryTile";
import AssetPool from "@/app/plan/_components/AssetPool";

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

function projectStatusLabel(thread: CreationThread): string {
  if (thread.status === "archived") return "Archived";
  if (creationJobFailed(thread)) return "Needs attention";
  if (thread.job?.status === "variants_ready_partial") return "Partially ready";
  if (thread.job && ["done", "variants_ready"].includes(thread.job.status)) return "Ready";
  if (creationJobReady(thread)) return creationJobPartial(thread) ? "Partially ready" : "Ready";
  if (thread.active_job_id) return "Rendering";
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
  return thread?.job?.variants.filter((variant) => variant.render_status === "ready" && variant.output_url) ?? [];
}

function readyVariant(thread: CreationThread | null) {
  const variants = readyVariants(thread);
  const selectedId = typeof thread?.state.selected_variant_id === "string" ? thread.state.selected_variant_id : null;
  return variants.find((variant) => variant.variant_id === selectedId) ?? variants[0] ?? null;
}

function failedVariant(thread: CreationThread | null) {
  return thread?.job?.variants.find((variant) =>
    ["failed", "error", "render_failed"].includes(String(variant.render_status)) && variant.variant_id,
  ) ?? null;
}

function variantLabel(variantId: string | undefined): string {
  return (variantId ?? "Cut").replaceAll("_", " ").replace(/\b\w/g, (character) => character.toUpperCase());
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
    <article className="overflow-hidden rounded-xl border bg-card" data-testid={`production-video-${job.id}`}>
      <div className="relative aspect-[9/16] bg-zinc-950">
        {/* Signed production posters use dynamic hosts that cannot be allowlisted for next/image. */}
        {/* eslint-disable-next-line @next/next/no-img-element */}
        {job.poster_url ? <img src={job.poster_url} alt="" className="size-full object-cover" /> : null}
        {!job.poster_url ? <div className="flex size-full items-center justify-center px-4 text-center text-xs text-zinc-400">{job.status === "generating" ? "Rendering…" : job.status === "failed" ? "Render failed" : "Video ready"}</div> : null}
        {playable ? <Button type="button" size="icon" className="absolute inset-0 m-auto size-12 rounded-full" aria-label={`Play ${title}`} onClick={() => window.open(job.output_url ?? "", "_blank", "noopener,noreferrer")}><Play /></Button> : null}
      </div>
      <div className="space-y-1 p-3"><p className="truncate text-sm font-medium">{title}</p><p className="text-xs capitalize text-muted-foreground">{job.status} · {job.mode.replaceAll("_", " ")}</p></div>
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
  onRetry,
  onAdjust,
}: {
  thread: CreationThread;
  busy: boolean;
  readOnly?: boolean;
  onRetry: () => void;
  onAdjust: () => void;
}) {
  return (
    <ChatArtifactCard badge={<Badge variant="outline">Render needs attention</Badge>} title="Your project is safe" description="The render did not finish, but your direction and footage are still here.">
      <div className="flex flex-wrap gap-2"><Button type="button" onClick={onRetry} disabled={busy || readOnly}><RefreshCw /> Retry render</Button><Button type="button" variant="outline" onClick={onAdjust} disabled={readOnly}>Adjust direction</Button></div>
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
    <ChatArtifactCard badge={<Badge variant="secondary"><Check /> {isPartial ? "Partially ready" : "Ready"}</Badge>} title={isPartial ? "Your cut is ready; one variant needs another pass" : "Your cut is ready"} description={isPartial ? "The playable cut is available now. Retry the failed variant whenever you’re ready." : "Play it here, download it, open the editor, or keep chatting for a confirmed revision."}>
      {readyVariants(thread).length > 1 ? <div className="mb-3 flex flex-wrap gap-2" role="group" aria-label="Available cuts">{readyVariants(thread).map((variant) => <Button key={variant.variant_id} type="button" variant={selectedReadyVariant?.variant_id === variant.variant_id ? "secondary" : "outline"} aria-pressed={selectedReadyVariant?.variant_id === variant.variant_id} disabled={busy || readOnly} onClick={() => onSelectVariant(variant.variant_id ?? "")}>{variantLabel(variant.variant_id)}</Button>)}</div> : null}
      <div className="flex flex-wrap gap-2">{selectedReadyVariant?.output_url ? <><Button type="button" onClick={() => window.open(selectedReadyVariant.output_url ?? "", "_blank", "noopener,noreferrer")}><Play /> Play</Button><Button type="button" variant="outline" asChild><a href={selectedReadyVariant.output_url ?? ""} download><Download /> Download</a></Button></> : null}<Button type="button" variant="outline" onClick={onOpenEditor} disabled={!thread.active_plan_item_id && !readOnly}><Pencil /> {readOnly ? "View video" : "Open editor"}</Button>{isPartial && selectedFailedVariant?.variant_id ? <Button type="button" variant="ghost" onClick={() => onRetryVariant(selectedFailedVariant.variant_id ?? "")} disabled={busy || readOnly}><RefreshCw /> Retry failed variant</Button> : null}</div>
    </ChatArtifactCard>
  );
}

export interface ChatCreationWorkspaceProps {
  onLegacyFallback?: () => void;
  /** When present, hydrate this exact project instead of choosing the latest. */
  initialThreadId?: string;
  /** Preview-only mode: hydrate the signed-in account's production data, while
   * keeping every server mutation disabled. Rename/delete remain local demos. */
  productionPreview?: boolean;
}

export default function ChatCreationWorkspace({
  onLegacyFallback,
  initialThreadId,
  productionPreview = false,
}: ChatCreationWorkspaceProps) {
  const { data: session } = useSession();
  const router = useRouter();
  const searchParams = useSearchParams();
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
  const [deleteTarget, setDeleteTarget] = useState<CreationThread | null>(null);
  const [projectActionBusy, setProjectActionBusy] = useState(false);
  const [projectActionError, setProjectActionError] = useState<string | null>(null);
  const [galleryJobs, setGalleryJobs] = useState<LibraryJob[]>([]);
  const [formatPickerOpen, setFormatPickerOpen] = useState(false);
  const [availableFormats, setAvailableFormats] = useState<CreationFormat[]>(["montage", "narrated_planned", "subtitled"]);
  const [capabilities, setCapabilities] = useState<Awaited<ReturnType<typeof getCreationCapabilities>>>(() => ({ formats: [] }));
  const visualsEnabled = process.env.NEXT_PUBLIC_OVERLAY_AUTOPLACE_ENABLED === "true"
    || process.env.NEXT_PUBLIC_GUIDED_EDIT_ENABLED === "true";
  const editorFrameRef = useRef<HTMLIFrameElement>(null);
  const transcriptRef = useRef<HTMLDivElement>(null);
  const latestMessageRef = useRef<HTMLDivElement>(null);
  const queuedMessageRef = useRef<{ threadId: string; message: string } | null>(null);
  const preparedRevisionRef = useRef<string | null>(null);
  const activeThreadIdRef = useRef<string | null>(null);
  const loadStartedRef = useRef(false);
  const productionGalleryLoadedRef = useRef(false);

  const activateThread = useCallback((next: CreationThread) => {
    activeThreadIdRef.current = next.id;
    setThreadUnavailable(false);
    setThread(next);
  }, []);

  const acceptThreadResponse = useCallback((expectedId: string, next: CreationThread) => {
    if (activeThreadIdRef.current === expectedId) setThread(next);
  }, []);

  const load = useCallback(async () => {
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
        productionGalleryLoadedRef.current = true;
        setGalleryJobs(library.jobs);
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
        activateThread(next);
        setChatFirstFallback(false);
        window.dispatchEvent(new CustomEvent("nova:chat-first-ready"));
        return;
      }
      const [listed, capabilities] = await Promise.all([listCreationThreads(), getCreationCapabilities()]);
      setAvailableFormats(capabilities.formats.map((item) => item.edit_format));
      setCapabilities(capabilities);
      setProjects(listed);
      const requestedId = initialThreadId?.trim() || null;
      const current = thread ? listed.find((item) => item.id === thread.id) : null;
      const summary = requestedId
        ? listed.find((item) => item.id === requestedId)
        : current ?? listed.find((item) => item.status === "active");
      const next = requestedId
        ? await refreshCreationThread(requestedId)
        : summary ? await refreshCreationThread(summary.id) : await createCreationThread();
      activateThread(next);
      if (!requestedId) router.replace(`/plan/${next.id}`, { scroll: false });
      setChatFirstFallback(false);
      window.dispatchEvent(new CustomEvent("nova:chat-first-ready"));
      if (!current && !listed.some((item) => item.id === next.id)) setProjects((items) => [next, ...items]);
    } catch (cause) {
      if (initialThreadId && cause instanceof CreationThreadError && cause.status === 404) {
        activeThreadIdRef.current = null;
        setThread(null);
        setThreadUnavailable(true);
        return;
      }
      if (cause instanceof CreationThreadError && cause.status === 404) {
        setChatFirstFallback(true);
        window.dispatchEvent(new CustomEvent("nova:chat-first-fallback"));
        onLegacyFallback?.();
        return;
      }
      setError("I couldn’t open this creation chat. Check your connection and try again.");
    }
  }, [activateThread, initialThreadId, onLegacyFallback, productionPreview, router, thread]);

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
        void refreshCreationThread(currentId)
          .then((next) => acceptThreadResponse(currentId, next))
          .catch(() => setError("Your editor save started, but I couldn’t refresh the render yet. Reconnecting…"));
      }
    };
    window.addEventListener("message", onEditorMessage);
    return () => window.removeEventListener("message", onEditorMessage);
  }, [acceptThreadResponse]);

  useEffect(() => {
    if (!thread?.active_job_id) return;
    let cancelled = false;
    let timer: number | undefined;
    const poll = async () => {
      try {
        const next = await refreshCreationThread(thread.id);
        if (cancelled || activeThreadIdRef.current !== thread.id) return;
        setPollReconnecting(false);
        setThread(next);
        if (!creationJobSettled(next)) timer = window.setTimeout(() => void poll(), 2500);
      } catch {
        if (!cancelled && activeThreadIdRef.current === thread.id) {
          setPollReconnecting(true);
          timer = window.setTimeout(() => void poll(), 5000);
        }
      }
    };
    void poll();
    return () => { cancelled = true; if (timer) window.clearTimeout(timer); };
  }, [thread?.active_job_id, thread?.id]);

  useEffect(() => {
    if (!galleryOpen) return;
    if (productionPreview && productionGalleryLoadedRef.current) return;
    void listMyJobs({ limit: productionPreview ? 24 : undefined })
      .then((page) => {
        if (productionPreview) productionGalleryLoadedRef.current = true;
        setGalleryJobs(page.jobs);
      })
      .catch(() => setError("I couldn’t load your Gallery. Your saved videos are still safe."));
  }, [galleryOpen, productionPreview]);

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
    void applyCreationAction(thread, "revise", { intent }, `revision-${thread.id}-${jobId}`)
      .then((next) => acceptThreadResponse(thread.id, next))
      .catch(() => {
        preparedRevisionRef.current = null;
        setError("Your revision is ready to review, but I couldn’t prepare it yet. Try again.");
      });
  }, [acceptThreadResponse, productionPreview, thread]);

  const format = formatFromThread(thread);
  const clipLimit = creationClipLimit(capabilities.formats, format);
  const mediaCount = creationThreadMediaCount(thread);
  const hasReady = Boolean(thread && creationJobReady(thread));
  const isPartial = Boolean(thread && creationJobPartial(thread));
  const eventMessages = useMemo(() => thread ? threadMessages(thread) : [], [thread]);
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
  const canConfirmDirection = !thread?.active_job_id || Boolean(thread && creationJobFailed(thread));
  const media = useMemo(() => attachedMedia(thread), [thread]);
  const variantStillRendering = Boolean(thread?.job?.variants.some((variant) => variant.render_status === "rendering"));
  const messages = useMemo(() => {
    if (!thread) return eventMessages;
    let lifecycleMessageId: string | null = null;
    const rows = eventMessages.map((message) => {
      if (!["progress", "result", "failure"].includes(String(message.artifact))) return message;
      lifecycleMessageId = message.id;
      return { ...message, artifact: undefined };
    });
    const synthetic = (artifact: NonNullable<(typeof eventMessages)[number]["artifact"]>, suffix: string) => ({
      id: `${thread.id}:${suffix}`,
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
    const lifecycleArtifact = creationJobFailed(thread) && !hasPendingConfirmation
      ? "failure"
      : thread.active_job_id && (!creationJobSettled(thread) || variantStillRendering)
        ? "progress"
        : hasReady ? "result" : null;
    if (lifecycleArtifact) {
      const lifecycleIndex = lifecycleMessageId
        ? rows.findIndex((message) => message.id === lifecycleMessageId)
        : -1;
      if (lifecycleIndex >= 0) {
        rows[lifecycleIndex] = { ...rows[lifecycleIndex], artifact: lifecycleArtifact };
      } else {
        insertAfter(lifecycleAnchor, synthetic(lifecycleArtifact, "lifecycle"));
      }
    }
    return rows;
  }, [eventMessages, format, formatPickerOpen, hasPendingConfirmation, hasReady, productionPreview, thread, variantStillRendering]);
  const lastMessageId = messages[messages.length - 1]?.id;
  const latestAudio = [...media].reverse().find((item) => item.kind === "audio") ?? null;
  const clipMedia = media.filter((item) => item.kind === "video");
  const clipCount = clipMedia.length || (media.length === 0 ? mediaCount : 0);
  const accountName = session?.user?.name ?? session?.user?.email ?? "Account";
  const headerSubtitle = productionPreview && isProductionLibraryThread(thread)
    ? `${projectStatusLabel(thread!)} · ${String(thread?.state.production_mode ?? "Kria").replaceAll("_", " ")}`
    : format
      ? `${creationFormatLabel(format)} · ${clipCount} ${clipCount === 1 ? "clip" : "clips"}`
      : "Start with a format, then tell me what you’re imagining";

  useEffect(() => {
    const transcript = transcriptRef.current;
    if (!transcript) return;
    latestMessageRef.current?.scrollIntoView?.({ block: "end" });
    transcript.scrollTop = transcript.scrollHeight;
  }, [hasReady, lastMessageId, thinking, thread?.id, variantStillRendering]);

  async function selectFormat(value: CreationFormat) {
    if (productionPreview || !thread || busy || (format && !formatPickerOpen) || !availableFormats.includes(value)) return;
    const threadId = thread.id;
    setBusy(true); setError(null);
    const paperFormat = value === "narrated_planned" ? "narrated" : value === "subtitled" ? "talking_to_camera" : "montage";
    try {
      const next = await applyCreationAction(thread, "select_format", { format: paperFormat });
      acceptThreadResponse(threadId, next);
      if (activeThreadIdRef.current === threadId) setFormatPickerOpen(false);
    }
    catch { setError("I couldn’t set that format. Try again in a moment."); }
    finally { setBusy(false); }
  }

  async function selectVariant(variantId: string) {
    if (productionPreview || !thread || busy) return;
    const threadId = thread.id;
    setBusy(true); setError(null);
    try { acceptThreadResponse(threadId, await applyCreationAction(thread, "select_variant", { variant_id: variantId })); }
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
      const next = await uploadCreationMedia(sourceThread, chosen, (progress, file) => {
        acceptThreadResponse(sourceThread.id, progress);
        if (activeThreadIdRef.current === sourceThread.id) {
          setPendingFiles((items) => items.filter((item) => item !== file));
        }
      });
      acceptThreadResponse(sourceThread.id, next);
    } catch (cause) {
      if (cause instanceof CreationThreadError && cause.status === 409 && activeThreadIdRef.current === sourceThread.id) {
        void refreshCreationThread(sourceThread.id).then((next) => acceptThreadResponse(sourceThread.id, next));
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
      const next = await uploadCreationMedia(sourceThread, [file]);
      acceptThreadResponse(sourceThread.id, next);
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
    const next = await uploadCreationMedia(sourceThread, [recording]);
    acceptThreadResponse(sourceThread.id, next);
    return { gcs_path: "creation-thread-media", kind: "audio" };
  }

  async function removeMedia(mediaId: string) {
    if (productionPreview || !thread || busy || uploading) return;
    const sourceThread = thread;
    setBusy(true); setError(null);
    try {
      acceptThreadResponse(sourceThread.id, await applyCreationAction(sourceThread, "remove_media", { media_id: mediaId }));
    } catch {
      setError("I couldn’t remove that file. Refresh the project and try again.");
    } finally { setBusy(false); }
  }

  const submitMessage = useCallback(async (sourceThread: CreationThread, message: string) => {
    setInput(""); setThinking(true); setError(null);
    try { acceptThreadResponse(sourceThread.id, await sendCreationMessage(sourceThread, message)); }
    catch (cause) {
      setInput(message);
      if (cause instanceof CreationThreadError && cause.status === 409) {
        try {
          const latest = await refreshCreationThread(sourceThread.id);
          acceptThreadResponse(sourceThread.id, latest);
          setError("This chat changed in another window. Your draft is still here; review the latest direction and send again.");
        } catch {
          setError("This chat changed in another window. Refresh the project, then send again.");
        }
      } else {
        setError("I couldn’t send that message. Your draft is still here; try again.");
      }
    }
    finally { setThinking(false); }
  }, [acceptThreadResponse]);

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
    const sourceThread = thread;
    setBusy(true); setError(null);
    try { acceptThreadResponse(sourceThread.id, await applyCreationAction(sourceThread, action, payload)); }
    catch { setError("I couldn’t start that render. Your project is safe—adjust the direction or try again."); }
    finally { setBusy(false); }
  }

  function beginRename(project: CreationThread) {
    setRenameTarget(project);
    setRenameValue(projectTitle(project));
    setProjectActionError(null);
  }

  async function renameProject() {
    const target = renameTarget;
    const name = renameValue.trim();
    if (!target || !name || projectActionBusy) return;
    setProjectActionBusy(true);
    setProjectActionError(null);
    setError(null);
    if (productionPreview) {
      const next = { ...target, title: name };
      setProjects((items) => items.map((item) => item.id === target.id ? next : item));
      setThread((current) => current?.id === target.id ? next : current);
      setRenameTarget(null);
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
      setError("I couldn’t rename that project. Try again in a moment.");
    } finally {
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

  async function startNew() {
    if (productionPreview || busy || thinking || uploading) return;
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
      const hydrated = await refreshCreationThread(project.id);
      acceptThreadResponse(project.id, productionPreview
        ? { ...hydrated, title: inferredProductionTitle(hydrated) }
        : hydrated);
    }
    catch { if (activeThreadIdRef.current === project.id) setError("I couldn’t open that project."); }
  }

  function openGallery() {
    setProjectsOpen(false);
    setGalleryOpen(true);
    router.replace(
      productionPreview
        ? "/dev-qa/chat-first-creation?live=1&view=gallery"
        : `${thread ? `/plan/${thread.id}` : "/plan"}?view=gallery`,
      { scroll: false },
    );
  }

  function closeGallery() {
    setGalleryOpen(false);
    router.replace(
      productionPreview
        ? `/dev-qa/chat-first-creation?live=1${thread ? `&project=${encodeURIComponent(thread.id)}` : ""}`
        : thread ? `/plan/${thread.id}` : "/plan",
      { scroll: false },
    );
  }

  const selectedReadyVariant = readyVariant(thread);
  const selectedFailedVariant = failedVariant(thread);
  const editorUrl = !productionPreview && thread?.active_plan_item_id
    ? `/plan/items/${thread.active_plan_item_id}/edit?embedded=1${selectedReadyVariant?.variant_id ? `&variant=${selectedReadyVariant.variant_id}` : ""}`
    : null;

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
    <aside className="flex h-full w-[260px] shrink-0 flex-col border-r border-border bg-background p-4" aria-label="Projects">
      <div className="flex items-center justify-between px-2">
        <span className="flex items-center gap-2 text-lg font-semibold"><Sparkles className="size-4" /> Kria</span>
        <Button type="button" variant="ghost" size="icon" className="size-9" aria-label="Hide project sidebar" onClick={() => setSidebarHidden(true)}><PanelLeftClose /></Button>
      </div>
      <Button type="button" className="mt-6 min-h-11 justify-start" disabled={productionPreview || busy || thinking || uploading} title={productionPreview ? "Production data is read-only in this preview." : undefined} onClick={() => void startNew()}><Film /> New video</Button>
      <div className="mt-8 flex items-center justify-between px-2"><p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Projects</p><Button type="button" variant="ghost" className="h-auto p-0 text-xs text-muted-foreground hover:text-foreground" disabled={busy || thinking || uploading} onClick={openGallery}>Gallery</Button></div>
      <nav className="mt-2 space-y-1 overflow-y-auto" aria-label="Recent projects">
        {projects.slice(0, 10).map((project) => {
          const title = projectTitle(project);
          return (
            <div key={project.id} className="flex min-w-0 items-center gap-1">
              <Button type="button" variant={project.id === thread?.id ? "secondary" : "ghost"} className="h-auto min-h-11 min-w-0 flex-1 justify-start text-left" disabled={busy || thinking || uploading} onClick={() => void openProject(project)}><FolderOpen className="shrink-0" /><span className="min-w-0"><span className="block truncate">{title}</span><span className="block truncate text-[11px] font-normal text-muted-foreground">{projectStatusLabel(project)}</span></span></Button>
              <DropdownMenu>
                <DropdownMenuTrigger asChild><Button type="button" variant="ghost" size="icon" className="size-9 shrink-0" aria-label={`Project actions for ${title}`} disabled={busy || thinking || uploading}><MoreHorizontal /></Button></DropdownMenuTrigger>
                <DropdownMenuContent align="end">
                  <DropdownMenuItem onSelect={() => beginRename(project)}>Rename project{productionPreview ? " (preview)" : ""}</DropdownMenuItem>
                  <DropdownMenuSeparator />
                  <DropdownMenuItem className="text-destructive focus:text-destructive" disabled={projectDeletionBlocked(project)} title={projectDeletionBlocked(project) ? "Finish the active render before deleting this project." : undefined} onSelect={() => setDeleteTarget(project)}>{projectDeletionBlocked(project) ? "Delete after rendering" : `Delete project${productionPreview ? " (preview)" : ""}`}</DropdownMenuItem>
                </DropdownMenuContent>
              </DropdownMenu>
            </div>
          );
        })}
      </nav>
      <div className="mt-auto border-t pt-4">
        <p className="truncate text-sm font-medium">{accountName}</p>
        <div className="mt-2 flex items-center gap-1"><Button type="button" variant="ghost" className="h-auto p-0 text-xs text-muted-foreground hover:text-foreground" onClick={openGallery}>My videos</Button><span className="text-muted-foreground">·</span><Button type="button" variant="ghost" className="h-auto p-0 text-xs text-muted-foreground hover:text-foreground" onClick={() => void signOut({ callbackUrl: "/" })}>Sign out</Button></div>
      </div>
    </aside>
  );

  const formatArtifact = (
    <ChatArtifactCard title="What are you making?" description="Pick a starting point. You can shape the creative direction together in chat.">
      <div className={cn(
        "gap-3",
        hasReady && editorOpen
          ? "grid grid-cols-1"
          : "grid grid-flow-col auto-cols-[minmax(220px,85%)] snap-x overflow-x-auto sm:grid-flow-row sm:auto-cols-auto sm:grid-cols-3 sm:overflow-visible",
      )}>
        {FORMATS.map((item) => <Button key={item.value} type="button" variant="outline" disabled={productionPreview || busy || (Boolean(format) && !formatPickerOpen) || !availableFormats.includes(item.value)} className={cn("h-auto min-h-[96px] snap-start flex-col items-start justify-start whitespace-normal p-4 text-left", format === item.value && "border-primary ring-1 ring-primary", !availableFormats.includes(item.value) && "opacity-60")} onClick={() => void selectFormat(item.value)}><span className="font-medium">{item.label}</span><span className="mt-1 text-xs font-normal text-muted-foreground">{availableFormats.includes(item.value) ? item.description : "Temporarily unavailable — choose another format."}</span></Button>)}
      </div>
    </ChatArtifactCard>
  );

  const uploadArtifact = (
    <ChatArtifactCard title={format ? FORMAT_GUIDANCE[format].title : "Add clips"} description={format ? FORMAT_GUIDANCE[format].description : "Choose the footage for your story."}>
      {!productionPreview && format === "narrated_planned" && !latestAudio ? <VoiceRecorder upload={uploadRecordedVoice} onVoiceover={() => undefined} /> : null}
      {format ? <Button type="button" variant="ghost" className="px-0 text-xs text-muted-foreground" disabled={productionPreview} onClick={() => setFormatPickerOpen(true)}>Change format</Button> : null}
      <Dropzone compact accept="video/*" multiple={format !== "subtitled"} disabled={productionPreview || uploading || clipCount >= clipLimit} title={productionPreview ? "Uploads are disabled in this read-only preview" : uploading ? "Uploading…" : clipCount >= clipLimit ? "Clip limit reached" : format === "subtitled" ? "Choose a clip or drop it here" : "Choose clips or drop them here"} subline={format === "subtitled" ? undefined : `Up to ${clipLimit} clips`} ariaLabel="Add primary video clips" inputAriaLabel="Upload primary video clips" onFiles={(files) => void attach(files)} />
      {pendingFiles.length > 0 ? <div className="mt-2 space-y-1">{pendingFiles.map((file) => <div key={`${file.name}-${file.size}`} className="flex items-center justify-between gap-2 rounded-md bg-muted px-2 py-1 text-xs"><span className="truncate">{file.name}</span><div className="flex shrink-0 items-center gap-1"><Button type="button" variant="ghost" size="sm" className="h-7 px-2" disabled={uploading} onClick={() => void retryFile(file)}>Retry</Button><Button type="button" variant="ghost" size="icon" className="size-7" aria-label={`Remove ${file.name}`} onClick={() => setPendingFiles((items) => items.filter((item) => item !== file))}><Trash2 className="size-3" /></Button></div></div>)}</div> : null}
      {media.length > 0 && !thread?.active_job_id ? <div className="mt-2 space-y-1" role="list">{media.map((item) => <div key={item.media_id} className="flex items-center justify-between gap-2 rounded-md bg-muted px-3 py-2 text-sm" role="listitem"><span className="min-w-0 truncate">{item.filename}{item.kind === "audio" ? <span className="ml-2 text-xs text-muted-foreground">Voiceover</span> : null}</span><Button type="button" variant="ghost" size="icon" className="size-8 shrink-0" disabled={productionPreview || busy || uploading} aria-label={`Remove attached ${item.filename}`} onClick={() => void removeMedia(item.media_id)}><Trash2 className="size-4" /></Button></div>)}</div> : null}
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
      <Dialog open={Boolean(renameTarget)} onOpenChange={(open) => { if (!open && !projectActionBusy) setRenameTarget(null); }}>
        <DialogContent>
          <DialogHeader><DialogTitle>Rename project{productionPreview ? " in preview" : ""}</DialogTitle><DialogDescription>{productionPreview ? "This changes only this browser preview and resets on reload. Your production project is untouched." : "Choose a short name you’ll recognize in your project list."}</DialogDescription></DialogHeader>
          <form onSubmit={(event) => { event.preventDefault(); void renameProject(); }} className="space-y-4">
            <Input aria-label="Project name" value={renameValue} maxLength={120} onChange={(event) => setRenameValue(event.target.value)} autoFocus aria-describedby={projectActionError ? "project-action-error" : undefined} />
            {projectActionError ? <p id="project-action-error" className="text-sm text-destructive" role="alert">{projectActionError}</p> : null}
            <DialogFooter><DialogClose asChild><Button type="button" variant="outline" disabled={projectActionBusy}>Cancel</Button></DialogClose><Button type="submit" disabled={!renameValue.trim() || projectActionBusy}>{projectActionBusy ? "Saving…" : "Save name"}</Button></DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
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
      <header className={cn("flex h-14 shrink-0 items-center justify-between border-b px-4 sm:px-6", sidebarHidden && "md:pl-14")}>
          <div className="min-w-0"><h1 data-testid="project-title" className="truncate font-display text-xl font-medium">{thread ? projectTitle(thread) : "Loading project…"}</h1><p className="truncate text-xs capitalize text-muted-foreground">{headerSubtitle}</p></div>
        <div className="flex items-center gap-1">
          {sidebarHidden ? <Button type="button" variant="ghost" size="icon" className="size-11" aria-label="Show project sidebar" onClick={() => setSidebarHidden(false)}><PanelLeftOpen /></Button> : null}
          <Button type="button" variant="ghost" size="icon" className="size-11 md:hidden" aria-label="Open projects" onClick={() => setProjectsOpen(true)}><Menu /></Button>
        </div>
      </header>
      <div ref={transcriptRef} role="log" aria-label="Conversation history" aria-live="polite" aria-relevant="additions text" tabIndex={0} className="min-h-0 flex-1 touch-pan-y overflow-y-auto overscroll-y-contain [scrollbar-gutter:stable]"><div className="mx-auto flex w-full max-w-2xl flex-col gap-4 px-4 py-6 sm:px-8">
        {!thread ? <div className="space-y-3" role="status"><div className="h-5 w-40 motion-safe:animate-pulse rounded bg-muted" /><div className="h-20 w-full motion-safe:animate-pulse rounded bg-muted" /></div> : null}
        {messages.map((message, index) => <div key={message.id} ref={index === messages.length - 1 ? latestMessageRef : undefined} className="space-y-3">{message.content ? <ChatBubble role={message.role}>{message.content}</ChatBubble> : null}{message.artifact === "format" && (!format || formatPickerOpen) ? formatArtifact : null}{message.artifact === "upload" && !thread?.active_job_id ? <>{uploadArtifact}{visualsArtifact}</> : null}{message.artifact === "voiceover" && !thread?.active_job_id ? uploadArtifact : null}{(message.artifact === "confirmation" || (message.artifact === "revision" && !hasReady)) && canConfirmDirection ? <ChatArtifactCard badge={<Badge variant="secondary">Creative direction</Badge>} title={`${creationFormatLabel(format)} is ready to make`} description={typeof thread?.state.intent === "string" && thread.state.intent ? thread.state.intent : "I’ll find the strongest opening and shape your footage into a concise first cut."}><Button type="button" className="min-h-11 w-full" disabled={productionPreview || busy || clipCount === 0} onClick={() => void confirm("generate")}><Sparkles />{busy ? "Starting…" : "Create this video"}</Button></ChatArtifactCard> : null}{message.artifact === "revision" && hasReady ? <ChatArtifactCard badge={<Badge variant="secondary">Revision ready</Badge>} title="Apply this direction?" description="This creates a new generation from the finished cut."><Button type="button" className="min-h-11 w-full" disabled={productionPreview || busy} onClick={() => void confirm("generate", { base_generation: thread?.job?.id })}><RefreshCw /> Create revision</Button></ChatArtifactCard> : null}{message.artifact === "progress" && thread?.active_job_id && !creationJobFailed(thread) ? <RenderStatusCard thread={thread} /> : null}{message.artifact === "failure" && thread && creationJobFailed(thread) && !hasPendingConfirmation ? <FailureStatusCard thread={thread} busy={busy} readOnly={productionPreview} onRetry={() => void confirm("retry")} onAdjust={() => setInput("Try a different opening and keep the pacing quick.")} /> : null}{message.artifact === "result" && thread && hasReady ? <ReadyStatusCard thread={thread} isPartial={isPartial} selectedReadyVariant={selectedReadyVariant} selectedFailedVariant={selectedFailedVariant} busy={busy} readOnly={productionPreview} onSelectVariant={(id) => void selectVariant(id)} onOpenEditor={() => { setEditorOpen(true); setMobileTab("editor"); }} onRetryVariant={(id) => void confirm("retry", { variant_id: id })} /> : null}</div>)}
        {thinking ? <ChatThinking /> : null}
      </div></div>
      <div className="shrink-0 border-t bg-background p-3 pb-[max(0.75rem,env(safe-area-inset-bottom))] sm:p-4"><form className="mx-auto flex max-w-2xl items-end gap-2 rounded-2xl border bg-background p-2 shadow-sm focus-within:ring-1 focus-within:ring-ring" onSubmit={(event) => { event.preventDefault(); void send(); }}><Button type="button" variant="ghost" size="icon" className="size-11 shrink-0 rounded-full" aria-label="Attach primary video clips" disabled={productionPreview || !thread || uploading || Boolean(thread?.active_job_id) || clipCount >= clipLimit} onClick={() => document.getElementById("creation-file-picker")?.click()}><Plus /></Button><input id="creation-file-picker" type="file" className="sr-only" accept="video/*" multiple={format !== "subtitled"} disabled={productionPreview} onChange={(event) => { void attach(event.target.files); event.target.value = ""; }} /><Textarea value={input} onChange={(event) => setInput(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); void send(); } }} placeholder={productionPreview ? "Read-only production preview" : "Tell Kria what you’re imagining…"} aria-label="Message Kria" rows={1} disabled={productionPreview} className="max-h-32 min-h-11 resize-none border-0 bg-transparent py-3 shadow-none focus-visible:ring-0" /><Button type="submit" size="icon" className="size-11 shrink-0 rounded-full" aria-label="Send message" disabled={productionPreview || !input.trim() || thinking || !thread}><ArrowUp /></Button></form>{offline ? <p className="mx-auto mt-2 flex max-w-2xl items-center gap-1 text-xs text-muted-foreground" role="status"><WifiOff className="size-3" /> Offline — messages stay in the composer until you reconnect.</p> : null}{pollReconnecting ? <p className="mx-auto mt-2 flex max-w-2xl items-center gap-1 text-xs text-muted-foreground" role="status"><RefreshCw className="size-3 motion-safe:animate-spin" /> Reconnecting to render status…</p> : null}{error ? <p className="mx-auto mt-2 max-w-2xl text-sm text-destructive" role="alert">{error}</p> : null}</div>
    </section>
    {projectDialogs}
    </>
  );

  const editor = <section className="flex min-w-0 flex-1 flex-col overflow-hidden border-l bg-muted/10" aria-label={productionPreview ? "Production video preview" : "Video editor"}><header className="flex h-14 shrink-0 items-center justify-between border-b bg-background px-4"><div><p className="text-sm font-medium">{productionPreview ? "Production video" : "Editor"}</p><p className="text-xs text-muted-foreground">{productionPreview ? "Real output · read-only playback" : "Feature-complete overlay editor"}</p></div><Badge variant="secondary"><Check /> Ready</Badge></header>{productionPreview && selectedReadyVariant?.output_url ? <div className="flex min-h-0 flex-1 items-center justify-center bg-zinc-950 p-4"><video key={selectedReadyVariant.output_url} controls playsInline preload="metadata" poster={selectedReadyVariant.poster_url ?? undefined} src={selectedReadyVariant.output_url} className="max-h-full max-w-full rounded-lg shadow-2xl" data-testid="production-video-player">Your browser cannot play this video.</video></div> : editorUrl ? <iframe ref={editorFrameRef} src={editorUrl} title="Full video editor" className="min-h-0 flex-1 border-0 bg-background" /> : <div className="flex flex-1 items-center justify-center p-6 text-center text-sm text-muted-foreground">The editor will appear when your first cut is ready.</div>}</section>;

  if (galleryOpen) return <div className="flex h-dvh flex-col overflow-hidden bg-background">{productionPreview ? <div className="border-b border-lime-300 bg-lime-50 px-4 py-2 text-center text-xs text-lime-950"><strong>Live production data</strong> · Read-only playback</div> : null}<header className="flex h-14 shrink-0 items-center justify-between border-b px-4"><div><h1 className="text-lg font-semibold">Gallery</h1>{productionPreview ? <p className="text-xs text-muted-foreground">{accountName} · {galleryJobs.length} recent videos</p> : null}</div><Button type="button" onClick={closeGallery}>Back to chat</Button></header><main className="min-h-0 flex-1 overflow-y-auto p-6"><ul className="mx-auto grid max-w-5xl grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-4">{galleryJobs.map((job) => {
    if (!productionPreview) return <li key={job.id}><LibraryTile job={job} /></li>;
    const matchingProject = projects.find((project) => project.active_job_id === job.id || project.id === `${PRODUCTION_LIBRARY_THREAD_PREFIX}${job.id}`);
    return <li key={job.id}><ProductionPreviewVideoCard job={job} title={matchingProject ? projectTitle(matchingProject) : productionLibraryTitle(job)} /></li>;
  })}</ul>{galleryJobs.length === 0 ? <p className="mx-auto max-w-md py-16 text-center text-sm text-muted-foreground">Your finished cuts will appear here.</p> : null}</main></div>;

  return <div className="relative flex h-dvh min-h-0 overflow-hidden bg-background text-foreground"><div className={cn("hidden md:block", sidebarHidden && "md:hidden")}>{sidebar}</div><Sheet open={projectsOpen} onOpenChange={setProjectsOpen}><SheetContent side="left" className="w-[260px] p-0 sm:max-w-[260px]"><SheetHeader className="sr-only"><SheetTitle>Projects</SheetTitle><SheetDescription>Move between creation projects and your gallery.</SheetDescription></SheetHeader>{sidebar}</SheetContent></Sheet><div className="flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden">{hasReady && editorOpen ? <div className="shrink-0 border-b p-2 lg:hidden"><Tabs value={mobileTab} onValueChange={(value) => setMobileTab(value as "chat" | "editor")}><TabsList className="grid h-11 w-full grid-cols-2"><TabsTrigger value="chat">Chat</TabsTrigger><TabsTrigger value="editor">Editor</TabsTrigger></TabsList></Tabs></div> : null}<div className="flex min-h-0 flex-1 overflow-hidden"><div className={cn("min-h-0 min-w-0 flex-1 flex-col overflow-hidden", hasReady && editorOpen && "lg:flex-none lg:w-[420px]", hasReady && mobileTab === "editor" ? "hidden lg:flex" : "flex")}>{chat}</div>{hasReady && editorOpen ? <div className={cn("min-h-0 min-w-0 flex-1 overflow-hidden", mobileTab === "chat" ? "hidden lg:flex" : "flex")}>{editor}</div> : null}</div></div></div>;
}
