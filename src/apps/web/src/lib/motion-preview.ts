export function creatorBlockPreviewFrame(
  startFrame: number,
  endFrameExclusive: number,
  prefersReducedMotion: boolean,
  elapsedMs = 0,
): number {
  const duration = Math.max(1, endFrameExclusive - startFrame);
  if (prefersReducedMotion) {
    return startFrame + Math.floor(duration * 0.45);
  }
  const elapsedFrames = Math.floor((elapsedMs / 1000) * 15) * 2;
  return startFrame + (elapsedFrames % duration);
}
