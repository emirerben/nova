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
import { Input } from "@/components/ui/input";
import { Slider } from "@/components/ui/slider";
import { Textarea } from "@/components/ui/textarea";
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
  | { kind: "text" }
  | { kind: "tool"; tool: DockTool }
  | { kind: "action"; tool: DockTool; action: string }
  | null;

interface FixtureSnapshot {
  windows: ClipWindow[];
  muted: boolean;
  look: string;
  clipLook: string;
  transition: string;
  textDraft: { id: string; text: string; position: string; preset: string } | null;
  caption: { text: string; style: string; status: string };
  musicTrack: string;
  musicGain: number;
  sfx: string[];
  visuals: string[];
  overlay: { name: string; durationS: number; position: string } | null;
  kriaStatus: string;
}

const MUSIC_TRACKS = ["City Lights", "Golden Hour", "Midnight Ferry"];
const SFX_OPTIONS = ["Camera click", "Whoosh", "Soft impact"];
const LOOK_OPTIONS = ["Clean", "Warm", "Film"];

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
  const captionEditRecordedRef = useRef(false);
  const [windows, setWindows] = useState(INITIAL_WINDOWS);
  const [history, setHistory] = useState<FixtureSnapshot[]>([]);
  const [selectedId, setSelectedId] = useState(INITIAL_WINDOWS[0].id);
  const [currentTimeS, setCurrentTimeS] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [muted, setMuted] = useState(false);
  const [panel, setPanel] = useState<FixturePanel>(null);
  const [look, setLook] = useState("Clean");
  const [clipLook, setClipLook] = useState("Clean");
  const [transition, setTransition] = useState("Cut");
  const [toolAction, setToolAction] = useState("");
  const [textDraft, setTextDraft] = useState<{
    id: string;
    text: string;
    position: string;
    preset: string;
  } | null>(null);
  const [caption, setCaption] = useState({
    text: "Three cities. One unforgettable summer.",
    style: "Clean",
    status: "Ready",
  });
  const [musicTrack, setMusicTrack] = useState(MUSIC_TRACKS[0]);
  const [musicGain, setMusicGain] = useState(70);
  const [sfx, setSfx] = useState<string[]>([]);
  const [visuals, setVisuals] = useState<string[]>([]);
  const [overlay, setOverlay] = useState<{
    name: string;
    durationS: number;
    position: string;
  } | null>(null);
  const [kriaStatus, setKriaStatus] = useState("Proposal ready");
  const [saveState, setSaveState] = useState<"idle" | "rendering">("idle");
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
  const activeTool =
    panel?.kind === "text"
      ? "text"
      : panel?.kind === "tool" || panel?.kind === "action"
        ? panel.tool
        : null;

  const seekTo = (seconds: number) => {
    const next = Math.min(totalDurationS, Math.max(0, seconds));
    setCurrentTimeS(next);
    if (videoRef.current) videoRef.current.currentTime = next;
  };

  const record = () => {
    const snapshot: FixtureSnapshot = {
      windows: windows.map((clip) => ({ ...clip })),
      muted,
      look,
      clipLook,
      transition,
      textDraft: textDraft ? { ...textDraft } : null,
      caption: { ...caption },
      musicTrack,
      musicGain,
      sfx: [...sfx],
      visuals: [...visuals],
      overlay: overlay ? { ...overlay } : null,
      kriaStatus,
    };
    setHistory((current) => [...current, snapshot]);
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
    setWindows(previous.windows);
    setMuted(previous.muted);
    setLook(previous.look);
    setClipLook(previous.clipLook);
    setTransition(previous.transition);
    setTextDraft(previous.textDraft);
    setCaption(previous.caption);
    setMusicTrack(previous.musicTrack);
    setMusicGain(previous.musicGain);
    setSfx(previous.sfx);
    setVisuals(previous.visuals);
    setOverlay(previous.overlay);
    setKriaStatus(previous.kriaStatus);
    setHistory((current) => current.slice(0, -1));
    setSelectedId(previous.windows[0]?.id ?? "");
    setReceipt("Undid last editor change");
  };

  const addText = (text = "Add a title", preset = "Title") => {
    record();
    setTextDraft({
      id: crypto.randomUUID(),
      text,
      position: "Center",
      preset,
    });
    setPanel({ kind: "text" });
    setToolAction("text:Add text");
    setReceipt("Text added · edit it now");
  };

  const openToolAction = (tool: DockTool, action: string) => {
    captionEditRecordedRef.current = false;
    setToolAction(`${tool}:${action}`);
    setPanel({ kind: "action", tool, action });
    setReceipt(`${action} opened`);
  };

  const acceptKriaProposal = () => {
    record();
    setWindows((current) =>
      current.map((clip, index) =>
        index === 0
          ? { ...clip, durationS: Math.max(0.1, clip.durationS - 0.2) }
          : clip,
      ),
    );
    setKriaStatus("Proposal applied");
    setReceipt("Kria edit applied · Undo available");
    setPanel(null);
  };

  const rejectKriaProposal = () => {
    record();
    setKriaStatus("Proposal rejected");
    setReceipt("Kria proposal rejected");
    setPanel(null);
  };

  const renderChoiceButtons = (
    options: string[],
    selected: string | null,
    onSelect: (option: string) => void,
  ) => (
    <div className="grid grid-cols-2 gap-2">
      {options.map((option) => (
        <Button
          key={option}
          type="button"
          variant={selected === option ? "secondary" : "outline"}
          className="min-h-11 justify-start"
          aria-pressed={selected === option}
          onClick={() => onSelect(option)}
        >
          {option}
        </Button>
      ))}
    </div>
  );

  const renderActionContent = (tool: DockTool, action: string) => {
    if (tool === "nova") {
      if (action === "Review proposal") {
        return (
          <div className="space-y-3">
            <div className="rounded-lg border border-border bg-muted/40 p-3">
              <p className="text-sm font-medium">Tighten the opening</p>
              <p className="mt-1 text-sm text-muted-foreground">
                Remove 0.2s from the first clip so the city reveal starts faster.
              </p>
              <p className="mt-2 text-xs font-medium text-foreground">
                {kriaStatus}
              </p>
            </div>
            <div className="grid grid-cols-2 gap-2">
              <Button type="button" className="min-h-11" onClick={acceptKriaProposal}>
                Accept edit
              </Button>
              <Button
                type="button"
                variant="outline"
                className="min-h-11"
                onClick={rejectKriaProposal}
              >
                Reject edit
              </Button>
            </div>
          </div>
        );
      }
      return (
        <Button
          type="button"
          variant={action === "Reject edit" ? "outline" : "default"}
          className="min-h-11 w-full"
          onClick={
            action === "Reject edit" ? rejectKriaProposal : acceptKriaProposal
          }
        >
          {action === "Reject edit" ? "Reject proposal" : "Apply proposal"}
        </Button>
      );
    }

    if (tool === "captions") {
      if (action === "Edit cue") {
        return (
          <div className="space-y-3">
            <Textarea
              autoFocus
              aria-label="Caption cue text"
              className="min-h-24 text-base"
              value={caption.text}
              onChange={(event) => {
                const text = event.currentTarget.value;
                if (!captionEditRecordedRef.current) {
                  record();
                  captionEditRecordedRef.current = true;
                }
                setCaption((current) => ({
                  ...current,
                  text,
                  status: "Edited",
                }));
                setReceipt("Caption cue edited");
              }}
            />
            <p className="text-xs text-muted-foreground">
              0:00–0:03 · {caption.status}
            </p>
          </div>
        );
      }
      if (action === "Style all") {
        return renderChoiceButtons(
          ["Clean", "Editorial", "Lime"],
          caption.style,
          (style) => {
            record();
            setCaption((current) => ({ ...current, style }));
            setReceipt(`${style} caption style applied`);
          },
        );
      }
      return (
        <div className="space-y-3">
          <p className="text-sm text-muted-foreground">
            Rebuild cues from the source audio while preserving global styling.
          </p>
          <Button
            type="button"
            className="min-h-11 w-full"
            onClick={() => {
              record();
              setCaption((current) => ({
                ...current,
                text: "Lisbon, Corfu, then Istanbul.",
                status: "Retranscribed",
              }));
              setReceipt("Captions retranscribed");
            }}
          >
            Retranscribe captions
          </Button>
        </div>
      );
    }

    if (tool === "visuals") {
      if (action === "Add media") {
        return renderChoiceButtons(
          ["Bridge photo", "Sea texture", "Night market"],
          null,
          (visual) => {
            record();
            setVisuals((current) => [...current, visual]);
            setReceipt(`${visual} added to Visuals`);
          },
        );
      }
      if (action === "Add text card") {
        return (
          <Button
            type="button"
            className="min-h-11 w-full"
            onClick={() => addText("Three cities, one summer", "Editorial")}
          >
            Add text card
          </Button>
        );
      }
      return (
        <Button
          type="button"
          className="min-h-11 w-full"
          onClick={() => {
            record();
            setVisuals((current) => [...current, "3-shot montage"]);
            setReceipt("Montage block added");
          }}
        >
          Add 3-shot montage
        </Button>
      );
    }

    if (tool === "sounds") {
      if (action === "Add SFX") {
        return renderChoiceButtons(SFX_OPTIONS, null, (effect) => {
          record();
          setSfx((current) => [...current, effect]);
          setReceipt(`${effect} added at ${formatTimecode(currentTimeS)}`);
        });
      }
      if (action === "Change music") {
        return renderChoiceButtons(MUSIC_TRACKS, musicTrack, (track) => {
          record();
          setMusicTrack(track);
          setReceipt(`Music changed to ${track}`);
        });
      }
      return (
        <div className="space-y-4">
          <div className="flex items-center justify-between text-sm">
            <span>Music level</span>
            <span className="tabular-nums text-muted-foreground">{musicGain}%</span>
          </div>
          <Slider
            aria-label="Music level"
            min={0}
            max={100}
            step={1}
            value={[musicGain]}
            onPointerDown={record}
            onKeyDown={record}
            onValueChange={([value]) => {
              setMusicGain(value);
              setReceipt(`Music level ${value}%`);
            }}
          />
        </div>
      );
    }

    if (tool === "overlays") {
      if (action === "Upload overlay") {
        return (
          <div className="space-y-3">
            <Input
              type="file"
              aria-label="Choose overlay file"
              accept="image/*,video/*"
              onChange={(event) => {
                const name = event.currentTarget.files?.[0]?.name;
                if (!name) return;
                record();
                setOverlay({ name, durationS: 2.5, position: "Center" });
                setReceipt(`${name} overlay added`);
              }}
            />
            <Button
              type="button"
              variant="outline"
              className="min-h-11 w-full"
              onClick={() => {
                record();
                setOverlay({
                  name: "Nova travel badge",
                  durationS: 2.5,
                  position: "Center",
                });
                setReceipt("Sample overlay added");
              }}
            >
              Use sample overlay
            </Button>
          </div>
        );
      }
      if (action === "Position overlay") {
        return renderChoiceButtons(
          ["Left", "Center", "Right"],
          overlay?.position ?? null,
          (position) => {
            record();
            setOverlay((current) => ({
              name: current?.name ?? "Nova travel badge",
              durationS: current?.durationS ?? 2.5,
              position,
            }));
            setReceipt(`Overlay moved ${position.toLowerCase()}`);
          },
        );
      }
      return (
        <div className="space-y-4">
          <div className="flex items-center justify-between text-sm">
            <span>Duration</span>
            <span className="tabular-nums text-muted-foreground">
              {(overlay?.durationS ?? 2.5).toFixed(1)}s
            </span>
          </div>
          <Slider
            aria-label="Overlay duration"
            min={0.5}
            max={5}
            step={0.1}
            value={[overlay?.durationS ?? 2.5]}
            onPointerDown={record}
            onKeyDown={record}
            onValueChange={([durationS]) => {
              setOverlay((current) => ({
                name: current?.name ?? "Nova travel badge",
                durationS,
                position: current?.position ?? "Center",
              }));
              setReceipt(`Overlay duration ${durationS.toFixed(1)}s`);
            }}
          />
        </div>
      );
    }

    if (tool === "styles") {
      if (action === "Set transition") {
        return renderChoiceButtons(["Cut", "Dissolve", "Dip"], transition, (option) => {
          record();
          setTransition(option);
          setReceipt(`${option} transition applied`);
        });
      }
      const isClipLook = action === "Adjust clip Look";
      return renderChoiceButtons(
        LOOK_OPTIONS,
        isClipLook ? clipLook : look,
        (option) => {
          record();
          if (isClipLook) setClipLook(option);
          else setLook(option);
          setReceipt(
            isClipLook
              ? `${option} applied to selected clip`
              : `${option} Look applied to the edit`,
          );
        },
      );
    }

    return null;
  };

  return (
    <main className="fixed inset-0 z-50 grid grid-rows-[56px_minmax(0,1fr)_auto] overflow-hidden bg-background pt-[env(safe-area-inset-top)] text-foreground">
      <output
        id="qa-state"
        className="hidden"
        data-windows={JSON.stringify(windows)}
        data-history-len={history.length}
        data-selected-id={selectedId}
        data-current-time={currentTimeS}
        data-receipt={receipt}
        data-panel={
          panel?.kind === "tool"
            ? panel.tool
            : panel?.kind === "action"
              ? `${panel.tool}:${panel.action}`
              : panel?.kind ?? ""
        }
        data-look={look}
        data-clip-look={clipLook}
        data-transition={transition}
        data-tool-action={toolAction}
        data-text={textDraft?.text ?? ""}
        data-text-position={textDraft?.position ?? ""}
        data-text-preset={textDraft?.preset ?? ""}
        data-caption={caption.text}
        data-caption-style={caption.style}
        data-caption-status={caption.status}
        data-music-track={musicTrack}
        data-music-gain={musicGain}
        data-sfx={JSON.stringify(sfx)}
        data-visuals={JSON.stringify(visuals)}
        data-overlay={overlay ? JSON.stringify(overlay) : ""}
        data-kria-status={kriaStatus}
        data-save-state={saveState}
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
          onClick={() => {
            setSaveState("rendering");
            setReceipt("Saved · rendering started");
          }}
        >
          {saveState === "rendering" ? "Rendering…" : "Save"}
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
        {textDraft && (
          <div
            data-testid="qa-text-overlay"
            data-text-id={textDraft.id}
            className={`pointer-events-none absolute left-1/2 max-w-[72%] -translate-x-1/2 -translate-y-1/2 text-center text-2xl font-semibold text-white [text-shadow:0_1px_4px_rgb(0_0_0/0.9)] ${
              textDraft.preset === "Editorial" ? "font-display" : ""
            }`}
            style={{
              top:
                textDraft.position === "Top"
                  ? "28%"
                  : textDraft.position === "Bottom"
                    ? "70%"
                    : "50%",
              letterSpacing: textDraft.preset === "Impact" ? "0.04em" : undefined,
              textTransform: textDraft.preset === "Impact" ? "uppercase" : undefined,
            }}
          >
            {textDraft.text || "Text"}
          </div>
        )}
        <div
          data-testid="qa-caption-overlay"
          className={`pointer-events-none absolute bottom-5 left-1/2 max-w-[76%] -translate-x-1/2 rounded bg-black/75 px-2 py-1 text-center text-sm font-medium text-white ${
            caption.style === "Editorial" ? "font-display" : ""
          }`}
          style={{
            backgroundColor: caption.style === "Lime" ? "#a3e635" : undefined,
            color: caption.style === "Lime" ? "#18181b" : undefined,
          }}
        >
          {caption.text}
        </div>
        <div className="pointer-events-none absolute right-4 top-4 flex max-w-[70%] flex-wrap justify-end gap-1">
          <span className="rounded-full bg-background/90 px-2 py-1 text-[10px] font-medium text-foreground shadow-sm">
            ♪ {musicTrack} · {musicGain}%
          </span>
          {sfx.map((effect) => (
            <span
              key={effect}
              className="rounded-full bg-background/90 px-2 py-1 text-[10px] text-foreground shadow-sm"
            >
              SFX · {effect}
            </span>
          ))}
          {visuals.map((visual) => (
            <span
              key={visual}
              className="rounded-full bg-background/90 px-2 py-1 text-[10px] text-foreground shadow-sm"
            >
              Visual · {visual}
            </span>
          ))}
        </div>
        {overlay && (
          <span
            data-testid="qa-media-overlay"
            className="pointer-events-none absolute rounded-md border border-white/70 bg-black/70 px-2 py-1 text-xs font-semibold text-white shadow"
            style={{
              left: overlay.position === "Left" ? "20%" : overlay.position === "Right" ? "70%" : "50%",
              top: "34%",
              transform: "translate(-50%, -50%)",
            }}
          >
            {overlay.name}
          </span>
        )}
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
            onToggleMute: () => {
              record();
              setMuted((current) => !current);
              setReceipt(muted ? "Clip audio restored" : "Clip muted");
            },
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
            if (tool === "text") {
              addText();
              return;
            }
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
                        record();
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
                        record();
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
          ) : panel?.kind === "text" && textDraft ? (
            <>
              <SheetHeader className="border-b border-border px-4 pb-3 pt-4 text-left">
                <SheetTitle className="text-base text-balance">
                  Edit text
                </SheetTitle>
                <SheetDescription className="text-pretty">
                  Type directly, then close this sheet to keep editing.
                </SheetDescription>
              </SheetHeader>
              <div className="grid gap-3 px-4 pt-4">
                <Textarea
                  autoFocus
                  aria-label="Text content"
                  value={textDraft.text}
                  className="min-h-24 text-base"
                  onChange={(event) => {
                    const text = event.currentTarget.value;
                    setTextDraft((current) =>
                      current ? { ...current, text } : current,
                    );
                    setReceipt("Unsaved text");
                  }}
                />
                <div className="space-y-2">
                  <p className="text-xs font-medium text-muted-foreground">Position</p>
                  <div className="grid grid-cols-3 gap-2">
                    {["Top", "Center", "Bottom"].map((position) => (
                      <Button
                        key={position}
                        type="button"
                        variant={
                          textDraft.position === position ? "secondary" : "outline"
                        }
                        className="min-h-11"
                        aria-pressed={textDraft.position === position}
                        onClick={() => {
                          record();
                          setTextDraft((current) =>
                            current ? { ...current, position } : current,
                          );
                          setReceipt(`Text moved to ${position.toLowerCase()}`);
                        }}
                      >
                        {position}
                      </Button>
                    ))}
                  </div>
                </div>
                <div className="space-y-2">
                  <p className="text-xs font-medium text-muted-foreground">Preset</p>
                  <div className="grid grid-cols-3 gap-2">
                    {["Title", "Editorial", "Impact"].map((preset) => (
                      <Button
                        key={preset}
                        type="button"
                        variant={
                          textDraft.preset === preset ? "secondary" : "outline"
                        }
                        className="min-h-11"
                        aria-pressed={textDraft.preset === preset}
                        onClick={() => {
                          record();
                          setTextDraft((current) =>
                            current ? { ...current, preset } : current,
                          );
                          setReceipt(`${preset} text preset applied`);
                        }}
                      >
                        {preset}
                      </Button>
                    ))}
                  </div>
                </div>
                <Button
                  type="button"
                  className="min-h-11"
                  onClick={() => setPanel(null)}
                >
                  Done
                </Button>
              </div>
            </>
          ) : panel?.kind === "action" ? (
            <>
              <SheetHeader className="border-b border-border px-4 pb-3 pt-4 text-left">
                <Button
                  type="button"
                  variant="ghost"
                  className="mb-1 -ml-3 min-h-11 w-fit justify-start px-3"
                  aria-label={`Back to ${TOOL_COPY[panel.tool].title}`}
                  onClick={() => setPanel({ kind: "tool", tool: panel.tool })}
                >
                  <ChevronLeft aria-hidden="true" />
                  {TOOL_COPY[panel.tool].title}
                </Button>
                <SheetTitle className="text-base text-balance">
                  {panel.action}
                </SheetTitle>
                <SheetDescription className="text-pretty">
                  Changes update the preview immediately and stay undoable.
                </SheetDescription>
              </SheetHeader>
              <div className="grid gap-4 px-4 pt-4">
                {renderActionContent(panel.tool, panel.action)}
                <Button
                  type="button"
                  variant="outline"
                  className="min-h-11"
                  onClick={() => setPanel(null)}
                >
                  Done
                </Button>
              </div>
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
                    onClick={() => openToolAction(panel.tool, action)}
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
