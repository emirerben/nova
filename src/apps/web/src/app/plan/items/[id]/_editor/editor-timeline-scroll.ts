export interface TimelineScrollAnchorInput {
  previousScrollLeft: number;
  viewportWidth: number;
  previousPxPerSecond: number;
  nextPxPerSecond: number;
  durationS: number;
  currentTimeS: number;
}

export function anchoredTimelineScrollLeft({
  previousScrollLeft,
  viewportWidth,
  previousPxPerSecond,
  nextPxPerSecond,
  durationS,
  currentTimeS,
}: TimelineScrollAnchorInput): number {
  if (
    viewportWidth <= 0 ||
    previousPxPerSecond <= 0 ||
    nextPxPerSecond <= 0 ||
    durationS <= 0
  ) {
    return 0;
  }

  const previousTrackWidth = durationS * previousPxPerSecond;
  const nextTrackWidth = durationS * nextPxPerSecond;
  const maxScrollLeft = Math.max(0, nextTrackWidth - viewportWidth);
  const previousPlayheadPx = currentTimeS * previousPxPerSecond;
  const playheadVisible =
    previousPlayheadPx >= previousScrollLeft &&
    previousPlayheadPx <= previousScrollLeft + viewportWidth;

  const anchorTimeS = playheadVisible
    ? currentTimeS
    : Math.min(durationS, Math.max(0, (previousScrollLeft + viewportWidth / 2) / previousPxPerSecond));
  const viewportOffsetPx = playheadVisible
    ? previousPlayheadPx - previousScrollLeft
    : Math.min(viewportWidth / 2, previousTrackWidth);
  const nextScrollLeft = anchorTimeS * nextPxPerSecond - viewportOffsetPx;

  return Math.min(maxScrollLeft, Math.max(0, nextScrollLeft));
}
