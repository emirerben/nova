/**
 * Next.js API proxy for the per-user "my" surface (/me/jobs, add-to-plan).
 *
 * Thin wrapper over the shared proxy (src/lib/api-proxy.ts) with the "me"
 * upstream prefix: /api/me/jobs → ${API_BASE}/me/jobs. Injects the NextAuth
 * session's X-User-Id + the server-only INTERNAL_API_KEY, so the backend's
 * strict CurrentUser dependency scopes every response to the signed-in user.
 */

import { makeProxyHandlers, proxyMaxDuration } from "@/lib/api-proxy";

export const maxDuration = proxyMaxDuration;

export const { GET, POST } = makeProxyHandlers("me");
