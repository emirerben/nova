/**
 * Next.js API proxy for plan endpoints (/personas, /content-plans, /plan-items, /auth).
 *
 * Thin wrapper over the shared proxy (src/lib/api-proxy.ts), which reads the
 * NextAuth session server-side and injects X-User-Id + INTERNAL_API_KEY before
 * forwarding to FastAPI. The browser never sees the internal key.
 */

import { makeProxyHandlers, proxyMaxDuration } from "@/lib/api-proxy";

export const maxDuration = proxyMaxDuration;

export const { GET, POST, PATCH, DELETE } = makeProxyHandlers();
