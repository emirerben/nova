/**
 * Next.js API proxy for plan endpoints (/personas, /content-plans, /plan-items, /auth).
 *
 * Reads the NextAuth session server-side, injects X-User-Id + INTERNAL_API_KEY,
 * and forwards to the FastAPI backend.  The browser never sees the internal key.
 *
 * Pattern mirrors /api/admin/[...path]/route.ts.
 */

import { getServerSession } from "next-auth";
import { type NextRequest, NextResponse } from "next/server";
import { authOptions } from "@/lib/auth";

const API_BASE =
  process.env.API_URL ?? process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
const INTERNAL_API_KEY = process.env.INTERNAL_API_KEY ?? "";

async function proxy(
  req: NextRequest,
  params: Promise<{ path: string[] }>,
): Promise<NextResponse> {
  const { path } = await params;
  const qs = req.nextUrl.search;

  // Map /api/plan/<segments> → FastAPI path.
  // e.g. /api/plan/personas/abc → /personas/abc
  //      /api/plan/content-plans → /content-plans
  const upstream = `${API_BASE}/${path.join("/")}${qs}`;

  // Require authentication.  The google-upsert call (from signIn callback) is
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
    console.error("[plan-proxy] upstream fetch failed", { upstream, error: String(err) });
    return NextResponse.json({ detail: "Backend unavailable" }, { status: 502 });
  }

  const resBody = await upstreamRes.arrayBuffer();
  return new NextResponse(resBody, {
    status: upstreamRes.status,
    headers: { "Content-Type": upstreamRes.headers.get("content-type") ?? "application/json" },
  });
}

export const maxDuration = 60;

export const GET = (req: NextRequest, ctx: { params: Promise<{ path: string[] }> }) =>
  proxy(req, ctx.params);
export const POST = (req: NextRequest, ctx: { params: Promise<{ path: string[] }> }) =>
  proxy(req, ctx.params);
export const PATCH = (req: NextRequest, ctx: { params: Promise<{ path: string[] }> }) =>
  proxy(req, ctx.params);
export const DELETE = (req: NextRequest, ctx: { params: Promise<{ path: string[] }> }) =>
  proxy(req, ctx.params);
