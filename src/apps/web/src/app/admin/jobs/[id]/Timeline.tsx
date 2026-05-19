"use client";

/**
 * Timeline tab for the admin job-debug view.
 *
 * Renders one ASCII-ish stacked timeline plus a per-slot table answering
 * "exactly what is being told to the renderer: cut here, transition there,
 * text Y between seconds Z1 and Z2". Data sources:
 *
 *   Job.assembly_plan.steps[]        — slot cuts (clip + moment per slot)
 *   Job.pipeline_trace[]
 *     stage="overlay" event="render_window"  — exact text-overlay windows
 *                                              after merge/clamp/override
 *     stage="transition"                     — xfade picks per slot
 *     stage="interstitial"                   — curtain-close, fade, holds
 *
 * Overlay windows come from pipeline_trace events emitted by
 * _collect_absolute_overlays. When those events are absent (legacy job
 * rendered before the instrumentation landed), we fall back to deriving
 * overlay positions from the per-slot text_overlays carried on
 * assembly_plan.steps[].slot, and surface a yellow "approximate" chip so
 * the operator knows the timings are recipe-relative, not render-time.
 */

import { JsonTreeView } from "@/components/JsonTreeView";
import type { JobDebugResponse, PipelineTraceEvent } from "@/lib/admin-jobs-api";

interface AssemblyStep {
  slot: AssemblySlot;
  clip_id?: string;
  clip_gcs_path?: string;
  moment?: { start_s?: number; end_s?: number; energy?: number; description?: string };
}

interface AssemblySlot {
  position?: number;
  target_duration_s?: number;
  text_overlays?: Array<{
    text?: string;
    sample_text?: string;
    start_offset_s?: number;
    duration_s?: number;
    position?: string;
    font_style?: string;
    text_size?: string;
    text_color?: string;
    effect?: string;
  }>;
  transition_in?: string;
}

interface DerivedSlot {
  position: number;
  abs_start_s: number;
  abs_end_s: number;
  clip_id?: string;
  moment?: { start_s?: number; end_s?: number; description?: string };
  transition_in_recipe?: string;
}

interface DerivedOverlay {
  text: string;
  abs_start_s: number;
  abs_end_s: number;
  slot_index: number | null;
  position?: string;
  effect?: string;
  text_size?: string;
  text_color?: string;
  font_cycle_accel_at_s?: number | null;
  clamped_by?: string | null;
  merged_from_slots?: number[] | null;
  approximate: boolean;
}

interface DerivedTransition {
  slot_index: number | null;
  abs_time_s: number;
  type?: string;
  duration_s?: number;
  raw: PipelineTraceEvent;
}

interface DerivedCurtain {
  slot_index: number | null;
  abs_time_s: number;
  raw: PipelineTraceEvent;
}

const _STAGE_TIMELINE_HEIGHT = 32; // px per row

function _formatTime(s: number): string {
  if (!Number.isFinite(s)) return "—";
  const m = Math.floor(s / 60);
  const r = s - m * 60;
  return m > 0 ? `${m}:${r.toFixed(2).padStart(5, "0")}` : `${r.toFixed(2)}s`;
}

function _deriveSlots(steps: AssemblyStep[]): {
  slots: DerivedSlot[];
  totalDuration: number;
} {
  let cursor = 0;
  const slots: DerivedSlot[] = steps.map((step, idx) => {
    const slot = step.slot ?? {};
    const duration = Number(slot.target_duration_s ?? 0) || 0;
    const position =
      typeof slot.position === "number" ? slot.position : idx + 1;
    const start = cursor;
    const end = cursor + duration;
    cursor = end;
    return {
      position,
      abs_start_s: start,
      abs_end_s: end,
      clip_id: step.clip_id,
      moment: step.moment,
      transition_in_recipe: slot.transition_in,
    };
  });
  return { slots, totalDuration: cursor };
}

function _deriveOverlaysFromEvents(
  events: PipelineTraceEvent[],
): DerivedOverlay[] {
  return events
    .filter((e) => e.stage === "overlay" && e.event === "render_window")
    .map((e) => {
      const d = e.data ?? {};
      return {
        text: (d.text as string) ?? "",
        abs_start_s: Number(d.abs_start_s ?? 0),
        abs_end_s: Number(d.abs_end_s ?? 0),
        slot_index: (d.slot_index as number | null) ?? null,
        position: d.position as string | undefined,
        effect: d.effect as string | undefined,
        text_size: d.text_size as string | undefined,
        text_color: d.text_color as string | undefined,
        font_cycle_accel_at_s:
          d.font_cycle_accel_at_s != null
            ? Number(d.font_cycle_accel_at_s)
            : null,
        clamped_by: (d.clamped_by as string | null | undefined) ?? null,
        merged_from_slots:
          (d.merged_from_slots as number[] | null | undefined) ?? null,
        approximate: false,
      };
    });
}

function _deriveOverlaysFromRecipe(
  steps: AssemblyStep[],
  slots: DerivedSlot[],
): DerivedOverlay[] {
  const out: DerivedOverlay[] = [];
  steps.forEach((step, idx) => {
    const slot = step.slot ?? {};
    const slotStart = slots[idx]?.abs_start_s ?? 0;
    (slot.text_overlays ?? []).forEach((ov) => {
      const text = (ov.text ?? ov.sample_text ?? "").trim();
      if (!text) return;
      const offset = Number(ov.start_offset_s ?? 0);
      const duration = Number(ov.duration_s ?? 0);
      const startAbs = slotStart + offset;
      const endAbs = startAbs + Math.max(duration, 0.01);
      out.push({
        text,
        abs_start_s: startAbs,
        abs_end_s: endAbs,
        slot_index: slots[idx]?.position ?? idx + 1,
        position: ov.position,
        effect: ov.effect,
        text_size: ov.text_size,
        text_color: ov.text_color,
        font_cycle_accel_at_s: null,
        clamped_by: null,
        merged_from_slots: null,
        approximate: true,
      });
    });
  });
  return out;
}

function _deriveTransitions(
  events: PipelineTraceEvent[],
  slots: DerivedSlot[],
): DerivedTransition[] {
  return events
    .filter((e) => e.stage === "transition")
    .map((e) => {
      const d = e.data ?? {};
      const slotIdx =
        typeof d.slot_index === "number"
          ? d.slot_index
          : typeof d.position === "number"
            ? d.position
            : null;
      const slot = slots.find((s) => s.position === slotIdx);
      return {
        slot_index: slotIdx,
        abs_time_s: slot?.abs_start_s ?? 0,
        type: d.type as string | undefined,
        duration_s: d.duration_s != null ? Number(d.duration_s) : undefined,
        raw: e,
      };
    });
}

function _deriveCurtains(
  events: PipelineTraceEvent[],
  slots: DerivedSlot[],
): DerivedCurtain[] {
  return events
    .filter((e) => e.stage === "interstitial")
    .map((e) => {
      const d = e.data ?? {};
      const slotIdx =
        typeof d.after_slot === "number"
          ? d.after_slot
          : typeof d.slot_index === "number"
            ? d.slot_index
            : null;
      const slot = slots.find((s) => s.position === slotIdx);
      return {
        slot_index: slotIdx,
        abs_time_s: slot?.abs_end_s ?? 0,
        raw: e,
      };
    });
}

interface TimelineBundle {
  slots: DerivedSlot[];
  overlays: DerivedOverlay[];
  transitions: DerivedTransition[];
  curtains: DerivedCurtain[];
  totalDuration: number;
  overlaysApproximate: boolean;
  truncated: boolean;
}

export function deriveTimeline(
  data: JobDebugResponse,
): TimelineBundle | null {
  const plan = data.job.assembly_plan as { steps?: AssemblyStep[] } | null;
  const steps = plan?.steps ?? [];
  if (steps.length === 0) return null;

  const { slots, totalDuration } = _deriveSlots(steps);
  const events = (data.job.pipeline_trace ?? []) as PipelineTraceEvent[];

  const overlayEvents = events.filter(
    (e) => e.stage === "overlay" && e.event === "render_window",
  );
  const overlaysApproximate = overlayEvents.length === 0;
  const overlays = overlaysApproximate
    ? _deriveOverlaysFromRecipe(steps, slots)
    : _deriveOverlaysFromEvents(events);

  const transitions = _deriveTransitions(events, slots);
  const curtains = _deriveCurtains(events, slots);

  // The orchestrator caps pipeline_trace at 500 events; bigger renders may
  // silently drop overlay events. Flag this so the operator knows some
  // overlays might be missing from the timeline.
  const truncated = events.length >= 500;

  return {
    slots,
    overlays,
    transitions,
    curtains,
    totalDuration,
    overlaysApproximate,
    truncated,
  };
}

// ── Renderer ──────────────────────────────────────────────────────────────────

export function Timeline({ data }: { data: JobDebugResponse }): JSX.Element {
  const bundle = deriveTimeline(data);

  if (!bundle) {
    return (
      <div className="rounded border border-zinc-800 px-4 py-8 text-center text-sm text-zinc-500">
        Assembly plan missing — job failed before matching, or this job
        type does not produce an assembly_plan.
      </div>
    );
  }

  const { slots, overlays, transitions, curtains, totalDuration } = bundle;
  const pct = (t: number) =>
    totalDuration > 0 ? `${(t / totalDuration) * 100}%` : "0%";

  return (
    <div className="space-y-6">
      <header className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <h2 className="text-sm font-semibold uppercase tracking-wider text-zinc-300">
          Assembly timeline
        </h2>
        <span className="text-xs text-zinc-500">
          {slots.length} slots · {totalDuration.toFixed(2)}s ·{" "}
          {overlays.length} overlay{overlays.length === 1 ? "" : "s"} ·{" "}
          {transitions.length} transition{transitions.length === 1 ? "" : "s"} ·{" "}
          {curtains.length} curtain{curtains.length === 1 ? "" : "s"}
        </span>
        {bundle.overlaysApproximate && (
          <span
            className="text-[10px] uppercase tracking-wider rounded bg-yellow-700/40 text-yellow-200 px-2 py-0.5"
            title="No overlay/render_window events in pipeline_trace — overlay timings derived from recipe slot offsets, not actual render-time values."
          >
            Overlay timings approximate
          </span>
        )}
        {bundle.truncated && (
          <span
            className="text-[10px] uppercase tracking-wider rounded bg-red-700/40 text-red-200 px-2 py-0.5"
            title="pipeline_trace hit the 500-event cap. Some events may be missing from this timeline."
          >
            Trace truncated
          </span>
        )}
      </header>

      <div className="rounded border border-zinc-800 bg-zinc-950 p-4 overflow-x-auto">
        <div className="min-w-[640px] relative">
          {/* Ruler */}
          <div className="relative h-5 mb-2 text-[10px] text-zinc-500 border-b border-zinc-800">
            {_buildTicks(totalDuration).map((t) => (
              <span
                key={t}
                className="absolute -translate-x-1/2"
                style={{ left: pct(t) }}
              >
                {_formatTime(t)}
              </span>
            ))}
          </div>

          {/* Slots row */}
          <Row title="Slots">
            {slots.map((slot) => (
              <div
                key={slot.position}
                className="absolute top-0 bottom-0 border-l border-r border-zinc-700 bg-zinc-800/50 text-[10px] text-zinc-200 px-1 py-1 truncate"
                style={{
                  left: pct(slot.abs_start_s),
                  width: pct(slot.abs_end_s - slot.abs_start_s),
                }}
                title={`Slot ${slot.position} · ${slot.abs_start_s.toFixed(2)}–${slot.abs_end_s.toFixed(2)}s · ${slot.clip_id ?? ""}`}
              >
                #{slot.position}
              </div>
            ))}
          </Row>

          {/* Transitions row */}
          <Row title="Transitions">
            {transitions.map((tr, i) => (
              <div
                key={`tr-${i}`}
                className="absolute top-0 bottom-0 bg-yellow-500/60 w-1"
                style={{ left: pct(tr.abs_time_s) }}
                title={`${tr.type ?? "transition"} at slot ${tr.slot_index ?? "?"} (${tr.duration_s ?? "?"}s)`}
              />
            ))}
            {/* Recipe-declared transitions (fallback when no events) */}
            {transitions.length === 0 &&
              slots
                .filter((s) => s.transition_in_recipe && s.position > 1)
                .map((s) => (
                  <div
                    key={`rt-${s.position}`}
                    className="absolute top-0 bottom-0 bg-yellow-700/50 w-1 opacity-60"
                    style={{ left: pct(s.abs_start_s) }}
                    title={`recipe: ${s.transition_in_recipe} (no trace event)`}
                  />
                ))}
          </Row>

          {/* Curtains row */}
          <Row title="Curtain / interstitial">
            {curtains.map((c, i) => (
              <div
                key={`c-${i}`}
                className="absolute top-0 bottom-0 bg-purple-500/60 w-2"
                style={{ left: pct(c.abs_time_s) }}
                title={`${c.raw.event} after slot ${c.slot_index ?? "?"}`}
              />
            ))}
          </Row>

          {/* Overlays row */}
          <Row title="Text overlays">
            {overlays.map((ov, i) => {
              const start = ov.abs_start_s;
              const end = Math.max(ov.abs_end_s, ov.abs_start_s + 0.05);
              const clampedColor =
                ov.clamped_by === "curtain_close"
                  ? "bg-purple-500/30 border-purple-400"
                  : ov.clamped_by === "override"
                    ? "bg-amber-500/30 border-amber-400"
                    : ov.clamped_by === "overlap_truncated"
                      ? "bg-red-500/30 border-red-400"
                      : ov.approximate
                        ? "bg-yellow-700/30 border-yellow-600 border-dashed"
                        : "bg-cyan-500/30 border-cyan-400";
              return (
                <div
                  key={`ov-${i}`}
                  className={`absolute top-0 bottom-0 border ${clampedColor} text-[10px] text-zinc-100 px-1 py-1 truncate`}
                  style={{
                    left: pct(start),
                    width: pct(end - start),
                  }}
                  title={_overlayTooltip(ov)}
                >
                  {ov.text}
                </div>
              );
            })}
          </Row>
        </div>
      </div>

      <SlotTable slots={slots} overlays={overlays} transitions={transitions} curtains={curtains} />

      <details className="rounded border border-zinc-800 bg-zinc-950 px-4 py-3">
        <summary className="text-xs uppercase tracking-wider text-zinc-500 cursor-pointer">
          Raw assembly_plan
        </summary>
        <div className="mt-3">
          <JsonTreeView value={data.job.assembly_plan} defaultDepth={2} />
        </div>
      </details>
    </div>
  );
}

function Row({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}): JSX.Element {
  return (
    <div className="relative mb-1.5">
      <div className="text-[10px] uppercase tracking-wider text-zinc-500 mb-1">
        {title}
      </div>
      <div
        className="relative bg-zinc-900/40 border border-zinc-800 rounded"
        style={{ height: _STAGE_TIMELINE_HEIGHT }}
      >
        {children}
      </div>
    </div>
  );
}

function _overlayTooltip(ov: DerivedOverlay): string {
  const lines: string[] = [
    `"${ov.text}"`,
    `${ov.abs_start_s.toFixed(2)}–${ov.abs_end_s.toFixed(2)}s (${(ov.abs_end_s - ov.abs_start_s).toFixed(2)}s long)`,
  ];
  if (ov.slot_index !== null) lines.push(`slot ${ov.slot_index}`);
  if (ov.merged_from_slots && ov.merged_from_slots.length > 1) {
    lines.push(`merged across slots [${ov.merged_from_slots.join(", ")}]`);
  }
  if (ov.position) lines.push(`position: ${ov.position}`);
  if (ov.effect) lines.push(`effect: ${ov.effect}`);
  if (ov.font_cycle_accel_at_s != null) {
    lines.push(`font-cycle accel @ ${ov.font_cycle_accel_at_s.toFixed(2)}s`);
  }
  if (ov.clamped_by) lines.push(`clamped by: ${ov.clamped_by}`);
  if (ov.approximate) lines.push("(approximate — derived from recipe)");
  return lines.join("\n");
}

function _buildTicks(total: number): number[] {
  if (total <= 0) return [0];
  const step = total > 30 ? 5 : total > 10 ? 2 : 1;
  const ticks: number[] = [];
  for (let t = 0; t <= total; t += step) ticks.push(Number(t.toFixed(2)));
  if (ticks[ticks.length - 1] !== Number(total.toFixed(2))) {
    ticks.push(Number(total.toFixed(2)));
  }
  return ticks;
}

// ── Per-slot table ────────────────────────────────────────────────────────────

function SlotTable({
  slots,
  overlays,
  transitions,
  curtains,
}: {
  slots: DerivedSlot[];
  overlays: DerivedOverlay[];
  transitions: DerivedTransition[];
  curtains: DerivedCurtain[];
}): JSX.Element {
  return (
    <div className="rounded border border-zinc-800 bg-zinc-950 overflow-x-auto">
      <table className="w-full text-xs">
        <thead className="bg-zinc-900/80 text-zinc-400 uppercase tracking-wider text-[10px]">
          <tr>
            <th className="px-3 py-2 text-left">#</th>
            <th className="px-3 py-2 text-left">Time</th>
            <th className="px-3 py-2 text-left">Clip</th>
            <th className="px-3 py-2 text-left">Moment</th>
            <th className="px-3 py-2 text-left">Transition in</th>
            <th className="px-3 py-2 text-left">Text overlays</th>
            <th className="px-3 py-2 text-left">After-slot</th>
          </tr>
        </thead>
        <tbody>
          {slots.map((s) => {
            const slotOverlays = overlays.filter(
              (o) =>
                o.slot_index === s.position ||
                (o.merged_from_slots ?? []).includes(s.position),
            );
            const slotTransitionIn = transitions.find(
              (t) => t.slot_index === s.position,
            );
            const slotCurtain = curtains.find((c) => c.slot_index === s.position);
            return (
              <tr
                key={s.position}
                className="border-t border-zinc-800 text-zinc-200 align-top"
              >
                <td className="px-3 py-2 font-mono">{s.position}</td>
                <td className="px-3 py-2 font-mono whitespace-nowrap">
                  {s.abs_start_s.toFixed(2)}–{s.abs_end_s.toFixed(2)}s
                </td>
                <td
                  className="px-3 py-2 font-mono text-zinc-500 truncate max-w-[14rem]"
                  title={s.clip_id ?? ""}
                >
                  {s.clip_id ?? "—"}
                </td>
                <td className="px-3 py-2 font-mono whitespace-nowrap">
                  {s.moment?.start_s != null && s.moment?.end_s != null
                    ? `${s.moment.start_s.toFixed(2)}–${s.moment.end_s.toFixed(2)}s`
                    : "—"}
                </td>
                <td className="px-3 py-2 whitespace-nowrap">
                  {slotTransitionIn
                    ? `${slotTransitionIn.type ?? "?"}${
                        slotTransitionIn.duration_s != null
                          ? ` ${slotTransitionIn.duration_s.toFixed(2)}s`
                          : ""
                      }`
                    : s.transition_in_recipe
                      ? `${s.transition_in_recipe} (recipe)`
                      : "—"}
                </td>
                <td className="px-3 py-2">
                  {slotOverlays.length === 0 ? (
                    <span className="text-zinc-500">—</span>
                  ) : (
                    <ul className="space-y-0.5">
                      {slotOverlays.map((o, i) => (
                        <li key={i} className="font-mono">
                          <span className="text-zinc-100">
                            &quot;{o.text}&quot;
                          </span>
                          <span className="text-zinc-500">
                            {" "}
                            {o.abs_start_s.toFixed(2)}–{o.abs_end_s.toFixed(2)}s
                          </span>
                          {o.clamped_by && (
                            <span className="ml-1 text-[10px] text-amber-300">
                              [{o.clamped_by}]
                            </span>
                          )}
                          {o.merged_from_slots &&
                            o.merged_from_slots.length > 1 && (
                              <span className="ml-1 text-[10px] text-zinc-400">
                                merged
                              </span>
                            )}
                        </li>
                      ))}
                    </ul>
                  )}
                </td>
                <td className="px-3 py-2 whitespace-nowrap">
                  {slotCurtain ? (
                    <span className="text-purple-300">
                      {slotCurtain.raw.event}
                    </span>
                  ) : (
                    <span className="text-zinc-500">—</span>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
