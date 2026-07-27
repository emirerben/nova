/**
 * True when this API cannot serve an editor-intelligence route at all.
 *
 * Two shapes, one meaning:
 *   - a `FeatureDisabledError` — the route is deployed but its flag is off
 *     (EDIT_COPILOT_ENABLED / EDIT_DIRECTOR_ENABLED).
 *   - a bare `"Not Found"` — the deployed API predates the route entirely,
 *     the version-skew case where new web ships ahead of the API.
 *
 * Neither clears on retry, so the editor latches the feature off and says so
 * once instead of surfacing a failure on every mount. The UI half of these
 * flags is separate (NEXT_PUBLIC_EDIT_*), so the drawer can render against an
 * API that cannot answer it; that skew is exactly what this detects.
 *
 * Deliberately narrow. Real 404s on the same routes carry specific details
 * ("Plan item not found", "No render to edit yet", "Variant not found") and
 * must keep surfacing as ordinary, retryable errors.
 *
 * Matched by `name`, not `instanceof`: this runs inside catch blocks, where
 * throwing a TypeError would discard the error being handled. Name matching
 * survives a duplicated plan-api module in the bundle and a partial test mock,
 * both of which make `instanceof` silently or loudly wrong.
 */
export function isFeatureUnavailable(caught: unknown): boolean {
  if (!(caught instanceof Error)) return false;
  return caught.name === "FeatureDisabledError" || caught.message === "Not Found";
}
