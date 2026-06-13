"use client";

/**
 * One layout option (Classic or Editorial) rendered as a small visual preview
 * on a near-black tile inside a white card — the magazine look of the burned
 * intro, so the user picks layout by sight rather than by label.
 *
 * - Classic: a single centered line of the user's text.
 * - Editorial: the REAL editorial word-cluster geometry (computeClusterBlocks,
 *   the TS port of intro_cluster.py's EDITORIAL_STYLE path — parity-guarded by
 *   overlay-cluster-layout.test.ts), laid out at 1080×1920 canvas scale and
 *   projected into the tile. Hook too short/long/unfittable for a cluster →
 *   fall back to a representative static mock (the server would render linear).
 *
 * `role="radio"` — render inside a `role="radiogroup"` parent (VariantCard does).
 * Selected = lime ring (DESIGN.md selection token). Min 44px touch target (W7).
 */

import { useEffect, useRef, useState } from "react";
import {
  CLUSTER_CANVAS_W,
  computeClusterBlocks,
  EDITORIAL_STYLE,
} from "@/lib/variant-editor/overlay-cluster-layout";
import {
  ensureClusterFontLoaded,
  makeCanvasClusterMeasure,
} from "@/lib/canvas-measure";
import { resolveClusterCssFont } from "@/lib/overlay-constants";

export type LayoutKind = "classic" | "editorial";

export function LayoutPreviewCard({
  kind,
  text,
  selected,
  disabled,
  title,
  onSelect,
}: {
  kind: LayoutKind;
  /** The user's hook text, sampled into the preview. */
  text: string;
  selected: boolean;
  disabled?: boolean;
  title?: string;
  onSelect: () => void;
}) {
  const label = kind === "classic" ? "Classic" : "Editorial";
  const sample = (text ?? "").trim();

  return (
    <button
      type="button"
      role="radio"
      aria-checked={selected}
      aria-label={`${label} layout`}
      disabled={disabled}
      title={title}
      onClick={onSelect}
      className={[
        "flex min-h-[44px] flex-1 basis-0 flex-col gap-1.5 rounded-lg border bg-white p-2 text-left transition-colors disabled:cursor-not-allowed disabled:opacity-50",
        selected
          ? "border-lime-600 ring-1 ring-lime-600"
          : "border-zinc-200 hover:border-zinc-400",
      ].join(" ")}
    >
      <span className="flex aspect-[5/3] w-full items-center justify-center overflow-hidden rounded-md bg-[#0c0c0e] px-2">
        {kind === "classic" ? (
          <ClassicMock text={sample} />
        ) : (
          <EditorialMock text={sample} />
        )}
      </span>
      <span className="text-[11px] font-medium text-[#3f3f46]">{label}</span>
    </button>
  );
}

/** Classic: one centered serif line of the user's text. */
function ClassicMock({ text }: { text: string }) {
  return (
    <span className="line-clamp-2 text-center font-display text-[13px] leading-tight text-white">
      {text || "Your hook"}
    </span>
  );
}

/** Editorial: the REAL word-cluster geometry, projected into the tile. The tile
 * (aspect-5/3) maps its width to the 1080px canvas, so blocks scale by
 * tileWidth / CANVAS_W. Hooks the engine declines (word count / fit) fall back to
 * the static mock — exactly the cases where the server renders linear. */
function EditorialMock({ text }: { text: string }) {
  const ref = useRef<HTMLSpanElement | null>(null);
  const [width, setWidth] = useState(0);
  const [fontTick, setFontTick] = useState(0);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    setWidth(el.getBoundingClientRect().width);
    const obs = new ResizeObserver((e) => setWidth(e[0]?.contentRect.width ?? 0));
    obs.observe(el);
    return () => obs.disconnect();
  }, []);

  useEffect(() => {
    let cancelled = false;
    void Promise.all([
      ensureClusterFontLoaded(EDITORIAL_STYLE.heroFont),
      ensureClusterFontLoaded(EDITORIAL_STYLE.bodyFont),
      ensureClusterFontLoaded(EDITORIAL_STYLE.accentFont),
    ]).then(() => {
      if (!cancelled) setFontTick((t) => t + 1);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  const blocks =
    width > 0
      ? computeClusterBlocks((text || "your bold hook here").trim(), makeCanvasClusterMeasure(), {
          baseSizePx: 60,
          revealWindowS: 0,
        })
      : null;
  // fontTick participates so the layout re-runs once the real faces load.
  void fontTick;

  const scale = width > 0 ? width / CLUSTER_CANVAS_W : 0;

  return (
    <span ref={ref} className="relative block h-full w-full">
      {blocks ? (
        blocks.map((b, i) => {
          const css = resolveClusterCssFont(b.family);
          return (
            <span
              key={`${b.text}-${i}`}
              className="absolute leading-none text-white"
              style={{
                left: `${b.positionXFrac * 100}%`,
                top: `${b.positionYFrac * 100}%`,
                transform: "translate(-50%, -50%)",
                whiteSpace: "nowrap",
                fontFamily: css.family,
                fontWeight: css.weight,
                fontStyle: css.style,
                fontSize: `${b.textSizePx * scale}px`,
              }}
            >
              {b.text}
            </span>
          );
        })
      ) : (
        <EditorialStaticMock text={text} />
      )}
    </span>
  );
}

/** Fallback when the hook doesn't suit a cluster (the server renders linear): a
 * representative static staggered mock so the tile still signals "magazine". */
function EditorialStaticMock({ text }: { text: string }) {
  const words = (text || "Your bold hook").split(/\s+/).filter(Boolean).slice(0, 4);
  const styles = [
    "text-[15px] font-bold -translate-y-0.5",
    "text-[11px] font-normal translate-y-1",
    "text-[18px] font-bold translate-y-0",
    "text-[10px] font-normal -translate-y-1",
  ];
  const accents = ["text-white", "text-lime-300", "text-white", "text-zinc-400"];
  return (
    <span className="absolute inset-0 flex flex-wrap items-center justify-center gap-x-1.5 gap-y-0.5 font-display leading-none">
      {words.map((w, i) => (
        <span
          key={`${w}-${i}`}
          className={`${styles[i % styles.length]} ${accents[i % accents.length]}`}
        >
          {w}
        </span>
      ))}
    </span>
  );
}
