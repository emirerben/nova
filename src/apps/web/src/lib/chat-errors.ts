/**
 * Filter opaque backend/proxy sentinels out of chat surfaces.
 *
 * Generalized from useEditCopilot's `copilotErrorMessage` (that hook is left
 * untouched — do not import this into it). Two shapes are opaque and never
 * worth showing a creator: snake_case diagnostic codes the backend uses as
 * internal error sentinels (e.g. `edit_guide_failed`), and the generic
 * `Request failed (NNN)` fallback plan-api's `request()` writes when the
 * body carried no `detail` at all (exactly what a raw slowapi 429 body
 * looks like). Anything else is treated as human-readable and passed through.
 */
export function chatErrorMessage(caught: unknown, fallback: string): string {
  if (!(caught instanceof Error) || !caught.message) return fallback;
  const opaque =
    /^[a-z0-9_]+$/.test(caught.message) || caught.message.startsWith("Request failed (");
  return opaque ? fallback : caught.message;
}
