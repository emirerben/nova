export const KRIA_STORY_STEPS = [
  { label: "Inputs waiting", range: "0%", start: 0 },
  { label: "Save time", range: "0–12%", start: 0.015 },
  { label: "Let AI edit your videos", range: "12–24%", start: 0.12 },
  { label: "Create more", range: "24–36%", start: 0.24 },
  { label: "Captions + visual effects", range: "36–52%", start: 0.36 },
  { label: "Place overlays", range: "52–68%", start: 0.52 },
  { label: "Visual effects appear", range: "68–82%", start: 0.68 },
  { label: "Add sound effects", range: "82–100%", start: 0.82 },
] as const;

export function getKriaStoryStep(progress: number): number {
  for (let step = KRIA_STORY_STEPS.length - 1; step >= 0; step -= 1) {
    if (progress >= KRIA_STORY_STEPS[step].start) return step;
  }
  return 0;
}

export function getKriaHeadlineLineCount(progress: number): number {
  if (progress < 0.87) return 0;
  if (progress < 0.93) return 1;
  if (progress < 0.99) return 2;
  return 3;
}

// The first 3.5s assemble the raw inputs. The authored 11.21s render then
// becomes the single source of truth for picture, captions, effects, and sound.
export const AUTO_RENDER_START_MS = 3_500;
export const AUTO_STORY_DURATION_MS = 14_710;
export const AUTO_SOUND_START_MS = 7_200;
const AUTO_SOUND_FADE_MS = 300;
const AUTO_SOUND_VOLUME = 0.8;
const AUTO_HEADLINE_TIMES_MS = [7_800, 8_800, 9_800] as const;

// 5.5s global time is 2.0s into the authored render: the Lisbon visual cut.
const AUTO_STORY_STEP_TIMES_MS = [
  0,
  300,
  1_100,
  1_900,
  AUTO_RENDER_START_MS,
  4_500,
  5_500,
  AUTO_SOUND_START_MS,
] as const;

export function getAutoStoryAudioVolume(elapsedMs: number): number {
  const fadeProgress = Math.min(
    Math.max((elapsedMs - AUTO_SOUND_START_MS) / AUTO_SOUND_FADE_MS, 0),
    1,
  );
  return AUTO_SOUND_VOLUME * fadeProgress;
}

export function getAutoKriaStoryStep(elapsedMs: number): number {
  for (let step = AUTO_STORY_STEP_TIMES_MS.length - 1; step >= 0; step -= 1) {
    if (elapsedMs >= AUTO_STORY_STEP_TIMES_MS[step]) return step;
  }
  return 0;
}

export function getAutoKriaHeadlineLineCount(elapsedMs: number): number {
  return AUTO_HEADLINE_TIMES_MS.filter((time) => elapsedMs >= time).length;
}
