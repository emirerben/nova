"use client";

import { SessionProvider } from "next-auth/react";

/**
 * Client-side providers wrapper. Hosts NextAuth's SessionProvider so the Header
 * and the /plan wizard can read auth state via `useSession()` instead of only
 * discovering it through a 401 on the first API call.
 */
export default function Providers({ children }: { children: React.ReactNode }) {
  return <SessionProvider>{children}</SessionProvider>;
}
