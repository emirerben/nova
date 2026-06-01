import type { NextAuthOptions } from "next-auth";
import CredentialsProvider from "next-auth/providers/credentials";
import GoogleProvider from "next-auth/providers/google";

const API_BASE =
  process.env.API_URL ?? process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
const INTERNAL_API_KEY = process.env.INTERNAL_API_KEY ?? "";

// DEV-ONLY email login, gated entirely behind ALLOW_DEV_LOGIN. It exists so the
// content-plan flow (which is Google-gated) can be exercised end-to-end in local
// dev + automated QA without an interactive Google consent. It is NEVER enabled
// in production: ALLOW_DEV_LOGIN must stay unset in Vercel/Fly. When the flag is
// off, this provider is not added at all (see `auth-dev-login.test.ts`).
//
// SECURITY: this lets anyone who can reach the server mint a session for any
// email. That is acceptable only because the flag is off everywhere except a
// developer's localhost. Do not set ALLOW_DEV_LOGIN in any shared environment.
const DEV_LOGIN_ENABLED = process.env.ALLOW_DEV_LOGIN === "true";

// Hard fail-safe: the comment above is not enough. If ALLOW_DEV_LOGIN ever leaks
// into a production build, refuse to boot rather than silently exposing a
// session-minting backdoor for any email. NODE_ENV is "production" on Vercel/Fly,
// "test" under Jest, and "development" locally — so this only fires where it must.
if (DEV_LOGIN_ENABLED && process.env.NODE_ENV === "production") {
  throw new Error(
    "ALLOW_DEV_LOGIN must never be set in production — it lets anyone mint a session for any email.",
  );
}

const providers: NextAuthOptions["providers"] = [
  GoogleProvider({
    // GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET are the canonical names.
    // The old YOUTUBE_CLIENT_ID/SECRET still work as a fallback so
    // existing deployments don't break until secrets are renamed.
    clientId:
      process.env.GOOGLE_CLIENT_ID ?? process.env.YOUTUBE_CLIENT_ID ?? "",
    clientSecret:
      process.env.GOOGLE_CLIENT_SECRET ?? process.env.YOUTUBE_CLIENT_SECRET ?? "",
    authorization: {
      params: {
        // openid + email + profile is sufficient for identity.
        // youtube.upload was here previously but requires Google app
        // verification and is not needed for the content-plan feature.
        scope: "openid email profile",
      },
    },
  }),
];

if (DEV_LOGIN_ENABLED) {
  providers.push(
    CredentialsProvider({
      id: "dev-login",
      name: "Dev login (local only)",
      credentials: { email: { label: "Email", type: "email" } },
      // Mirror the Google path: upsert the user in Nova's DB and hand the UUID
      // back as `dbId` so the jwt callback embeds it just like a Google sign-in.
      async authorize(credentials) {
        const email = credentials?.email?.trim();
        if (!email) return null;
        try {
          const res = await fetch(`${API_BASE}/auth/google-upsert`, {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              Authorization: `Bearer ${INTERNAL_API_KEY}`,
            },
            body: JSON.stringify({ email, name: email.split("@")[0] }),
          });
          if (!res.ok) return null;
          const data = (await res.json()) as { user_id: string };
          return { id: data.user_id, email, name: email.split("@")[0], dbId: data.user_id };
        } catch {
          return null;
        }
      },
    }),
  );
}

export const authOptions: NextAuthOptions = {
  providers,
  callbacks: {
    async signIn({ user, account }) {
      // On first sign-in: upsert the user in the Nova DB and store the UUID.
      if (account?.provider === "google" && user.email) {
        try {
          const res = await fetch(`${API_BASE}/auth/google-upsert`, {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              Authorization: `Bearer ${INTERNAL_API_KEY}`,
            },
            body: JSON.stringify({ email: user.email, name: user.name ?? null }),
          });
          if (res.ok) {
            const data = (await res.json()) as { user_id: string };
            // Attach DB uuid to the user object so jwt callback can embed it.
            (user as unknown as Record<string, unknown>).dbId = data.user_id;
          }
        } catch {
          // Non-fatal: the user can still sign in; plan routes will 401.
        }
      }
      return true;
    },

    async jwt({ token, user }) {
      // On initial sign-in `user` is populated; persist dbId into the token.
      if (user && (user as unknown as Record<string, unknown>).dbId) {
        token.userId = (user as unknown as Record<string, unknown>).dbId as string;
      }
      return token;
    },

    async session({ session, token }) {
      if (session.user) {
        // Use ONLY the Nova DB uuid (token.userId). Do NOT fall back to
        // token.sub: that is the raw Google OAuth id, which the backend rejects
        // as a non-UUID anyway — forwarding it just masks a failed google-upsert
        // behind a confusing 401 instead of an empty (clearly unauthenticated) id.
        (session.user as unknown as Record<string, unknown>).id = token.userId ?? "";
      }
      return session;
    },
  },
  session: {
    strategy: "jwt",
  },
};
