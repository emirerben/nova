"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { signIn, signOut, useSession } from "next-auth/react";
import { useEffect, useRef, useState } from "react";

import { BRAND_NAME } from "@/lib/brand";
import { resetPersona } from "@/lib/plan-api";

export default function Header() {
  const pathname = usePathname() ?? "";
  const isAdmin = pathname.startsWith("/admin");
  const { status: authStatus } = useSession();
  const [progress, setProgress] = useState(0);

  useEffect(() => {
    if (isAdmin) return;
    let raf = 0;
    const update = () => {
      raf = 0;
      setProgress(Math.min(window.scrollY / 80, 1));
    };
    const onScroll = () => {
      if (!raf) raf = requestAnimationFrame(update);
    };
    update();
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => {
      window.removeEventListener("scroll", onScroll);
      if (raf) cancelAnimationFrame(raf);
    };
  }, [isAdmin]);

  if (isAdmin) return null;

  // Light surfaces: landing + all plan pages (incl. /plan/items) + library + generative.
  // Dark: template render job flow (/template-jobs) and /admin (early-return above).
  const isLight =
    pathname === "/" ||
    pathname.startsWith("/plan") ||
    pathname.startsWith("/library") ||
    pathname.startsWith("/generative");

  return (
    <header
      className={`z-40 h-14 ${isLight ? "bg-[#fafaf8] border-b border-zinc-200/70" : "sticky top-0"}`}
      style={
        isLight
          ? {}
          : {
              backgroundColor: `rgba(0, 0, 0, ${0.6 * progress})`,
              backdropFilter: `blur(${12 * progress}px)`,
              WebkitBackdropFilter: `blur(${12 * progress}px)`,
            }
      }
    >
      <div className="mx-auto flex h-full max-w-6xl items-center justify-between px-4">
        <Link
          href="/"
          aria-label={`${BRAND_NAME} — home`}
          className={`font-semibold tracking-tight ${isLight ? "text-[#0c0c0e]" : "text-white"}`}
        >
          {BRAND_NAME}
        </Link>
        <nav className="flex items-center gap-2 sm:gap-4">
          {authStatus === "authenticated" && (
            <Link
              href="/plan"
              className={`text-sm transition-colors ${
                isLight
                  ? `hover:text-[#0c0c0e] ${pathname.startsWith("/plan") ? "text-[#0c0c0e]" : "text-[#71717a]"}`
                  : `hover:text-white ${pathname.startsWith("/plan") ? "text-white" : "text-zinc-400"}`
              }`}
            >
              Plan
            </Link>
          )}
          {authStatus === "authenticated" && (
            <Link
              href="/library"
              className={`text-sm transition-colors ${
                isLight
                  ? `hover:text-[#0c0c0e] ${pathname.startsWith("/library") ? "text-[#0c0c0e]" : "text-[#71717a]"}`
                  : `hover:text-white ${pathname.startsWith("/library") ? "text-white" : "text-zinc-400"}`
              }`}
            >
              Library
            </Link>
          )}
          <AuthControl isLight={isLight} />
        </nav>
      </div>
    </header>
  );
}

function AuthControl({ isLight = false }: { isLight?: boolean }) {
  const { data: session, status } = useSession();
  const [open, setOpen] = useState(false);
  const [signingIn, setSigningIn] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [resetting, setResetting] = useState(false);
  const [resetError, setResetError] = useState<string | null>(null);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    function onClick(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    }
    document.addEventListener("mousedown", onClick);
    return () => document.removeEventListener("mousedown", onClick);
  }, []);

  if (status === "loading") {
    return (
      <div
        className={`h-8 w-8 motion-safe:animate-pulse rounded-full ${isLight ? "bg-zinc-200" : "bg-zinc-800"}`}
      />
    );
  }

  if (!session?.user) {
    return (
      <button
        onClick={() => {
          setSigningIn(true);
          // signIn redirects away on success; if it returns (popup blocked,
          // back button) the component is still mounted so re-enable the button.
          void signIn("google", { callbackUrl: "/plan" }).finally(() =>
            setSigningIn(false),
          );
        }}
        disabled={signingIn}
        className={`rounded-full border px-4 py-1.5 text-sm font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-60 ${
          isLight
            ? "border-zinc-300 text-[#0c0c0e] hover:border-zinc-500"
            : "border-zinc-700 text-zinc-200 hover:border-zinc-400 hover:text-white"
        }`}
      >
        {signingIn ? "Signing in…" : "Sign in"}
      </button>
    );
  }

  const name = session.user.name ?? session.user.email ?? "You";
  const image = session.user.image ?? null;
  const initial = name.trim().charAt(0).toUpperCase() || "Y";

  return (
    <div ref={ref} className="relative">
      <button
        onClick={() => setOpen((v) => !v)}
        aria-label="Account menu"
        className={`flex h-8 w-8 items-center justify-center overflow-hidden rounded-full border text-sm font-medium transition-colors ${isLight ? "border-zinc-300 bg-lime-600 text-white hover:border-zinc-400" : "border-zinc-700 bg-zinc-800 text-zinc-200 hover:border-zinc-400"}`}
      >
        {image ? (
          // eslint-disable-next-line @next/next/no-img-element
          <img src={image} alt="" className="h-full w-full object-cover" />
        ) : (
          initial
        )}
      </button>
      {open && (
        <div className={`absolute right-0 mt-2 w-44 overflow-hidden rounded-lg border py-1 shadow-xl ${isLight ? "border-zinc-200 bg-white" : "border-zinc-800 bg-zinc-950"}`}>
          <p className={`truncate px-3 py-2 text-xs ${isLight ? "text-[#a1a1aa]" : "text-zinc-500"}`}>{name}</p>
          <Link
            href="/plan"
            onClick={() => setOpen(false)}
            className={`block px-3 py-2 text-sm ${isLight ? "text-[#3f3f46] hover:bg-[#fafaf8]" : "text-zinc-200 hover:bg-zinc-900"}`}
          >
            My plan
          </Link>
          <Link
            href="/library"
            onClick={() => setOpen(false)}
            className={`block px-3 py-2 text-sm ${isLight ? "text-[#3f3f46] hover:bg-[#fafaf8]" : "text-zinc-200 hover:bg-zinc-900"}`}
          >
            My videos
          </Link>
          <Link
            href="/plan/persona"
            onClick={() => setOpen(false)}
            className={`block px-3 py-2 text-sm ${isLight ? "text-[#3f3f46] hover:bg-[#fafaf8]" : "text-zinc-200 hover:bg-zinc-900"}`}
          >
            Your persona
          </Link>
          {!confirming ? (
            <button
              onClick={() => {
                setResetError(null);
                setConfirming(true);
              }}
              className={`block w-full px-3 py-2 text-left text-sm ${isLight ? "text-[#71717a] hover:bg-[#fafaf8] hover:text-[#0c0c0e]" : "text-zinc-400 hover:bg-zinc-900 hover:text-white"}`}
            >
              Start over
            </button>
          ) : (
            <div className="px-3 py-2">
              <p className={`mb-1.5 text-xs ${isLight ? "text-[#71717a]" : "text-zinc-400"}`}>
                Deletes your plan &amp; persona — keeps your videos.
              </p>
              <div className="flex gap-2">
                <button
                  onClick={async () => {
                    setResetting(true);
                    setResetError(null);
                    try {
                      await resetPersona();
                      window.location.assign("/plan");
                    } catch (err) {
                      setResetError(err instanceof Error ? err.message : "Reset failed");
                      setResetting(false);
                    }
                  }}
                  disabled={resetting}
                  className="rounded px-2 py-1 text-xs font-medium bg-red-600 text-white hover:bg-red-700 disabled:opacity-60 disabled:cursor-not-allowed"
                >
                  {resetting ? "Resetting…" : "Yes, start over"}
                </button>
                <button
                  onClick={() => {
                    setConfirming(false);
                    setResetError(null);
                  }}
                  disabled={resetting}
                  className={`rounded px-2 py-1 text-xs font-medium disabled:opacity-60 ${isLight ? "text-[#3f3f46] hover:bg-[#fafaf8]" : "text-zinc-300 hover:bg-zinc-800"}`}
                >
                  Cancel
                </button>
              </div>
              {resetError && (
                <p className="mt-1 text-xs text-red-500">{resetError}</p>
              )}
            </div>
          )}
          <button
            onClick={() => signOut({ callbackUrl: "/" })}
            className={`block w-full px-3 py-2 text-left text-sm ${isLight ? "text-[#71717a] hover:bg-[#fafaf8] hover:text-[#0c0c0e]" : "text-zinc-400 hover:bg-zinc-900 hover:text-white"}`}
          >
            Sign out
          </button>
        </div>
      )}
    </div>
  );
}
