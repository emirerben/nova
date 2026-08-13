"use client";

import { useEffect, useRef, type CSSProperties } from "react";
import type { PoolAsset, VisualBlock, VisualShot } from "@/lib/plan-api";
import {
  useEditorPlaybackTime,
  type EditorPlaybackClock,
} from "./editor-playback-clock";

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
  currentTime,
  frameDriven = false,
  playbackClock,
  playing = false,
}: {
  blocks: VisualBlock[];
  assets: PoolAsset[];
  currentTime: number;
  frameDriven?: boolean;
  playbackClock?: EditorPlaybackClock | null;
  playing?: boolean;
}) {
  const sampledTime = useEditorPlaybackTime(playbackClock, currentTime);
  const block = blocks.find(
    (candidate) => sampledTime >= candidate.start_s && sampledTime < candidate.end_s,
  );
  if (!block) return null;
  const urls = new Map(assets.map((asset) => [asset.id, asset.display_url ?? null]));

  let content: React.ReactNode = null;
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
      style={{ zIndex: 10, opacity: Math.max(0, Math.min(1, opacity)) }}
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
