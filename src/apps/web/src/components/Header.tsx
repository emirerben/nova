"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { signIn, signOut, useSession } from "next-auth/react";
import { useEffect, useRef, useState } from "react";

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

  const isLight = pathname === "/" || (pathname.startsWith("/plan") && !pathname.startsWith("/plan/items/"));

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
          aria-label="Nova — home"
          className={`font-semibold tracking-tight ${isLight ? "text-[#0c0c0e]" : "text-white"}`}
        >
          Nova
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
        <div className="absolute right-0 mt-2 w-44 overflow-hidden rounded-lg border border-zinc-800 bg-zinc-950 py-1 shadow-xl">
          <p className="truncate px-3 py-2 text-xs text-zinc-500">{name}</p>
          <Link
            href="/plan"
            onClick={() => setOpen(false)}
            className="block px-3 py-2 text-sm text-zinc-200 hover:bg-zinc-900"
          >
            My plan
          </Link>
          <Link
            href="/library"
            onClick={() => setOpen(false)}
            className="block px-3 py-2 text-sm text-zinc-200 hover:bg-zinc-900"
          >
            My videos
          </Link>
          <button
            onClick={() => signOut({ callbackUrl: "/" })}
            className="block w-full px-3 py-2 text-left text-sm text-zinc-400 hover:bg-zinc-900 hover:text-white"
          >
            Sign out
          </button>
        </div>
      )}
    </div>
  );
}
