"use client";

import { useEffect, useMemo, useState } from "react";
import { AgentApprovalCard } from "@/components/chat/AgentApprovalCard";
import { AgentComposer } from "@/components/chat/AgentComposer";
import { ChatMessage } from "@/components/chat/ChatMessage";
import { ChatThinking, WAIT_LADDER_MS } from "@/components/chat/ChatThinking";
import { BeamLoader } from "@/components/progress";
import {
  SpeechCleanupDecisionCard,
  SpeechCleanupReceipt,
} from "@/app/plan/_components/workspace/SpeechCleanupDecisionCard";
import { ChatArtifactCard } from "@/components/chat/ChatArtifactCard";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/cn";
import type {
  CreationSpeechCleanupChoice,
  CreationSpeechCleanupOutcomeStatus,
  CreationSpeechCleanupProjection,
} from "@/lib/creation-thread-api";

type SpeechFixtureState =
  | "speech-checking"
  | "speech-findings"
  | "speech-no-findings"
  | "speech-failed-retryable"
  | "speech-failed-nonretryable"
  | "speech-stale"
  | "speech-saving"
  | "speech-rendering"
  | "speech-cleanup-failed"
  | "speech-ready-applied"
  | "speech-ready-no-change"
  | "speech-ready-declined"
  | "speech-ready-bypassed-unchecked"
  | "speech-audio-only";

type FixtureState =
  | "choose"
  | "upload"
  | "confirm"
  | "rendering"
  | "ready"
  | "revision"
  | "upload-failed"
  | "voiceover"
  | "stale"
  | "offline"
  | "unavailable"
  | "partial"
  | "failed"
  | "thinking"
  | "deleted"
  | SpeechFixtureState;
type View = "chat" | "editor" | "projects" | "gallery";

const STATES: FixtureState[] = [
  "choose",
  "upload",
  "confirm",
  "rendering",
  "ready",
  "revision",
  "upload-failed",
  "voiceover",
  "stale",
  "offline",
  "unavailable",
  "partial",
  "failed",
  "thinking",
  "deleted",
  "speech-checking",
  "speech-findings",
  "speech-no-findings",
  "speech-failed-retryable",
  "speech-failed-nonretryable",
  "speech-stale",
  "speech-saving",
  "speech-rendering",
  "speech-cleanup-failed",
  "speech-ready-applied",
  "speech-ready-no-change",
  "speech-ready-declined",
  "speech-ready-bypassed-unchecked",
  "speech-audio-only",
];

const SPEECH_STATES = new Set<FixtureState>(STATES.filter((state) => state.startsWith("speech-")));

function isSpeechFixtureState(state: FixtureState): state is SpeechFixtureState {
  return SPEECH_STATES.has(state);
}

function speechFixtureAnnouncement(state: FixtureState): string {
  if (state === "speech-checking" || state === "speech-stale") return "Checking for filler sounds.";
  if (state === "speech-findings" || state === "speech-audio-only") return "Speech check complete. Choose whether to clean up the speech.";
  if (state === "speech-no-findings") return "Speech check complete. No cleanup suggested.";
  if (state === "speech-failed-retryable" || state === "speech-failed-nonretryable") return "Kria could not check the speech. Choose a recovery action.";
  if (state === "speech-saving") return "Saving your speech cleanup choice.";
  if (state === "speech-rendering") return "Cleaning speech and creating your video.";
  if (state === "speech-cleanup-failed") return "Speech cleanup did not complete. Choose a recovery action.";
  if (state === "speech-ready-applied") return "Your video is ready with speech cleanup applied.";
  if (state === "speech-ready-no-change") return "Your video is ready. Speech was checked and no safe cuts were needed.";
  if (state === "speech-ready-declined") return "Your video is ready with speech kept as recorded.";
  if (state === "speech-ready-bypassed-unchecked") return "Your video is ready without a speech check.";
  return "";
}

const FORMAT_COPY = {
  montage: { title: "Montage", detail: "Music-led cuts from your strongest moments." },
  narrated: { title: "Narrated", detail: "Let your voice guide the story." },
  talking: { title: "Talking to camera", detail: "A clean, captioned edit from your delivery." },
} as const;

const FIXTURE_STYLES = `
  .chat-fixture * { box-sizing: border-box; }
  .chat-fixture button, .chat-fixture input, .chat-fixture textarea { font: inherit; }
  .chat-fixture button { cursor: pointer; }
  .chat-fixture .fade { animation: fixture-fade 220ms ease-out both; }
  .chat-fixture-reduced-motion .fade { animation: none; }
  .chat-fixture-reduced-motion .beam-loader__beam,
  .chat-fixture-reduced-motion .beam-loader__bloom,
  .chat-fixture-reduced-motion .beam-loader__line { animation: none !important; }
  .chat-fixture-reduced-motion .animate-bounce,
  .chat-fixture-reduced-motion .motion-safe\\:animate-shimmer { animation: none !important; }
  .chat-fixture[data-view="editor"] .editor-pane { display: flex; }
  .chat-fixture[data-view="editor"] .chat-rail { flex: 0 0 420px; }
  @keyframes fixture-fade { from { opacity: .1; transform: translateY(5px); } to { opacity: 1; transform: none; } }
  @media (max-width: 767px) { .chat-fixture .project-rail { display:none; } .chat-fixture .chat-rail { width:100%; border:0; } .chat-fixture .editor-pane { display:none; } .chat-fixture[data-view="editor"] .chat-rail { display:none; } .chat-fixture[data-view="editor"] .editor-pane { display:flex; width:100%; } }
`;

function readParam(name: string, fallback: string) {
  if (typeof window === "undefined") return fallback;
  const value = new URLSearchParams(window.location.search).get(name);
  return value ?? fallback;
}

export default function ChatFirstCreationFixture() {
  const [state, setState] = useState<FixtureState>("choose");
  const [view, setView] = useState<View>("chat");
  const [mediaCount, setMediaCount] = useState(0);
  const [format, setFormat] = useState<keyof typeof FORMAT_COPY>("montage");
  const [composer, setComposer] = useState("");
  const [reducedMotion, setReducedMotion] = useState(false);
  const [sidebarVisible, setSidebarVisible] = useState(true);
  const [renameOpen, setRenameOpen] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [projectName, setProjectName] = useState("Weekend in Corfu");
  const [renameDraft, setRenameDraft] = useState(projectName);
  const [deleted, setDeleted] = useState(false);
  const [projectId, setProjectId] = useState("project-corfu");
  const [thinkingElapsed, setThinkingElapsed] = useState(0);
  const [speechChoice, setSpeechChoice] = useState<CreationSpeechCleanupChoice>("clean");

  useEffect(() => {
    const requestedState = readParam("state", "choose") as FixtureState;
    const requestedView = readParam("view", "chat") as View;
    const nextState = STATES.includes(requestedState) ? requestedState : "choose";
    setState(nextState);
    setView(requestedView === "editor" ? "editor" : "chat");
    setMediaCount(nextState === "choose" ? 0 : 3);
    setDeleted(nextState === "deleted");
    setProjectId(readParam("project", "project-corfu"));
    setThinkingElapsed(Number.parseInt(readParam("elapsed", "0"), 10) || 0);
    const requestedName = readParam("name", "Weekend in Corfu");
    setProjectName(requestedName);
    setRenameDraft(requestedName);
  }, []);

  useEffect(() => {
    const onOnline = () => setState("choose");
    window.addEventListener("online", onOnline);
    return () => window.removeEventListener("online", onOnline);
  }, []);

  const projectStatus = useMemo(() => {
    if (state === "ready" || state === "revision" || state === "partial" || state.startsWith("speech-ready-")) return "Ready to review";
    if (state === "rendering" || state === "speech-rendering") return "Rendering";
    if (state === "failed" || state.includes("failed")) return "Needs attention";
    return "In progress";
  }, [state]);

  function navigate(nextState: FixtureState, nextView: View = "chat") {
    if (nextState === "deleted") setDeleted(true);
    setState(nextState);
    setView(nextView);
    if (nextState !== "choose") setMediaCount((count) => Math.max(count, 3));
    const params = new URLSearchParams({ project: projectId, state: nextState, view: nextView });
    window.history.replaceState(null, "", `/dev-qa/chat-first-creation?${params}`);
  }

  function selectView(nextView: View) {
    setView(nextView);
    const params = new URLSearchParams({ project: projectId, state, view: nextView });
    window.history.replaceState(null, "", `/dev-qa/chat-first-creation?${params}`);
  }

  function saveRename() {
    const nextName = renameDraft.trim();
    if (!nextName) return;
    setProjectName(nextName);
    setRenameOpen(false);
  }

  function confirmDelete() {
    setDeleteOpen(false);
    navigate("deleted");
  }

  return (
    <main
      className={`chat-fixture flex h-dvh min-h-0 overflow-hidden bg-[#f7f7f5] text-[#0c0c0e] ${
        reducedMotion ? "chat-fixture-reduced-motion" : ""
      }`}
      data-testid="chat-first-creation-fixture"
      data-state={state}
      data-view={view}
      data-project-id={projectId}
    >
      <p
        className="sr-only"
        aria-live="polite"
        aria-atomic="true"
        data-testid="fixture-live-announcer"
      >
        {speechFixtureAnnouncement(state)}
      </p>
      <style dangerouslySetInnerHTML={{ __html: FIXTURE_STYLES }} />

      {!deleted && sidebarVisible ? <aside className="project-rail flex w-[260px] shrink-0 flex-col border-r border-[#deded9] bg-[#f1f1ee] p-5" aria-label="Projects">
        <div className="flex items-center justify-between">
          <span className="font-display text-xl font-medium">Kria</span>
          <button aria-label="Hide projects" className="rounded-full p-2 text-[#6e6e68] hover:bg-white" onClick={() => setSidebarVisible(false)}>×</button>
        </div>
        <button className="mt-8 flex items-center justify-between rounded-lg bg-[#d7ff90] px-3 py-3 text-left text-sm font-semibold" onClick={() => navigate("choose")}>
          <span>New project</span><span aria-hidden>＋</span>
        </button>
        <p className="mt-8 text-[11px] font-semibold uppercase tracking-[.18em] text-[#8c8c85]">Projects</p>
        <button data-testid="project-link" className="mt-3 rounded-lg bg-white px-3 py-3 text-left shadow-sm" onClick={() => selectView("chat")}>
          <span className="block text-sm font-medium">{projectName}</span>
          <span className="mt-1 block text-xs text-[#7c7c75]">{projectStatus}</span>
        </button>
        <div className="mt-2 grid grid-cols-2 gap-2">
          <button className="rounded-md border border-[#deded9] px-2 py-2 text-xs text-[#6e6e68]" onClick={() => { setRenameDraft(projectName); setRenameOpen(true); }}>Rename</button>
          <button className="rounded-md border border-[#deded9] px-2 py-2 text-xs text-[#9d3c32]" onClick={() => setDeleteOpen(true)}>Delete</button>
        </div>
        <button className="mt-2 rounded-lg px-3 py-3 text-left text-sm text-[#6e6e68]" onClick={() => selectView("gallery")}>Gallery</button>
        <div className="mt-auto border-t border-[#deded9] pt-4 text-xs text-[#7c7c75]">emir@example.com</div>
      </aside> : null}

      {!deleted && !sidebarVisible ? <button data-testid="show-projects" aria-label="Show projects" className="project-sidebar-reveal absolute left-3 top-3 z-10 rounded-md border border-[#deded9] bg-white px-2 py-1.5 text-xs text-[#6e6e68] shadow-sm" onClick={() => setSidebarVisible(true)}>Projects</button> : null}

      {deleted ? <DeletedState onRestore={() => { setDeleted(false); navigate("choose"); }} /> : view === "projects" ? (
        <section className="flex min-w-0 flex-1 flex-col bg-white p-6" aria-label="Project list">
          <button className="self-start text-sm text-[#6e6e68]" onClick={() => selectView("chat")}>← Back to chat</button>
          <h1 className="font-display mt-10 text-4xl font-medium">Projects</h1>
          <div className="mt-8 grid gap-3 sm:grid-cols-2">
            <button className="rounded-xl border border-[#deded9] bg-[#f7f7f5] p-5 text-left" onClick={() => selectView("chat")}><span className="block font-medium">{projectName}</span><span className="mt-1 block text-sm text-[#7c7c75]">{projectStatus}</span></button>
            <button className="rounded-xl border border-dashed border-[#b8b8b0] p-5 text-left text-sm text-[#7c7c75]" onClick={() => navigate("choose")}>＋ New project</button>
          </div>
        </section>
      ) : view === "gallery" ? (
        <section className="flex min-w-0 flex-1 flex-col bg-white p-6" aria-label="Gallery">
          <button className="self-start text-sm text-[#6e6e68]" onClick={() => selectView("chat")}>← Back to chat</button>
          <h1 className="font-display mt-10 text-4xl font-medium">Gallery</h1>
          <p className="mt-2 text-sm text-[#7c7c75]">Your finished cuts live here.</p>
          <div className="mt-8 grid gap-4 sm:grid-cols-3">
            {['Weekend in Corfu', 'Alberobello', 'Lisbon walk'].map((title) => <button key={title} className="aspect-[9/12] rounded-xl bg-[#20201e] p-4 text-left text-white" onClick={() => selectView("chat")}><span className="mt-auto block pt-32 text-sm">{title}</span></button>)}
          </div>
        </section>
      ) : (
        <>
          <section className="chat-rail flex min-w-0 flex-1 flex-col border-r border-[#deded9] bg-white" aria-label="Creation conversation">
            <header className={`flex h-14 shrink-0 items-center justify-between border-b border-[#ededE8] ${sidebarVisible ? "px-5" : "pl-24 pr-5"}`}>
              <div className="flex min-w-0 items-center gap-3"><span aria-hidden="true" className="h-2 w-2 shrink-0 rounded-full bg-[#b6dc67]" /><div className="min-w-0"><p data-testid="chat-project-title" className="truncate text-sm font-medium">{projectName}</p><p className="text-[11px] text-[#85857e]">{projectStatus}</p></div></div>
              <div className="flex items-center gap-2"><button className="rounded-md px-2 py-1 text-xs text-[#6e6e68] hover:bg-[#f1f1ee]" onClick={() => selectView("projects")}>Projects</button><button className="rounded-md px-2 py-1 text-xs text-[#6e6e68] hover:bg-[#f1f1ee]" onClick={() => selectView("gallery")}>Gallery</button></div>
            </header>
            <div className="flex-1 overflow-y-auto px-5 py-7 sm:px-10">
              <div className="mx-auto max-w-[620px]">
                {state === "choose" ? (
                  <ChooseState format={format} setFormat={setFormat} onContinue={() => navigate("upload")} />
                ) : isSpeechFixtureState(state) ? (
                  <SpeechCleanupFixtureState
                    state={state}
                    speechChoice={speechChoice}
                    setSpeechChoice={setSpeechChoice}
                    onState={navigate}
                  />
                ) : (
                  <ConversationState
                    state={state}
                    mediaCount={mediaCount}
                    onState={navigate}
                    thinkingElapsed={thinkingElapsed}
                    projectName={projectName}
                  />
                )}
              </div>
            </div>
            <div className="shrink-0 border-t border-[#ededE8] bg-white p-4 pb-[max(1rem,env(safe-area-inset-bottom))] sm:px-10">
              <AgentComposer
                className="mx-auto max-w-[620px]"
                value={composer}
                onValueChange={setComposer}
                onSubmit={() => { setComposer(""); navigate(state === "ready" ? "revision" : "confirm"); }}
                placeholder="Tell Kria what you want to make…"
                inputLabel="Message Kria"
                submitLabel="Send message"
                leadingAction={<label className="flex size-11 shrink-0 items-center justify-center rounded-full bg-lime-200 text-lg" aria-label="Attach primary video clips"><input className="sr-only" type="file" accept="video/*" multiple onChange={() => { setMediaCount((count) => count + 1); navigate(state === "speech-audio-only" ? "speech-findings" : "upload"); }} />＋</label>}
                status={<p className="text-center text-[11px] text-[#a0a098]">Kria won’t render until you confirm the direction.</p>}
              />
            </div>
          </section>
          <section className="editor-pane hidden min-w-0 flex-1 items-center justify-center bg-[#20201e]" aria-label="Embedded editor">
            <div className="flex h-full w-full flex-col"><div className="flex h-14 shrink-0 items-center justify-between border-b border-white/10 px-5 text-white"><span className="text-sm">Editor</span><button className="rounded-md border border-white/20 px-3 py-1 text-xs" onClick={() => selectView("chat")}>Back to chat</button></div><div className="flex flex-1 items-center justify-center text-center text-sm text-white/60"><div><div className="mx-auto aspect-[9/16] w-48 rounded-lg bg-[#373733] shadow-2xl" data-testid="embedded-editor-canvas" /><p className="mt-4">Embedded EditorShell · overlay mode</p></div></div></div>
          </section>
        </>
      )}
      <button data-testid="reduced-motion-toggle" className="sr-only" onClick={() => setReducedMotion((value) => !value)}>Toggle reduced motion</button>
      {renameOpen ? <RenameDialog value={renameDraft} onChange={setRenameDraft} onCancel={() => setRenameOpen(false)} onSave={saveRename} /> : null}
      {deleteOpen ? <DeleteDialog projectName={projectName} onCancel={() => setDeleteOpen(false)} onDelete={confirmDelete} /> : null}
    </main>
  );
}

function ChooseState({ format, setFormat, onContinue }: { format: keyof typeof FORMAT_COPY; setFormat: (format: keyof typeof FORMAT_COPY) => void; onContinue: () => void }) {
  return (
    <div className="fade space-y-4" data-testid="choose-state">
      <ChatArtifactCard
        data-testid="format-artifact"
        title="What are you making?"
        description="Pick a starting point. You can shape the creative direction together in chat."
      >
        <div
          className="grid grid-flow-col auto-cols-[minmax(220px,85%)] snap-x gap-3 overflow-x-auto sm:grid-flow-row sm:auto-cols-auto sm:grid-cols-3 sm:overflow-visible"
          data-testid="format-choice-grid"
          role="radiogroup"
          aria-label="Video format"
        >
          {(Object.keys(FORMAT_COPY) as Array<keyof typeof FORMAT_COPY>).map((key) => {
            const copy = FORMAT_COPY[key];
            return (
              <Button
                key={key}
                type="button"
                variant="outline"
                role="radio"
                aria-checked={format === key}
                data-format={key}
                className={cn(
                  "h-auto min-h-[96px] snap-start flex-col items-start justify-start whitespace-normal p-4 text-left",
                  format === key && "border-primary ring-1 ring-primary",
                )}
                onClick={() => setFormat(key)}
              >
                <span className="font-medium">{copy.title}</span>
                <span className="mt-1 text-xs font-normal text-muted-foreground">{copy.detail}</span>
              </Button>
            );
          })}
        </div>
      </ChatArtifactCard>
      <Button type="button" onClick={onContinue}>Add footage <span aria-hidden>→</span></Button>
    </div>
  );
}

function speechCleanupProjection(state: SpeechFixtureState): CreationSpeechCleanupProjection {
  const projection: CreationSpeechCleanupProjection = {
    applicable: true,
    analysis: {
      id: state === "speech-stale" ? "analysis-fixture-new" : "analysis-fixture",
      status: "ready",
      has_findings: true,
      candidate_count: 5,
      category_counts: { filler_sound: 4, long_pause: 1 },
      estimated_removed_ms: 2800,
      error: null,
    },
    decision: null,
    requires_choice: true,
    render_blocker: state === "speech-audio-only" ? "video_required" : null,
    outcome: null,
  };

  if (state === "speech-checking" || state === "speech-stale") {
    projection.analysis = { id: projection.analysis!.id, status: "running" };
    projection.requires_choice = false;
  } else if (state === "speech-no-findings") {
    projection.analysis = {
      id: projection.analysis!.id,
      status: "no_findings",
      has_findings: false,
      candidate_count: 0,
    };
    projection.requires_choice = false;
  } else if (state === "speech-failed-retryable") {
    projection.analysis = {
      id: projection.analysis!.id,
      status: "failed",
      error: { code: "analysis_timeout", retryable: true },
    };
    projection.requires_choice = false;
  } else if (state === "speech-failed-nonretryable") {
    projection.analysis = {
      id: projection.analysis!.id,
      status: "failed",
      error: { code: "unsupported_media", retryable: false },
    };
    projection.requires_choice = false;
  } else if (state === "speech-cleanup-failed") {
    projection.decision = "clean";
    projection.requires_choice = false;
    projection.outcome = {
      job_id: "job-fixture",
      render_generation_id: "generation-fixture",
      status: "failed",
      error: { code: "audio_apply_failed", retryable: true },
    };
  }
  return projection;
}

function readyOutcome(state: SpeechFixtureState): CreationSpeechCleanupOutcomeStatus | null {
  if (state === "speech-ready-applied") return "applied";
  if (state === "speech-ready-no-change") return "checked_no_change";
  if (state === "speech-ready-declined") return "declined";
  if (state === "speech-ready-bypassed-unchecked") return "bypassed_unchecked";
  return null;
}

function SpeechCleanupFixtureState({
  state,
  speechChoice,
  setSpeechChoice,
  onState,
}: {
  state: SpeechFixtureState;
  speechChoice: CreationSpeechCleanupChoice;
  setSpeechChoice: (choice: CreationSpeechCleanupChoice) => void;
  onState: (state: FixtureState, view?: View) => void;
}) {
  const outcomeStatus = readyOutcome(state);
  if (outcomeStatus) {
    return (
      <div className="fade" data-testid={`${state}-state`}>
        <ChatArtifactCard
          className="rounded-2xl border-zinc-200 bg-white shadow-sm"
          badge={<Badge variant="secondary">Ready</Badge>}
          title={<h2>Your cut is ready</h2>}
          description={<span className="text-base">Play it here, download it, open the editor, or keep chatting for a confirmed revision.</span>}
        >
          <div className="space-y-4">
            <SpeechCleanupReceipt
              outcome={{
                job_id: "job-fixture",
                render_generation_id: "generation-fixture",
                status: outcomeStatus,
                removal_count: outcomeStatus === "applied" ? 5 : null,
                removed_ms: outcomeStatus === "applied" ? 2800 : null,
              }}
            />
            <div className="flex flex-wrap gap-3">
              <Button type="button">Play</Button>
              <Button type="button" variant="outline">Download</Button>
              <Button type="button" variant="outline" onClick={() => onState(state, "editor")}>Open editor</Button>
            </div>
          </div>
        </ChatArtifactCard>
      </div>
    );
  }

  if (state === "speech-rendering") {
    return (
      <div className="fade" data-testid="speech-rendering-state">
        <ChatArtifactCard
          className="rounded-2xl border-zinc-200 bg-white shadow-sm"
          badge={<Badge variant="secondary">Rendering</Badge>}
          title={<h2>Creating your video</h2>}
          description={<span className="text-base">Kria is applying the confirmed speech choice and keeping captions in sync.</span>}
        >
          <Button
            type="button"
            className="min-h-11"
            onClick={() => onState(speechChoice === "clean" ? "speech-ready-applied" : "speech-ready-declined")}
          >
            Complete fixture render
          </Button>
        </ChatArtifactCard>
      </div>
    );
  }

  const cleanup = speechCleanupProjection(state);
  return (
    <div className="fade" data-testid={`${state}-state`}>
      <SpeechCleanupDecisionCard
        cleanup={cleanup}
        formatLabel={state === "speech-audio-only" ? "Narrated" : "Talking to camera"}
        direction="Keep the delivery crisp and add synchronized captions."
        busy={state === "speech-saving"}
        pendingAction={state === "speech-saving" ? "clean" : null}
        onGenerate={(choice) => {
          if (!choice) {
            onState("speech-ready-no-change");
            return;
          }
          setSpeechChoice(choice);
          onState("speech-rendering");
        }}
        onRetryAnalysis={() => onState("speech-checking")}
        onRetryRender={() => onState("speech-rendering")}
        onCreateWithoutCleanup={() => onState("speech-ready-bypassed-unchecked")}
      />
    </div>
  );
}

type ConversationFixtureState = Exclude<FixtureState, "choose" | SpeechFixtureState>;

function ConversationState({
  state,
  mediaCount,
  onState,
  thinkingElapsed,
  projectName,
}: {
  state: ConversationFixtureState;
  mediaCount: number;
  onState: (state: FixtureState, view?: View) => void;
  thinkingElapsed: number;
  projectName: string;
}) {
  const copy: Record<ConversationFixtureState, { eyebrow: string; title: string; body: string }> = {
    upload: { eyebrow: "Clips", title: "Show me the moments.", body: "Add primary video clips here. Supporting photos and short videos stay in Visuals; Narrated voiceover uses the recorder." },
    confirm: { eyebrow: "Direction", title: "Here’s the cut I’m proposing.", body: "A bright, quick montage with the ferry arrival first, then the blue-water swim. Keep the laughs and let the music breathe between scenes." },
    rendering: { eyebrow: "Rendering", title: "Your cut is taking shape.", body: "I’m assembling the footage and sound now. You can keep chatting; any new direction will wait for this render to finish." },
    ready: { eyebrow: "Ready", title: "Your first cut is ready.", body: "Play it through, download it, or open the editor. If something feels off, tell me exactly what to change and I’ll prepare a new render for your confirmation." },
    revision: { eyebrow: "Revision", title: "I’ve queued that direction.", body: "I’ll make the opening slower and hold the harbor shot longer. Confirm this exact change when you’re ready for the next render." },
    "upload-failed": { eyebrow: "Couldn’t upload", title: "One file didn’t make it.", body: "Your other footage is safe. Retry the upload or remove the failed file before continuing." },
    voiceover: { eyebrow: "Voiceover needed", title: "Your voice will make this work.", body: "Record a short voiceover here, or switch to Montage and let the footage lead." },
    stale: { eyebrow: "Newer version", title: "This project changed elsewhere.", body: "Reload the latest direction before sending it. Your draft message will stay in the composer." },
    offline: { eyebrow: "Reconnecting", title: "You’re offline for a moment.", body: "Your message is saved locally and will send when the connection returns." },
    unavailable: { eyebrow: "Format unavailable", title: "That format isn’t ready here yet.", body: "Try Montage or Talking to camera while we finish setting this one up." },
    partial: { eyebrow: "Partially ready", title: "One version is ready to watch.", body: "The original-audio cut is ready. A second variant needs another try; you can play the available cut now." },
    failed: { eyebrow: "Render stopped", title: "That render didn’t finish.", body: "Your footage and direction are still here. Retry the render or adjust the direction and try again." },
    thinking: { eyebrow: "Thinking", title: "Kria is working through your direction.", body: "The message stays in the conversation, with more useful context as the response takes shape." },
    deleted: { eyebrow: "Deleted", title: "This project is gone.", body: "The project no longer appears in your project list." },
  };
  const current = copy[state];
  if (state === "thinking") return <ThinkingState elapsed={thinkingElapsed} />;
  if (state === "revision") return <ChronologicalRevisionState mediaCount={mediaCount} onState={onState} projectName={projectName} />;

  return <div className="fade" data-testid={`${state}-state`}><p className="text-xs font-semibold uppercase tracking-[.18em] text-[#77776f]">{current.eyebrow}</p><h1 className="font-display mt-4 max-w-xl text-4xl font-medium leading-tight sm:text-5xl">{current.title}</h1><p className="mt-4 max-w-xl text-sm leading-6 text-[#686860]">{current.body}</p><p className="mt-7 text-xs text-[#85857e]" data-testid="media-count">{mediaCount} primary clips attached</p><div className="mt-8 flex flex-wrap gap-2">{state === "upload" && <><button className="rounded-lg border border-[#bdbdb5] px-4 py-2 text-sm">Add visuals (optional)</button><button className="rounded-lg border border-[#bdbdb5] px-4 py-2 text-sm" onClick={() => onState("confirm")}>Continue with footage</button></>}{state === "confirm" && <AgentApprovalCard className="w-full" title="Create this video?" description="Rendering starts only after you approve this direction." actions={<button className="min-h-11 rounded-full bg-[#0c0c0e] px-5 py-2.5 text-sm font-semibold text-white" onClick={() => onState("rendering")}>Confirm & render</button>} />}{state === "rendering" && <BeamLoader tone="light" mode="line" strength="medium" ariaLabel="Rendering your video"><div data-testid="render-progress" className="space-y-3 rounded-lg border border-[#deded9] bg-white/80 p-4"><div className="flex items-center justify-between text-sm font-medium"><span>Rendering your video</span><span>68%</span></div><div className="h-2 overflow-hidden rounded-full bg-[#e7e7e1]"><div className="h-full w-[68%] rounded-full bg-[#9ac34f]" /></div><p className="text-xs text-[#686860]">Assembling clips, sound, and captions.</p></div></BeamLoader>}{state === "ready" && <><button className="rounded-full bg-[#0c0c0e] px-5 py-2.5 text-sm font-semibold text-white">Play cut</button><button className="rounded-full border border-[#bdbdb5] px-5 py-2.5 text-sm" onClick={() => onState("ready", "editor")}>Open editor</button><button className="rounded-full border border-[#bdbdb5] px-5 py-2.5 text-sm">Download</button></>}{state === "upload-failed" && <><button className="rounded-lg bg-[#0c0c0e] px-4 py-2 text-sm font-semibold text-white" onClick={() => onState("upload")}>Retry upload</button><button className="rounded-lg border border-[#bdbdb5] px-4 py-2 text-sm" onClick={() => onState("upload")}>Remove file</button></>}{state === "voiceover" && <><button className="rounded-lg bg-[#0c0c0e] px-4 py-2 text-sm font-semibold text-white" onClick={() => onState("upload")}>Record voiceover</button><button className="rounded-lg border border-[#bdbdb5] px-4 py-2 text-sm" onClick={() => onState("choose")}>Change format</button></>}{state === "stale" && <button className="rounded-lg bg-[#0c0c0e] px-4 py-2.5 text-sm font-semibold text-white" onClick={() => onState("ready")}>Reload latest</button>}{state === "offline" && <button className="rounded-lg border border-[#bdbdb5] px-4 py-2 text-sm" onClick={() => onState("choose")}>Try again</button>}{state === "unavailable" && <><button className="rounded-lg bg-[#0c0c0e] px-4 py-2 text-sm font-semibold text-white" onClick={() => onState("choose")}>Choose Montage</button><button className="rounded-lg border border-[#bdbdb5] px-4 py-2 text-sm" onClick={() => onState("choose")}>Choose Talking to camera</button></>}{state === "partial" && <><BeamLoader tone="light" mode="pulse" active={false} ariaLabel="One render variant is ready"><div data-testid="partial-progress" className="space-y-2 rounded-lg border border-[#deded9] bg-white/80 p-4"><p className="text-sm font-medium">Original-audio cut · Ready</p><p className="text-xs text-[#686860]">Song-text variant · Needs another pass</p></div></BeamLoader><button className="rounded-full bg-[#0c0c0e] px-5 py-2.5 text-sm font-semibold text-white">Play available cut</button><button className="rounded-lg border border-[#bdbdb5] px-4 py-2 text-sm" onClick={() => onState("rendering")}>Retry variant</button></>}{state === "failed" && <><button className="rounded-lg bg-[#0c0c0e] px-4 py-2 text-sm font-semibold text-white" onClick={() => onState("rendering")}>Retry render</button><button className="rounded-lg border border-[#bdbdb5] px-4 py-2 text-sm" onClick={() => onState("revision")}>Adjust direction</button></>}</div></div>;
}

function ThinkingState({ elapsed }: { elapsed: number }) {
  const tier = elapsed < WAIT_LADDER_MS.QUIET ? "initial" : elapsed < WAIT_LADDER_MS.SPECIFIC ? "reading" : elapsed < WAIT_LADDER_MS.LONG ? "shaping" : "long";
  return <div className="fade" data-testid="thinking-state" data-thinking-tier={tier} data-thinking-elapsed={elapsed}><ChatThinking elapsedMs={elapsed} /></div>;
}

function ChronologicalRevisionState({ mediaCount, onState, projectName }: { mediaCount: number; onState: (state: FixtureState, view?: View) => void; projectName: string }) {
  return <div className="fade" data-testid="revision-state"><p className="text-xs font-semibold uppercase tracking-[.18em] text-[#77776f]">Revision</p><h1 className="font-display mt-4 max-w-xl text-4xl font-medium leading-tight sm:text-5xl">Your direction stays in sequence.</h1><div className="mt-6 space-y-3" data-testid="chronological-transcript" role="log" aria-label="Conversation history"><div data-testid="clips-section" className="rounded-xl border border-[#deded9] bg-[#f7f7f5] p-4"><p className="text-xs font-semibold uppercase tracking-[.14em] text-[#85857e]">Clips</p><p className="mt-2 text-sm font-medium">{mediaCount} clips attached to {projectName}</p></div><ChatMessage role="user" data-testid="post-clip-user-message">Hold the harbor shot longer.</ChatMessage><ChatMessage role="assistant" data-testid="post-clip-assistant-message">I’ll hold that shot, then prepare the exact revision for your confirmation.</ChatMessage><p data-testid="latest-chat-anchor" className="text-xs text-[#85857e]">Latest message · no scrolling upward required</p></div><div className="mt-6 flex flex-wrap gap-2"><button className="rounded-full bg-[#0c0c0e] px-5 py-2.5 text-sm font-semibold text-white" onClick={() => onState("rendering")}>Confirm revision</button></div></div>;
}

function DeletedState({ onRestore }: { onRestore: () => void }) {
  return <section className="flex min-w-0 flex-1 flex-col items-center justify-center bg-white p-6 text-center" data-testid="deleted-state"><p className="text-xs font-semibold uppercase tracking-[.18em] text-[#77776f]">Deleted</p><h1 className="font-display mt-4 text-4xl font-medium">Project deleted</h1><p className="mt-3 max-w-md text-sm leading-6 text-[#686860]">It has been removed from the project list. This fixture offers a restore action so the flow can be replayed.</p><button className="mt-7 rounded-full bg-[#0c0c0e] px-5 py-2.5 text-sm font-semibold text-white" onClick={onRestore}>Restore fixture</button></section>;
}

function RenameDialog({ value, onChange, onCancel, onSave }: { value: string; onChange: (value: string) => void; onCancel: () => void; onSave: () => void }) {
  return <div className="fixed inset-0 z-30 flex items-center justify-center bg-black/30 p-5" role="dialog" aria-modal="true" aria-labelledby="rename-title"><form className="w-full max-w-sm rounded-xl bg-white p-5 shadow-xl" onSubmit={(event) => { event.preventDefault(); onSave(); }}><h2 id="rename-title" className="text-lg font-semibold">Rename project</h2><label className="mt-4 block text-sm font-medium" htmlFor="rename-project">Project name</label><input id="rename-project" autoFocus value={value} onChange={(event) => onChange(event.target.value)} className="mt-2 w-full rounded-lg border border-[#cfcfc8] px-3 py-2 text-sm" /><div className="mt-5 flex justify-end gap-2"><button type="button" className="rounded-lg border border-[#bdbdb5] px-4 py-2 text-sm" onClick={onCancel}>Cancel</button><button type="submit" className="rounded-lg bg-[#0c0c0e] px-4 py-2 text-sm font-semibold text-white">Save name</button></div></form></div>;
}

function DeleteDialog({ projectName, onCancel, onDelete }: { projectName: string; onCancel: () => void; onDelete: () => void }) {
  return <div className="fixed inset-0 z-30 flex items-center justify-center bg-black/30 p-5" role="dialog" aria-modal="true" aria-labelledby="delete-title"><div className="w-full max-w-sm rounded-xl bg-white p-5 shadow-xl"><h2 id="delete-title" className="text-lg font-semibold">Delete project?</h2><p className="mt-2 text-sm leading-6 text-[#686860]">The chat, uploads, edit data, and completed Kria videos for “{projectName}” are permanently removed and cannot be recovered. Published TikTok posts remain.</p><div className="mt-5 flex justify-end gap-2"><button type="button" className="rounded-lg border border-[#bdbdb5] px-4 py-2 text-sm" onClick={onCancel}>Keep project</button><button type="button" className="rounded-lg bg-[#9d3c32] px-4 py-2 text-sm font-semibold text-white" onClick={onDelete}>Delete project</button></div></div></div>;
}
