/**
 * Shared types for the unified multi-lane timeline.
 *
 * Modeled on lib/variant-editor/types.ts — a small cross-lane vocabulary that
 * every lane and primitive can import without pulling in heavy lane-specific
 * logic.
 */

/** Identifiers for the four editor lanes. */
export type LaneId = "clips" | "text" | "overlays" | "sfx";

/**
 * A positioned bar in the timeline — read-only representation used by
 * non-interactive lanes (clips, text, overlays) to render their blocks.
 */
export interface TimelineLaneBar {
  /** Stable key for React reconciliation. */
  id: string;
  /** Human-readable label shown inside the bar (truncated to fit). */
  label: string;
  /** Bar start in seconds (from video origin). */
  startS: number;
  /** Bar end in seconds. */
  endS: number;
  /**
   * Tailwind background class (or arbitrary hex via `bg-[#...]`).
   * Convention: sfx = lime, overlays = violet, text = amber, clips = sky.
   */
  color: string;
  /** When true the lane hosts its own drag logic; false = click-through only. */
  interactive: boolean;
}
