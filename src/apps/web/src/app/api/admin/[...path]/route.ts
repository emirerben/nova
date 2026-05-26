/**
 * Next.js API proxy for admin/music-tracks endpoints.
 *
 * Reads ADMIN_TOKEN server-side (never exposed in the browser bundle).
 * The frontend uses /api/admin/... instead of calling the FastAPI admin
 * endpoints directly, so NEXT_PUBLIC_ADMIN_TOKEN is no longer needed.
 */

import { type NextRequest, NextResponse } from "next/server";

// Server-only `API_URL` takes precedence so deploys can decouple the admin
// upstream from the public client bundle, but fall back to the same
// `NEXT_PUBLIC_API_URL` the rest of the web app uses. Without this fallback,
// a Vercel deploy that only sets `NEXT_PUBLIC_API_URL` (the documented var in
// CLAUDE.md) drops every admin proxy call to localhost:8000 → ECONNREFUSED
// → 502 "Backend unavailable" from the catch block below. Surfaced when the
// new "Music" admin nav link prompted the first real prod hit on the proxy.
const API_BASE =
  process.env.API_URL ?? process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
const ADMIN_TOKEN = process.env.ADMIN_TOKEN ?? "";

async function proxy(
  req: NextRequest,
  params: Promise<{ path: string[] }>,
): Promise<NextResponse> {
  const { path } = await params;
  const qs = req.nextUrl.search;
  const upstream = `${API_BASE}/admin/${path.join("/")}${qs}`;

  // Short-circuit if the web server is missing its env, rather than forwarding
  // an empty `X-Admin-Token` and surfacing a misleading 401 from FastAPI.
  // Next.js loads env from `src/apps/web/.env*`, NOT the repo-root `.env` —
  // `dev-auto.sh` works because it `source`s the root file before launching.
  if (!ADMIN_TOKEN) {
    return NextResponse.json(
      {
        detail:
          "ADMIN_TOKEN not set on the web server. Either run dev-auto.sh, " +
          "or add ADMIN_TOKEN to src/apps/web/.env.local.",
      },
      { status: 500 },
    );
  }

  // SECURITY: do not forward Authorization or Cookie upstream.
  // The BasicAuth credential is consumed by middleware.ts (the /admin/* and
  // /api/admin/* shared-secret gate). FastAPI's only auth signal is the
  // server-side X-Admin-Token injected below; FastAPI must never see the
  // browser's BasicAuth credentials or any session cookie. Only the
  // incoming Content-Type is copied so the upstream parses bodies correctly.
  const headers: Record<string, string> = {
    "X-Admin-Token": ADMIN_TOKEN,
  };
  const contentType = req.headers.get("content-type");
  if (contentType) headers["Content-Type"] = contentType;

  const body =
    req.method !== "GET" && req.method !== "HEAD"
      ? await req.arrayBuffer()
      : undefined;

  let upstreamRes: Response;
  try {
    upstreamRes = await fetch(upstream, {
      method: req.method,
      headers,
      body: body ? Buffer.from(body) : undefined,
    });
  } catch (err) {
    console.error("[admin-proxy] upstream fetch failed", { upstream, error: String(err) });
    return NextResponse.json(
      { detail: "Backend unavailable" },
      { status: 502 },
    );
  }

  const resBody = await upstreamRes.arrayBuffer();
  return new NextResponse(resBody, {
    status: upstreamRes.status,
    headers: { "Content-Type": upstreamRes.headers.get("content-type") ?? "application/json" },
  });
}

// App Router route handlers stream the request body directly (via
// `req.arrayBuffer()`), so the legacy Pages-router `bodyParser` flag
// is unnecessary and is rejected by Next.js 14's build (see
// https://nextjs.org/docs/app/api-reference/file-conventions/route-segment-config).
// `maxDuration` is the supported App Router segment config.
export const maxDuration = 60;

export const GET = (req: NextRequest, ctx: { params: Promise<{ path: string[] }> }) =>
  proxy(req, ctx.params);
export const POST = (req: NextRequest, ctx: { params: Promise<{ path: string[] }> }) =>
  proxy(req, ctx.params);
export const PATCH = (req: NextRequest, ctx: { params: Promise<{ path: string[] }> }) =>
  proxy(req, ctx.params);
export const DELETE = (req: NextRequest, ctx: { params: Promise<{ path: string[] }> }) =>
  proxy(req, ctx.params);
