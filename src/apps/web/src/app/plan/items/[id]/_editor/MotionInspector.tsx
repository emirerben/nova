"use client";

import {
  CREATOR_BLOCK_CATALOG,
  MOTION_FPS,
  creatorBlockEntry,
  type MotionPresetInstanceV1,
  type MotionPresetPatch,
} from "@nova/motion-runtime";
import { isBoundedCreatorImageAsset, type PoolAsset } from "@/lib/plan-api";

export default function MotionInspector({
  scene,
  durationS,
  assets,
  showClose = true,
  onPatch,
  onRemove,
  onClose,
}: {
  scene: MotionPresetInstanceV1;
  durationS: number;
  assets: PoolAsset[];
  showClose?: boolean;
  onPatch: (id: string, patch: MotionPresetPatch) => void;
  onRemove: (id: string) => void;
  onClose: () => void;
}) {
  const label = scene.preset_id === "route_trace"
    ? "Route trace"
    : CREATOR_BLOCK_CATALOG.find((entry) => entry.preset_id === scene.preset_id)?.label;
  return (
    <div
      data-testid="selected-motion-inspector"
      className="min-h-0 flex-1 overflow-y-auto px-5 pb-6 pt-4 motion-safe:animate-fade-up motion-safe:[animation-duration:150ms]"
    >
      <div className="flex items-center justify-between gap-3">
        <div className="min-w-0">
          <p className="text-[11px] font-semibold uppercase tracking-wide text-[#71717a]">
            Creator Block
          </p>
          <h2 className="truncate font-display text-[18px] text-[#0c0c0e]">{label}</h2>
        </div>
        {showClose && (
          <button
            type="button"
            aria-label="Close (clears selection)"
            onClick={onClose}
            className="flex h-7 w-7 shrink-0 items-center justify-center rounded-lg text-[13px] text-[#71717a] hover:bg-zinc-100 focus-visible:outline-2 focus-visible:outline-[#0c0c0e]"
          >
            ✕
          </button>
        )}
      </div>

      {scene.preset_id !== "route_trace" && (
        <fieldset className="mt-5">
          <legend className="text-[12px] font-semibold text-[#3f3f46]">Content</legend>
          <CreatorBlockFields scene={scene} assets={assets} onPatch={onPatch} />
        </fieldset>
      )}

      <fieldset className="mt-5 border-t border-zinc-200 pt-4">
        <legend className="text-[12px] font-semibold text-[#3f3f46]">Motion</legend>
        <label className="mt-3 block text-[11px] text-[#71717a]">
          <span className="flex items-center justify-between">
            <span>Intensity</span>
            <span className="tabular-nums">{Math.round(scene.intensity * 100)}%</span>
          </span>
          <input
            aria-label="Intensity"
            type="range"
            min={0}
            max={1}
            step={0.05}
            value={scene.intensity}
            onChange={(event) => onPatch(scene.id, { intensity: Number(event.target.value) })}
            className="mt-2 w-full accent-lime-500"
          />
        </label>
      </fieldset>

      <fieldset className="mt-5 border-t border-zinc-200 pt-4">
        <legend className="text-[12px] font-semibold text-[#3f3f46]">Timing</legend>
        <div className="mt-3 grid grid-cols-2 gap-3">
          <label className="text-[10px] text-[#71717a]">
            Start (seconds)
            <input
              type="number"
              min={0}
              max={Math.max(0, (scene.end_frame_exclusive - 1) / MOTION_FPS)}
              step={1 / MOTION_FPS}
              value={(scene.start_frame / MOTION_FPS).toFixed(2)}
              onChange={(event) => {
                const next = Math.max(
                  0,
                  Math.min(
                    scene.end_frame_exclusive - 1,
                    Math.round(Number(event.target.value) * MOTION_FPS),
                  ),
                );
                onPatch(scene.id, { start_frame: next });
              }}
              className="mt-1 h-11 w-full rounded-lg border border-zinc-200 px-2 text-[16px] text-[#0c0c0e] focus-visible:outline focus-visible:outline-2 focus-visible:outline-lime-500 sm:text-[11px]"
            />
          </label>
          <label className="text-[10px] text-[#71717a]">
            End (seconds)
            <input
              type="number"
              min={(scene.start_frame + 1) / MOTION_FPS}
              max={durationS > 0 ? durationS : undefined}
              step={1 / MOTION_FPS}
              value={(scene.end_frame_exclusive / MOTION_FPS).toFixed(2)}
              onChange={(event) => {
                const requested = Math.round(Number(event.target.value) * MOTION_FPS);
                const durationFrames = durationS > 0
                  ? Math.max(1, Math.round(durationS * MOTION_FPS))
                  : requested;
                const next = Math.max(
                  scene.start_frame + 1,
                  Math.min(durationFrames, requested),
                );
                onPatch(scene.id, { end_frame_exclusive: next });
              }}
              className="mt-1 h-11 w-full rounded-lg border border-zinc-200 px-2 text-[16px] text-[#0c0c0e] focus-visible:outline focus-visible:outline-2 focus-visible:outline-lime-500 sm:text-[11px]"
            />
          </label>
        </div>
      </fieldset>

      <fieldset className="mt-5 border-t border-zinc-200 pt-4">
        <legend className="text-[12px] font-semibold text-[#3f3f46]">Colors</legend>
        <div className="mt-3 grid grid-cols-2 gap-3">
          {(["primary", "accent"] as const).map((slot) => (
            <label
              key={slot}
              className="flex min-h-11 items-center justify-between rounded-lg border border-zinc-200 px-3 text-[11px] capitalize text-[#52525b]"
            >
              {slot}
              <input
                aria-label={slot}
                type="color"
                value={scene.palette[slot]}
                onChange={(event) => onPatch(scene.id, {
                  palette: { ...scene.palette, [slot]: event.target.value },
                })}
                className="h-8 w-8 cursor-pointer rounded border-0 bg-transparent focus-visible:outline focus-visible:outline-2 focus-visible:outline-lime-500"
              />
            </label>
          ))}
        </div>
      </fieldset>

      <button
        type="button"
        onClick={() => onRemove(scene.id)}
        className="mt-6 min-h-11 w-full rounded-lg border border-red-200 px-3 text-[12px] font-semibold text-red-600 hover:bg-red-50 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500"
      >
        Remove block
      </button>
    </div>
  );
}

function CreatorBlockFields({
  scene,
  assets,
  onPatch,
}: {
  scene: Exclude<MotionPresetInstanceV1, { preset_id: "route_trace" }>;
  assets: PoolAsset[];
  onPatch: (id: string, patch: MotionPresetPatch) => void;
}) {
  const parameters = creatorBlockEntry(scene.preset_id).parameters;
  const parameter = (key: string) => {
    const value = parameters.find((candidate) => candidate.key === key);
    if (!value) throw new Error(`Missing Creator Block parameter metadata: ${key}`);
    return value;
  };
  const patchParams = (patch: Record<string, unknown>) =>
    onPatch(scene.id, { params: { ...scene.params, ...patch } as MotionPresetPatch["params"] });
  const textField = (label: string, key: string, value: string, maxLength: number) => (
    <label className="mt-3 block text-[11px] text-[#71717a]">
      {label}
      <input
        value={value}
        maxLength={maxLength}
        onChange={(event) => patchParams({ [key]: event.target.value })}
        className="mt-1 h-11 w-full rounded-lg border border-zinc-200 px-3 text-[16px] text-[#0c0c0e] focus-visible:outline focus-visible:outline-2 focus-visible:outline-lime-500 sm:text-[12px]"
      />
    </label>
  );
  if (scene.preset_id === "kinetic_word") {
    return textField("Text", "text", scene.params.text, parameter("text").max_length!);
  }
  if (scene.preset_id === "flow_field") return (
    <>
      {textField("Headline", "headline", scene.params.headline, parameter("headline").max_length!)}
      {textField("Kicker", "kicker", scene.params.kicker ?? "", parameter("kicker").max_length!)}
    </>
  );
  if (scene.preset_id === "offer_swap") return (
    <>
      {textField("First phrase", "primary_text", scene.params.primary_text, parameter("primary_text").max_length!)}
      {textField("Second phrase", "alternate_text", scene.params.alternate_text, parameter("alternate_text").max_length!)}
    </>
  );
  if (scene.preset_id === "donut_text") return (
    <>
      {textField("Left arc", "left_text", scene.params.left_text, parameter("left_text").max_length!)}
      {textField("Right arc", "right_text", scene.params.right_text, parameter("right_text").max_length!)}
    </>
  );
  if (scene.preset_id === "tag_stack" || scene.preset_id === "cloud_break") {
    const key = scene.preset_id === "tag_stack" ? "labels" : "lines";
    const values = scene.preset_id === "tag_stack" ? scene.params.labels : scene.params.lines;
    const maxItems = parameter(key).max_items!;
    return (
      <label className="mt-3 block text-[11px] text-[#71717a]">
        One line per item
        <textarea
          value={values.join("\n")}
          onChange={(event) => patchParams({ [key]: event.target.value.split("\n").slice(0, maxItems) })}
          rows={Math.min(5, values.length + 1)}
          className="mt-1 min-h-24 w-full resize-none rounded-lg border border-zinc-200 px-3 py-2 text-[16px] leading-relaxed text-[#0c0c0e] focus-visible:outline focus-visible:outline-2 focus-visible:outline-lime-500 sm:text-[12px]"
        />
      </label>
    );
  }
  const ready = assets.filter(isBoundedCreatorImageAsset);
  const selected = scene.params.assets;
  const max = parameter("assets").max_items!;
  const min = parameter("assets").min_items!;
  return (
    <div className="mt-3">
      <p className="text-[11px] text-[#71717a]">Images ({selected.length}/{max})</p>
      <div className="mt-2 grid grid-cols-4 gap-2">
        {ready.map((asset) => {
          const active = selected.some((item) => item.asset_id === asset.id);
          return (
            <button
              key={asset.id}
              type="button"
              aria-pressed={active}
              aria-label={`${active ? "Remove" : "Add"} ${asset.source_filename ?? asset.subject ?? "image"}`}
              title={active && selected.length <= min ? `Keep at least ${min} images` : undefined}
              disabled={active && selected.length <= min}
              onClick={() => patchParams({
                assets: active
                  ? selected.length > min
                    ? selected.filter((item) => item.asset_id !== asset.id)
                    : selected
                  : selected.length < max
                    ? [...selected, { asset_id: asset.id, gcs_path: asset.gcs_path }]
                    : selected,
              })}
              className={`min-h-11 min-w-11 aspect-square overflow-hidden rounded-lg border-2 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500 ${active ? "border-lime-500" : "border-zinc-200"}`}
            >
              {asset.display_url ? (
                // eslint-disable-next-line @next/next/no-img-element -- signed asset-pool thumbnail
                <img src={asset.display_url} alt="" className="h-full w-full object-cover" />
              ) : null}
            </button>
          );
        })}
      </div>
      {selected.length > 1 && (
        <ol className="mt-3 space-y-1">
          {selected.map((item, index) => (
            <li key={item.asset_id} className="flex min-h-11 items-center justify-between rounded-lg bg-zinc-50 px-3 text-[11px] text-[#52525b]">
              <span>Image {index + 1}</span>
              <button
                type="button"
                aria-label={`Move image ${index + 1} up`}
                disabled={index === 0}
                onClick={() => {
                  const next = [...selected];
                  [next[index - 1], next[index]] = [next[index], next[index - 1]];
                  patchParams({ assets: next });
                }}
                className="min-h-9 px-2 font-semibold focus-visible:outline focus-visible:outline-2 focus-visible:outline-lime-500 disabled:opacity-30"
              >
                Move up
              </button>
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}
