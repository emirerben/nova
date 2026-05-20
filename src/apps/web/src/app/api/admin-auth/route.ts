/**
 * Admin gate validation.
 *
 * Compares a typed token to the server-side `ADMIN_TOKEN` env var so the
 * AuthGate UX can decide whether to unlock the admin shell. The actual
 * upstream API calls go through `/api/admin/[...path]`, which injects the
 * same server-side token — the browser never sees or sends it.
 */

import { timingSafeEqual } from "node:crypto";
import { type NextRequest, NextResponse } from "next/server";

const ADMIN_TOKEN = process.env.ADMIN_TOKEN ?? "";

export async function POST(req: NextRequest): Promise<NextResponse> {
  if (!ADMIN_TOKEN) {
    return NextResponse.json(
      { detail: "ADMIN_TOKEN not configured on the web server" },
      { status: 500 },
    );
  }

  let body: { token?: unknown };
  try {
    body = await req.json();
  } catch {
    return NextResponse.json({ detail: "Invalid JSON" }, { status: 400 });
  }

  const candidate = typeof body.token === "string" ? body.token : "";
  const a = Buffer.from(candidate);
  const b = Buffer.from(ADMIN_TOKEN);
  const ok = a.length === b.length && timingSafeEqual(a, b);

  return NextResponse.json({ ok }, { status: ok ? 200 : 401 });
}
