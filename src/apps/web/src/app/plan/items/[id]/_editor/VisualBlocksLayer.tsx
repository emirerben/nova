"use client";

import { useEffect, useRef, useState, type CSSProperties } from "react";
import type { MediaVisualBlock, PoolAsset, VisualBlock, VisualShot } from "@/lib/plan-api";
import { Button } from "@/components/ui/button";
import {
  useEditorPlaybackTime,
  type EditorPlaybackClock,
} from "./editor-playback-clock";
import { mediaPreviewGeometry } from "./editor-media-visuals";

function shotAt(block: Extract<VisualBlock, { kind: "montage" }>, timeS: number) {
  const offset = timeS - block.start_s;
  const shot = block.shots.find(
    (shot) => offset >= shot.start_offset_s && offset < shot.start_offset_s + shot.duration_s,
  ) ?? block.shots.at(-1) ?? null;
  return shot ? { shot, localTimeS: Math.max(0, offset - shot.start_offset_s) } : null;
}

const VISUAL_BLOCK_FPS = 30;
// Keep these parity-critical values aligned with
// app/pipeline/visual_blocks.py::_motion_exprs.
const VISUAL_MOTION_ZOOM_FACTOR = 1.08;
const VISUAL_MOTION_PAN_FRACTION = 0.08;
const VISUAL_VIDEO_SEEK_TOLERANCE_S = 0.15;
const VISUAL_VIDEO_CORRECTION_INTERVAL_MS = 500;

function MediaBlockPreview({
  block,
  url,
  localTimeS,
  frameDriven,
  playing,
}: {
  block: MediaVisualBlock;
  url: string | null;
  localTimeS: number;
  frameDriven: boolean;
  playing: boolean;
}) {
  const ref = useRef<HTMLVideoElement>(null);
  const lastCorrectionMsRef = useRef(Number.NEGATIVE_INFINITY);
  const [failed, setFailed] = useState(false);
  const trimStart = block.trim_start_s ?? 0;
  useEffect(() => setFailed(false), [url]);
  useEffect(() => {
    if (!frameDriven || block.media_kind !== "video" || !ref.current) return;
    const video = ref.current;
    if (playing) void video.play().catch(() => {});
    else video.pause();
  }, [block.media_kind, frameDriven, playing, url]);
  useEffect(() => {
    if (!frameDriven || block.media_kind !== "video" || !ref.current) return;
    const video = ref.current;
    const now = performance.now();
    if (playing && now - lastCorrectionMsRef.current < VISUAL_VIDEO_CORRECTION_INTERVAL_MS) {
      return;
    }
    lastCorrectionMsRef.current = now;
    const target = trimStart + Math.max(0, localTimeS);
    if (Math.abs(video.currentTime - target) > VISUAL_VIDEO_SEEK_TOLERANCE_S) {
      try { video.currentTime = target; } catch { /* metadata is not ready */ }
    }
  }, [block.media_kind, frameDriven, localTimeS, playing, trimStart, url]);
  if (!url || failed) return <div className="flex h-full w-full items-center justify-center bg-zinc-800 text-[11px] text-white/70">Preview unavailable</div>;
  const style: CSSProperties = {
    objectFit: block.transform.fit_mode === "cover" ? "cover" : "contain",
    objectPosition: "center",
  };
  if (block.media_kind === "video") {
    return <video ref={ref} src={url} muted loop playsInline autoPlay={playing} className="h-full w-full" style={style} onLoadedMetadata={(event) => { event.currentTarget.currentTime = trimStart + Math.max(0, localTimeS); }} onError={() => setFailed(true)} />;
  }
  return <img src={url} alt="" className="h-full w-full" style={style} draggable={false} onError={() => setFailed(true)} />;
}

export function visualShotPreviewState(shot: VisualShot, localTimeS: number) {
  const frames = Math.max(1, Math.round(shot.duration_s * VISUAL_BLOCK_FPS));
  const progress =
    frames <= 1
      ? 1
      : Math.max(0, Math.min(1, (localTimeS * VISUAL_BLOCK_FPS) / (frames - 1)));
  const cropScale = Math.max(1, shot.crop.scale);
  const startScale =
    shot.motion === "zoom_out" ? cropScale * VISUAL_MOTION_ZOOM_FACTOR : cropScale;
  const endScale =
    shot.motion === "zoom_in" ? cropScale * VISUAL_MOTION_ZOOM_FACTOR : cropScale;
  const panDx =
    shot.motion === "pan_right"
      ? VISUAL_MOTION_PAN_FRACTION
      : shot.motion === "pan_left"
        ? -VISUAL_MOTION_PAN_FRACTION
        : 0;
  return {
    progress,
    scale: startScale + (endScale - startScale) * progress,
    xFrac: Math.max(0, Math.min(1, shot.crop.x_frac + panDx * progress)),
    yFrac: Math.max(0, Math.min(1, shot.crop.y_frac)),
  };
}

function legacyMotionClass(shot: VisualShot): string {
  if (shot.motion === "zoom_in") return "visual-zoom-in";
  if (shot.motion === "zoom_out") return "visual-zoom-out";
  if (shot.motion === "pan_left") return "visual-pan-left";
  if (shot.motion === "pan_right") return "visual-pan-right";
  return "";
}

function Shot({
  shot,
  url,
  localTimeS,
  frameDriven,
  playing,
}: {
  shot: VisualShot;
  url: string | null;
  localTimeS: number;
  frameDriven: boolean;
  playing: boolean;
}) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const lastCorrectionMsRef = useRef(Number.NEGATIVE_INFINITY);
  const sourceTimeS = (shot.trim_start_s ?? 0) + localTimeS;
  const previewState = visualShotPreviewState(shot, localTimeS);
  useEffect(() => {
    if (!frameDriven) return;
    const video = videoRef.current;
    if (!video) return;
    if (playing) {
      void video.play().catch(() => {
        // Autoplay policies may reject; the next transport action retries.
      });
    } else {
      video.pause();
    }
  }, [frameDriven, playing, url]);
  useEffect(() => {
    if (!frameDriven) return;
    const video = videoRef.current;
    if (!video) return;
    const now = performance.now();
    if (playing && now - lastCorrectionMsRef.current < VISUAL_VIDEO_CORRECTION_INTERVAL_MS) {
      return;
    }
    lastCorrectionMsRef.current = now;
    if (Math.abs(video.currentTime - sourceTimeS) <= VISUAL_VIDEO_SEEK_TOLERANCE_S) return;
    try {
      video.currentTime = sourceTimeS;
    } catch {
      // Metadata may not be ready. onLoadedMetadata retries below.
    }
  }, [frameDriven, playing, sourceTimeS]);
  if (!url) {
    return <div className="h-full w-full bg-zinc-800" aria-label="Missing visual asset" />;
  }
  const style: CSSProperties = {
    objectPosition: frameDriven
      ? `${previewState.xFrac * 100}% ${previewState.yFrac * 100}%`
      : `${shot.crop.x_frac * 100}% ${shot.crop.y_frac * 100}%`,
    transform: frameDriven
      ? `scale(${previewState.scale})`
      : `scale(${Math.max(1, shot.crop.scale)})`,
    animationDuration: frameDriven ? undefined : `${Math.max(0.05, shot.duration_s)}s`,
  };
  if (shot.kind === "video") {
    return (
      <video
        ref={videoRef}
        src={
          shot.trim_start_s != null
            ? `${url}#t=${shot.trim_start_s},${shot.trim_start_s + shot.duration_s}`
            : url
        }
        muted
        autoPlay={!frameDriven || playing}
        loop
        playsInline
        className={`h-full w-full object-cover ${frameDriven ? "" : legacyMotionClass(shot)}`}
        style={style}
        onLoadedMetadata={(event) => {
          if (frameDriven) event.currentTarget.currentTime = sourceTimeS;
        }}
      />
    );
  }
  return (
    // Pool URLs are signed object URLs and cannot use next/image optimization.
    // eslint-disable-next-line @next/next/no-img-element
    <img
      src={url}
      alt=""
      className={`h-full w-full object-cover ${frameDriven ? "" : legacyMotionClass(shot)}`}
      style={style}
    />
  );
}

export default function VisualBlocksLayer({
  blocks,
  assets,
  previewUrls = {},
  currentTime,
  frameDriven = false,
  playbackClock,
  playing = false,
  allowManipulation = false,
  selectedMediaBlockId = null,
  onSelectMediaBlock,
  onPreviewMediaBlock,
  onPatchMediaBlock,
  onRecordMediaBlock,
}: {
  blocks: VisualBlock[];
  assets: PoolAsset[];
  previewUrls?: Record<string, string>;
  currentTime: number;
  frameDriven?: boolean;
  playbackClock?: EditorPlaybackClock | null;
  playing?: boolean;
  allowManipulation?: boolean;
  selectedMediaBlockId?: string | null;
  onSelectMediaBlock?: (id: string) => void;
  onPreviewMediaBlock?: (id: string, patch: Partial<MediaVisualBlock>) => void;
  onPatchMediaBlock?: (id: string, patch: Partial<MediaVisualBlock>) => void;
  onRecordMediaBlock?: () => void;
}) {
  const sampledTime = useEditorPlaybackTime(playbackClock, currentTime);
  const activeBlocks = blocks
    .filter((candidate) => sampledTime >= candidate.start_s && sampledTime < candidate.end_s)
    .sort((a, b) => {
      const aMedia = a.kind === "media" ? 1 : 0;
      const bMedia = b.kind === "media" ? 1 : 0;
      return aMedia - bMedia || (a.kind === "media" && b.kind === "media" ? a.z - b.z : 0);
    });
  if (activeBlocks.length === 0) return null;
  const urls = new Map(assets.map((asset) => [asset.id, asset.display_url ?? asset.preview_url ?? null]));
  const aspects = new Map(assets.map((asset) => [asset.id, asset.aspect]));

  return (
    <div
      data-visual-block-layer="true"
      className="pointer-events-none absolute inset-0 isolate overflow-hidden"
      style={{ zIndex: 10 }}
    >
      {activeBlocks.map((block) => (
        <VisualBlockContent
          key={block.id}
          block={block}
          sampledTime={sampledTime}
          frameDriven={frameDriven}
          playing={playing}
          urls={urls}
          aspects={aspects}
          previewUrls={previewUrls}
          allowManipulation={allowManipulation}
          selected={selectedMediaBlockId === block.id}
          onSelect={onSelectMediaBlock}
          onPreview={onPreviewMediaBlock}
          onPatch={onPatchMediaBlock}
          onRecord={onRecordMediaBlock}
        />
      ))}
    </div>
  );
}

function VisualBlockContent({
  block,
  sampledTime,
  frameDriven,
  playing,
  urls,
  aspects,
  previewUrls,
  allowManipulation,
  selected,
  onSelect,
  onPreview,
  onPatch,
  onRecord,
}: {
  block: VisualBlock;
  sampledTime: number;
  frameDriven: boolean;
  playing: boolean;
  urls: Map<string, string | null>;
  aspects: Map<string, number | null>;
  previewUrls: Record<string, string>;
  allowManipulation: boolean;
  selected: boolean;
  onSelect?: (id: string) => void;
  onPreview?: (id: string, patch: Partial<MediaVisualBlock>) => void;
  onPatch?: (id: string, patch: Partial<MediaVisualBlock>) => void;
  onRecord?: () => void;
}) {

  let content: React.ReactNode = null;
  if (block.kind === "media") {
    const url = previewUrls[block.id] ?? previewUrls[block.asset_id] ?? urls.get(block.asset_id) ?? null;
    const aspect = aspects.get(block.asset_id) ?? null;
    const localTimeS = Math.max(0, sampledTime - block.start_s);
    const mediaContent = (
      <MediaBlockPreview
        block={block}
        url={url}
        localTimeS={localTimeS}
        frameDriven={frameDriven}
        playing={playing}
      />
    );
    const isFullscreen = block.display_mode === "fullscreen";
    const geometry = mediaPreviewGeometry(block, aspect);
    const duration = Math.max(0.001, block.end_s - block.start_s);
    const fadeS = Math.min(0.15, duration / 3);
    const fadeEnabled = duration > 0.3;
    const opacity = fadeEnabled && block.transition_in === "fade" && localTimeS < fadeS
      ? localTimeS / fadeS
      : fadeEnabled && block.transition_out === "fade" && duration - localTimeS < fadeS
        ? (duration - localTimeS) / fadeS
        : 1;
    return (
      <div
        data-visual-block-id={block.id}
        data-media-visual-block="true"
        className={`absolute overflow-hidden ${allowManipulation ? "pointer-events-auto touch-none" : "pointer-events-none"}`}
        style={{
          left: `${geometry.leftPct}%`,
          top: `${geometry.topPct}%`,
          width: `${geometry.widthPct}%`,
          height: `${geometry.heightPct}%`,
          // Structured visuals are composited first by FFmpeg, followed by
          // media blocks. Reserve 0 for that structured pass so the live
          // canvas has the same ordering even when a media block's z is 0.
          zIndex: block.z + 1,
          opacity: Math.max(0, Math.min(1, opacity)),
        }}
        onPointerDown={(event) => {
          if (!allowManipulation || (event.button != null && event.button !== 0)) return;
          event.stopPropagation();
          onSelect?.(block.id);
          onRecord?.();
          const stage = event.currentTarget.parentElement?.getBoundingClientRect();
          if (!stage) return;
          const startX = block.x_frac;
          const startY = block.y_frac;
          const startFocalX = block.transform.focal_x;
          const startFocalY = block.transform.focal_y;
          const focalAfterDrag = (
            start: number,
            deltaPx: number,
            stagePx: number,
            renderedPct: number,
          ) => {
            const availablePct = 100 - renderedPct;
            if (stagePx <= 0 || Math.abs(availablePct) < 0.001) return start;
            return Math.max(
              0,
              Math.min(1, start + ((deltaPx / stagePx) * 100) / availablePct),
            );
          };
          const pointerPatch = (clientX: number, clientY: number): Partial<MediaVisualBlock> =>
            isFullscreen
              ? {
                  transform: {
                    ...block.transform,
                    focal_x: focalAfterDrag(
                      startFocalX,
                      clientX - event.clientX,
                      stage.width,
                      geometry.widthPct,
                    ),
                    focal_y: focalAfterDrag(
                      startFocalY,
                      clientY - event.clientY,
                      stage.height,
                      geometry.heightPct,
                    ),
                  },
                }
              : {
                  x_frac: Math.max(0, Math.min(1, startX + (clientX - event.clientX) / stage.width)),
                  y_frac: Math.max(0, Math.min(1, startY + (clientY - event.clientY) / stage.height)),
                };
          const move = (moveEvent: PointerEvent) => {
            onPreview?.(block.id, pointerPatch(moveEvent.clientX, moveEvent.clientY));
          };
          const up = (upEvent: PointerEvent) => {
            window.removeEventListener("pointermove", move);
            window.removeEventListener("pointerup", up);
            onPatch?.(block.id, pointerPatch(upEvent.clientX, upEvent.clientY));
          };
          window.addEventListener("pointermove", move);
          window.addEventListener("pointerup", up, { once: true });
        }}
      >
        {mediaContent}
        {selected && allowManipulation && block.display_mode === "overlay" && (
          <div className="pointer-events-none absolute inset-0 border-[1.5px] border-lime-500">
            <Button
              type="button"
              variant="ghost"
              size="icon"
              aria-label="Resize media overlay"
              className="pointer-events-auto absolute bottom-0 right-0 h-11 w-11 rounded-none bg-transparent p-0 hover:bg-transparent"
              onPointerDown={(event) => {
                event.stopPropagation();
                const stage = event.currentTarget.parentElement?.parentElement?.getBoundingClientRect();
                if (!stage) return;
                onRecord?.();
                const startScale = block.scale;
                const startDistance = Math.max(1, Math.hypot(event.clientX - stage.left, event.clientY - stage.top));
                const move = (moveEvent: PointerEvent) => onPreview?.(block.id, { scale: Math.max(0.05, Math.min(1, startScale * Math.hypot(moveEvent.clientX - stage.left, moveEvent.clientY - stage.top) / startDistance)) });
                const up = (upEvent: PointerEvent) => {
                  window.removeEventListener("pointermove", move);
                  window.removeEventListener("pointerup", up);
                  onPatch?.(block.id, { scale: Math.max(0.05, Math.min(1, startScale * Math.hypot(upEvent.clientX - stage.left, upEvent.clientY - stage.top) / startDistance)) });
                };
                window.addEventListener("pointermove", move);
                window.addEventListener("pointerup", up, { once: true });
              }}
            >
              <span className="absolute bottom-0 right-0 h-5 w-5 rounded-sm border border-zinc-900 bg-white" />
            </Button>
          </div>
        )}
      </div>
    );
  }
  if (block.kind === "montage") {
    const active = shotAt(block, sampledTime);
    content = active ? (
      <Shot
        shot={active.shot}
        url={urls.get(active.shot.asset_id) ?? null}
        localTimeS={active.localTimeS}
        frameDriven={frameDriven}
        playing={playing}
      />
    ) : null;
  } else if (block.background.type === "solid") {
    content = <div className="h-full w-full" style={{ backgroundColor: block.background.color }} />;
  } else if (block.background.type === "gradient") {
    content = (
      <div
        data-visual-background="gradient"
        className="h-full w-full"
        style={{
          background: `linear-gradient(${block.background.angle_deg}deg, ${block.background.from}, ${block.background.to})`,
        }}
      />
    );
  } else if (block.background.type === "blur_previous") {
    content = (
      <div
        className="h-full w-full bg-black/20 backdrop-blur-2xl"
        style={{ backdropFilter: `blur(${block.background.blur_px}px)` }}
      />
    );
  } else {
    const shot = block.background.shot;
    content = (
      <Shot
        shot={shot}
        url={urls.get(shot.asset_id) ?? null}
        localTimeS={Math.max(0, sampledTime - block.start_s)}
        frameDriven={frameDriven}
        playing={playing}
      />
    );
  }

  const localTime = sampledTime - block.start_s;
  const duration = block.end_s - block.start_s;
  const fadeS = Math.min(0.15, duration / 3);
  let opacity = 1;
  if (block.transition_in === "fade" && localTime < fadeS) {
    opacity = localTime / fadeS;
  }
  if (block.transition_out === "fade" && duration - localTime < fadeS) {
    opacity = Math.min(opacity, (duration - localTime) / fadeS);
  }

  return (
    <div
      data-visual-block-id={block.id}
      className="pointer-events-none absolute inset-0 overflow-hidden"
      style={{ zIndex: 0, opacity: Math.max(0, Math.min(1, opacity)) }}
    >
      {content}
      {!frameDriven && (
        <style jsx>{`
          @media (prefers-reduced-motion: no-preference) {
            :global(.visual-zoom-in) { animation: visualZoomIn 8s ease-out both; }
            :global(.visual-zoom-out) { animation: visualZoomOut 8s ease-out both; }
            :global(.visual-pan-left) { animation: visualPanLeft 8s ease-out both; }
            :global(.visual-pan-right) { animation: visualPanRight 8s ease-out both; }
          }
          @keyframes visualZoomIn { from { scale: 1; } to { scale: ${VISUAL_MOTION_ZOOM_FACTOR}; } }
          @keyframes visualZoomOut { from { scale: ${VISUAL_MOTION_ZOOM_FACTOR}; } to { scale: 1; } }
          @keyframes visualPanLeft { from { translate: 3% 0; } to { translate: -3% 0; } }
          @keyframes visualPanRight { from { translate: -3% 0; } to { translate: 3% 0; } }
        `}</style>
      )}
    </div>
  );
}
