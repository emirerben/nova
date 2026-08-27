"use client";

import Link from "next/link";
import { useMemo, useRef, useState } from "react";
import { ChevronLeft, RotateCcw } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { formatTimecode } from "@/lib/timeline/time-format";
import { ContextStrip } from "@/app/plan/items/[id]/_editor/ContextStrip";
import {
  MiniStrip,
  type MiniStripSegment,
  type MiniStripTrimPatch,
} from "@/app/plan/items/[id]/_editor/MiniStrip";
import { ToolDock, type DockTool } from "@/app/plan/items/[id]/_editor/ToolDock";
import {
  PauseIcon,
  PlayIcon,
} from "@/app/plan/items/[id]/_editor/editor-icons";

const SOURCE_URL = "/landing/raw-story/travel-render.mp4";
const SOURCE_DURATION_S = 11.21;

interface ClipWindow {
  id: string;
  inS: number;
  durationS: number;
  label: string;
}

const INITIAL_WINDOWS: ClipWindow[] = [
  { id: "lisbon", inS: 0, durationS: 3.6, label: "Lisbon arrival" },
  { id: "corfu", inS: 3.6, durationS: 3.1, label: "Corfu water" },
  { id: "istanbul", inS: 6.7, durationS: 3.8, label: "Istanbul night" },
];

type FixturePanel =
  | { kind: "precision" }
  | { kind: "tool"; tool: DockTool }
  | null;

const TOOL_COPY: Record<
  DockTool,
  { title: string; description: string; actions: string[] }
> = {
  nova: {
    title: "Kria",
    description: "Review a proposed edit before accepting or rejecting it.",
    actions: ["Review proposal", "Accept edit", "Reject edit"],
  },
  text: {
    title: "Text",
    description: "Add titles, place them, and apply a reusable preset.",
    actions: ["Add text", "Smart place", "Choose preset"],
  },
  captions: {
    title: "Captions",
    description:
      "Edit cue copy and timing, then style or retranscribe all cues.",
    actions: ["Edit cue", "Style all", "Retranscribe"],
  },
  visuals: {
    title: "Visuals",
    description: "Add source media, montage blocks, sequences, or text cards.",
    actions: ["Add media", "Add montage", "Add text card"],
  },
  sounds: {
    title: "Sounds",
    description:
      "Place sound effects, change the music window, and balance the mix.",
    actions: ["Add SFX", "Change music", "Adjust mix"],
  },
  overlays: {
    title: "Overlays",
    description: "Upload an overlay, retime it, and adjust its placement.",
    actions: ["Upload overlay", "Adjust timing", "Position overlay"],
  },
  styles: {
    title: "Styles",
    description: "Apply an edit-wide Look or refine a clip and its transition.",
    actions: ["Apply Look", "Adjust clip Look", "Set transition"],
  },
};

function projectSegments(windows: ClipWindow[]): MiniStripSegment[] {
  let cursor = 0;
  return windows.map((clip) => {
    const segment = {
      id: clip.id,
      startS: cursor,
      endS: cursor + clip.durationS,
      sourceUrl: SOURCE_URL,
      sourceId: "nova-travel-story",
      sourceStartS: clip.inS,
      sourceDurationS: SOURCE_DURATION_S,
      minDurationS: 0.1,
      label: clip.label,
      hasMarks: clip.id === "corfu",
    } satisfies MiniStripSegment;
    cursor = segment.endS;
    return segment;
  });
}

export default function MobileEditorFixture() {
  const videoRef = useRef<HTMLVideoElement>(null);
  const [windows, setWindows] = useState(INITIAL_WINDOWS);
  const [history, setHistory] = useState<ClipWindow[][]>([]);
  const [selectedId, setSelectedId] = useState(INITIAL_WINDOWS[0].id);
  const [currentTimeS, setCurrentTimeS] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [muted, setMuted] = useState(false);
  const [panel, setPanel] = useState<FixturePanel>(null);
  const [look, setLook] = useState("Clean");
  const [transition, setTransition] = useState("Cut");
  const [toolAction, setToolAction] = useState("");
  const [receipt, setReceipt] = useState("Draft · local QA fixture");
  const segments = useMemo(() => projectSegments(windows), [windows]);
  const totalDurationS = segments.at(-1)?.endS ?? 0;
  const selectedIndex = windows.findIndex((clip) => clip.id === selectedId);
  const selectedWindow = selectedIndex >= 0 ? windows[selectedIndex] : null;
  const selectedSegment = selectedIndex >= 0 ? segments[selectedIndex] : null;
  const splitOffsetS = selectedSegment
    ? currentTimeS - selectedSegment.startS
    : Number.NaN;
  const splitDisabledReason =
    !selectedWindow ||
    !selectedSegment ||
    splitOffsetS < 0.1 ||
    selectedWindow.durationS - splitOffsetS < 0.1
      ? "Move the playhead inside the selected clip to split"
      : null;
  const deleteDisabledReason =
    windows.length <= 1 ? "At least one clip must remain" : null;
  const activeTool = panel?.kind === "tool" ? panel.tool : null;

  const seekTo = (seconds: number) => {
    const next = Math.min(totalDurationS, Math.max(0, seconds));
    setCurrentTimeS(next);
    if (videoRef.current) videoRef.current.currentTime = next;
  };

  const record = () => {
    setHistory((current) => [...current, windows.map((clip) => ({ ...clip }))]);
    setReceipt("Unsaved changes");
  };

  const previewTrim = (id: string, patch: MiniStripTrimPatch) => {
    setWindows((current) =>
      current.map((clip) =>
        clip.id === id
          ? { ...clip, inS: patch.inS, durationS: patch.durationS }
          : clip,
      ),
    );
    setReceipt("Unsaved trim");
  };

  const nudgeSelectedTrim = (edge: "in" | "out") => {
    if (!selectedWindow) return;
    const nextDuration = Math.max(0.1, selectedWindow.durationS - 0.1);
    if (nextDuration === selectedWindow.durationS) {
      setReceipt("The clip is already at its minimum duration");
      return;
    }
    record();
    setWindows((current) =>
      current.map((clip) =>
        clip.id === selectedWindow.id
          ? {
              ...clip,
              inS: edge === "in" ? clip.inS + 0.1 : clip.inS,
              durationS: nextDuration,
            }
          : clip,
      ),
    );
    setReceipt(
      edge === "in" ? "Source In moved +0.1s" : "Source Out moved −0.1s",
    );
  };

  const splitSelected = () => {
    const index = segments.findIndex((segment) => segment.id === selectedId);
    const segment = segments[index];
    const clip = windows[index];
    if (!segment || !clip) return;
    const leftDuration = currentTimeS - segment.startS;
    if (leftDuration < 0.1 || clip.durationS - leftDuration < 0.1) return;
    record();
    const left = { ...clip, id: `${clip.id}-a`, durationS: leftDuration };
    const right = {
      ...clip,
      id: `${clip.id}-b`,
      inS: clip.inS + leftDuration,
      durationS: clip.durationS - leftDuration,
    };
    setWindows((current) => [
      ...current.slice(0, index),
      left,
      right,
      ...current.slice(index + 1),
    ]);
    setSelectedId(right.id);
    setReceipt("Split added · Undo available");
  };

  const deleteSelected = () => {
    if (windows.length <= 1) {
      setReceipt("At least one clip must remain");
      return;
    }
    record();
    const index = windows.findIndex((clip) => clip.id === selectedId);
    const next = windows.filter((clip) => clip.id !== selectedId);
    setWindows(next);
    setSelectedId(next[Math.min(Math.max(0, index), next.length - 1)].id);
    setReceipt("Clip deleted · Undo available");
  };

  const undo = () => {
    const previous = history.at(-1);
    if (!previous) return;
    setWindows(previous);
    setHistory((current) => current.slice(0, -1));
    setSelectedId(previous[0]?.id ?? "");
    setReceipt("Undid last timeline change");
  };

  return (
    <main className="fixed inset-0 z-50 grid grid-rows-[56px_minmax(0,1fr)_auto] overflow-hidden bg-background text-foreground">
      <output
        id="qa-state"
        className="hidden"
        data-windows={JSON.stringify(windows)}
        data-history-len={history.length}
        data-selected-id={selectedId}
        data-current-time={currentTimeS}
        data-receipt={receipt}
        data-panel={panel?.kind === "tool" ? panel.tool : panel?.kind ?? ""}
        data-look={look}
        data-transition={transition}
        data-tool-action={toolAction}
      />
      <header className="flex items-center gap-2 border-b border-border px-3">
        <Button asChild variant="ghost" size="icon" aria-label="Back to video">
          <Link href="/plan">
            <ChevronLeft aria-hidden="true" />
          </Link>
        </Button>
        <div className="min-w-0 flex-1">
          <p className="truncate text-sm font-medium">Summer in three cities</p>
          <p className="truncate text-xs text-muted-foreground">{receipt}</p>
        </div>
        <Button
          type="button"
          size="sm"
          onClick={() => setReceipt("Saved · rendering started")}
        >
          Save
        </Button>
      </header>

      <section className="relative flex min-h-0 items-center justify-center overflow-hidden bg-muted/30 p-3">
        <video
          ref={videoRef}
          src={SOURCE_URL}
          muted={muted}
          playsInline
          className="h-full max-w-full rounded-xl border border-border bg-black object-cover shadow-sm"
          style={{ aspectRatio: "9 / 16" }}
          onPlay={() => setPlaying(true)}
          onPause={() => setPlaying(false)}
          onTimeUpdate={(event) => setCurrentTimeS(event.currentTarget.currentTime)}
        />
        {history.length > 0 && (
          <Button
            type="button"
            variant="outline"
            size="icon"
            aria-label="Undo"
            onClick={undo}
            className="absolute left-3 top-3 bg-background/95"
          >
            <RotateCcw aria-hidden="true" />
          </Button>
        )}
      </section>

      <section className="min-w-0 bg-background">
        <div className="flex items-center gap-3 border-t border-border px-3 py-2">
          <Button
            type="button"
            variant="ghost"
            size="icon"
            aria-label={playing ? "Pause video" : "Play video"}
            className="size-11"
            onClick={() => {
              const video = videoRef.current;
              if (!video) return;
              if (video.paused) void video.play();
              else video.pause();
            }}
          >
            {playing ? (
              <PauseIcon className="size-5" />
            ) : (
              <PlayIcon className="size-5" />
            )}
          </Button>
          <p className="ml-auto text-sm tabular-nums text-muted-foreground">
            {formatTimecode(currentTimeS)} / {formatTimecode(totalDurationS)}
          </p>
        </div>

        <MiniStrip
          segments={segments}
          durationS={totalDurationS}
          currentTimeS={currentTimeS}
          selectedClipId={selectedId}
          onScrubStart={() => videoRef.current?.pause()}
          onScrub={seekTo}
          onSelectClip={(id, seconds) => {
            setSelectedId(id);
            seekTo(seconds);
          }}
          onTrimStart={record}
          onPreviewTrim={previewTrim}
          onDisabledTap={(reason) => setReceipt(reason)}
        />

        <ContextStrip
          selection={{
            type: "clip",
            onAdjust: () => {
              setPanel({ kind: "precision" });
              setReceipt("Precision controls opened");
            },
            onSplit: splitSelected,
            splitDisabledReason,
            muted,
            onToggleMute: () => setMuted((current) => !current),
            onDelete: deleteSelected,
            deleteDisabledReason,
          }}
          onDisabledTap={(reason) => setReceipt(reason)}
          className="border-t border-border px-2 py-1"
        />

        <ToolDock
          activeTool={activeTool}
          disabledTools={{}}
          novaEnabled
          onToggleTool={(tool) => {
            const closing = panel?.kind === "tool" && panel.tool === tool;
            setPanel(closing ? null : { kind: "tool", tool });
            setReceipt(
              closing
                ? `${tool === "nova" ? "Kria" : TOOL_COPY[tool].title} tools closed`
                : `${TOOL_COPY[tool].title} tools opened`,
            );
          }}
          onDisabledTap={(reason) => setReceipt(reason)}
        />
      </section>

      <Sheet
        open={panel !== null}
        onOpenChange={(open) => {
          if (!open) setPanel(null);
        }}
      >
        <SheetContent
          side="bottom"
          className="max-h-[62dvh] overflow-y-auto p-0 pb-[max(16px,env(safe-area-inset-bottom))]"
        >
          {panel?.kind === "precision" && selectedWindow ? (
            <>
              <SheetHeader className="border-b border-border px-4 pb-3 pt-4 text-left">
                <SheetTitle className="text-base text-balance">
                  Clip {selectedIndex + 1} adjustments
                </SheetTitle>
                <SheetDescription className="text-pretty">
                  Refine source timing, Look, and the outgoing transition.
                </SheetDescription>
              </SheetHeader>
              <Tabs defaultValue="trim" className="px-4 pt-3">
                <TabsList className="grid h-11 w-full grid-cols-3">
                  <TabsTrigger value="trim" className="min-h-11">
                    Trim
                  </TabsTrigger>
                  <TabsTrigger value="look" className="min-h-11">
                    Look
                  </TabsTrigger>
                  <TabsTrigger value="transition" className="min-h-11">
                    Transition
                  </TabsTrigger>
                </TabsList>
                <TabsContent value="trim" className="mt-4 space-y-3">
                  <div className="grid grid-cols-3 gap-2 rounded-lg border border-border p-3 text-center">
                    <div>
                      <p className="text-xs text-muted-foreground">Source In</p>
                      <p className="mt-1 text-sm font-medium tabular-nums">
                        {formatTimecode(selectedWindow.inS)}
                      </p>
                    </div>
                    <div>
                      <p className="text-xs text-muted-foreground">Source Out</p>
                      <p className="mt-1 text-sm font-medium tabular-nums">
                        {formatTimecode(
                          selectedWindow.inS + selectedWindow.durationS,
                        )}
                      </p>
                    </div>
                    <div>
                      <p className="text-xs text-muted-foreground">Output</p>
                      <p className="mt-1 text-sm font-medium tabular-nums">
                        {formatTimecode(selectedWindow.durationS)}
                      </p>
                    </div>
                  </div>
                  <div className="grid grid-cols-2 gap-2">
                    <Button
                      type="button"
                      variant="outline"
                      className="min-h-11"
                      onClick={() => nudgeSelectedTrim("in")}
                    >
                      In +0.1s
                    </Button>
                    <Button
                      type="button"
                      variant="outline"
                      className="min-h-11"
                      onClick={() => nudgeSelectedTrim("out")}
                    >
                      Out −0.1s
                    </Button>
                  </div>
                </TabsContent>
                <TabsContent value="look" className="mt-4 grid grid-cols-3 gap-2">
                  {["Clean", "Warm", "Film"].map((option) => (
                    <Button
                      key={option}
                      type="button"
                      variant={look === option ? "secondary" : "outline"}
                      className="min-h-11"
                      aria-pressed={look === option}
                      onClick={() => {
                        setLook(option);
                        setReceipt(`${option} Look applied`);
                      }}
                    >
                      {option}
                    </Button>
                  ))}
                </TabsContent>
                <TabsContent value="transition" className="mt-4 grid grid-cols-3 gap-2">
                  {["Cut", "Dissolve", "Dip"].map((option) => (
                    <Button
                      key={option}
                      type="button"
                      variant={transition === option ? "secondary" : "outline"}
                      className="min-h-11"
                      aria-pressed={transition === option}
                      onClick={() => {
                        setTransition(option);
                        setReceipt(`${option} transition applied`);
                      }}
                    >
                      {option}
                    </Button>
                  ))}
                </TabsContent>
              </Tabs>
            </>
          ) : panel?.kind === "tool" ? (
            <>
              <SheetHeader className="border-b border-border px-4 pb-3 pt-4 text-left">
                <SheetTitle className="text-base text-balance">
                  {TOOL_COPY[panel.tool].title}
                </SheetTitle>
                <SheetDescription className="text-pretty">
                  {TOOL_COPY[panel.tool].description}
                </SheetDescription>
              </SheetHeader>
              <div className="grid gap-2 px-4 pt-4">
                {TOOL_COPY[panel.tool].actions.map((action, index) => (
                  <Button
                    key={action}
                    type="button"
                    variant={index === 0 ? "secondary" : "outline"}
                    className="min-h-11 justify-start"
                    onClick={() => {
                      setToolAction(`${panel.tool}:${action}`);
                      setReceipt(`${action} selected`);
                    }}
                  >
                    {action}
                  </Button>
                ))}
              </div>
            </>
          ) : null}
        </SheetContent>
      </Sheet>
    </main>
  );
}
