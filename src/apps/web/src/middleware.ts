import { NextResponse, type NextRequest } from "next/server";
import {
  ADMIN_REALM,
  APEX_WEB_ORIGIN,
  CANONICAL_WEB_ORIGIN,
  LEGACY_WEB_ORIGIN,
  LOCAL_WEB_ORIGIN,
} from "@/lib/brand";

// SECURITY: Stopgap shared-secret BasicAuth gate for /admin/* and /api/admin/*.
// The Vercel deployment is "Deployment Protection: preview-only (production is public)"
// (see CLAUDE.md), and the admin proxy at /api/admin/[...path] injects ADMIN_TOKEN
// unconditionally on every proxied call — so without this middleware every admin page
// and admin API endpoint is publicly reachable. This middleware is a stopgap until
// per-user auth replaces it; see follow-ups in
// ~/.claude/plans/when-i-say-install-partitioned-breeze.md.
//
// Also enforces a same-origin CSRF guard on /api/admin/* mutating methods. CORS is
// not an auth boundary — BasicAuth only protects against unauthenticated callers,
// not authenticated-browser CSRF.

// Edge runtime reads process.env at module init. Rotating these secrets
// requires a redeploy (Vercel) / restart (Fly) — there is no runtime
// refresh path, and that's intentional for shared-secret simplicity.
const USER = process.env.ADMIN_BASIC_AUTH_USER ?? "";
const PASS = process.env.ADMIN_BASIC_AUTH_PASSWORD ?? "";

const ALLOWED_ORIGINS = new Set<string>([
  CANONICAL_WEB_ORIGIN,
  APEX_WEB_ORIGIN,
  LEGACY_WEB_ORIGIN,
  LOCAL_WEB_ORIGIN,
]);

const MUTATING_METHODS = new Set<string>(["POST", "PUT", "PATCH", "DELETE"]);

function timingSafeEqualStr(a: string, b: string): boolean {
  // Constant-time string compare for the Edge runtime. No crypto.timingSafeEqual
  // there, so iterate max(len(a), len(b)) always and fold the length difference
  // into the result. Avoids early-exit on length mismatch.
  const lenA = a.length;
  const lenB = b.length;
  const maxLen = lenA > lenB ? lenA : lenB;
  let diff = lenA ^ lenB;
  for (let i = 0; i < maxLen; i++) {
    const ca = i < lenA ? a.charCodeAt(i) : 0;
    const cb = i < lenB ? b.charCodeAt(i) : 0;
    diff |= ca ^ cb;
  }
  return diff === 0;
}

function unauthorized(): NextResponse {
  return new NextResponse("Authentication required", {
    status: 401,
    headers: {
      "WWW-Authenticate": `Basic realm="${ADMIN_REALM}", charset="UTF-8"`,
      "Cache-Control": "no-store",
    },
  });
}

// UTF-8-aware base64 decode for BasicAuth payloads.
// RFC 7617 §2.1 defaults the charset to UTF-8. `atob` alone returns a
// Latin-1 string where every UTF-8 byte becomes one Latin-1 char, so a
// password like `pässwørd` would never match the UTF-8 env var on this
// server. The Edge runtime ships TextDecoder; this round-trips via
// Uint8Array to get bytes → UTF-8 string.
function decodeUtf8Base64(b64: string): string {
  const bin = atob(b64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return new TextDecoder("utf-8", { fatal: false }).decode(bytes);
}

function checkBasicAuth(req: NextRequest): NextResponse | null {
  const auth = req.headers.get("authorization");
  if (!auth || !auth.toLowerCase().startsWith("basic ")) {
    return unauthorized();
  }
  let decoded: string;
  try {
    decoded = decodeUtf8Base64(auth.slice(6).trim());
  } catch {
    return unauthorized();
  }
  const sep = decoded.indexOf(":");
  if (sep < 0) return unauthorized();
  const u = decoded.slice(0, sep);
  const p = decoded.slice(sep + 1);
  if (!timingSafeEqualStr(u, USER) || !timingSafeEqualStr(p, PASS)) {
    return unauthorized();
  }
  return null;
}

function isAllowedOriginValue(origin: string): boolean {
  if (ALLOWED_ORIGINS.has(origin)) return true;
  // chrome-extension://* — interim broad allowance. manifest.key is deferred so
  // unpacked installs get random IDs; Phase-2 distribution will pin this to a
  // deterministic ID.
  return origin.startsWith("chrome-extension://");
}

function checkCsrf(req: NextRequest): NextResponse | null {
  if (!MUTATING_METHODS.has(req.method)) return null;
  if (!req.nextUrl.pathname.startsWith("/api/admin/")) return null;
  const origin = req.headers.get("origin");
  if (origin) {
    if (!isAllowedOriginValue(origin)) {
      return new NextResponse("Forbidden: disallowed origin", { status: 403 });
    }
    return null;
  }
  // No Origin header: fall back to Sec-Fetch-Site. Browsers send this on
  // every fetch; "same-origin" or "none" (top-level navigation) is acceptable.
  const sfs = req.headers.get("sec-fetch-site");
  if (sfs === "same-origin" || sfs === "none") return null;
  return new NextResponse("Forbidden: missing Origin / Sec-Fetch-Site", {
    status: 403,
  });
}

export function middleware(req: NextRequest): NextResponse {
  if (!USER || !PASS) {
    // Fail closed. ADMIN_BASIC_AUTH_USER / ADMIN_BASIC_AUTH_PASSWORD must be set.
    return new NextResponse(
      "Admin auth not configured (ADMIN_BASIC_AUTH_USER / ADMIN_BASIC_AUTH_PASSWORD)",
      { status: 503, headers: { "Cache-Control": "no-store" } },
    );
  }
  const authFail = checkBasicAuth(req);
  if (authFail) return authFail;
  const csrfFail = checkCsrf(req);
  if (csrfFail) return csrfFail;
  return NextResponse.next();
}

export const config = {
  matcher: ["/admin/:path*", "/api/admin/:path*"],
};
