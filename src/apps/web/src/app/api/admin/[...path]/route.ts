/**
 * Next.js API proxy for admin/music-tracks endpoints.
 *
 * Reads ADMIN_TOKEN server-side (never exposed in the browser bundle).
 * The frontend uses /api/admin/... instead of calling the FastAPI admin
 * endpoints directly, so NEXT_PUBLIC_ADMIN_TOKEN is no longer needed.
 */

import { type NextRequest, NextResponse } from "next/server";

const API_BASE = process.env.API_URL ?? "http://localhost:8000";
const ADMIN_TOKEN = process.env.ADMIN_TOKEN ?? "";

async function proxy(
  req: NextRequest,
  params: Promise<{ path: string[] }>,
): Promise<NextResponse> {
  const { path } = await params;
  const upstream = `${API_BASE}/admin/${path.join("/")}`;

  const headers: Record<string, string> = {
    "X-Admin-Token": ADMIN_TOKEN,
  };
  const contentType = req.headers.get("content-type");
  if (contentType) headers["Content-Type"] = contentType;

  const body =
    req.method !== "GET" && req.method !== "HEAD"
      ? await req.arrayBuffer()
      : undefined;

  const upstreamRes = await fetch(upstream, {
    method: req.method,
    headers,
    body: body ? Buffer.from(body) : undefined,
  });

  const resBody = await upstreamRes.arrayBuffer();
  return new NextResponse(resBody, {
    status: upstreamRes.status,
    headers: { "Content-Type": upstreamRes.headers.get("content-type") ?? "application/json" },
  });
}

export const GET = (req: NextRequest, ctx: { params: Promise<{ path: string[] }> }) =>
  proxy(req, ctx.params);
export const POST = (req: NextRequest, ctx: { params: Promise<{ path: string[] }> }) =>
  proxy(req, ctx.params);
export const PATCH = (req: NextRequest, ctx: { params: Promise<{ path: string[] }> }) =>
  proxy(req, ctx.params);
export const DELETE = (req: NextRequest, ctx: { params: Promise<{ path: string[] }> }) =>
  proxy(req, ctx.params);
