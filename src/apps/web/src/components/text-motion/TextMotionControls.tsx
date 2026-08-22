"use client";

import { useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  normalizeTextMotion,
  TEXT_MOTION_BLUR_MAX_PX,
  TEXT_MOTION_CURSOR_BLINK_MAX_MS,
  TEXT_MOTION_CURSOR_BLINK_MIN_MS,
  TEXT_MOTION_HOLD_CONTROL_MAX_S,
  TEXT_MOTION_REVEAL_RAMP_MAX_MS,
  TEXT_MOTION_REVEAL_RAMP_MIN_MS,
  TEXT_MOTION_SPEED_MAX,
  TEXT_MOTION_SPEED_MIN,
  TEXT_MOTION_STAGGER_MAX_MS,
  TEXT_MOTION_TRAVEL_MAX_PX,
  textMotionCapabilities,
  type TextMotionConfigV2,
} from "@/lib/text-motion-v2";

function RangeCommit({
  label,
  value,
  min,
  max,
  step,
  suffix,
  onCommit,
  onPreview,
  onBegin,
}: {
  label: string;
  value: number;
  min: number;
  max: number;
  step: number;
  suffix: string;
  onCommit: (value: number) => void;
  onPreview?: (value: number) => void;
  onBegin?: () => void;
}) {
  const [draft, setDraft] = useState(value);
  const activeRef = useRef(false);
  const initialRef = useRef(value);
  useEffect(() => setDraft(value), [value]);
  const begin = () => {
    if (activeRef.current) return;
    activeRef.current = true;
    initialRef.current = value;
    onBegin?.();
  };
  const commit = () => {
    if (!activeRef.current) return;
    activeRef.current = false;
    if (Math.abs(draft - initialRef.current) > 1e-9) onCommit(draft);
  };
  return (
    <label className="block text-[12px] font-semibold text-[#3f3f46]">
      <span className="flex items-center justify-between">
        <span>{label}</span>
        <span className="font-normal tabular-nums text-[#71717a]">{draft}{suffix}</span>
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
          onPreview?.(next);
        }}
        onPointerUp={commit}
        onKeyUp={commit}
        onBlur={commit}
        className="mt-1 h-11 w-full accent-[#0c0c0e] sm:h-auto"
      />
    </label>
  );
}

export default function TextMotionControls({
  effect,
  motion,
  onChange,
  onPreview,
  onBegin,
  compact = false,
  onResetLegacy,
}: {
  effect: string;
  motion: TextMotionConfigV2;
  onChange: (patch: Partial<TextMotionConfigV2>) => void;
  onPreview?: (patch: Partial<TextMotionConfigV2>) => void;
  onBegin?: () => void;
  compact?: boolean;
  onResetLegacy?: () => void;
}) {
  const value = normalizeTextMotion(effect, motion);
  const capabilities = textMotionCapabilities(effect);
  const hasAdvanced = Object.values(capabilities).some(Boolean);
  const commitDiscrete = (patch: Partial<TextMotionConfigV2>) => {
    onBegin?.();
    onChange(patch);
  };

  return (
    <div className={compact ? "mt-3 space-y-3" : "mt-4 space-y-3 border-t border-zinc-100 pt-4"}>
      <RangeCommit
        label="Speed"
        value={value.speed}
        min={TEXT_MOTION_SPEED_MIN}
        max={TEXT_MOTION_SPEED_MAX}
        step={0.25}
        suffix="×"
        onCommit={(speed) => onChange({ speed })}
        onPreview={(speed) => onPreview?.({ speed })}
        onBegin={onBegin}
      />
      <RangeCommit
        label="Intensity"
        value={Math.round(value.intensity * 100)}
        min={0}
        max={100}
        step={1}
        suffix="%"
        onCommit={(intensity) => onChange({ intensity: intensity / 100 })}
        onPreview={(intensity) => onPreview?.({ intensity: intensity / 100 })}
        onBegin={onBegin}
      />

      {hasAdvanced && (
        <details className="rounded-lg border border-zinc-200 bg-zinc-50/60 px-3 py-2">
          <summary className="flex min-h-11 cursor-pointer select-none items-center text-base font-semibold text-[#3f3f46] sm:min-h-0 sm:text-[12px]">
            Advanced motion
          </summary>
          <div className="mt-3 space-y-3">
            {capabilities.easing && (
              <label className="block text-[12px] font-semibold text-[#3f3f46]">
                Easing
                <Select
                  value={value.easing}
                  onValueChange={(next) => commitDiscrete({ easing: next as TextMotionConfigV2["easing"] })}
                >
                  <SelectTrigger
                    aria-label="Motion easing"
                    className="mt-1 min-h-11 rounded-lg border-zinc-200 px-2 text-base font-normal sm:h-9 sm:min-h-0 sm:text-[13px]"
                  >
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="ease-out-cubic">Ease out</SelectItem>
                    <SelectItem value="ease-in-out-cubic">Ease in & out</SelectItem>
                    <SelectItem value="linear">Linear</SelectItem>
                  </SelectContent>
                </Select>
              </label>
            )}
            {capabilities.stagger && (
              <RangeCommit label="Stagger" value={value.stagger_ms} min={0} max={TEXT_MOTION_STAGGER_MAX_MS} step={5} suffix="ms" onCommit={(stagger_ms) => onChange({ stagger_ms })} onPreview={(stagger_ms) => onPreview?.({ stagger_ms })} onBegin={onBegin} />
            )}
            {capabilities.revealRamp && (
              <RangeCommit label="Reveal ramp" value={value.reveal_ramp_ms} min={TEXT_MOTION_REVEAL_RAMP_MIN_MS} max={TEXT_MOTION_REVEAL_RAMP_MAX_MS} step={10} suffix="ms" onCommit={(reveal_ramp_ms) => onChange({ reveal_ramp_ms })} onPreview={(reveal_ramp_ms) => onPreview?.({ reveal_ramp_ms })} onBegin={onBegin} />
            )}
            {capabilities.order && (
              <label className="block text-[12px] font-semibold text-[#3f3f46]">
                Order
                <Select value={value.order} onValueChange={(next) => commitDiscrete({ order: next as TextMotionConfigV2["order"] })}>
                  <SelectTrigger
                    aria-label="Reveal order"
                    className="mt-1 min-h-11 rounded-lg border-zinc-200 px-2 text-base font-normal sm:h-9 sm:min-h-0 sm:text-[13px]"
                  >
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="forward">Forward</SelectItem>
                    <SelectItem value="reverse">Reverse</SelectItem>
                    <SelectItem value="center-out">Center out</SelectItem>
                  </SelectContent>
                </Select>
              </label>
            )}
            {capabilities.direction && (
              <label className="block text-[12px] font-semibold text-[#3f3f46]">
                Direction
                <Select value={value.direction} onValueChange={(next) => commitDiscrete({ direction: next as TextMotionConfigV2["direction"] })}>
                  <SelectTrigger
                    aria-label="Entrance direction"
                    className="mt-1 min-h-11 rounded-lg border-zinc-200 px-2 text-base font-normal sm:h-9 sm:min-h-0 sm:text-[13px]"
                  >
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="none">None</SelectItem>
                    <SelectItem value="up">Up</SelectItem>
                    <SelectItem value="down">Down</SelectItem>
                    <SelectItem value="left">Left</SelectItem>
                    <SelectItem value="right">Right</SelectItem>
                  </SelectContent>
                </Select>
              </label>
            )}
            {capabilities.travel && (
              <RangeCommit label="Travel" value={value.travel_px} min={0} max={TEXT_MOTION_TRAVEL_MAX_PX} step={2} suffix="px" onCommit={(travel_px) => onChange({ travel_px })} onPreview={(travel_px) => onPreview?.({ travel_px })} onBegin={onBegin} />
            )}
            {capabilities.overshoot && (
              <RangeCommit label="Overshoot" value={Math.round(value.overshoot * 100)} min={0} max={100} step={1} suffix="%" onCommit={(overshoot) => onChange({ overshoot: overshoot / 100 })} onPreview={(overshoot) => onPreview?.({ overshoot: overshoot / 100 })} onBegin={onBegin} />
            )}
            {capabilities.blur && (
              <RangeCommit label="Blur" value={value.blur_px} min={0} max={TEXT_MOTION_BLUR_MAX_PX} step={0.5} suffix="px" onCommit={(blur_px) => onChange({ blur_px })} onPreview={(blur_px) => onPreview?.({ blur_px })} onBegin={onBegin} />
            )}
            {capabilities.cursor && (
              <>
                <label className="block text-[12px] font-semibold text-[#3f3f46]">
                  Cursor
                  <Select value={value.cursor_style} onValueChange={(next) => commitDiscrete({ cursor_style: next as TextMotionConfigV2["cursor_style"] })}>
                    <SelectTrigger
                      aria-label="Cursor style"
                      className="mt-1 min-h-11 rounded-lg border-zinc-200 px-2 text-base font-normal sm:h-9 sm:min-h-0 sm:text-[13px]"
                    >
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="none">None</SelectItem>
                      <SelectItem value="bar">Bar</SelectItem>
                      <SelectItem value="block">Block</SelectItem>
                      <SelectItem value="underscore">Underscore</SelectItem>
                    </SelectContent>
                  </Select>
                </label>
                {value.cursor_style !== "none" && (
                  <RangeCommit label="Blink" value={value.cursor_blink_ms} min={TEXT_MOTION_CURSOR_BLINK_MIN_MS} max={TEXT_MOTION_CURSOR_BLINK_MAX_MS} step={50} suffix="ms" onCommit={(cursor_blink_ms) => onChange({ cursor_blink_ms })} onPreview={(cursor_blink_ms) => onPreview?.({ cursor_blink_ms })} onBegin={onBegin} />
                )}
              </>
            )}
            {capabilities.hold && (
              <RangeCommit label="Hold" value={value.hold_s} min={0} max={TEXT_MOTION_HOLD_CONTROL_MAX_S} step={0.1} suffix="s" onCommit={(hold_s) => onChange({ hold_s })} onPreview={(hold_s) => onPreview?.({ hold_s })} onBegin={onBegin} />
            )}
          </div>
        </details>
      )}
      {onResetLegacy && (
        <Button
          type="button"
          variant="link"
          onClick={onResetLegacy}
          className="h-auto min-h-11 items-center p-0 text-base font-semibold text-[#71717a] underline underline-offset-2 hover:text-[#0c0c0e] sm:min-h-0 sm:text-[11px]"
        >
          Use legacy timing
        </Button>
      )}
    </div>
  );
}
