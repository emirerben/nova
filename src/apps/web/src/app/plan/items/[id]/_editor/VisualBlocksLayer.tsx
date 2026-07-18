"use client";

import type { CSSProperties } from "react";
import type { PoolAsset, VisualBlock, VisualShot } from "@/lib/plan-api";

function shotAt(block: Extract<VisualBlock, { kind: "montage" }>, timeS: number) {
  const offset = timeS - block.start_s;
  return block.shots.find(
    (shot) => offset >= shot.start_offset_s && offset < shot.start_offset_s + shot.duration_s,
  ) ?? block.shots.at(-1) ?? null;
}

function motionClass(shot: VisualShot): string {
  if (shot.motion === "zoom_in") return "visual-zoom-in";
  if (shot.motion === "zoom_out") return "visual-zoom-out";
  if (shot.motion === "pan_left") return "visual-pan-left";
  if (shot.motion === "pan_right") return "visual-pan-right";
  return "";
}

function Shot({ shot, url }: { shot: VisualShot; url: string | null }) {
  if (!url) {
    return <div className="h-full w-full bg-zinc-800" aria-label="Missing visual asset" />;
  }
  const style: CSSProperties = {
    objectPosition: `${shot.crop.x_frac * 100}% ${shot.crop.y_frac * 100}%`,
    transform: `scale(${Math.max(1, shot.crop.scale)})`,
    animationDuration: `${Math.max(0.05, shot.duration_s)}s`,
  };
  if (shot.kind === "video") {
    return (
      <video
        src={
          shot.trim_start_s != null
            ? `${url}#t=${shot.trim_start_s},${shot.trim_start_s + shot.duration_s}`
            : url
        }
        muted
        autoPlay
        loop
        playsInline
        className={`h-full w-full object-cover ${motionClass(shot)}`}
        style={style}
      />
    );
  }
  return (
    // Pool URLs are signed object URLs and cannot use next/image optimization.
    // eslint-disable-next-line @next/next/no-img-element
    <img
      src={url}
      alt=""
      className={`h-full w-full object-cover ${motionClass(shot)}`}
      style={style}
    />
  );
}

export default function VisualBlocksLayer({
  blocks,
  assets,
  currentTime,
}: {
  blocks: VisualBlock[];
  assets: PoolAsset[];
  currentTime: number;
}) {
  const block = blocks.find(
    (candidate) => currentTime >= candidate.start_s && currentTime < candidate.end_s,
  );
  if (!block) return null;
  const urls = new Map(assets.map((asset) => [asset.id, asset.display_url ?? null]));

  let content: React.ReactNode = null;
  if (block.kind === "montage") {
    const shot = shotAt(block, currentTime);
    content = shot ? <Shot shot={shot} url={urls.get(shot.asset_id) ?? null} /> : null;
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
    content = <Shot shot={shot} url={urls.get(shot.asset_id) ?? null} />;
  }

  const localTime = currentTime - block.start_s;
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
      <style jsx>{`
        @media (prefers-reduced-motion: no-preference) {
          :global(.visual-zoom-in) { animation: visualZoomIn 8s ease-out both; }
          :global(.visual-zoom-out) { animation: visualZoomOut 8s ease-out both; }
          :global(.visual-pan-left) { animation: visualPanLeft 8s ease-out both; }
          :global(.visual-pan-right) { animation: visualPanRight 8s ease-out both; }
        }
        @keyframes visualZoomIn { from { scale: 1; } to { scale: 1.08; } }
        @keyframes visualZoomOut { from { scale: 1.08; } to { scale: 1; } }
        @keyframes visualPanLeft { from { translate: 3% 0; } to { translate: -3% 0; } }
        @keyframes visualPanRight { from { translate: -3% 0; } to { translate: 3% 0; } }
      `}</style>
    </div>
  );
}
