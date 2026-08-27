"use client";

import { useMemo, useRef, useState } from "react";
import { ChevronLeft, RotateCcw } from "lucide-react";
import { Button } from "@/components/ui/button";
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
  const [activeTool, setActiveTool] = useState<DockTool | null>(null);
  const [receipt, setReceipt] = useState("Draft · local QA fixture");
  const segments = useMemo(() => projectSegments(windows), [windows]);
  const totalDurationS = segments.at(-1)?.endS ?? 0;

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
      />
      <header className="flex items-center gap-2 border-b border-border px-3">
        <Button type="button" variant="ghost" size="icon" aria-label="Back to video">
          <ChevronLeft aria-hidden="true" />
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
            onAdjust: () => setReceipt("Precision sheet · Trim / Look / Transition"),
            onSplit: splitSelected,
            splitDisabledReason: null,
            muted,
            onToggleMute: () => setMuted((current) => !current),
            onDelete: deleteSelected,
          }}
          onDisabledTap={(reason) => setReceipt(reason)}
          className="border-t border-border px-2 py-1"
        />

        <ToolDock
          activeTool={activeTool}
          disabledTools={{}}
          novaEnabled
          onToggleTool={(tool) => {
            setActiveTool((current) => (current === tool ? null : tool));
            setReceipt(`${tool === "nova" ? "Kria" : tool} tools selected`);
          }}
          onDisabledTap={(reason) => setReceipt(reason)}
        />
      </section>
    </main>
  );
}
