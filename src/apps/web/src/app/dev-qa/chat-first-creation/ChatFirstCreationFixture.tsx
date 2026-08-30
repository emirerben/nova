"use client";

import { useEffect, useMemo, useState } from "react";

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
  | "failed";
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
];

const FORMAT_COPY = {
  montage: { title: "Montage", detail: "A quick, music-led cut", tone: "Classic" },
  narrated: { title: "Narrated", detail: "Your voice tells the story", tone: "Voiceover" },
  talking: { title: "Talking to camera", detail: "Keep the best moments of you", tone: "Captions" },
} as const;

function readParam(name: string, fallback: string) {
  if (typeof window === "undefined") return fallback;
  const value = new URLSearchParams(window.location.search).get(name);
  return value ?? fallback;
}

export default function ChatFirstCreationFixture() {
  const initialState = readParam("state", "choose") as FixtureState;
  const initialView = readParam("view", "chat") as View;
  const [state, setState] = useState<FixtureState>(STATES.includes(initialState) ? initialState : "choose");
  const [view, setView] = useState<View>(initialView === "editor" ? "editor" : "chat");
  const [mediaCount, setMediaCount] = useState(state === "choose" ? 0 : 3);
  const [format, setFormat] = useState<keyof typeof FORMAT_COPY>("montage");
  const [composer, setComposer] = useState("");
  const [reducedMotion, setReducedMotion] = useState(false);

  useEffect(() => {
    const onOnline = () => setState("choose");
    window.addEventListener("online", onOnline);
    return () => window.removeEventListener("online", onOnline);
  }, []);

  const projectStatus = useMemo(() => {
    if (state === "ready" || state === "revision" || state === "partial") return "Ready to review";
    if (state === "rendering") return "Rendering";
    if (state === "failed") return "Needs attention";
    return "In progress";
  }, [state]);

  function navigate(nextState: FixtureState, nextView: View = "chat") {
    setState(nextState);
    setView(nextView);
    if (nextState !== "choose") setMediaCount((count) => Math.max(count, 3));
    const params = new URLSearchParams({ state: nextState, view: nextView });
    window.history.replaceState(null, "", `/dev-qa/chat-first-creation?${params}`);
  }

  function selectView(nextView: View) {
    setView(nextView);
    const params = new URLSearchParams({ state, view: nextView });
    window.history.replaceState(null, "", `/dev-qa/chat-first-creation?${params}`);
  }

  return (
    <main
      className={`chat-fixture flex h-dvh min-h-0 overflow-hidden bg-[#f7f7f5] text-[#0c0c0e] ${
        reducedMotion ? "chat-fixture-reduced-motion" : ""
      }`}
      data-testid="chat-first-creation-fixture"
      data-state={state}
      data-view={view}
    >
      <style>{`
        .chat-fixture * { box-sizing: border-box; }
        .chat-fixture button, .chat-fixture input, .chat-fixture textarea { font: inherit; }
        .chat-fixture button { cursor: pointer; }
        .chat-fixture .fade { animation: fixture-fade 220ms ease-out both; }
        .chat-fixture-reduced-motion .fade { animation: none; }
        .chat-fixture[data-view="editor"] .editor-pane { display: flex; }
        .chat-fixture[data-view="editor"] .chat-rail { flex: 0 0 420px; }
        @keyframes fixture-fade { from { opacity: .1; transform: translateY(5px); } to { opacity: 1; transform: none; } }
        @media (max-width: 767px) { .chat-fixture .project-rail { display:none; } .chat-fixture .chat-rail { width:100%; border:0; } .chat-fixture .editor-pane { display:none; } .chat-fixture[data-view="editor"] .chat-rail { display:none; } .chat-fixture[data-view="editor"] .editor-pane { display:flex; width:100%; } }
      `}</style>

      <aside className="project-rail flex w-[260px] shrink-0 flex-col border-r border-[#deded9] bg-[#f1f1ee] p-5" aria-label="Projects">
        <div className="flex items-center justify-between">
          <span className="font-display text-xl font-medium">Kria</span>
          <button aria-label="Close projects" className="rounded-full p-2 text-[#6e6e68] hover:bg-white">×</button>
        </div>
        <button className="mt-8 flex items-center justify-between rounded-lg bg-[#d7ff90] px-3 py-3 text-left text-sm font-semibold" onClick={() => navigate("choose")}>
          <span>New project</span><span aria-hidden>＋</span>
        </button>
        <p className="mt-8 text-[11px] font-semibold uppercase tracking-[.18em] text-[#8c8c85]">Projects</p>
        <button className="mt-3 rounded-lg bg-white px-3 py-3 text-left shadow-sm" onClick={() => selectView("chat")}>
          <span className="block text-sm font-medium">Weekend in Corfu</span>
          <span className="mt-1 block text-xs text-[#7c7c75]">{projectStatus}</span>
        </button>
        <button className="mt-2 rounded-lg px-3 py-3 text-left text-sm text-[#6e6e68]" onClick={() => selectView("gallery")}>Gallery</button>
        <div className="mt-auto border-t border-[#deded9] pt-4 text-xs text-[#7c7c75]">emir@example.com</div>
      </aside>

      {view === "projects" ? (
        <section className="flex min-w-0 flex-1 flex-col bg-white p-6" aria-label="Project list">
          <button className="self-start text-sm text-[#6e6e68]" onClick={() => selectView("chat")}>← Back to chat</button>
          <h1 className="font-display mt-10 text-4xl font-medium">Projects</h1>
          <div className="mt-8 grid gap-3 sm:grid-cols-2">
            <button className="rounded-xl border border-[#deded9] bg-[#f7f7f5] p-5 text-left" onClick={() => selectView("chat")}><span className="block font-medium">Weekend in Corfu</span><span className="mt-1 block text-sm text-[#7c7c75]">{projectStatus}</span></button>
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
            <header className="flex h-14 shrink-0 items-center justify-between border-b border-[#ededE8] px-5">
              <div className="min-w-0"><p className="truncate text-sm font-medium">Weekend in Corfu</p><p className="text-[11px] text-[#85857e]">{projectStatus}</p></div>
              <div className="flex items-center gap-2"><button className="rounded-md px-2 py-1 text-xs text-[#6e6e68] hover:bg-[#f1f1ee]" onClick={() => selectView("projects")}>Projects</button><button className="rounded-md px-2 py-1 text-xs text-[#6e6e68] hover:bg-[#f1f1ee]" onClick={() => selectView("gallery")}>Gallery</button></div>
            </header>
            <div className="flex-1 overflow-y-auto px-5 py-7 sm:px-10">
              <div className="mx-auto max-w-[620px]">
                {state === "choose" ? <ChooseState format={format} setFormat={setFormat} onContinue={() => navigate("upload")} /> : <ConversationState state={state as Exclude<FixtureState, "choose">} mediaCount={mediaCount} onState={navigate} />}
              </div>
            </div>
            <div className="shrink-0 border-t border-[#ededE8] bg-white p-4 pb-[max(1rem,env(safe-area-inset-bottom))] sm:px-10">
              <div className="mx-auto flex max-w-[620px] items-end gap-2 rounded-xl border border-[#cfcfc8] bg-[#fafaf8] p-2 focus-within:border-[#8c8c85]">
                <label className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg bg-[#d7ff90] text-lg" aria-label="Attach video, image, or audio"><input className="sr-only" type="file" accept="video/*,image/*,audio/*" onChange={() => { setMediaCount((count) => count + 1); navigate("upload"); }} />＋</label>
                <textarea aria-label="Message Kria" value={composer} onChange={(event) => setComposer(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); navigate(state === "ready" ? "revision" : "confirm"); } }} placeholder="Tell Kria what you want to make…" rows={1} className="max-h-28 min-h-10 flex-1 resize-none bg-transparent px-2 py-2 text-sm outline-none placeholder:text-[#9b9b94]" />
                <button aria-label="Send message" className="h-10 rounded-lg bg-[#0c0c0e] px-4 text-sm font-semibold text-white disabled:opacity-40" disabled={!composer.trim()} onClick={() => { setComposer(""); navigate(state === "ready" ? "revision" : "confirm"); }}>Send</button>
              </div>
              <p className="mx-auto mt-2 max-w-[620px] text-center text-[11px] text-[#a0a098]">Kria won’t render until you confirm the direction.</p>
            </div>
          </section>
          <section className="editor-pane hidden min-w-0 flex-1 items-center justify-center bg-[#20201e]" aria-label="Embedded editor">
            <div className="flex h-full w-full flex-col"><div className="flex h-14 shrink-0 items-center justify-between border-b border-white/10 px-5 text-white"><span className="text-sm">Editor</span><button className="rounded-md border border-white/20 px-3 py-1 text-xs" onClick={() => selectView("chat")}>Back to chat</button></div><div className="flex flex-1 items-center justify-center text-center text-sm text-white/60"><div><div className="mx-auto aspect-[9/16] w-48 rounded-lg bg-[#373733] shadow-2xl" data-testid="embedded-editor-canvas" /><p className="mt-4">Embedded EditorShell · overlay mode</p></div></div></div>
          </section>
        </>
      )}
      <button data-testid="reduced-motion-toggle" className="sr-only" onClick={() => setReducedMotion((value) => !value)}>Toggle reduced motion</button>
    </main>
  );
}

function ChooseState({ format, setFormat, onContinue }: { format: keyof typeof FORMAT_COPY; setFormat: (format: keyof typeof FORMAT_COPY) => void; onContinue: () => void }) {
  return <div className="fade" data-testid="choose-state"><p className="text-xs font-semibold uppercase tracking-[.18em] text-[#77776f]">Start with a direction</p><h1 className="font-display mt-4 text-4xl font-medium leading-tight sm:text-5xl">What are we making?</h1><p className="mt-3 max-w-lg text-sm leading-6 text-[#686860]">Pick a format, add your footage, and talk me through the feeling you want.</p><div className="mt-8 flex snap-x gap-3 overflow-x-auto pb-2" role="radiogroup" aria-label="Video format">{(Object.keys(FORMAT_COPY) as Array<keyof typeof FORMAT_COPY>).map((key) => { const copy = FORMAT_COPY[key]; return <button key={key} role="radio" aria-checked={format === key} data-format={key} className={`min-w-[210px] snap-start rounded-xl border p-4 text-left transition ${format === key ? "border-[#0c0c0e] bg-[#d7ff90]" : "border-[#deded9] bg-[#f7f7f5]"}`} onClick={() => setFormat(key)}><span className="block text-sm font-semibold">{copy.title}</span><span className="mt-2 block text-xs leading-5 text-[#686860]">{copy.detail}</span><span className="mt-7 block text-[11px] uppercase tracking-[.14em] text-[#77776f]">{copy.tone}</span></button>; })}</div><button className="mt-8 rounded-full bg-[#0c0c0e] px-6 py-3 text-sm font-semibold text-white" onClick={onContinue}>Add footage <span aria-hidden>→</span></button></div>;
}

function ConversationState({ state, mediaCount, onState }: { state: Exclude<FixtureState, "choose">; mediaCount: number; onState: (state: FixtureState, view?: View) => void }) {
  const copy: Record<Exclude<FixtureState, "choose">, { eyebrow: string; title: string; body: string }> = {
    upload: { eyebrow: "Footage", title: "Show me the moments.", body: "Add videos, photos, or audio. I’ll keep the story moving and leave anything uncertain for you to decide." },
    confirm: { eyebrow: "Direction", title: "Here’s the cut I’m proposing.", body: "A bright, quick montage with the ferry arrival first, then the blue-water swim. Keep the laughs and let the music breathe between scenes." },
    rendering: { eyebrow: "Rendering", title: "Your cut is in the oven.", body: "I’m assembling the footage and sound now. You can keep chatting; any new direction will wait for this render to finish." },
    ready: { eyebrow: "Ready", title: "Your first cut is ready.", body: "Play it through, download it, or open the editor. If something feels off, tell me exactly what to change and I’ll prepare a new render for your confirmation." },
    revision: { eyebrow: "Revision", title: "I’ve queued that direction.", body: "I’ll make the opening slower and hold the harbor shot longer. Confirm this exact change when you’re ready for the next render." },
    "upload-failed": { eyebrow: "Couldn’t upload", title: "One file didn’t make it.", body: "Your other footage is safe. Retry the upload or remove the failed file before continuing." },
    voiceover: { eyebrow: "Voiceover needed", title: "Your voice will make this work.", body: "Record a short voiceover here, or switch to Montage and let the footage lead." },
    stale: { eyebrow: "Newer version", title: "This project changed elsewhere.", body: "Reload the latest direction before sending it. Your draft message will stay in the composer." },
    offline: { eyebrow: "Reconnecting", title: "You’re offline for a moment.", body: "Your message is saved locally and will send when the connection returns." },
    unavailable: { eyebrow: "Format unavailable", title: "That format isn’t ready here yet.", body: "Try Montage or Talking to camera while we finish setting this one up." },
    partial: { eyebrow: "Partially ready", title: "One version is ready to watch.", body: "The original-audio cut is ready. A second variant needs another try; you can play the available cut now." },
    failed: { eyebrow: "Render stopped", title: "That render didn’t finish.", body: "Your footage and direction are still here. Retry the render or adjust the direction and try again." },
  };
  const current = copy[state];
  return <div className="fade" data-testid={`${state}-state`}><p className="text-xs font-semibold uppercase tracking-[.18em] text-[#77776f]">{current.eyebrow}</p><h1 className="font-display mt-4 max-w-xl text-4xl font-medium leading-tight sm:text-5xl">{current.title}</h1><p className="mt-4 max-w-xl text-sm leading-6 text-[#686860]">{current.body}</p><p className="mt-7 text-xs text-[#85857e]" data-testid="media-count">{mediaCount} media items attached</p><div className="mt-8 flex flex-wrap gap-2">{state === "upload" && <button className="rounded-lg border border-[#bdbdb5] px-4 py-2 text-sm" onClick={() => onState("confirm")}>Continue with footage</button>}{state === "confirm" && <button className="rounded-full bg-[#0c0c0e] px-5 py-2.5 text-sm font-semibold text-white" onClick={() => onState("rendering")}>Confirm & render</button>}{state === "rendering" && <span className="rounded-full bg-[#f1f1ee] px-4 py-2 text-sm text-[#686860]">Rendering…</span>}{state === "ready" && <><button className="rounded-full bg-[#0c0c0e] px-5 py-2.5 text-sm font-semibold text-white">Play cut</button><button className="rounded-full border border-[#bdbdb5] px-5 py-2.5 text-sm" onClick={() => onState("ready", "editor")}>Open editor</button><button className="rounded-full border border-[#bdbdb5] px-5 py-2.5 text-sm">Download</button></>}{state === "revision" && <button className="rounded-full bg-[#0c0c0e] px-5 py-2.5 text-sm font-semibold text-white" onClick={() => onState("rendering")}>Confirm revision</button>}{state === "upload-failed" && <><button className="rounded-lg bg-[#0c0c0e] px-4 py-2 text-sm font-semibold text-white" onClick={() => onState("upload")}>Retry upload</button><button className="rounded-lg border border-[#bdbdb5] px-4 py-2 text-sm" onClick={() => onState("upload")}>Remove file</button></>}{state === "voiceover" && <><button className="rounded-lg bg-[#0c0c0e] px-4 py-2 text-sm font-semibold text-white" onClick={() => onState("upload")}>Record voiceover</button><button className="rounded-lg border border-[#bdbdb5] px-4 py-2 text-sm" onClick={() => onState("choose")}>Change format</button></>}{state === "stale" && <button className="rounded-lg bg-[#0c0c0e] px-4 py-2 text-sm font-semibold text-white" onClick={() => onState("ready")}>Reload latest</button>}{state === "offline" && <button className="rounded-lg border border-[#bdbdb5] px-4 py-2 text-sm" onClick={() => onState("choose")}>Try again</button>}{state === "unavailable" && <><button className="rounded-lg bg-[#0c0c0e] px-4 py-2 text-sm font-semibold text-white" onClick={() => onState("choose")}>Choose Montage</button><button className="rounded-lg border border-[#bdbdb5] px-4 py-2 text-sm" onClick={() => onState("choose")}>Choose Talking to camera</button></>}{state === "partial" && <><button className="rounded-full bg-[#0c0c0e] px-5 py-2.5 text-sm font-semibold text-white">Play available cut</button><button className="rounded-lg border border-[#bdbdb5] px-4 py-2 text-sm" onClick={() => onState("rendering")}>Retry variant</button></>}{state === "failed" && <><button className="rounded-lg bg-[#0c0c0e] px-4 py-2 text-sm font-semibold text-white" onClick={() => onState("rendering")}>Retry render</button><button className="rounded-lg border border-[#bdbdb5] px-4 py-2 text-sm" onClick={() => onState("revision")}>Adjust direction</button></>}</div></div>;
}
