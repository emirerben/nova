import type { SoundEffectPlacement } from "@/lib/plan-api";

export function sfxPlaybackOffsetAt(
  placement: Pick<
    SoundEffectPlacement,
    "at_s" | "duration_s" | "trim_start_s" | "trim_end_s"
  >,
  timeS: number,
  fallbackDurationS = 60,
): number | null {
  const trimStartS = Math.max(0, placement.trim_start_s ?? 0);
  const sourceDurationS = Math.max(0, placement.duration_s ?? fallbackDurationS);
  const trimEndS = Math.max(
    trimStartS,
    Math.min(placement.trim_end_s ?? sourceDurationS, sourceDurationS),
  );
  const activeDurationS = Math.max(0, trimEndS - trimStartS);
  const offsetInPlacement = timeS - placement.at_s;
  if (offsetInPlacement < 0 || offsetInPlacement >= activeDurationS) return null;
  return trimStartS + offsetInPlacement;
}

export function sfxPlacementsStartingInWindow<T extends Pick<SoundEffectPlacement, "at_s">>(
  placements: T[],
  fromS: number,
  toS: number,
): T[] {
  if (toS < fromS) {
    return placements.filter((placement) => placement.at_s > fromS || placement.at_s <= toS);
  }
  return placements.filter((placement) => placement.at_s > fromS && placement.at_s <= toS);
}
