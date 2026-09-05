export function shouldSurfacePendingRenderFailure(
  targetGeneration: string | null | undefined,
  pendingEditOwnsFailureCopy: boolean,
): boolean {
  return targetGeneration != null || pendingEditOwnsFailureCopy;
}

/** A failed status is terminal for the edit that owns the pending action even
 * when its finished-at fingerprint did not advance before the worker failed. */
export function isFreshPendingFailure(
  renderStatus: string | null | undefined,
  pendingEditOwnsFailureCopy: boolean,
  isFreshRender: boolean,
  dispatchSucceeded: boolean,
): boolean {
  return (
    renderStatus === "failed" &&
    (isFreshRender || (pendingEditOwnsFailureCopy && dispatchSucceeded))
  );
}
