import { createRef, type RefObject } from "react";

import { buildVirtualTimeline } from "@/app/plan/items/[id]/_editor/virtual-timeline";
import type {
  VirtualPreviewController,
  VirtualPreviewVideoProps,
} from "@/app/plan/items/[id]/_editor/useVirtualPreview";
import type { DraftSlot } from "@/app/generative/timeline-math";
import type { TimelineMediaLayout } from "@/lib/generative-api";

export interface EditorCanvasVirtualPreviewOptions {
  mediaUrl?: string | null;
  layout?: TimelineMediaLayout | null;
}

export function createEditorCanvasVirtualPreview(
  noop: () => void = () => undefined,
  options: EditorCanvasVirtualPreviewOptions = {},
): VirtualPreviewController {
  const slots: DraftSlot[] = [{
    key: "geometry-slot",
    slotId: "geometry-slot",
    clipIndex: 0,
    inS: 0,
    durationBeats: null,
    durationS: 2,
    removed: false,
    layout: options.layout,
    momentDescription: null,
  }];
  const timeline = buildVirtualTimeline(slots, [{
    clip_index: 0,
    signed_url: options.mediaUrl ?? null,
    kind: "video",
  }], []);
  const videoProps = (
    deck: "a" | "b",
    ref: RefObject<HTMLVideoElement>,
  ): VirtualPreviewVideoProps => ({
    ref,
    muted: true,
    playsInline: true,
    preload: "auto",
    "data-virtual-preview-deck": deck,
    "data-active": deck === "a",
    onLoadedMetadata: noop,
    onCanPlay: noop,
    onPlaying: noop,
    onWaiting: noop,
    onSeeking: noop,
    onSeeked: noop,
    onTimeUpdate: noop,
    onEnded: noop,
    onPlay: noop,
    onPause: noop,
    onError: noop,
  });

  return {
    timeline,
    activeDeck: "a",
    buffering: false,
    videoAProps: videoProps("a", createRef<HTMLVideoElement>()),
    videoBProps: videoProps("b", createRef<HTMLVideoElement>()),
    musicAudioProps: null,
    play: noop,
    pause: noop,
    toggle: noop,
    seekTo: noop,
  };
}
