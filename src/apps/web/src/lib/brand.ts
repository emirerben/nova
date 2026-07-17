export const BRAND_NAME = "Kria";
// usekria.com apex 308-redirects to www.usekria.com (Vercel domain config), so the
// origin every real browser actually sends is the `www` form. CANONICAL_WEB_ORIGIN
// must match that, since it's also consumed as an allowed-origin check (see
// middleware.ts ALLOWED_ORIGINS), not just as metadata. APEX_WEB_ORIGIN is kept
// alongside it for allowlist consumers so the pre-redirect origin isn't rejected.
export const CANONICAL_WEB_ORIGIN = "https://www.usekria.com";
export const APEX_WEB_ORIGIN = "https://usekria.com";
export const LEGACY_WEB_ORIGIN = "https://nova-video.vercel.app";
export const LOCAL_WEB_ORIGIN = "http://localhost:3000";
export const ADMIN_REALM = `${BRAND_NAME} Admin`;
