"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";

import { BRAND_NAME } from "@/lib/brand";
import KriaWordmark from "@/components/KriaWordmark";

// The web editor was removed (KRI-1398); creation happens in the iPhone app.
// The web keeps the landing page, the legal pages and /admin, so the header is
// just the wordmark: there is nothing to sign in to here.
export default function Header() {
  const pathname = usePathname() ?? "";
  const [isScrollComparison, setIsScrollComparison] = useState(false);

  useEffect(() => {
    setIsScrollComparison(
      pathname === "/" &&
        new URLSearchParams(window.location.search).get("mode") === "scroll",
    );
  }, [pathname]);

  if (pathname.startsWith("/admin")) return null;
  if (pathname === "/" && !isScrollComparison) return null;

  return (
    <header className="z-40 h-14 bg-[#ffffff]">
      <div className="mx-auto flex h-full max-w-6xl items-center px-4">
        <Link
          href="/"
          aria-label={`${BRAND_NAME} — home`}
          className="flex items-center text-[#9BCAFF]"
        >
          <KriaWordmark className="h-7 w-[76px] text-[24px]" />
        </Link>
      </div>
    </header>
  );
}
