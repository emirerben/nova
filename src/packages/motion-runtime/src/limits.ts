import limitsJson from "../motion-limits.json" with { type: "json" };

/** Shared editor/runtime guardrails. Keep the Python adapter in sync by reading
 * the same motion-limits.json file from the copied runtime package. */
export const EDITOR_MAX_TIMELINE_SLOTS = limitsJson.timeline_max_slots;
export const MOTION_FPS = limitsJson.motion_fps;
export const MOTION_MAX_INSTANCES = limitsJson.motion_max_instances;
export const MOTION_MAX_INSTANCE_FRAMES =
  limitsJson.motion_max_instance_seconds * MOTION_FPS;
export const MOTION_MAX_ACTIVE_FRAMES =
  limitsJson.motion_max_active_seconds * MOTION_FPS;
export const MOTION_MAX_CONCURRENT_COMPLEXITY =
  limitsJson.motion_max_concurrent_complexity;
export const MOTION_MAX_COMPLEXITY_MULTIPLIER =
  limitsJson.motion_max_complexity_multiplier;
export const MOTION_MAX_WEIGHTED_ACTIVE_FRAMES =
  MOTION_MAX_ACTIVE_FRAMES * MOTION_MAX_COMPLEXITY_MULTIPLIER;
