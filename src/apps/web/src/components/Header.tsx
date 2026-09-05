"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { signIn, signOut, useSession } from "next-auth/react";
import { useEffect, useState, useSyncExternalStore } from "react";

import { BRAND_NAME } from "@/lib/brand";
import KriaMark from "@/components/KriaMark";
import {
  CHAT_FIRST_CREATION_ENABLED,
  getChatFirstFallback,
  setChatFirstFallback,
  subscribeChatFirstFallback,
} from "@/lib/chat-first";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";

/** Canonical chat-first project routes, excluding the other plan surfaces. */
export function isChatFirstPlanPath(pathname: string): boolean {
  if (pathname === "/plan") return true;
  if (!pathname.startsWith("/plan/")) return false;
  return !["items", "new", "persona", "style"].some((segment) =>
    pathname === `/plan/${segment}` || pathname.startsWith(`/plan/${segment}/`),
  );
}

export default function Header() {
  const pathname = usePathname() ?? "";
  const { status } = useSession();
  const isAdmin = pathname.startsWith("/admin");
  const chatFallback = useSyncExternalStore(
    subscribeChatFirstFallback,
    getChatFirstFallback,
    () => false,
  );
  const isChatFirstWorkspace =
    pathname === "/dev-qa/chat-first-creation" ||
    (isChatFirstPlanPath(pathname) && CHAT_FIRST_CREATION_ENABLED &&
      !chatFallback &&
      status !== "unauthenticated");
  const isLanding = pathname === "/" || pathname === "/auto-story";
  const [progress, setProgress] = useState(0);

  useEffect(() => {
    const handleFallback = () => setChatFirstFallback(true);
    const handleReady = () => setChatFirstFallback(false);
    window.addEventListener("nova:chat-first-fallback", handleFallback);
    window.addEventListener("nova:chat-first-ready", handleReady);
    return () => {
      window.removeEventListener("nova:chat-first-fallback", handleFallback);
      window.removeEventListener("nova:chat-first-ready", handleReady);
    };
  }, []);

  useEffect(() => {
    if (!isChatFirstPlanPath(pathname)) setChatFirstFallback(false);
  }, [pathname]);

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
  if (isChatFirstWorkspace) return null;

  // Light surfaces: landing variants + all plan pages (incl. /plan/items) + library + TikTok + generative
  // + the static legal pages (cream canvas, would clash with the dark sticky header).
  // Dark: template render job flow (/template-jobs) and /admin (early-return above).
  const isLight =
    isLanding ||
    pathname.startsWith("/plan") ||
    pathname.startsWith("/create") ||
    pathname.startsWith("/library") ||
    pathname.startsWith("/tiktok") ||
    pathname.startsWith("/generative") ||
    pathname === "/terms" ||
    pathname === "/privacy";

  return (
    <header
      className={`z-40 h-14 ${
        isLight ? "bg-[#ffffff]" : "sticky top-0"
      }`}
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
          className={`flex items-center gap-2 font-semibold tracking-tight ${isLight ? "text-[#0c0c0e]" : "text-white"}`}
        >
          <KriaMark
            className={`h-[22px] w-auto ${isLight ? "text-lime-600" : "text-white"}`}
          />
          {BRAND_NAME}
        </Link>
        <nav className="flex items-center gap-2 sm:gap-4">
          <AuthControl isLight={isLight} isLanding={isLanding} />
        </nav>
      </div>
    </header>
  );
}

function AuthControl({
  isLight = false,
  isLanding = false,
}: {
  isLight?: boolean;
  isLanding?: boolean;
}) {
  const { data: session, status } = useSession();
  const [signingIn, setSigningIn] = useState(false);

  if (status === "loading") {
    return (
      <div
        className={`h-8 w-8 motion-safe:animate-pulse rounded-full ${isLight ? "bg-zinc-200" : "bg-zinc-800"}`}
      />
    );
  }

  if (!session?.user) {
    if (isLanding) {
      return null;
    }

    return (
      // relative + absolute caption: the header row is a fixed h-14, and a
      // flex-col taller than that would spill the caption past the header's
      // bottom border into page content. Absolute positioning keeps the
      // caption out of the row's own height calculation entirely.
      <div className="relative">
        <Button
          variant="outline"
          size="sm"
          onClick={() => {
            setSigningIn(true);
            // signIn redirects away on success; if it returns (popup blocked,
            // back button) the component is still mounted so re-enable the button.
            void signIn("google", { callbackUrl: "/plan" }).finally(() =>
              setSigningIn(false),
            );
          }}
          disabled={signingIn}
          className={
            isLight
              ? undefined
              : "border-zinc-700 bg-transparent text-zinc-200 hover:border-zinc-400 hover:bg-transparent hover:text-white"
          }
        >
          {signingIn ? "Signing in…" : "Sign in"}
        </Button>
        {/* Clickwrap notice: continuing past sign-in is the affirmative act of
            acceptance the terms-of-service skill calls for (browsewrap alone
            is weakly enforceable). Absolutely positioned below the button so
            it doesn't block the click or grow the header. */}
        <p
          className={`absolute right-0 top-full mt-1 whitespace-nowrap text-[10px] leading-snug ${
            isLight ? "text-[#a1a1aa]" : "text-zinc-500"
          }`}
        >
          By signing in, you agree to Kria&apos;s{" "}
          <Link href="/terms" className="underline underline-offset-2 hover:text-lime-700">
            Terms of Service
          </Link>{" "}
          and{" "}
          <Link href="/privacy" className="underline underline-offset-2 hover:text-lime-700">
            Privacy Policy
          </Link>
          .
        </p>
      </div>
    );
  }

  const name = session.user.name ?? session.user.email ?? "You";
  const image = session.user.image ?? null;
  const initial = name.trim().charAt(0).toUpperCase() || "Y";

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          variant="ghost"
          size="icon"
          aria-label="Account menu"
          className={`h-8 w-8 overflow-hidden rounded-full border p-0 ${
            isLight
              ? "border-zinc-300 bg-lime-600 text-white hover:border-zinc-400 hover:bg-lime-600"
              : "border-zinc-700 bg-zinc-800 text-zinc-200 hover:border-zinc-400 hover:bg-zinc-800"
          }`}
        >
          {image ? (
            // eslint-disable-next-line @next/next/no-img-element
            <img src={image} alt="" className="h-full w-full object-cover" />
          ) : (
            initial
          )}
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className={`w-44 ${isLight ? "" : "dark"}`}>
        <DropdownMenuLabel className="truncate text-[11px] font-normal text-[#a1a1aa]">
          {name}
        </DropdownMenuLabel>
        <DropdownMenuItem asChild>
          <Link href="/plan">My videos</Link>
        </DropdownMenuItem>
        <DropdownMenuSeparator />
        <DropdownMenuItem onSelect={() => signOut({ callbackUrl: "/" })}>
          Sign out
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
