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
import { authOptions } from "@/lib/auth";

const API_BASE =
  process.env.API_URL ?? process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
const INTERNAL_API_KEY = process.env.INTERNAL_API_KEY ?? "";

type RouteCtx = { params: Promise<{ path: string[] }> };

async function proxy(
  req: NextRequest,
  params: Promise<{ path: string[] }>,
  upstreamPrefix: string,
): Promise<NextResponse> {
  const { path } = await params;
  const qs = req.nextUrl.search;
  const prefix = upstreamPrefix ? `${upstreamPrefix}/` : "";
  const upstream = `${API_BASE}/${prefix}${path.join("/")}${qs}`;

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
    return NextResponse.json(
      {
        detail:
          "INTERNAL_API_KEY not set on the web server. " +
          "Add INTERNAL_API_KEY to src/apps/web/.env.local.",
      },
      { status: 500 },
    );
  }

  const headers: Record<string, string> = {
    Authorization: `Bearer ${INTERNAL_API_KEY}`,
    "X-User-Id": userId,
  };
  const contentType = req.headers.get("content-type");
  if (contentType) headers["Content-Type"] = contentType;

  const body =
    req.method !== "GET" && req.method !== "HEAD" ? await req.arrayBuffer() : undefined;

  let upstreamRes: Response;
  try {
    upstreamRes = await fetch(upstream, {
      method: req.method,
      headers,
      body: body ? Buffer.from(body) : undefined,
    });
  } catch (err) {
    console.error("[api-proxy] upstream fetch failed", { upstream, error: String(err) });
    return NextResponse.json({ detail: "Backend unavailable" }, { status: 502 });
  }

  const resBody = await upstreamRes.arrayBuffer();
  return new NextResponse(resBody, {
    status: upstreamRes.status,
    headers: { "Content-Type": upstreamRes.headers.get("content-type") ?? "application/json" },
  });
}

export const proxyMaxDuration = 60;

/** Build Next.js route handlers that proxy to `${API_BASE}/${upstreamPrefix}/<path>`. */
export function makeProxyHandlers(upstreamPrefix = "") {
  const handler = (req: NextRequest, ctx: RouteCtx) => proxy(req, ctx.params, upstreamPrefix);
  return { GET: handler, POST: handler, PUT: handler, PATCH: handler, DELETE: handler };
}
