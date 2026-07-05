import type { SoundEffectPlacement } from "@/lib/plan-api";

export function sfxPlaybackOffsetAt(
  placement: Pick<SoundEffectPlacement, "at_s" | "duration_s">,
  timeS: number,
  fallbackDurationS = 60,
): number | null {
  const offset = timeS - placement.at_s;
  const durationS = placement.duration_s ?? fallbackDurationS;
  if (offset < 0 || offset >= durationS) return null;
  return offset;
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
