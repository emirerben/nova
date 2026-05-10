"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";

export default function Header() {
  const pathname = usePathname() ?? "";
  const isAdmin = pathname.startsWith("/admin");
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

  return (
    <header
      className="sticky top-0 z-40 h-14"
      style={{
        backgroundColor: `rgba(0, 0, 0, ${0.6 * progress})`,
        backdropFilter: `blur(${12 * progress}px)`,
        WebkitBackdropFilter: `blur(${12 * progress}px)`,
      }}
    >
      <div className="h-full max-w-6xl mx-auto px-4 flex items-center">
        <Link
          href="/"
          aria-label="Nova — home"
          className="text-white font-semibold tracking-tight"
        >
          Nova
        </Link>
      </div>
    </header>
  );
}
