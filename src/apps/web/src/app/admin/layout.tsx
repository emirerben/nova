"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import {
  adminValidateToken,
  clearAdminToken,
  getAdminToken,
  setAdminToken,
} from "@/lib/admin-api";

// ── Font face declarations for all registry fonts ────────────────────────────
// Admin-only (2 users), so eager loading is fine. TTF files served from /fonts/.
const FONT_FACES = `
@font-face {
  font-family: 'Inter';
  src: url('/fonts/Inter-Regular.ttf') format('truetype');
  font-weight: 400;
  font-display: swap;
}
@font-face {
  font-family: 'Inter';
  src: url('/fonts/Inter-Medium.ttf') format('truetype');
  font-weight: 500;
  font-display: swap;
}
@font-face {
  font-family: 'Playfair Display';
  src: url('/fonts/PlayfairDisplay-Bold.ttf') format('truetype');
  font-weight: 700;
  font-display: swap;
}
@font-face {
  font-family: 'Playfair Display';
  src: url('/fonts/PlayfairDisplay-Regular.ttf') format('truetype');
  font-weight: 400;
  font-display: swap;
}
@font-face {
  font-family: 'Montserrat';
  src: url('/fonts/Montserrat-ExtraBold.ttf') format('truetype');
  font-weight: 800;
  font-display: swap;
}
@font-face {
  font-family: 'Space Grotesk';
  src: url('/fonts/SpaceGrotesk-Bold.ttf') format('truetype');
  font-weight: 700;
  font-display: swap;
}
@font-face {
  font-family: 'DM Sans';
  src: url('/fonts/DMSans-Bold.ttf') format('truetype');
  font-weight: 700;
  font-display: swap;
}
@font-face {
  font-family: 'Instrument Serif';
  src: url('/fonts/InstrumentSerif-Regular.ttf') format('truetype');
  font-weight: 400;
  font-display: swap;
}
@font-face {
  font-family: 'Bodoni Moda';
  src: url('/fonts/BodoniModa-Bold.ttf') format('truetype');
  font-weight: 700;
  font-display: swap;
}
@font-face {
  font-family: 'Fraunces';
  src: url('/fonts/Fraunces-Bold.ttf') format('truetype');
  font-weight: 700;
  font-display: swap;
}
@font-face {
  font-family: 'Space Mono';
  src: url('/fonts/SpaceMono-Bold.ttf') format('truetype');
  font-weight: 700;
  font-display: swap;
}
@font-face {
  font-family: 'Outfit';
  src: url('/fonts/Outfit-Bold.ttf') format('truetype');
  font-weight: 700;
  font-display: swap;
}
@font-face {
  font-family: 'Permanent Marker';
  src: url('/fonts/PermanentMarker-Regular.ttf') format('truetype');
  font-weight: 400;
  font-display: swap;
}
@font-face {
  font-family: 'Pacifico';
  src: url('/fonts/Pacifico-Regular.ttf') format('truetype');
  font-weight: 400;
  font-display: swap;
}
`;

/**
 * Admin layout: wraps all /admin pages with auth gate + nav.
 *
 * On first visit, shows a token prompt. Token is stored in sessionStorage
 * (clears on tab close). Validated by making a lightweight API call.
 */
export default function AdminLayout({ children }: { children: React.ReactNode }) {
  const [authed, setAuthed] = useState<boolean | null>(null); // null = checking

  useEffect(() => {
    const token = getAdminToken();
    if (!token) {
      setAuthed(false);
      return;
    }
    adminValidateToken().then(setAuthed);
  }, []);

  if (authed === null) {
    return (
      <main className="min-h-screen bg-black text-white flex items-center justify-center">
        <div className="w-6 h-6 border-2 border-zinc-600 border-t-white rounded-full animate-spin" />
      </main>
    );
  }

  if (!authed) {
    return <AuthGate onAuth={() => setAuthed(true)} />;
  }

  return (
    <div className="min-h-screen bg-black text-white flex flex-col">
      {/* eslint-disable-next-line react/no-danger -- static font-face CSS for admin */}
      <style dangerouslySetInnerHTML={{ __html: FONT_FACES }} />
      <AdminNav onLogout={() => { clearAdminToken(); setAuthed(false); }} />
      {children}
    </div>
  );
}

// ── Auth Gate ──────────────────────────────────────────────────────────────────

function AuthGate({ onAuth }: { onAuth: () => void }) {
  const [token, setToken] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [checking, setChecking] = useState(false);

  const handleSubmit = useCallback(
    async (e: React.FormEvent) => {
      e.preventDefault();
      if (!token.trim()) return;
      setChecking(true);
      setError(null);

      setAdminToken(token.trim());
      const valid = await adminValidateToken();

      if (valid) {
        onAuth();
      } else {
        clearAdminToken();
        setError("Invalid admin token");
      }
      setChecking(false);
    },
    [token, onAuth],
  );

  return (
    <main className="min-h-screen bg-black text-white flex items-center justify-center px-4">
      <form onSubmit={handleSubmit} className="w-full max-w-sm space-y-4">
        <h1 className="text-xl font-semibold text-center">Nova Admin</h1>
        <p className="text-sm text-zinc-500 text-center">Enter your admin token to continue</p>
        <input
          type="password"
          value={token}
          onChange={(e) => setToken(e.target.value)}
          placeholder="Admin token"
          autoFocus
          className="w-full bg-zinc-900 border border-zinc-700 rounded px-3 py-2.5 text-white text-sm focus:outline-none focus:border-zinc-500"
        />
        {error && <p className="text-red-400 text-sm text-center">{error}</p>}
        <button
          type="submit"
          disabled={checking || !token.trim()}
          className="w-full py-2.5 text-sm bg-white text-black rounded font-medium hover:bg-zinc-200 disabled:opacity-50"
        >
          {checking ? "Checking..." : "Sign In"}
        </button>
      </form>
    </main>
  );
}

// ── Nav ────────────────────────────────────────────────────────────────────────

function AdminNav({ onLogout }: { onLogout: () => void }) {
  const pathname = usePathname();

  return (
    <nav className="border-b border-zinc-800 px-6 py-3 flex items-center justify-between">
      <div className="flex items-center gap-6">
        <Link href="/admin" className="text-sm font-semibold text-white">
          Nova Admin
        </Link>
        <div className="flex items-center gap-1">
          <NavLink href="/admin" active={pathname === "/admin"}>
            Dashboard
          </NavLink>
          <NavLink href="/admin/templates/new" active={pathname === "/admin/templates/new"}>
            New Template
          </NavLink>
        </div>
      </div>
      <button onClick={onLogout} className="text-xs text-zinc-500 hover:text-zinc-300">
        Sign Out
      </button>
    </nav>
  );
}

function NavLink({
  href,
  active,
  children,
}: {
  href: string;
  active: boolean;
  children: React.ReactNode;
}) {
  return (
    <Link
      href={href}
      className={`px-3 py-1.5 text-sm rounded transition-colors ${
        active ? "bg-zinc-800 text-white" : "text-zinc-500 hover:text-zinc-300"
      }`}
    >
      {children}
    </Link>
  );
}
