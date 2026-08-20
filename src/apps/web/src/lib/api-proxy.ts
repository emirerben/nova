/**
 * Shared same-origin → FastAPI proxy used by /api/plan and /api/me.
 *
 * Reads the NextAuth session server-side, injects X-User-Id + the server-only
 * INTERNAL_API_KEY, and forwards to the backend. The browser never sees the key.
 *
 * Centralised on purpose: the X-User-Id injection is the ONLY thing identifying
 * the user to the strict backend routes, so keeping it in a single audited place
 * (rather than copy-pasted per route) means there is exactly one spot to reason
 * about the auth boundary. /api/plan forwards to `${API_BASE}/<path>`; /api/me
 * forwards to `${API_BASE}/me/<path>` (pass `upstreamPrefix="me"`).
 */

import { getServerSession } from "next-auth";
import { type NextRequest, NextResponse } from "next/server";
import { randomUUID } from "node:crypto";
import { authOptions } from "@/lib/auth";

const API_BASE =
  process.env.API_URL ?? process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
const INTERNAL_API_KEY = process.env.INTERNAL_API_KEY ?? "";

// Narrow 5xx pass-through whitelist (G5): these two codes carry a message
// worth showing a creator verbatim (guided-edit chat failures) — everything
// else on a 5xx stays the generic safe envelope below. Length-capped so an
// unexpectedly large upstream message can't blow up the response.
const PASSTHROUGH_5XX_CODES = new Set(["edit_guide_failed", "proposal_dispatch_failed"]);
const PASSTHROUGH_MESSAGE_MAX = 300;

type RouteCtx = { params: Promise<{ path: string[] }> };

async function proxy(
  req: NextRequest,
  params: Promise<{ path: string[] }>,
  upstreamPrefix: string,
): Promise<NextResponse> {
  const { path } = await params;
  const qs = req.nextUrl.search;
  const prefix = upstreamPrefix ? `${upstreamPrefix}/` : "";
  const route = `${prefix}${path.join("/")}`;
  const upstream = `${API_BASE}/${route}${qs}`;
  const requestId = req.headers.get("x-request-id") ?? randomUUID();
  const correlationId = req.headers.get("x-correlation-id") ?? requestId;

  const boundaryFailure = (stage: string, err: unknown): NextResponse => {
    console.error("[api-proxy] boundary failure", {
      requestId,
      correlationId,
      method: req.method,
      route,
      stage,
      errorCode: "upstream_unavailable",
      errorType: err instanceof Error ? err.name : typeof err,
    });
    return NextResponse.json(
      {
        detail: "Kria couldn't reach the video service. Retry in a moment.",
        code: "upstream_unavailable",
        stage,
        retryable: true,
        request_id: requestId,
        correlation_id: correlationId,
      },
      {
        status: 502,
        headers: { "X-Request-Id": requestId, "X-Correlation-Id": correlationId },
      },
    );
  };

  // Require authentication. The google-upsert call (from the signIn callback) is
  // made server-side with the internal key directly, not through this proxy.
  const session = await getServerSession(authOptions);
  const userId = (session?.user as Record<string, unknown> | undefined)?.id as
    | string
    | undefined;

  if (!userId) {
    return NextResponse.json({ detail: "Authentication required" }, { status: 401 });
  }

  if (!INTERNAL_API_KEY) {
    console.error("[api-proxy] INTERNAL_API_KEY missing", {
      requestId,
      correlationId,
      method: req.method,
      route,
      stage: "proxy_config",
      errorCode: "server_misconfigured",
    });
    return NextResponse.json(
      {
        detail: "Kria couldn't reach the video service. Retry in a moment.",
        code: "server_misconfigured",
        stage: "proxy_config",
        retryable: false,
        request_id: requestId,
        correlation_id: correlationId,
      },
      {
        status: 500,
        headers: { "X-Request-Id": requestId, "X-Correlation-Id": correlationId },
      },
    );
  }

  const headers: Record<string, string> = {
    Authorization: `Bearer ${INTERNAL_API_KEY}`,
    "X-User-Id": userId,
    "X-Request-Id": requestId,
    "X-Correlation-Id": correlationId,
  };
  const contentType = req.headers.get("content-type");
  if (contentType) headers["Content-Type"] = contentType;

  let body: ArrayBuffer | undefined;
  try {
    body = req.method !== "GET" && req.method !== "HEAD" ? await req.arrayBuffer() : undefined;
  } catch (err) {
    return boundaryFailure("request_body", err);
  }

  let upstreamRes: Response;
  try {
    upstreamRes = await fetch(upstream, {
      method: req.method,
      headers,
      body: body ? Buffer.from(body) : undefined,
    });
  } catch (err) {
    return boundaryFailure("upstream_fetch", err);
  }

  if (upstreamRes.status >= 500) {
    // The generic envelope below is still the default — an upstream exception
    // body may contain raw provider or application details the creator must
    // never see. The one narrow exception: a body shaped exactly
    // {"detail":{"code":<whitelisted>,"message":<str>}} is a deliberate,
    // human-safe error the backend wrote on purpose (not a stack trace), so
    // pass its message through instead of flattening it to the generic copy.
    let passthroughMessage: string | null = null;
    let passthroughCode: string | null = null;
    try {
      const buf = await upstreamRes.arrayBuffer();
      const parsed = JSON.parse(Buffer.from(buf).toString("utf8")) as {
        detail?: { code?: unknown; message?: unknown };
      };
      const code = parsed?.detail?.code;
      const message = parsed?.detail?.message;
      if (
        typeof code === "string" &&
        typeof message === "string" &&
        PASSTHROUGH_5XX_CODES.has(code)
      ) {
        passthroughCode = code;
        passthroughMessage = message.slice(0, PASSTHROUGH_MESSAGE_MAX);
      }
    } catch {
      // Not JSON, or doesn't match the whitelisted shape — stays generic below.
    }
    console.error("[api-proxy] upstream server error", {
      requestId,
      correlationId,
      method: req.method,
      route,
      upstreamStatus: upstreamRes.status,
      stage: "upstream_response",
      errorCode: passthroughCode ?? "upstream_error",
    });
    if (passthroughMessage && passthroughCode) {
      return NextResponse.json(
        { detail: passthroughMessage, code: passthroughCode, retryable: true },
        {
          status: upstreamRes.status,
          headers: { "X-Request-Id": requestId, "X-Correlation-Id": correlationId },
        },
      );
    }
    return NextResponse.json(
      {
        detail: "Kria couldn't complete that request. Retry in a moment.",
        code: "upstream_error",
        stage: "upstream_response",
        retryable: true,
        request_id: requestId,
        correlation_id: correlationId,
      },
      {
        status: upstreamRes.status,
        headers: { "X-Request-Id": requestId, "X-Correlation-Id": correlationId },
      },
    );
  }

  // Fetch forbids bodies on HEAD responses and 204/205/304 statuses. Even an
  // empty ArrayBuffer is considered a body and makes NextResponse throw,
  // turning a successful upstream DELETE into a 500 at the proxy boundary.
  const bodylessResponse =
    req.method === "HEAD" || [204, 205, 304].includes(upstreamRes.status);
  try {
    const resBody = bodylessResponse ? null : await upstreamRes.arrayBuffer();
    return new NextResponse(resBody, {
      status: upstreamRes.status,
      // `fetch` decodes compressed upstream bodies but keeps their original
      // Content-Encoding/Content-Length headers. Forwarding those transport
      // headers makes the browser try to decode the already-decoded bytes again
      // (Fly currently serves JSON with zstd), so keep this boundary allowlisted.
      headers: {
        "Content-Type": upstreamRes.headers.get("content-type") ?? "application/json",
        "X-Request-Id": requestId,
        "X-Correlation-Id": correlationId,
      },
    });
  } catch (err) {
    return boundaryFailure("response_finalize", err);
  }
}

export const proxyMaxDuration = 60;

/** Build Next.js route handlers that proxy to `${API_BASE}/${upstreamPrefix}/<path>`. */
export function makeProxyHandlers(upstreamPrefix = "") {
  const handler = (req: NextRequest, ctx: RouteCtx) => proxy(req, ctx.params, upstreamPrefix);
  return { GET: handler, POST: handler, PUT: handler, PATCH: handler, DELETE: handler };
}
