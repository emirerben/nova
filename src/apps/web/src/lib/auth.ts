import type { NextAuthOptions } from "next-auth";
import GoogleProvider from "next-auth/providers/google";

const API_BASE =
  process.env.API_URL ?? process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
const INTERNAL_API_KEY = process.env.INTERNAL_API_KEY ?? "";

export const authOptions: NextAuthOptions = {
  providers: [
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
  ],
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
            (user as Record<string, unknown>).dbId = data.user_id;
          }
        } catch {
          // Non-fatal: the user can still sign in; plan routes will 401.
        }
      }
      return true;
    },

    async jwt({ token, user }) {
      // On initial sign-in `user` is populated; persist dbId into the token.
      if (user && (user as Record<string, unknown>).dbId) {
        token.userId = (user as Record<string, unknown>).dbId as string;
      }
      return token;
    },

    async session({ session, token }) {
      if (session.user) {
        (session.user as Record<string, unknown>).id = token.userId ?? token.sub ?? "";
      }
      return session;
    },
  },
  session: {
    strategy: "jwt",
  },
};
