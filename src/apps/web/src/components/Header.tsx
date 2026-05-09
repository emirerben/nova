"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

export default function Header() {
  const pathname = usePathname() ?? "";

  // Admin pages render their own nav; don't double-stack headers there.
  if (pathname.startsWith("/admin")) return null;

  return (
    <header className="sticky top-0 z-40 h-14 border-b border-zinc-900 bg-black/80 backdrop-blur supports-[backdrop-filter]:bg-black/60">
      <div className="h-full max-w-6xl mx-auto px-4 flex items-center justify-between">
        <Link
          href="/"
          aria-label="Nova — home"
          className="text-white font-semibold tracking-tight"
        >
          Nova
        </Link>
        <button
          type="button"
          className="text-xs text-zinc-400 hover:text-white transition-colors"
          aria-label="Sign in"
        >
          Sign in
        </button>
      </div>
    </header>
  );
}
