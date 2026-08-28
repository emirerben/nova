"use client";

import Image from "next/image";
import Link from "next/link";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ChevronLeft, Move, RotateCcw } from "lucide-react";
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
import { resolveCssFont } from "@/lib/overlay-constants";
import {
  resolveLetterSpacingEm,
  resolveLineSpacing,
  resolveMaxWidthFrac,
} from "@/lib/overlay-layout";
import { TEXT_PRESETS } from "@/lib/text-presets";
import {
  EDITOR_TEXT_SIZE_MAX,
  EDITOR_TEXT_SIZE_MIN,
} from "@/app/plan/items/[id]/_editor/text-control-options";
import {
  defaultTextMotion,
  textMotionHasControls,
  type TextMotionConfigV2,
} from "@/lib/text-motion-v2";
import { formatTimecode } from "@/lib/timeline/time-format";
import { ContextStrip } from "@/app/plan/items/[id]/_editor/ContextStrip";
import {
  MiniStrip,
  type MiniStripLane,
  type MiniStripLaneItem,
  type MiniStripSegment,
  type MiniStripTrimPatch,
} from "@/app/plan/items/[id]/_editor/MiniStrip";
import { ToolDock, type DockTool } from "@/app/plan/items/[id]/_editor/ToolDock";
import {
  PauseIcon,
  PlayIcon,
} from "@/app/plan/items/[id]/_editor/editor-icons";
import { createEditorPlaybackClock } from "@/app/plan/items/[id]/_editor/editor-playback-clock";
import {
  buildMobilePreviewTimeline,
  mobilePreviewOutputAtSource,
  mobilePreviewSourceAtOutput,
} from "./mobile-editor-preview";
import {
  MobileToolPanel,
  type MobileToolActionValue,
} from "./MobileToolPanel";

const SOURCE_URL = "/landing/raw-story/travel-render.mp4";
const SOURCE_DURATION_S = 11.21;
const VISUAL_PREVIEW_SOURCES = [
  "/landing/raw-story/lisbon.jpg",
  "/landing/raw-story/corfu.jpg",
  "/landing/raw-story/istanbul.jpg",
];
const TEXT_MOTION_V2_UI_ENABLED =
  process.env.NEXT_PUBLIC_TEXT_MOTION_V2_ENABLED === "true";

function visualBaseLabel(label: string): string {
  return label.split(" · ")[0];
}

function visualDisplayMode(label: string): "fullscreen" | "overlay" {
  return label.endsWith(" · Overlay") ? "overlay" : "fullscreen";
}

function visualPreviewSource(
  visual: FixtureTimelineItem,
  currentTimeS: number,
): string | null {
  if (visual.previewUrl) return visual.previewUrl;
  const label = visualBaseLabel(visual.label);
  if (label === "Text card") return null;
  if (label === "Bridge photo") return VISUAL_PREVIEW_SOURCES[0];
  if (label === "Sea texture") return VISUAL_PREVIEW_SOURCES[1];
  if (label === "Night market") return VISUAL_PREVIEW_SOURCES[2];
  if (label === "Media block") return "/landing/raw-story/trulli-street.jpg";
  if (label === "3-shot montage" || label === "Media sequence") {
    const durationS = Math.max(0.1, visual.endS - visual.startS);
    const shotIndex = Math.min(
      VISUAL_PREVIEW_SOURCES.length - 1,
      Math.floor(
        ((currentTimeS - visual.startS) / durationS) *
          VISUAL_PREVIEW_SOURCES.length,
      ),
    );
    return VISUAL_PREVIEW_SOURCES[Math.max(0, shotIndex)];
  }
  return "/landing/raw-story/alberobello.jpg";
}

interface ClipWindow {
  id: string;
  inS: number;
  durationS: number;
  label: string;
}

interface FixtureTimelineItem {
  id: string;
  label: string;
  startS: number;
  endS: number;
  previewUrl?: string;
  mediaKind?: "image" | "video";
}

interface FixtureOverlay {
  id: string;
  name: string;
  startS: number;
  endS: number;
  durationS: number;
  position: string;
}

const INITIAL_WINDOWS: ClipWindow[] = [
  { id: "lisbon", inS: 0, durationS: 3.6, label: "Lisbon arrival" },
  { id: "corfu", inS: 3.6, durationS: 3.1, label: "Corfu water" },
  { id: "istanbul", inS: 6.7, durationS: 3.8, label: "Istanbul night" },
];

type SheetDockTool = Exclude<DockTool, "text">;

type FixturePanel =
  | { kind: "precision" }
  | { kind: "tool"; tool: SheetDockTool }
  | { kind: "action"; tool: SheetDockTool; action: string }
  | null;

interface FixtureSnapshot {
  windows: ClipWindow[];
  muted: boolean;
  look: string;
  clipLook: string;
  transition: string;
  textDraft: {
    id: string;
    text: string;
    startS: number;
    endS: number;
    xPct: number;
    yPct: number;
    preset: string;
    font: string;
    color: string;
    size: number;
    alignment: string;
    boxPosition: string;
    effect: string;
    motion: TextMotionConfigV2 | null;
    themeTransition: string;
    themeTargetGlyph: string;
    highlightColor: string | null;
    strokeWidth: number;
    textCase: string;
    letterSpacing: number;
    lineSpacing: number;
    maxWidthFrac: number;
    shadowEnabled: boolean;
    shadowStyle: string;
    behindSubject: boolean;
  } | null;
  caption: {
    text: string;
    style: string;
    status: string;
    enabled: boolean;
    font: string;
    color: string;
    size: number;
    stroke: number;
    shadow: boolean;
    language: string;
    startS: number;
    endS: number;
  };
  musicTrack: string;
  musicGain: number;
  sfx: FixtureTimelineItem[];
  visuals: FixtureTimelineItem[];
  overlay: FixtureOverlay | null;
  kriaStatus: string;
}

const MUSIC_TRACKS = ["City Lights", "Golden Hour", "Midnight Ferry"];
const SFX_OPTIONS = ["Camera click", "Whoosh", "Soft impact"];
const LOOK_OPTIONS = ["Clean", "Warm", "Film"];

const TOOL_COPY: Record<
  SheetDockTool,
  { title: string; description: string; actions: string[] }
> = {
  nova: {
    title: "Kria",
    description: "Review a proposed edit before accepting or rejecting it.",
    actions: ["Review proposal", "Accept edit", "Reject edit"],
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
  const playbackClockRef = useRef<ReturnType<
    typeof createEditorPlaybackClock
  > | null>(null);
  if (playbackClockRef.current == null) {
    playbackClockRef.current = createEditorPlaybackClock(0);
  }
  const playbackClock = playbackClockRef.current;
  const pendingSourceSeekRef = useRef<number | null>(null);
  const sourceSeekInFlightRef = useRef(false);
  const activePreviewEntryIndexRef = useRef(0);
  const previewCanvasRef = useRef<HTMLDivElement>(null);
  const textEditorRef = useRef<HTMLDivElement | null>(null);
  const textFrameRef = useRef<HTMLDivElement>(null);
  const pendingTextFocusRef = useRef(false);
  const textEditRecordedRef = useRef(false);
  const textDragRef = useRef<{
    pointerId: number;
    startClientX: number;
    startClientY: number;
    startXPct: number;
    startYPct: number;
    minXPct: number;
    maxXPct: number;
    minYPct: number;
    maxYPct: number;
    moved: boolean;
  } | null>(null);
  const captionEditRecordedRef = useRef(false);
  const [windows, setWindows] = useState(INITIAL_WINDOWS);
  const [history, setHistory] = useState<FixtureSnapshot[]>([]);
  const [selectedId, setSelectedId] = useState(INITIAL_WINDOWS[0].id);
  const [currentTimeS, setCurrentTimeS] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [muted, setMuted] = useState(false);
  const [panel, setPanel] = useState<FixturePanel>(null);
  const [activeDockTool, setActiveDockTool] = useState<DockTool | null>(null);
  const [look, setLook] = useState("Clean");
  const [clipLook, setClipLook] = useState("Clean");
  const [transition, setTransition] = useState("Cut");
  const [toolAction, setToolAction] = useState("");
  const [textDraft, setTextDraft] = useState<{
    id: string;
    text: string;
    startS: number;
    endS: number;
    xPct: number;
    yPct: number;
    preset: string;
    font: string;
    color: string;
    size: number;
    alignment: string;
    boxPosition: string;
    effect: string;
    motion: TextMotionConfigV2 | null;
    themeTransition: string;
    themeTargetGlyph: string;
    highlightColor: string | null;
    strokeWidth: number;
    textCase: string;
    letterSpacing: number;
    lineSpacing: number;
    maxWidthFrac: number;
    shadowEnabled: boolean;
    shadowStyle: string;
    behindSubject: boolean;
  } | null>(null);
  const [textSelected, setTextSelected] = useState(false);
  const [caption, setCaption] = useState({
    text: "Three cities. One unforgettable summer.",
    style: "Clean",
    status: "Ready",
    enabled: true,
    font: "Inter",
    color: "#FFFFFF",
    size: 52,
    stroke: 4,
    shadow: true,
    language: "English",
    startS: 0,
    endS: 3.2,
  });
  const [musicTrack, setMusicTrack] = useState(MUSIC_TRACKS[0]);
  const [musicGain, setMusicGain] = useState(70);
  const [sfx, setSfx] = useState<FixtureTimelineItem[]>([]);
  const [visuals, setVisuals] = useState<FixtureTimelineItem[]>([]);
  const [overlay, setOverlay] = useState<FixtureOverlay | null>(null);
  const [selectedLaneItem, setSelectedLaneItem] = useState<{
    kind: MiniStripLaneItem["kind"];
    id: string;
  } | null>(null);
  const [kriaStatus, setKriaStatus] = useState("Proposal ready");
  const [saveState, setSaveState] = useState<"idle" | "rendering">("idle");
  const [receipt, setReceipt] = useState("Draft · local QA fixture");
  const segments = useMemo(() => projectSegments(windows), [windows]);
  const totalDurationS = segments.at(-1)?.endS ?? 0;
  const activeVisual = useMemo(
    () =>
      [...visuals]
        .reverse()
        .find(
          (visual) =>
            currentTimeS >= visual.startS && currentTimeS < visual.endS,
        ) ?? null,
    [currentTimeS, visuals],
  );
  const activeVisualLabel = activeVisual
    ? visualBaseLabel(activeVisual.label)
    : null;
  const activeVisualMode = activeVisual
    ? visualDisplayMode(activeVisual.label)
    : null;
  const activeVisualSource = activeVisual
    ? visualPreviewSource(activeVisual, currentTimeS)
    : null;
  const timelineWindowAtPlayhead = useCallback(
    (label: string, durationS = 2): FixtureTimelineItem => {
      const startS = Math.min(
        Math.max(0, totalDurationS - 0.1),
        Math.max(0, currentTimeS),
      );
      const endS = Math.min(
        totalDurationS,
        Math.max(startS + 0.1, startS + durationS),
      );
      return { id: crypto.randomUUID(), label, startS, endS };
    },
    [currentTimeS, totalDurationS],
  );
  const previewTimeline = useMemo(
    () =>
      buildMobilePreviewTimeline(
        segments.map((segment) => ({
          id: segment.id,
          startS: segment.startS,
          endS: segment.endS,
          sourceStartS: segment.sourceStartS ?? 0,
          sourceUrl: segment.sourceUrl ?? null,
        })),
      ),
    [segments],
  );
  const previewTimelineRef = useRef(previewTimeline);
  previewTimelineRef.current = previewTimeline;
  const fixtureTimelineLanes = useMemo<MiniStripLane[]>(() => {
    const lanes: MiniStripLane[] = [];
    if (textDraft) {
      lanes.push({
        id: "text",
        label: "Text",
        items: [
          {
            id: textDraft.id,
            kind: "text",
            startS: textDraft.startS,
            endS: textDraft.endS,
            label: textDraft.text || "Text",
          },
        ],
      });
    }
    if (caption.enabled) {
      lanes.push({
        id: "captions",
        label: "Captions",
        items: [
          {
            id: "caption-cue-1",
            kind: "text",
            startS: caption.startS,
            endS: Math.min(totalDurationS, caption.endS),
            label: caption.text || "Caption",
          },
        ],
      });
    }
    if (visuals.length > 0) {
      lanes.push({
        id: "visuals",
        label: "Visuals",
        items: visuals.map((item) => ({ ...item, kind: "visual" as const })),
      });
    }
    if (sfx.length > 0) {
      lanes.push({
        id: "sfx",
        label: "Sound effects",
        items: sfx.map((item) => ({ ...item, kind: "sfx" as const })),
      });
    }
    if (musicTrack !== "No music") {
      lanes.push({
        id: "music",
        label: "Music",
        items: [
          {
            id: "background",
            kind: "music",
            startS: 0,
            endS: totalDurationS,
            label: musicTrack,
            resizable: false,
          },
        ],
      });
    }
    if (overlay) {
      lanes.push({
        id: "overlays",
        label: "Overlays",
        items: [
          {
            id: overlay.id,
            kind: "overlay",
            startS: overlay.startS,
            endS: overlay.endS,
            label: overlay.name,
          },
        ],
      });
    }
    return lanes;
  }, [
    caption.enabled,
    caption.endS,
    caption.startS,
    caption.text,
    musicTrack,
    overlay,
    sfx,
    textDraft,
    totalDurationS,
    visuals,
  ]);
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
  const activeTool = activeDockTool;
  const resolvedTextFont = resolveCssFont(textDraft?.font);
  const resolvedCaptionFont = resolveCssFont(caption.font);

  useEffect(() => {
    if (!textDraft || !textSelected || !pendingTextFocusRef.current) return;
    pendingTextFocusRef.current = false;
    const frame = window.requestAnimationFrame(() => {
      const editor = textEditorRef.current;
      if (!editor) return;
      editor.focus();
      const selection = window.getSelection();
      if (!selection) return;
      const range = document.createRange();
      range.selectNodeContents(editor);
      selection.removeAllRanges();
      selection.addRange(range);
    });
    return () => window.cancelAnimationFrame(frame);
  }, [textDraft, textSelected]);

  const flushPendingSourceSeek = useCallback(() => {
    const video = videoRef.current;
    const target = pendingSourceSeekRef.current;
    if (!video || target == null || sourceSeekInFlightRef.current) return;
    if (Math.abs(video.currentTime - target) < 1 / 240) {
      pendingSourceSeekRef.current = null;
      return;
    }
    sourceSeekInFlightRef.current = true;
    video.currentTime = target;
  }, []);

  const queueSourceSeek = useCallback(
    (sourceTimeS: number) => {
      pendingSourceSeekRef.current = Math.max(
        0,
        Math.min(SOURCE_DURATION_S, sourceTimeS),
      );
      flushPendingSourceSeek();
    },
    [flushPendingSourceSeek],
  );

  const seekTo = useCallback(
    (seconds: number) => {
      const next = Math.min(totalDurationS, Math.max(0, seconds));
      const mapping = mobilePreviewSourceAtOutput(previewTimeline, next);
      if (!mapping) return;
      activePreviewEntryIndexRef.current = mapping.entryIndex;
      setCurrentTimeS(next);
      playbackClock.publish(next);
      queueSourceSeek(mapping.sourceTimeS);
    },
    [playbackClock, previewTimeline, queueSourceSeek, totalDurationS],
  );

  const publishDecodedSourceTime = useCallback(
    (sourceTimeS: number, commit: boolean) => {
      const timeline = previewTimelineRef.current;
      const projected = mobilePreviewOutputAtSource(
        timeline,
        activePreviewEntryIndexRef.current,
        sourceTimeS,
      );
      if (!projected) return;
      if (projected.reachedEnd) {
        const nextIndex = activePreviewEntryIndexRef.current + 1;
        const nextEntry = timeline.entries[nextIndex];
        if (nextEntry?.kind === "clip") {
          activePreviewEntryIndexRef.current = nextIndex;
          playbackClock.publish(nextEntry.startS);
          setCurrentTimeS(nextEntry.startS);
          queueSourceSeek(nextEntry.inS);
          return;
        }
        playbackClock.publish(timeline.totalDurationS);
        setCurrentTimeS(timeline.totalDurationS);
        videoRef.current?.pause();
        return;
      }
      playbackClock.publish(projected.outputTimeS);
      if (commit) setCurrentTimeS(projected.outputTimeS);
    },
    [playbackClock, queueSourceSeek],
  );

  useEffect(() => {
    if (!playing) return;
    const video = videoRef.current;
    if (!video) return;
    let live = true;
    let frameCallbackId = 0;
    let animationFrameId = 0;

    if (typeof video.requestVideoFrameCallback === "function") {
      const sample: VideoFrameRequestCallback = (_now, metadata) => {
        if (!live) return;
        publishDecodedSourceTime(metadata.mediaTime, false);
        frameCallbackId = video.requestVideoFrameCallback(sample);
      };
      frameCallbackId = video.requestVideoFrameCallback(sample);
    } else {
      const sample = () => {
        if (!live) return;
        publishDecodedSourceTime(video.currentTime, false);
        animationFrameId = window.requestAnimationFrame(sample);
      };
      animationFrameId = window.requestAnimationFrame(sample);
    }

    return () => {
      live = false;
      if (frameCallbackId) video.cancelVideoFrameCallback(frameCallbackId);
      if (animationFrameId) window.cancelAnimationFrame(animationFrameId);
    };
  }, [playing, publishDecodedSourceTime]);

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
      sfx: sfx.map((item) => ({ ...item })),
      visuals: visuals.map((item) => ({ ...item })),
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

  const previewLaneTiming = (
    item: MiniStripLaneItem,
    patch: { startS: number; endS: number },
    handle: "left" | "right",
  ) => {
    if (item.id === "caption-cue-1") {
      setCaption((current) => ({
        ...current,
        startS: patch.startS,
        endS: patch.endS,
      }));
    } else if (item.kind === "text") {
      setTextDraft((current) =>
        current?.id === item.id
          ? { ...current, startS: patch.startS, endS: patch.endS }
          : current,
      );
    } else if (item.kind === "visual") {
      setVisuals((current) =>
        current.map((visual) =>
          visual.id === item.id ? { ...visual, ...patch } : visual,
        ),
      );
    } else if (item.kind === "sfx") {
      setSfx((current) =>
        current.map((effect) =>
          effect.id === item.id ? { ...effect, ...patch } : effect,
        ),
      );
    } else if (item.kind === "overlay") {
      setOverlay((current) =>
        current?.id === item.id
          ? {
              ...current,
              startS: patch.startS,
              endS: patch.endS,
              durationS: patch.endS - patch.startS,
            }
          : current,
      );
    }
    setReceipt(
      `${item.label} ${handle === "left" ? "start" : "end"} resized`,
    );
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
    setTextSelected(Boolean(previous.textDraft));
    setSelectedLaneItem(
      previous.textDraft
        ? { kind: "text", id: previous.textDraft.id }
        : null,
    );
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

  const addText = (text = "Add a title", preset = "clean-caption") => {
    const productionPreset =
      TEXT_PRESETS.find(
        (candidate) => candidate.id === preset || candidate.label === preset,
      ) ?? TEXT_PRESETS.find((candidate) => candidate.id === "clean-caption");
    record();
    const id = crypto.randomUUID();
    const startS = Math.min(
      Math.max(0, totalDurationS - 0.1),
      Math.max(0, currentTimeS),
    );
    setTextDraft({
      id,
      text,
      startS,
      endS: totalDurationS,
      xPct: 50,
      yPct: 50,
      preset: productionPreset?.id ?? "clean-caption",
      font: productionPreset?.fields.font_family ?? "Inter",
      color: productionPreset?.fields.color ?? "#FFFFFF",
      size: 64,
      alignment: "center",
      boxPosition: "center",
      effect: productionPreset?.fields.effect ?? "none",
      motion: null,
      themeTransition: "none",
      themeTargetGlyph: "",
      highlightColor: productionPreset?.fields.highlight_color ?? null,
      strokeWidth: productionPreset?.fields.stroke_width ?? 0,
      textCase: "none",
      letterSpacing: 0,
      lineSpacing: 1.15,
      maxWidthFrac: 0.9,
      shadowEnabled: true,
      shadowStyle: "standard",
      behindSubject: false,
    });
    pendingTextFocusRef.current = true;
    textEditRecordedRef.current = false;
    setTextSelected(true);
    setSelectedLaneItem({ kind: "text", id });
    setPanel(null);
    setActiveDockTool("text");
    setToolAction("text:Add text");
    setReceipt("Text added · type on screen or drag the move handle");
  };

  const moveTextByKeyboard = (dxPct: number, dyPct: number) => {
    if (!textDraft) return;
    record();
    setTextDraft((current) =>
      current
        ? {
            ...current,
            xPct: Math.min(94, Math.max(6, current.xPct + dxPct)),
            yPct: Math.min(94, Math.max(6, current.yPct + dyPct)),
          }
        : current,
    );
    setReceipt("Text moved · Undo available");
  };

  const startTextDrag = (event: React.PointerEvent<HTMLElement>) => {
    if (!textDraft) return;
    const preview = previewCanvasRef.current?.getBoundingClientRect();
    const frame = textFrameRef.current?.getBoundingClientRect();
    if (!preview || !frame || preview.width <= 0 || preview.height <= 0) return;
    event.currentTarget.setPointerCapture(event.pointerId);
    const halfWidthPct = (frame.width / preview.width) * 50;
    const halfHeightPct = (frame.height / preview.height) * 50;
    textDragRef.current = {
      pointerId: event.pointerId,
      startClientX: event.clientX,
      startClientY: event.clientY,
      startXPct: textDraft.xPct,
      startYPct: textDraft.yPct,
      minXPct: Math.min(50, halfWidthPct + 2),
      maxXPct: Math.max(50, 98 - halfWidthPct),
      minYPct: Math.min(50, halfHeightPct + 2),
      maxYPct: Math.max(50, 98 - halfHeightPct),
      moved: false,
    };
    setTextSelected(true);
  };

  const moveTextDrag = (event: React.PointerEvent<HTMLElement>) => {
    const drag = textDragRef.current;
    const preview = previewCanvasRef.current?.getBoundingClientRect();
    if (!drag || drag.pointerId !== event.pointerId || !preview) return;
    const dx = event.clientX - drag.startClientX;
    const dy = event.clientY - drag.startClientY;
    if (!drag.moved && Math.hypot(dx, dy) < 4) return;
    if (!drag.moved) {
      record();
      drag.moved = true;
      window.getSelection()?.removeAllRanges();
    }
    event.preventDefault();
    const xPct = drag.startXPct + (dx / preview.width) * 100;
    const yPct = drag.startYPct + (dy / preview.height) * 100;
    setTextDraft((current) =>
      current
        ? {
            ...current,
            xPct: Math.min(drag.maxXPct, Math.max(drag.minXPct, xPct)),
            yPct: Math.min(drag.maxYPct, Math.max(drag.minYPct, yPct)),
          }
        : current,
    );
    setReceipt("Moving text…");
  };

  const finishTextDrag = (event: React.PointerEvent<HTMLElement>) => {
    const drag = textDragRef.current;
    if (!drag || drag.pointerId !== event.pointerId) return;
    textDragRef.current = null;
    if (event.currentTarget.hasPointerCapture(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId);
    }
    if (drag.moved) setReceipt("Text moved · Undo available");
  };

  const openToolAction = (tool: SheetDockTool, action: string) => {
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

  const renderActionContent = (tool: SheetDockTool, action: string) => {
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
            setVisuals((current) => [
              ...current,
              timelineWindowAtPlayhead(visual),
            ]);
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
            setVisuals((current) => [
              ...current,
              timelineWindowAtPlayhead("3-shot montage", 3),
            ]);
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
          setSfx((current) => [
            ...current,
            timelineWindowAtPlayhead(effect, 0.8),
          ]);
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
                const item = timelineWindowAtPlayhead(name, 2.5);
                setOverlay({
                  id: item.id,
                  name,
                  startS: item.startS,
                  endS: item.endS,
                  durationS: item.endS - item.startS,
                  position: "Center",
                });
                setReceipt(`${name} overlay added`);
              }}
            />
            <Button
              type="button"
              variant="outline"
              className="min-h-11 w-full"
              onClick={() => {
                record();
                const item = timelineWindowAtPlayhead("Nova travel badge", 2.5);
                setOverlay({
                  id: item.id,
                  name: item.label,
                  startS: item.startS,
                  endS: item.endS,
                  durationS: item.endS - item.startS,
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
              id: current?.id ?? crypto.randomUUID(),
              name: current?.name ?? "Nova travel badge",
              startS: current?.startS ?? currentTimeS,
              endS:
                current?.endS ??
                Math.min(totalDurationS, currentTimeS + 2.5),
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
                id: current?.id ?? crypto.randomUUID(),
                name: current?.name ?? "Nova travel badge",
                startS: current?.startS ?? currentTimeS,
                endS: Math.min(
                  totalDurationS,
                  (current?.startS ?? currentTimeS) + durationS,
                ),
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

  const runMobileToolAction = (
    action: string,
    value?: MobileToolActionValue,
  ) => {
    setToolAction(action);

    if (action.startsWith("text.")) {
      if (action === "text.delete") {
        if (!textDraft) return;
        record();
        setTextDraft(null);
        setTextSelected(false);
        setSelectedLaneItem(null);
        setReceipt("Text deleted · Undo available");
        return;
      }
      if (!textDraft) addText();
      if (action === "text.smartPlace") {
        record();
        setTextDraft((current) =>
          current
            ? { ...current, xPct: 50, yPct: 38, boxPosition: "center" }
            : current,
        );
        setReceipt("Text placed away from the subject · Undo available");
        return;
      }
      if (action === "text.startHere" || action === "text.endHere") {
        record();
        setTextDraft((current) => {
          if (!current) return current;
          if (action === "text.startHere") {
            return {
              ...current,
              startS: Math.min(current.endS - 0.1, currentTimeS),
            };
          }
          return {
            ...current,
            endS: Math.max(current.startS + 0.1, currentTimeS),
          };
        });
        setReceipt(
          action === "text.startHere"
            ? `Text starts at ${formatTimecode(currentTimeS)}`
            : `Text ends at ${formatTimecode(currentTimeS)}`,
        );
        return;
      }
      const property = action.slice("text.".length);
      record();
      setTextDraft((current) => {
        if (!current) return current;
        if (property === "font" && typeof value === "string") {
          return { ...current, font: value };
        }
        if (property === "color" && typeof value === "string") {
          return { ...current, color: value.toUpperCase() };
        }
        if (property === "size" && typeof value === "number") {
          return {
            ...current,
            size: Math.max(
              EDITOR_TEXT_SIZE_MIN,
              Math.min(EDITOR_TEXT_SIZE_MAX, value),
            ),
          };
        }
        if (property === "alignment" && typeof value === "string") {
          return { ...current, alignment: value };
        }
        if (property === "boxPosition" && typeof value === "string") {
          const xPct = value === "left" ? 18 : value === "right" ? 82 : 50;
          return { ...current, boxPosition: value, xPct };
        }
        if (property === "effect" && typeof value === "string") {
          return {
            ...current,
            effect: value,
            motion:
              TEXT_MOTION_V2_UI_ENABLED && textMotionHasControls(value)
                ? { ...defaultTextMotion(value), version: 2 }
                : null,
          };
        }
        if (
          property === "motion" &&
          value != null &&
          typeof value === "object"
        ) {
          return {
            ...current,
            motion: {
              ...(current.motion ?? defaultTextMotion(current.effect)),
              ...value,
              version: 2,
            },
          };
        }
        if (property === "resetMotion") {
          return { ...current, motion: null };
        }
        if (property === "themeTransition" && typeof value === "string") {
          return {
            ...current,
            themeTransition: value,
            themeTargetGlyph:
              value === "giant-title-wipe" ? current.themeTargetGlyph : "",
          };
        }
        if (property === "themeTargetGlyph" && typeof value === "string") {
          return { ...current, themeTargetGlyph: value.slice(0, 1) };
        }
        if (property === "highlightColor" && typeof value === "string") {
          return { ...current, highlightColor: value.toUpperCase() };
        }
        if (property === "strokeWidth" && typeof value === "number") {
          return { ...current, strokeWidth: Math.max(0, Math.min(12, value)) };
        }
        if (property === "textCase" && typeof value === "string") {
          return { ...current, textCase: value };
        }
        if (property === "letterSpacing" && typeof value === "number") {
          return { ...current, letterSpacing: resolveLetterSpacingEm(value) };
        }
        if (property === "lineSpacing" && typeof value === "number") {
          return { ...current, lineSpacing: resolveLineSpacing(value) };
        }
        if (property === "maxWidthFrac" && typeof value === "number") {
          return { ...current, maxWidthFrac: resolveMaxWidthFrac(value) };
        }
        if (property === "shadow" && typeof value === "string") {
          return {
            ...current,
            shadowEnabled: value !== "off",
            shadowStyle:
              value === "high_visibility" ? "high_visibility" : "standard",
          };
        }
        if (property === "behindSubject" && typeof value === "boolean") {
          return { ...current, behindSubject: value };
        }
        if (property === "preset" && typeof value === "string") {
          const preset = TEXT_PRESETS.find((candidate) => candidate.id === value);
          if (!preset) return current;
          return {
            ...current,
            preset: preset.id,
            font: preset.fields.font_family ?? current.font,
            color: preset.fields.color ?? current.color,
            highlightColor: preset.fields.highlight_color ?? null,
            strokeWidth: preset.fields.stroke_width ?? 0,
            effect: preset.fields.effect ?? current.effect,
            motion:
              TEXT_MOTION_V2_UI_ENABLED &&
              textMotionHasControls(preset.fields.effect ?? current.effect)
                ? {
                    ...defaultTextMotion(preset.fields.effect ?? current.effect),
                    version: 2,
                  }
                : null,
          };
        }
        return current;
      });
      setReceipt("Text style updated · Undo available");
      return;
    }

    if (action.startsWith("captions.")) {
      const property = action.slice("captions.".length);
      record();
      if (property === "retranscribe") {
        setCaption((current) => ({
          ...current,
          text: "Lisbon, Corfu, then Istanbul.",
          status: "Retranscribed",
        }));
        setReceipt("Captions re-transcribed");
        return;
      }
      setCaption((current) => {
        if (property === "text" && typeof value === "string") {
          return { ...current, text: value, status: "Edited" };
        }
        if (property === "enabled" && typeof value === "boolean") {
          return { ...current, enabled: value };
        }
        if (property === "font" && typeof value === "string") {
          return { ...current, font: value };
        }
        if (property === "color" && typeof value === "string") {
          return { ...current, color: value.toUpperCase() };
        }
        if (property === "size" && typeof value === "number") {
          return { ...current, size: value };
        }
        if (property === "stroke" && typeof value === "number") {
          return { ...current, stroke: value };
        }
        if (property === "shadow" && typeof value === "boolean") {
          return { ...current, shadow: value };
        }
        if (property === "language" && typeof value === "string") {
          return { ...current, language: value, status: "Retranscribed" };
        }
        return current;
      });
      setReceipt(
        property === "language"
          ? `Captions changed to ${String(value)} and re-transcribed`
          : "Captions updated · Undo available",
      );
      return;
    }

    if (action.startsWith("visuals.")) {
      const property = action.slice("visuals.".length);
      record();
      if (property === "delete") {
        setVisuals((current) => current.slice(0, -1));
        setReceipt("Visual deleted · Undo available");
        return;
      }
      if (property === "display" && typeof value === "string") {
        setVisuals((current) =>
          current.map((visual, index) =>
            index === current.length - 1
              ? {
                  ...visual,
                  label: `${visual.label.split(" · ")[0]} · ${value}`,
                }
              : visual,
          ),
        );
        setReceipt(`Visual display changed to ${value}`);
        return;
      }
      if (property === "retime") {
        setReceipt(`Visual starts at ${formatTimecode(currentTimeS)}`);
        return;
      }
      const uploadValue =
        typeof value === "object" && value != null
          ? (value as Record<string, unknown>)
          : null;
      const uploadedVisual =
        property === "upload" &&
        typeof uploadValue?.name === "string" &&
        typeof uploadValue.previewUrl === "string" &&
        (uploadValue.mediaKind === "image" ||
          uploadValue.mediaKind === "video")
          ? {
              name: uploadValue.name,
              previewUrl: uploadValue.previewUrl,
              mediaKind: uploadValue.mediaKind as "image" | "video",
            }
          : null;
      const labels: Record<string, string> = {
        upload: uploadedVisual?.name ?? "Uploaded visual",
        montage: "3-shot montage",
        media: "Media block",
        sequence: "Media sequence",
        textCard: "Text card",
      };
      const label = labels[property];
      if (label) {
        setVisuals((current) => [
          ...current,
          {
            ...timelineWindowAtPlayhead(
              label,
              property === "montage" ? 3 : 2,
            ),
            previewUrl: uploadedVisual?.previewUrl,
            mediaKind: uploadedVisual?.mediaKind,
          },
        ]);
        setReceipt(`${label} added to Visuals`);
      }
      return;
    }

    if (action.startsWith("sounds.")) {
      const property = action.slice("sounds.".length);
      record();
      if (property === "sfx" && typeof value === "string") {
        setSfx((current) => [
          ...current,
          timelineWindowAtPlayhead(value, 0.8),
        ]);
        setReceipt(`${value} added at ${formatTimecode(currentTimeS)}`);
      } else if (property === "music" && typeof value === "string") {
        setMusicTrack(value);
        setReceipt(`Music changed to ${value}`);
      } else if (property === "removeMusic") {
        setMusicTrack("No music");
        setReceipt("Music removed · Undo available");
      } else if (property === "gain" && typeof value === "number") {
        setMusicGain(value);
        setReceipt(`Music level ${value}%`);
      }
      return;
    }

    if (action.startsWith("overlays.")) {
      const property = action.slice("overlays.".length);
      record();
      if (property === "delete") {
        setOverlay(null);
        setReceipt("Overlay deleted · Undo available");
      } else if (property === "position" && typeof value === "string") {
        setOverlay((current) => (current ? { ...current, position: value } : current));
        setReceipt(`Overlay moved ${String(value).toLowerCase()}`);
      } else if (property === "duration" && typeof value === "number") {
        setOverlay((current) =>
          current
            ? {
                ...current,
                durationS: value,
                endS: Math.min(totalDurationS, current.startS + value),
              }
            : current,
        );
        setReceipt(`Overlay duration ${value.toFixed(1)}s`);
      } else {
        const name =
          property === "suggest"
            ? "Kria suggested skyline"
            : typeof value === "string"
              ? value
              : "Uploaded overlay";
        const item = timelineWindowAtPlayhead(name, 2);
        setOverlay({
          id: item.id,
          name,
          startS: item.startS,
          endS: item.endS,
          durationS: item.endS - item.startS,
          position: "Center",
        });
        setReceipt(`${name} added`);
      }
      return;
    }

    if (action.startsWith("styles.")) {
      const property = action.slice("styles.".length);
      if (typeof value !== "string") return;
      record();
      if (property === "look") setLook(value);
      if (property === "clipLook") setClipLook(value);
      if (property === "transition") setTransition(value);
      setReceipt(`${value} ${property === "transition" ? "transition" : "Look"} applied`);
      return;
    }

    if (action === "nova.accept") acceptKriaProposal();
    if (action === "nova.reject") rejectKriaProposal();
  };

  return (
    <main className="fixed inset-0 z-50 grid grid-cols-[minmax(0,1fr)] grid-rows-[56px_minmax(0,1fr)_auto] overflow-hidden bg-background pt-[env(safe-area-inset-top)] text-foreground">
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
        data-text-start={textDraft?.startS ?? ""}
        data-text-end={textDraft?.endS ?? ""}
        data-text-x={textDraft?.xPct ?? ""}
        data-text-y={textDraft?.yPct ?? ""}
        data-text-preset={textDraft?.preset ?? ""}
        data-text-font={textDraft?.font ?? ""}
        data-text-color={textDraft?.color ?? ""}
        data-text-size={textDraft?.size ?? ""}
        data-text-alignment={textDraft?.alignment ?? ""}
        data-text-box-position={textDraft?.boxPosition ?? ""}
        data-text-effect={textDraft?.effect ?? ""}
        data-text-motion={textDraft?.motion ? JSON.stringify(textDraft.motion) : ""}
        data-text-theme-transition={textDraft?.themeTransition ?? ""}
        data-text-theme-target-glyph={textDraft?.themeTargetGlyph ?? ""}
        data-text-highlight-color={textDraft?.highlightColor ?? ""}
        data-text-stroke-width={textDraft?.strokeWidth ?? ""}
        data-text-case={textDraft?.textCase ?? ""}
        data-text-letter-spacing={textDraft?.letterSpacing ?? ""}
        data-text-line-spacing={textDraft?.lineSpacing ?? ""}
        data-text-max-width={textDraft?.maxWidthFrac ?? ""}
        data-text-shadow={
          textDraft
            ? textDraft.shadowEnabled
              ? textDraft.shadowStyle
              : "off"
            : ""
        }
        data-caption={caption.text}
        data-caption-style={caption.style}
        data-caption-status={caption.status}
        data-caption-enabled={caption.enabled}
        data-caption-font={caption.font}
        data-caption-color={caption.color}
        data-caption-size={caption.size}
        data-caption-language={caption.language}
        data-caption-start={caption.startS}
        data-caption-end={caption.endS}
        data-music-track={musicTrack}
        data-music-gain={musicGain}
        data-sfx={JSON.stringify(sfx.map((item) => item.label))}
        data-visuals={JSON.stringify(visuals.map((item) => item.label))}
        data-overlay={
          overlay
            ? JSON.stringify({
                name: overlay.name,
                durationS: overlay.durationS,
                position: overlay.position,
              })
            : ""
        }
        data-selected-lane-kind={selectedLaneItem?.kind ?? ""}
        data-selected-lane-id={selectedLaneItem?.id ?? ""}
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
        <div
          ref={previewCanvasRef}
          data-testid="qa-preview-canvas"
          className="relative h-full max-w-full overflow-hidden rounded-xl bg-black shadow-sm"
          style={{ aspectRatio: "9 / 16" }}
        >
          <video
            ref={videoRef}
            src={SOURCE_URL}
            muted={muted}
            playsInline
            preload="auto"
            className="h-full w-full rounded-xl border border-border bg-black object-contain"
            onPlay={() => setPlaying(true)}
            onPause={() => setPlaying(false)}
            onSeeked={(event) => {
              sourceSeekInFlightRef.current = false;
              const target = pendingSourceSeekRef.current;
              if (
                target != null &&
                Math.abs(event.currentTarget.currentTime - target) < 1 / 120
              ) {
                pendingSourceSeekRef.current = null;
              }
              flushPendingSourceSeek();
            }}
            onTimeUpdate={(event) => {
              if (
                sourceSeekInFlightRef.current ||
                pendingSourceSeekRef.current != null
              ) {
                return;
              }
              publishDecodedSourceTime(event.currentTarget.currentTime, true);
            }}
          />
          {activeVisual && activeVisualLabel && activeVisualMode && (
            <div
              data-testid="qa-visual-preview"
              data-display-mode={activeVisualMode}
              data-visual-label={activeVisualLabel}
              aria-label={`Visual preview: ${activeVisualLabel}`}
              className={
                activeVisualMode === "fullscreen"
                  ? "pointer-events-none absolute inset-0 z-10 overflow-hidden rounded-xl bg-black"
                  : "pointer-events-none absolute bottom-3 right-3 z-10 h-[38%] w-[44%] overflow-hidden rounded-lg border border-white/80 bg-black shadow-lg"
              }
            >
              {activeVisualSource && activeVisual.mediaKind === "video" ? (
                <video
                  src={activeVisualSource}
                  aria-label={activeVisualLabel}
                  muted
                  autoPlay={playing}
                  loop
                  playsInline
                  className="h-full w-full object-cover"
                />
              ) : activeVisualSource ? (
                <Image
                  src={activeVisualSource}
                  alt={activeVisualLabel}
                  fill
                  sizes="(max-width: 430px) 260px"
                  className="object-cover"
                  unoptimized={activeVisualSource.startsWith("blob:")}
                  priority
                />
              ) : (
                <div className="flex h-full w-full items-center justify-center bg-zinc-950 px-5 text-center text-lg font-semibold leading-tight text-white">
                  Three cities,
                  <br />
                  one unforgettable summer.
                </div>
              )}
            </div>
          )}
          {textDraft &&
            currentTimeS >= textDraft.startS &&
            currentTimeS < textDraft.endS && (
            <div
              ref={textFrameRef}
              data-testid="qa-text-frame"
              data-selected={textSelected}
              role="group"
              aria-label="Text overlay"
              className={`absolute -translate-x-1/2 -translate-y-1/2 touch-none rounded-md px-2 py-1 text-center text-2xl font-semibold text-white ${
                textSelected
                  ? "cursor-grab border border-dashed border-white/90 bg-black/15 shadow-[0_0_0_1px_rgb(0_0_0/0.35)] active:cursor-grabbing"
                  : "border border-transparent"
              }`}
              style={{
                left: `${textDraft.xPct}%`,
                top: `${textDraft.yPct}%`,
                maxWidth: `${textDraft.maxWidthFrac * 100}%`,
                fontFamily: resolvedTextFont.family,
                fontWeight: resolvedTextFont.weight,
                // Match the 1080px production canvas scale instead of letting
                // large editor values consume most of the compact preview.
                fontSize: `${Math.max(8, textDraft.size * 0.32)}px`,
                color: textDraft.highlightColor ? "#18181b" : textDraft.color,
                textAlign: textDraft.alignment as
                  | "left"
                  | "center"
                  | "right",
                backgroundColor: textDraft.highlightColor ?? undefined,
                letterSpacing: `${textDraft.letterSpacing}em`,
                lineHeight: textDraft.lineSpacing,
                textTransform:
                  textDraft.textCase === "upper"
                    ? "uppercase"
                    : textDraft.textCase === "lower"
                      ? "lowercase"
                      : textDraft.textCase === "title"
                        ? "capitalize"
                        : "none",
                WebkitTextStroke:
                  textDraft.strokeWidth > 0
                    ? `${Math.max(0.25, textDraft.strokeWidth * 0.12)}px #000000`
                    : undefined,
                textShadow: !textDraft.shadowEnabled
                  ? "none"
                  : textDraft.shadowStyle === "high_visibility"
                    ? "0 3px 8px rgb(0 0 0 / 0.95), 0 0 2px #000000"
                    : "0 1px 4px rgb(0 0 0 / 0.9)",
                animation:
                  textDraft.effect === "fade-in"
                    ? "fade-up 180ms ease-out"
                    : textDraft.effect === "pop-in" ||
                        textDraft.effect === "bounce"
                      ? "fade-up 160ms ease-out"
                      : undefined,
              }}
              onPointerDown={startTextDrag}
              onPointerMove={moveTextDrag}
              onPointerUp={finishTextDrag}
              onPointerCancel={finishTextDrag}
            >
              <div
                ref={(element) => {
                  textEditorRef.current = element;
                  if (
                    element &&
                    document.activeElement !== element &&
                    element.textContent !== textDraft.text
                  ) {
                    element.textContent = textDraft.text;
                  }
                }}
                data-testid="qa-text-overlay"
                data-text-id={textDraft.id}
                role="textbox"
                aria-label="Text content"
                aria-multiline="true"
                contentEditable
                suppressContentEditableWarning
                spellCheck
                className="min-w-20 whitespace-pre-wrap break-words rounded-sm px-1 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-white"
                onFocus={() => {
                  setTextSelected(true);
                }}
                onBlur={() => {
                  textEditRecordedRef.current = false;
                }}
                onInput={(event) => {
                  if (!textEditRecordedRef.current) {
                    record();
                    textEditRecordedRef.current = true;
                  }
                  const text = event.currentTarget.textContent ?? "";
                  setTextDraft((current) =>
                    current ? { ...current, text } : current,
                  );
                  setReceipt("Editing text on screen");
                }}
                onPointerDown={(event) => event.stopPropagation()}
                onKeyDown={(event) => {
                  if (event.key === "Escape") {
                    event.preventDefault();
                    event.currentTarget.blur();
                    setTextSelected(false);
                    setReceipt("Text placed");
                    return;
                  }
                  if (!event.altKey) return;
                  const directions: Record<string, [number, number]> = {
                    ArrowLeft: [-2, 0],
                    ArrowRight: [2, 0],
                    ArrowUp: [0, -2],
                    ArrowDown: [0, 2],
                  };
                  const direction = directions[event.key];
                  if (!direction) return;
                  event.preventDefault();
                  moveTextByKeyboard(...direction);
                }}
              />
              {textSelected && (
                <Button
                  type="button"
                  variant="secondary"
                  size="icon"
                  aria-label="Move text"
                  className="absolute -right-5 -top-5 size-11 touch-none rounded-full border border-white bg-black/75 p-0 text-white shadow hover:bg-black/85"
                  onPointerDown={(event) => {
                    event.stopPropagation();
                    startTextDrag(event);
                  }}
                >
                  <Move className="size-3" />
                </Button>
              )}
            </div>
            )}
          {caption.enabled &&
            currentTimeS >= caption.startS &&
            currentTimeS < caption.endS && (
            <div
              data-testid="qa-caption-overlay"
              className={`pointer-events-none absolute bottom-5 left-1/2 max-w-[76%] -translate-x-1/2 rounded bg-black/75 px-2 py-1 text-center font-medium ${
                caption.style === "Editorial" ? "font-display" : ""
              }`}
              style={{
                fontFamily: resolvedCaptionFont.family,
                fontWeight: resolvedCaptionFont.weight,
                fontSize: `${Math.max(12, caption.size * 0.3)}px`,
                color:
                  caption.style === "Lime" ? "#18181b" : caption.color,
                backgroundColor:
                  caption.style === "Lime" ? "#a3e635" : undefined,
                WebkitTextStroke:
                  caption.stroke > 0
                    ? `${Math.max(0.25, caption.stroke * 0.08)}px #000000`
                    : undefined,
                textShadow: caption.shadow ? "0 2px 4px rgb(0 0 0 / 0.85)" : undefined,
              }}
            >
              {caption.text}
            </div>
          )}
          <div className="pointer-events-none absolute right-4 top-4 flex max-w-[70%] flex-wrap justify-end gap-1">
            <span
              className="rounded-full bg-background/90 px-2 py-1 text-[10px] font-medium text-foreground shadow-sm"
            >
              ♪ {musicTrack} · {musicGain}%
            </span>
            {sfx.map((effect) => (
              <span
                key={effect.id}
                className="rounded-full bg-background/90 px-2 py-1 text-[10px] text-foreground shadow-sm"
              >
                SFX · {effect.label}
              </span>
            ))}
          </div>
          {overlay && (
            <span
              data-testid="qa-media-overlay"
              className="pointer-events-none absolute rounded-md border border-white/70 bg-black/70 px-2 py-1 text-xs font-semibold text-white shadow"
              style={{
                left:
                  overlay.position === "Left"
                    ? "20%"
                    : overlay.position === "Right"
                      ? "70%"
                      : "50%",
                top: "34%",
                transform: "translate(-50%, -50%)",
              }}
            >
              {overlay.name}
            </span>
          )}
        </div>
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
          playbackClock={playbackClock}
          selectedClipId={selectedLaneItem ? null : selectedId}
          lanes={fixtureTimelineLanes}
          selectedLaneItem={selectedLaneItem}
          onScrubStart={() => videoRef.current?.pause()}
          onScrub={seekTo}
          onSelectClip={(id, seconds) => {
            setSelectedId(id);
            setSelectedLaneItem(null);
            seekTo(seconds);
          }}
          onSelectLaneItem={(item, seconds) => {
            setSelectedLaneItem({ kind: item.kind, id: item.id });
            seekTo(seconds);
            if (item.kind === "text") {
              const captionSelected = item.id.startsWith("caption-cue-");
              setActiveDockTool(captionSelected ? "captions" : "text");
              setTextSelected(!captionSelected);
            } else if (item.kind === "visual") {
              setActiveDockTool("visuals");
            } else if (item.kind === "sfx" || item.kind === "music") {
              setActiveDockTool("sounds");
            } else if (item.kind === "overlay") {
              setActiveDockTool("overlays");
            }
            setReceipt(`${item.label} selected in timeline`);
          }}
          onTrimStart={record}
          onPreviewTrim={previewTrim}
          onLaneResizeStart={record}
          onPreviewLaneTiming={previewLaneTiming}
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

        {activeDockTool && (
          <MobileToolPanel
            tool={activeDockTool}
            state={{
              text: {
                font: textDraft?.font ?? "Inter",
                color: textDraft?.color ?? "#FFFFFF",
                size: textDraft?.size ?? 64,
                alignment: textDraft?.alignment ?? "center",
                boxPosition: textDraft?.boxPosition ?? "center",
                effect: textDraft?.effect ?? "none",
                motion: textDraft?.motion ?? null,
                themeTransition: textDraft?.themeTransition ?? "none",
                themeTargetGlyph: textDraft?.themeTargetGlyph ?? "",
                preset: textDraft?.preset ?? "clean-caption",
                highlightColor: textDraft?.highlightColor ?? "#A3E635",
                strokeWidth: textDraft?.strokeWidth ?? 0,
                textCase: textDraft?.textCase ?? "none",
                letterSpacing: textDraft?.letterSpacing ?? 0,
                lineSpacing: textDraft?.lineSpacing ?? 1.15,
                maxWidthFrac: textDraft?.maxWidthFrac ?? 0.9,
                shadowEnabled: textDraft?.shadowEnabled ?? true,
                shadowStyle: textDraft?.shadowStyle ?? "standard",
                behindSubject: textDraft?.behindSubject ?? false,
              },
              captions: {
                text: caption.text,
                enabled: caption.enabled,
                font: caption.font,
                color: caption.color,
                size: caption.size,
                stroke: caption.stroke,
                shadow: caption.shadow,
                language: caption.language,
              },
              musicTrack,
              musicGain,
              visuals: visuals.map((item) => item.label),
              overlay: overlay
                ? {
                    name: overlay.name,
                    durationS: overlay.durationS,
                    position: overlay.position,
                  }
                : null,
              look,
              clipLook,
              transition,
              kriaStatus,
            }}
            onAction={runMobileToolAction}
            onClose={() => {
              setActiveDockTool(null);
              setReceipt("Tool controls closed");
            }}
            onDisabledTap={(reason) => setReceipt(reason)}
          />
        )}

        <ToolDock
          activeTool={activeTool}
          disabledTools={{}}
          novaEnabled
          onToggleTool={(tool) => {
            if (tool === "text") {
              if (activeDockTool === "text") {
                setActiveDockTool(null);
                setTextSelected(false);
                setReceipt("Text controls closed");
                return;
              }
              if (!textDraft) addText();
              else {
                setTextSelected(true);
                setActiveDockTool("text");
                setReceipt("Text controls opened");
              }
              return;
            }
            const sheetTool: SheetDockTool = tool;
            setTextSelected(false);
            setPanel(null);
            const closing = activeDockTool === sheetTool;
            setActiveDockTool(closing ? null : sheetTool);
            setReceipt(
              closing
                ? `${sheetTool === "nova" ? "Kria" : TOOL_COPY[sheetTool].title} tools closed`
                : `${TOOL_COPY[sheetTool].title} tools opened`,
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
