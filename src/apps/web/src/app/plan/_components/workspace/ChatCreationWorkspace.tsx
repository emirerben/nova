"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { useSession, signOut } from "next-auth/react";
import { useRouter, useSearchParams } from "next/navigation";
import {
  ArrowUp, Check, Download, Film, FolderOpen, Menu, PanelLeftClose,
  PanelLeftOpen, Pencil, Play, Plus, RefreshCw, Sparkles, Trash2, WifiOff,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Dropzone } from "@/components/ui/dropzone";
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from "@/components/ui/sheet";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Textarea } from "@/components/ui/textarea";
import { ChatBubble } from "@/components/chat/ChatBubble";
import { ChatThinking } from "@/components/chat/ChatThinking";
import { ChatArtifactCard } from "@/components/chat/ChatArtifactCard";
import { VoiceRecorder } from "@/app/generative/VoiceRecorder";
import {
  applyCreationAction, creationFormat, creationFormatLabel, creationJobFailed,
  creationJobPartial, creationJobReady, creationJobSettled, creationThreadMediaCount, createCreationThread,
  CreationThreadError, getCreationCapabilities, listCreationThreads, refreshCreationThread,
  sendCreationMessage, threadMessages, uploadCreationMedia,
  type CreationFormat, type CreationThread,
} from "@/lib/creation-thread-api";
import { setChatFirstFallback } from "@/lib/chat-first";
import { cn } from "@/lib/cn";
import { listMyJobs, type LibraryJob } from "@/lib/me-api";
import LibraryTile from "@/components/library/LibraryTile";

const FORMATS: Array<{ value: CreationFormat; label: string; description: string }> = [
  { value: "montage", label: "Montage", description: "Music-led cuts from your strongest moments." },
  { value: "narrated_planned", label: "Narrated", description: "Let your voice guide the story." },
  { value: "subtitled", label: "Talking to camera", description: "A clean, captioned edit from your delivery." },
];
const MAX_FILES = 20;

function formatFromThread(thread: CreationThread | null): CreationFormat | null {
  return creationFormat(thread?.state.edit_format);
}

function projectTitle(thread: CreationThread): string {
  const intent = thread.state.intent;
  if (typeof intent === "string" && intent.trim()) return intent;
  const creatorAgent = thread.state.creator_agent;
  if (creatorAgent && typeof creatorAgent === "object" && "summary" in creatorAgent) {
    const summary = creatorAgent.summary;
    if (typeof summary === "string" && summary.trim()) return summary;
  }
  return "Untitled video";
}

function projectStatusLabel(thread: CreationThread): string {
  if (thread.status === "archived") return "Archived";
  if (creationJobFailed(thread)) return "Needs attention";
  if (creationJobReady(thread)) return creationJobPartial(thread) ? "Partially ready" : "Ready";
  if (thread.active_job_id) return "Rendering";
  if (creationThreadMediaCount(thread) > 0) return "Shaping direction";
  return "New project";
}

function hasAudioMedia(thread: CreationThread | null): boolean {
  const media = thread?.state.media;
  return Array.isArray(media) && media.some((entry) => {
    if (!entry || typeof entry !== "object") return false;
    const item = entry as Record<string, unknown>;
    return item.kind === "audio" || (typeof item.content_type === "string" && item.content_type.startsWith("audio/"));
  });
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

export interface ChatCreationWorkspaceProps {
  onLegacyFallback?: () => void;
}

export default function ChatCreationWorkspace({ onLegacyFallback }: ChatCreationWorkspaceProps) {
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
  const [galleryJobs, setGalleryJobs] = useState<LibraryJob[]>([]);
  const [formatPickerOpen, setFormatPickerOpen] = useState(false);
  const [availableFormats, setAvailableFormats] = useState<CreationFormat[]>(["montage", "narrated_planned", "subtitled"]);
  const editorFrameRef = useRef<HTMLIFrameElement>(null);
  const queuedMessageRef = useRef<{ threadId: string; message: string } | null>(null);
  const preparedRevisionRef = useRef<string | null>(null);
  const activeThreadIdRef = useRef<string | null>(null);
  const loadStartedRef = useRef(false);

  const activateThread = useCallback((next: CreationThread) => {
    activeThreadIdRef.current = next.id;
    setThread(next);
  }, []);

  const acceptThreadResponse = useCallback((expectedId: string, next: CreationThread) => {
    if (activeThreadIdRef.current === expectedId) setThread(next);
  }, []);

  const load = useCallback(async () => {
    try {
      const [listed, capabilities] = await Promise.all([listCreationThreads(), getCreationCapabilities()]);
      setAvailableFormats(capabilities.map((item) => item.edit_format));
      setProjects(listed);
      const current = thread ? listed.find((item) => item.id === thread.id) : null;
      const summary = current ?? listed.find((item) => item.status === "active");
      const next = summary ? await refreshCreationThread(summary.id) : await createCreationThread();
      activateThread(next);
      setChatFirstFallback(false);
      window.dispatchEvent(new CustomEvent("nova:chat-first-ready"));
      if (!current && !listed.some((item) => item.id === next.id)) setProjects((items) => [next, ...items]);
    } catch (cause) {
      if (cause instanceof CreationThreadError && cause.status === 404) {
        setChatFirstFallback(true);
        window.dispatchEvent(new CustomEvent("nova:chat-first-fallback"));
        onLegacyFallback?.();
        return;
      }
      setError("I couldn’t open this creation chat. Check your connection and try again.");
    }
  }, [activateThread, onLegacyFallback, thread]);

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
    void listMyJobs()
      .then((page) => setGalleryJobs(page.jobs))
      .catch(() => setError("I couldn’t load your Gallery. Your saved videos are still safe."));
  }, [galleryOpen]);

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
  }, [acceptThreadResponse, thread]);

  const format = formatFromThread(thread);
  const mediaCount = creationThreadMediaCount(thread);
  const hasReady = Boolean(thread && creationJobReady(thread));
  const isPartial = Boolean(thread && creationJobPartial(thread));
  const messages = useMemo(() => thread ? threadMessages(thread) : [], [thread]);
  const media = useMemo(() => attachedMedia(thread), [thread]);
  const latestAudio = [...media].reverse().find((item) => item.kind === "audio") ?? null;
  const accountName = session?.user?.name ?? session?.user?.email ?? "Account";

  async function selectFormat(value: CreationFormat) {
    if (!thread || busy || (format && !formatPickerOpen) || !availableFormats.includes(value)) return;
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
    if (!thread || busy) return;
    const threadId = thread.id;
    setBusy(true); setError(null);
    try { acceptThreadResponse(threadId, await applyCreationAction(thread, "select_variant", { variant_id: variantId })); }
    catch { setError("I couldn’t switch to that cut. Try again in a moment."); }
    finally { setBusy(false); }
  }

  async function attach(files: FileList | null) {
    if (!thread || !files?.length) return;
    const sourceThread = thread;
    const chosen = Array.from(files).slice(0, MAX_FILES);
    setPendingFiles((items) => [...items, ...chosen].slice(0, MAX_FILES));
    setUploading(true); setError(null);
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
    if (!thread || uploading) return;
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
    if (!thread) throw new Error("Start a creation project before recording.");
    const sourceThread = thread;
    const recording = file instanceof File ? file : new File([file], filename, { type: file.type || "audio/webm" });
    const next = await uploadCreationMedia(sourceThread, [recording]);
    acceptThreadResponse(sourceThread.id, next);
    return { gcs_path: "creation-thread-media", kind: "audio" };
  }

  async function removeMedia(mediaId: string) {
    if (!thread || busy || uploading) return;
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
    if (!message || !thread || thinking) return;
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
    if (!thread || busy) return;
    const sourceThread = thread;
    setBusy(true); setError(null);
    try { acceptThreadResponse(sourceThread.id, await applyCreationAction(sourceThread, action, payload)); }
    catch { setError("I couldn’t start that render. Your project is safe—adjust the direction or try again."); }
    finally { setBusy(false); }
  }

  async function startNew() {
    if (busy || thinking || uploading) return;
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
      router.replace("/plan", { scroll: false });
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
    router.replace("/plan", { scroll: false });
    try { acceptThreadResponse(project.id, await refreshCreationThread(project.id)); }
    catch { if (activeThreadIdRef.current === project.id) setError("I couldn’t open that project."); }
  }

  function openGallery() {
    setProjectsOpen(false);
    setGalleryOpen(true);
    router.replace("/plan?view=gallery", { scroll: false });
  }

  function closeGallery() {
    setGalleryOpen(false);
    router.replace("/plan", { scroll: false });
  }

  const selectedReadyVariant = readyVariant(thread);
  const selectedFailedVariant = failedVariant(thread);
  const editorUrl = thread?.active_plan_item_id
    ? `/plan/items/${thread.active_plan_item_id}/edit?embedded=1${selectedReadyVariant?.variant_id ? `&variant=${selectedReadyVariant.variant_id}` : ""}`
    : null;

  const sidebar = (
    <aside className="flex h-full w-[260px] shrink-0 flex-col border-r border-border bg-background p-4" aria-label="Projects">
      <div className="flex items-center justify-between px-2">
        <span className="flex items-center gap-2 text-lg font-semibold"><Sparkles className="size-4" /> Kria</span>
        <Button type="button" variant="ghost" size="icon" className="size-9" aria-label="Hide project sidebar" onClick={() => setSidebarHidden(true)}><PanelLeftClose /></Button>
      </div>
      <Button type="button" className="mt-6 min-h-11 justify-start" disabled={busy || thinking || uploading} onClick={() => void startNew()}><Film /> New video</Button>
      <div className="mt-8 flex items-center justify-between px-2"><p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Projects</p><Button type="button" variant="ghost" className="h-auto p-0 text-xs text-muted-foreground hover:text-foreground" disabled={busy || thinking || uploading} onClick={openGallery}>Gallery</Button></div>
      <nav className="mt-2 space-y-1 overflow-y-auto" aria-label="Recent projects">
        {projects.slice(0, 10).map((project) => <Button key={project.id} type="button" variant={project.id === thread?.id ? "secondary" : "ghost"} className="h-auto min-h-11 w-full justify-start text-left" disabled={busy || thinking || uploading} onClick={() => void openProject(project)}><FolderOpen className="shrink-0" /><span className="min-w-0"><span className="block truncate">{projectTitle(project)}</span><span className="block truncate text-[11px] font-normal text-muted-foreground">{projectStatusLabel(project)}</span></span></Button>)}
      </nav>
      <div className="mt-auto border-t pt-4">
        <p className="truncate text-sm font-medium">{accountName}</p>
        <div className="mt-2 flex items-center gap-1"><Link href="/plan" className="text-xs text-muted-foreground hover:text-foreground">My videos</Link><span className="text-muted-foreground">·</span><Button type="button" variant="ghost" className="h-auto p-0 text-xs text-muted-foreground hover:text-foreground" onClick={() => void signOut({ callbackUrl: "/" })}>Sign out</Button></div>
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
        {FORMATS.map((item) => <Button key={item.value} type="button" variant="outline" disabled={busy || (Boolean(format) && !formatPickerOpen) || !availableFormats.includes(item.value)} className={cn("h-auto min-h-[96px] snap-start flex-col items-start justify-start whitespace-normal p-4 text-left", format === item.value && "border-primary ring-1 ring-primary", !availableFormats.includes(item.value) && "opacity-60")} onClick={() => void selectFormat(item.value)}><span className="font-medium">{item.label}</span><span className="mt-1 text-xs font-normal text-muted-foreground">{availableFormats.includes(item.value) ? item.description : "Temporarily unavailable — choose another format."}</span></Button>)}
      </div>
    </ChatArtifactCard>
  );

  const uploadArtifact = (
    <ChatArtifactCard title={format === "narrated_planned" ? "Add footage and your voice" : "Add your footage"} description="Video, photos, and audio are welcome. You can add more later.">
      {format === "narrated_planned" && !latestAudio ? <VoiceRecorder upload={uploadRecordedVoice} onVoiceover={() => undefined} /> : null}
      {format ? <Button type="button" variant="ghost" className="px-0 text-xs text-muted-foreground" onClick={() => setFormatPickerOpen(true)}>Change format</Button> : null}
      <Dropzone compact accept="video/*,image/*,audio/*" multiple disabled={uploading} title={uploading ? "Uploading…" : "Choose files or drop them here"} subline="Up to 20 files" ariaLabel="Add footage, photos, or audio" inputAriaLabel="Upload video, image, or audio files" onFiles={(files) => void attach(files)} />
      {mediaCount > 0 ? <div className="mt-3 flex items-center justify-between text-sm text-muted-foreground"><span role="status">{mediaCount} {mediaCount === 1 ? "file" : "files"} attached</span>{pendingFiles.length > 0 ? <span>Retrying {pendingFiles.length}…</span> : null}</div> : null}
      {pendingFiles.length > 0 ? <div className="mt-2 space-y-1">{pendingFiles.map((file) => <div key={`${file.name}-${file.size}`} className="flex items-center justify-between gap-2 rounded-md bg-muted px-2 py-1 text-xs"><span className="truncate">{file.name}</span><div className="flex shrink-0 items-center gap-1"><Button type="button" variant="ghost" size="sm" className="h-7 px-2" disabled={uploading} onClick={() => void retryFile(file)}>Retry</Button><Button type="button" variant="ghost" size="icon" className="size-7" aria-label={`Remove ${file.name}`} onClick={() => setPendingFiles((items) => items.filter((item) => item !== file))}><Trash2 className="size-3" /></Button></div></div>)}</div> : null}
    </ChatArtifactCard>
  );

  const attachedMediaArtifact = media.length > 0 && !thread?.active_job_id ? (
    <ChatArtifactCard title="Attached media" description="Remove anything you don’t want Kria to use before rendering.">
      <div className="space-y-1" role="list">
        {media.map((item) => <div key={item.media_id} className="flex items-center justify-between gap-2 rounded-md bg-muted px-3 py-2 text-sm" role="listitem"><span className="min-w-0 truncate">{item.filename}<span className="ml-2 text-xs capitalize text-muted-foreground">{item.kind}</span></span><Button type="button" variant="ghost" size="icon" className="size-8 shrink-0" disabled={busy || uploading} aria-label={`Remove attached ${item.filename}`} onClick={() => void removeMedia(item.media_id)}><Trash2 className="size-4" /></Button></div>)}
      </div>
    </ChatArtifactCard>
  ) : null;

  const chat = (
    <section className="flex min-h-0 flex-1 flex-col" aria-label="Kria creation chat">
      <header className="flex h-14 shrink-0 items-center justify-between border-b px-4 sm:px-6"><div className="min-w-0"><h1 className="truncate text-xl font-semibold">Create with Kria</h1><p className="truncate text-xs text-muted-foreground">{format ? `${creationFormatLabel(format)} · ${mediaCount} ${mediaCount === 1 ? "file" : "files"}` : "Start with a format, then tell me what you’re imagining"}</p></div><div className="flex items-center gap-1">{format && mediaCount > 0 && !thread?.active_job_id ? <Button type="button" variant="ghost" className="h-9 px-2 text-xs" onClick={() => setFormatPickerOpen(true)}>Change format</Button> : null}<Button type="button" variant="ghost" size="icon" className="size-11 md:hidden" aria-label="Open projects" onClick={() => setProjectsOpen(true)}><Menu /></Button></div></header>
      <div className="min-h-0 flex-1 overflow-y-auto"><div className="mx-auto flex w-full max-w-2xl flex-col gap-4 px-4 py-6 sm:px-8">
        {!thread ? <div className="space-y-3" role="status"><div className="h-5 w-40 motion-safe:animate-pulse rounded bg-muted" /><div className="h-20 w-full motion-safe:animate-pulse rounded bg-muted" /></div> : null}
        {messages.map((message) => <div key={message.id} className="space-y-3"><ChatBubble role={message.role}>{message.content}</ChatBubble>{message.artifact === "format" && (!format || formatPickerOpen) ? formatArtifact : null}{message.artifact === "upload" && !thread?.active_job_id ? uploadArtifact : null}{message.artifact === "voiceover" && !thread?.active_job_id ? uploadArtifact : null}{message.artifact === "confirmation" && !thread?.active_job_id ? <ChatArtifactCard badge={<Badge variant="secondary">Creative direction</Badge>} title={`${creationFormatLabel(format)} is ready to make`} description={typeof thread?.state.intent === "string" && thread.state.intent ? thread.state.intent : "I’ll find the strongest opening and shape your footage into a concise first cut."}><Button type="button" className="min-h-11 w-full" disabled={busy || mediaCount === 0} onClick={() => void confirm("generate")}><Sparkles />{busy ? "Starting…" : "Create this video"}</Button></ChatArtifactCard> : null}{message.artifact === "revision" && hasReady ? <ChatArtifactCard badge={<Badge variant="secondary">Revision ready</Badge>} title="Apply this direction?" description="This creates a new generation from the finished cut."><Button type="button" className="min-h-11 w-full" disabled={busy} onClick={() => void confirm("generate", { base_generation: thread?.job?.id })}><RefreshCw /> Create revision</Button></ChatArtifactCard> : null}</div>)}
        {thread && (!format || formatPickerOpen) && !messages.some((message) => message.artifact === "format") ? formatArtifact : null}
        {thread && format && mediaCount === 0 && !messages.some((message) => message.artifact === "upload" || message.artifact === "voiceover") ? uploadArtifact : null}
        {thread && format === "narrated_planned" && mediaCount > 0 && !hasAudioMedia(thread) && !thread.active_job_id && !messages.some((message) => message.artifact === "upload" || message.artifact === "voiceover") ? uploadArtifact : null}
        {attachedMediaArtifact}
        {thinking ? <ChatThinking /> : null}
        {thread && thread.active_job_id && !hasReady && !creationJobFailed(thread) ? <ChatArtifactCard badge={<Badge variant="secondary">Rendering</Badge>} title="Kria is building your first cut…" description={thread.job?.current_phase ?? "Finding the story in your footage."}><div className="h-2 w-full motion-safe:animate-pulse rounded-full bg-muted" /></ChatArtifactCard> : null}
        {thread && creationJobFailed(thread) ? <ChatArtifactCard badge={<Badge variant="destructive">Render needs attention</Badge>} title="Your project is safe" description={thread.job?.failure_reason ?? "The render did not finish, but your direction and footage are still here."}><div className="flex flex-wrap gap-2"><Button type="button" onClick={() => void confirm("retry")} disabled={busy}><RefreshCw /> Retry render</Button><Button type="button" variant="outline" onClick={() => setInput("Try a different opening and keep the pacing quick.")}>Adjust direction</Button></div></ChatArtifactCard> : null}
        {thread && hasReady ? <ChatArtifactCard badge={<Badge variant="secondary"><Check /> {isPartial ? "Partially ready" : "Ready"}</Badge>} title={isPartial ? "Your cut is ready; one variant needs another pass" : "Your cut is ready"} description={isPartial ? "The playable cut is available now. Retry the failed variant whenever you’re ready." : "Play it here, download it, open the editor, or keep chatting for a confirmed revision."}>{readyVariants(thread).length > 1 ? <div className="mb-3 flex flex-wrap gap-2" role="group" aria-label="Available cuts">{readyVariants(thread).map((variant) => <Button key={variant.variant_id} type="button" variant={selectedReadyVariant?.variant_id === variant.variant_id ? "secondary" : "outline"} aria-pressed={selectedReadyVariant?.variant_id === variant.variant_id} disabled={busy} onClick={() => void selectVariant(variant.variant_id ?? "")}>{variantLabel(variant.variant_id)}</Button>)}</div> : null}<div className="flex flex-wrap gap-2">{selectedReadyVariant?.output_url ? <><Button type="button" onClick={() => window.open(selectedReadyVariant.output_url ?? "", "_blank", "noopener,noreferrer")}><Play /> Play</Button><Button type="button" variant="outline" asChild><a href={selectedReadyVariant.output_url ?? ""} download><Download /> Download</a></Button></> : null}<Button type="button" variant="outline" onClick={() => { setEditorOpen(true); setMobileTab("editor"); }} disabled={!thread.active_plan_item_id}><Pencil /> Open editor</Button>{isPartial && selectedFailedVariant?.variant_id ? <Button type="button" variant="ghost" onClick={() => void confirm("retry", { variant_id: selectedFailedVariant.variant_id })} disabled={busy}><RefreshCw /> Retry failed variant</Button> : null}</div></ChatArtifactCard> : null}
      </div></div>
      <div className="shrink-0 border-t bg-background p-3 pb-[max(0.75rem,env(safe-area-inset-bottom))] sm:p-4"><form className="mx-auto flex max-w-2xl items-end gap-2 rounded-2xl border bg-background p-2 shadow-sm focus-within:ring-1 focus-within:ring-ring" onSubmit={(event) => { event.preventDefault(); void send(); }}><Button type="button" variant="ghost" size="icon" className="size-11 shrink-0 rounded-full" aria-label="Attach video, image, or audio" disabled={!thread || uploading || Boolean(thread?.active_job_id)} onClick={() => document.getElementById("creation-file-picker")?.click()}><Plus /></Button><input id="creation-file-picker" type="file" className="sr-only" accept="video/*,image/*,audio/*" multiple onChange={(event) => { void attach(event.target.files); event.target.value = ""; }} /><Textarea value={input} onChange={(event) => setInput(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); void send(); } }} placeholder="Tell Kria what you’re imagining…" aria-label="Message Kria" rows={1} className="max-h-32 min-h-11 resize-none border-0 bg-transparent py-3 shadow-none focus-visible:ring-0" /><Button type="submit" size="icon" className="size-11 shrink-0 rounded-full" aria-label="Send message" disabled={!input.trim() || thinking || !thread}><ArrowUp /></Button></form>{offline ? <p className="mx-auto mt-2 flex max-w-2xl items-center gap-1 text-xs text-muted-foreground" role="status"><WifiOff className="size-3" /> Offline — messages stay in the composer until you reconnect.</p> : null}{pollReconnecting ? <p className="mx-auto mt-2 flex max-w-2xl items-center gap-1 text-xs text-muted-foreground" role="status"><RefreshCw className="size-3 motion-safe:animate-spin" /> Reconnecting to render status…</p> : null}{error ? <p className="mx-auto mt-2 max-w-2xl text-sm text-destructive" role="alert">{error}</p> : null}</div>
    </section>
  );

  const editor = <section className="flex min-w-0 flex-1 flex-col overflow-hidden border-l bg-muted/10" aria-label="Video editor"><header className="flex h-14 shrink-0 items-center justify-between border-b bg-background px-4"><div><p className="text-sm font-medium">Editor</p><p className="text-xs text-muted-foreground">Feature-complete overlay editor</p></div><Badge variant="secondary"><Check /> Ready</Badge></header>{editorUrl ? <iframe ref={editorFrameRef} src={editorUrl} title="Full video editor" className="min-h-0 flex-1 border-0 bg-background" /> : <div className="flex flex-1 items-center justify-center p-6 text-center text-sm text-muted-foreground">The editor will appear when your first cut is ready.</div>}</section>;

  if (galleryOpen) return <div className="flex h-dvh flex-col overflow-hidden bg-background"><header className="flex h-14 shrink-0 items-center justify-between border-b px-4"><h1 className="text-lg font-semibold">Gallery</h1><Button type="button" onClick={closeGallery}>Back to chat</Button></header><main className="min-h-0 flex-1 overflow-y-auto p-6"><ul className="mx-auto grid max-w-5xl grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-4">{galleryJobs.map((job) => <li key={job.id}><LibraryTile job={job} /></li>)}</ul>{galleryJobs.length === 0 ? <p className="mx-auto max-w-md py-16 text-center text-sm text-muted-foreground">Your finished cuts will appear here.</p> : null}</main></div>;

  return <div className="relative flex h-dvh min-h-0 overflow-hidden bg-background text-foreground"><div className={cn("hidden md:block", sidebarHidden && "md:hidden")}>{sidebar}</div><Sheet open={projectsOpen} onOpenChange={setProjectsOpen}><SheetContent side="left" className="w-[260px] p-0 sm:max-w-[260px]"><SheetHeader className="sr-only"><SheetTitle>Projects</SheetTitle><SheetDescription>Move between creation projects and your gallery.</SheetDescription></SheetHeader>{sidebar}</SheetContent></Sheet><div className="flex min-w-0 flex-1 flex-col">{hasReady && editorOpen ? <div className="border-b p-2 lg:hidden"><Tabs value={mobileTab} onValueChange={(value) => setMobileTab(value as "chat" | "editor")}><TabsList className="grid h-11 w-full grid-cols-2"><TabsTrigger value="chat">Chat</TabsTrigger><TabsTrigger value="editor">Editor</TabsTrigger></TabsList></Tabs></div> : null}<div className="flex min-h-0 flex-1">{sidebarHidden ? <Button type="button" variant="ghost" size="icon" className="absolute left-2 top-2 z-10 hidden size-9 md:inline-flex" aria-label="Show project sidebar" onClick={() => setSidebarHidden(false)}><PanelLeftOpen /></Button> : null}<div className={cn("min-w-0 flex-1", hasReady && editorOpen && "lg:flex-none lg:w-[420px]", hasReady && mobileTab === "editor" && "hidden lg:block")}>{chat}</div>{hasReady && editorOpen ? <div className={cn("min-w-0 flex-1", mobileTab === "chat" ? "hidden lg:flex" : "flex")}>{editor}</div> : null}</div></div></div>;
}
