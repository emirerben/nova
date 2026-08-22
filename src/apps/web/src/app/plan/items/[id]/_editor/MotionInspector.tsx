"use client";

import { useEffect, useRef, useState } from "react";
import {
  CREATOR_BLOCK_CATALOG,
  MOTION_FPS,
  creatorBlockControl,
  creatorBlockEntry,
  type CreatorBlockMotionConfigV2,
  type MotionPresetInstance,
  type MotionPresetPatch,
} from "@nova/motion-runtime";
import { isBoundedCreatorImageAsset, type PoolAsset } from "@/lib/plan-api";

export interface CreatorBlockMotionControlPatch {
  motion?: Partial<CreatorBlockMotionConfigV2>;
  intensity?: number;
  params?: Record<string, unknown>;
}

function MotionRange({
  label,
  value,
  min,
  max,
  step,
  suffix,
  onBegin,
  onPreview,
  onCommit,
  onCancel,
}: {
  label: string;
  value: number;
  min: number;
  max: number;
  step: number;
  suffix: string;
  onBegin: () => void;
  onPreview: (value: number) => void;
  onCommit: (value: number) => void;
  onCancel: () => void;
}) {
  const [draft, setDraft] = useState(value);
  const activeRef = useRef(false);
  const initialRef = useRef(value);
  useEffect(() => setDraft(value), [value]);
  const begin = () => {
    if (activeRef.current) return;
    activeRef.current = true;
    initialRef.current = value;
    onBegin();
  };
  const commit = () => {
    if (!activeRef.current) return;
    activeRef.current = false;
    if (Math.abs(draft - initialRef.current) > 1e-9) onCommit(draft);
    else onCancel();
  };
  const cancel = () => {
    if (!activeRef.current) return;
    activeRef.current = false;
    setDraft(initialRef.current);
    onCancel();
  };
  return (
    <label className="block text-[11px] text-[#71717a]">
      <span className="flex items-center justify-between">
        <span>{label}</span>
        <span className="tabular-nums">{draft}{suffix}</span>
      </span>
      <input
        aria-label={label}
        type="range"
        min={min}
        max={max}
        step={step}
        value={draft}
        onPointerDown={begin}
        onKeyDown={begin}
        onChange={(event) => {
          begin();
          const next = Number(event.target.value);
          setDraft(next);
          onPreview(next);
        }}
        onPointerUp={commit}
        onPointerCancel={cancel}
        onKeyUp={commit}
        onBlur={commit}
        className="mt-2 h-11 w-full accent-lime-500 sm:h-auto"
      />
    </label>
  );
}

export default function MotionInspector({
  scene,
  durationS,
  assets,
  evolvingTypeEnabled,
  editable = true,
  disabledReason = null,
  showClose = true,
  onPatch,
  onPatchMotionControl,
  onBeginMotionControl,
  onPreviewMotionControl,
  onCommitMotionControl,
  onCancelMotionControl,
  onRemove,
  onClose,
}: {
  scene: MotionPresetInstance;
  durationS: number;
  assets: PoolAsset[];
  evolvingTypeEnabled: boolean;
  editable?: boolean;
  disabledReason?: string | null;
  showClose?: boolean;
  onPatch: (id: string, patch: MotionPresetPatch) => void;
  onPatchMotionControl: (id: string, patch: CreatorBlockMotionControlPatch) => void;
  onBeginMotionControl: () => void;
  onPreviewMotionControl: (id: string, patch: CreatorBlockMotionControlPatch) => void;
  onCommitMotionControl: (id: string, patch: CreatorBlockMotionControlPatch) => void;
  onCancelMotionControl: () => void;
  onRemove: (id: string) => void;
  onClose: () => void;
}) {
  const controlsEditable = editable && (scene.preset_id !== "evolving_type" || evolvingTypeEnabled);
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

      {!editable && disabledReason && (
        <p className="mt-5 rounded-lg border border-zinc-200 bg-zinc-50 px-3 py-2 text-[11px] leading-relaxed text-[#52525b]" role="status">
          {disabledReason}
        </p>
      )}

      {scene.preset_id === "evolving_type" && !evolvingTypeEnabled && (
        <p className="mt-5 rounded-lg border border-zinc-200 bg-zinc-50 px-3 py-2 text-[11px] leading-relaxed text-[#52525b]">
          This saved Evolving Type block is preserved. Enable Evolving Type to edit its controls.
        </p>
      )}

      <fieldset disabled={!editable} className="contents disabled:opacity-60">
      {scene.preset_id !== "route_trace" && controlsEditable && (
        <fieldset className="mt-5">
          <legend className="text-[12px] font-semibold text-[#3f3f46]">Content</legend>
          <CreatorBlockFields scene={scene} assets={assets} onPatch={onPatch} />
        </fieldset>
      )}
      {scene.preset_id === "route_trace" && (
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
              className="mt-2 h-11 w-full accent-lime-500 sm:h-auto"
            />
          </label>
        </fieldset>
      )}

      {scene.preset_id !== "route_trace" && controlsEditable && (
        <CreatorBlockMotionFields
          scene={scene}
          onPatch={onPatchMotionControl}
          onBegin={onBeginMotionControl}
          onPreview={onPreviewMotionControl}
          onCommit={onCommitMotionControl}
          onCancel={onCancelMotionControl}
        />
      )}

      {controlsEditable && <fieldset className="mt-5 border-t border-zinc-200 pt-4">
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
      </fieldset>}

      {controlsEditable && <fieldset className="mt-5 border-t border-zinc-200 pt-4">
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
      </fieldset>}

      <button
        type="button"
        disabled={!editable}
        onClick={() => onRemove(scene.id)}
        className="mt-6 min-h-11 w-full rounded-lg border border-red-200 px-3 text-[12px] font-semibold text-red-600 hover:bg-red-50 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500 disabled:cursor-not-allowed disabled:opacity-45"
      >
        Remove block
      </button>
      </fieldset>
    </div>
  );
}

function CreatorBlockFields({
  scene,
  assets,
  onPatch,
}: {
  scene: Exclude<MotionPresetInstance, { preset_id: "route_trace" }>;
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
  if (scene.preset_id === "evolving_type") return (
    <>
      {textField("Headline", "headline", scene.params.headline, parameter("headline").max_length!)}
      {textField("Subtitle", "subtitle", scene.params.subtitle, parameter("subtitle").max_length!)}
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

function controlLabel(key: string): string {
  const labels: Record<string, string> = {
    hold_frames: "Hold",
    icon_count: "Icon count",
    icon_style: "Icon style",
    text_stagger_ms: "Text stagger",
    icon_stagger_ms: "Icon stagger",
    morph_amplitude: "Morph amplitude",
    typography_scale: "Typography scale",
    backdrop_opacity: "Backdrop opacity",
    split_icons: "Split icons",
  };
  return labels[key] ?? key.replace(/_/g, " ").replace(/^./, (char) => char.toUpperCase());
}

function CreatorBlockMotionFields({
  scene,
  onPatch,
  onBegin,
  onPreview,
  onCommit,
  onCancel,
}: {
  scene: Exclude<MotionPresetInstance, { preset_id: "route_trace" }>;
  onPatch: (id: string, patch: CreatorBlockMotionControlPatch) => void;
  onBegin: () => void;
  onPreview: (id: string, patch: CreatorBlockMotionControlPatch) => void;
  onCommit: (id: string, patch: CreatorBlockMotionControlPatch) => void;
  onCancel: () => void;
}) {
  const entry = creatorBlockEntry(scene.preset_id);
  const defaults = entry.motion_defaults;
  const motion = scene.preset_version === 2 ? scene.motion : defaults;
  const speed = creatorBlockControl(entry, "speed")!;
  const intensity = creatorBlockControl(entry, "intensity")!;
  const hold = creatorBlockControl(entry, "hold_frames")!;
  const easing = creatorBlockControl(entry, "easing")!;
  const advancedParams = entry.parameters.filter((parameter) =>
    parameter.type === "number" || parameter.type === "enum" || parameter.type === "boolean",
  );
  return (
    <fieldset className="mt-5 border-t border-zinc-200 pt-4">
      <legend className="text-[12px] font-semibold text-[#3f3f46]">Motion</legend>
      <div className="mt-3 space-y-3">
        <MotionRange
          label="Speed"
          value={motion.speed}
          min={speed.minimum!}
          max={speed.maximum!}
          step={speed.step!}
          suffix="×"
          onBegin={onBegin}
          onPreview={(value) => onPreview(scene.id, { motion: { speed: value } })}
          onCommit={(value) => onCommit(scene.id, { motion: { speed: value } })}
          onCancel={onCancel}
        />
        <MotionRange
          label="Intensity"
          value={Math.round(scene.intensity * 100)}
          min={Math.round(intensity.minimum! * 100)}
          max={Math.round(intensity.maximum! * 100)}
          step={Math.max(1, Math.round(intensity.step! * 100))}
          suffix="%"
          onBegin={onBegin}
          onPreview={(value) => onPreview(scene.id, { intensity: value / 100 })}
          onCommit={(value) => onCommit(scene.id, { intensity: value / 100 })}
          onCancel={onCancel}
        />
      </div>
      <details className="mt-4 rounded-lg border border-zinc-200 bg-zinc-50/60 px-3 py-2">
        <summary className="flex min-h-11 cursor-pointer select-none items-center text-[12px] font-semibold text-[#3f3f46] sm:min-h-0">
          Advanced motion
        </summary>
        <div className="mt-3 space-y-3">
          <label className="block text-[11px] text-[#71717a]">
            Easing
            <select
              aria-label="Motion easing"
              value={motion.easing}
              onChange={(event) => onPatch(scene.id, {
                motion: { easing: event.target.value as CreatorBlockMotionConfigV2["easing"] },
              })}
              className="mt-1 min-h-11 w-full rounded-lg border border-zinc-200 bg-white px-2 text-[13px]"
            >
              {(easing.values ?? []).map((value) => (
                <option key={value} value={value}>{controlLabel(value)}</option>
              ))}
            </select>
          </label>
          <MotionRange
            label="Hold"
            value={motion.hold_frames}
            min={hold.minimum!}
            max={hold.maximum!}
            step={hold.step!}
            suffix="f"
            onBegin={onBegin}
            onPreview={(value) => onPreview(scene.id, { motion: { hold_frames: value } })}
            onCommit={(value) => onCommit(scene.id, { motion: { hold_frames: value } })}
            onCancel={onCancel}
          />
          {advancedParams.map((parameter) => {
            const value = (scene.params as unknown as Record<string, unknown>)[parameter.key];
            if (parameter.type === "number" && typeof value === "number") {
              const percent = parameter.maximum === 1 && parameter.minimum === 0;
              return (
                <MotionRange
                  key={parameter.key}
                  label={controlLabel(parameter.key)}
                  value={percent ? Math.round(value * 100) : value}
                  min={percent ? 0 : parameter.minimum!}
                  max={percent ? 100 : parameter.maximum!}
                  step={percent ? Math.max(1, Math.round(parameter.step! * 100)) : parameter.step!}
                  suffix={percent ? "%" : parameter.key.endsWith("_ms") ? "ms" : ""}
                  onBegin={onBegin}
                  onPreview={(next) => onPreview(scene.id, {
                    params: { [parameter.key]: percent ? next / 100 : next },
                  })}
                  onCommit={(next) => onCommit(scene.id, {
                    params: { [parameter.key]: percent ? next / 100 : next },
                  })}
                  onCancel={onCancel}
                />
              );
            }
            if (parameter.type === "enum" && typeof value === "string") {
              return (
                <label key={parameter.key} className="block text-[11px] text-[#71717a]">
                  {controlLabel(parameter.key)}
                  <select
                    aria-label={controlLabel(parameter.key)}
                    value={value}
                    onChange={(event) => onPatch(scene.id, {
                      params: { [parameter.key]: event.target.value },
                    })}
                    className="mt-1 min-h-11 w-full rounded-lg border border-zinc-200 bg-white px-2 text-[13px]"
                  >
                    {(parameter.values ?? []).map((option) => (
                      <option key={option} value={option}>{controlLabel(option)}</option>
                    ))}
                  </select>
                </label>
              );
            }
            if (parameter.type === "boolean" && typeof value === "boolean") {
              return (
                <label key={parameter.key} className="flex min-h-11 items-center justify-between text-[11px] text-[#71717a]">
                  {controlLabel(parameter.key)}
                  <input
                    aria-label={controlLabel(parameter.key)}
                    type="checkbox"
                    checked={value}
                    onChange={(event) => onPatch(scene.id, {
                      params: { [parameter.key]: event.target.checked },
                    })}
                    className="h-5 w-5 accent-lime-500"
                  />
                </label>
              );
            }
            return null;
          })}
        </div>
      </details>
    </fieldset>
  );
}
