"use client";

import Link from "next/link";
import { Suspense } from "react";
import { useSession } from "next-auth/react";
import { useSearchParams } from "next/navigation";
import { ArrowLeft } from "lucide-react";
import SignInPrompt from "../_components/SignInPrompt";
import TikTokConnectionPanel from "../_components/TikTokConnectionPanel";
import { LightShell } from "../_components/ui/LightShell";

export default function TikTokConnectionPage() {
  return (
    <Suspense fallback={<TikTokLoadingState />}>
      <TikTokConnectionPageInner />
    </Suspense>
  );
}

function TikTokConnectionPageInner() {
  const { status } = useSession();
  const searchParams = useSearchParams();
  const query = searchParams.toString();
  const callbackUrl = query ? `/plan/tiktok?${query}` : "/plan/tiktok";

  if (status === "loading") return <TikTokLoadingState />;

  if (status === "unauthenticated") {
    return (
      <LightShell size="narrow">
        <SignInPrompt callbackUrl={callbackUrl} />
      </LightShell>
    );
  }

  return (
    <LightShell size="narrow">
      <Link href="/plan" className="inline-flex min-h-11 items-center text-sm text-muted-foreground underline-offset-4 hover:text-foreground hover:underline focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-lime-600">
        <ArrowLeft className="mr-2 h-4 w-4" aria-hidden="true" />
        Back to workspace
      </Link>
      <div className="mt-8 space-y-3">
        <p className="text-xs font-semibold uppercase tracking-wide text-lime-700">Delivery</p>
        <h1 className="font-display text-3xl font-medium">Connect TikTok</h1>
        <p className="max-w-xl text-pretty text-sm leading-relaxed text-muted-foreground">
          Manage the account Kria can use when you release an approved edit.
        </p>
      </div>
      <div className="mt-8">
        <TikTokConnectionPanel />
      </div>
    </LightShell>
  );
}

function TikTokLoadingState() {
  return (
    <div
      className="flex h-dvh items-center justify-center bg-background"
      role="status"
      aria-label="Opening TikTok"
    >
      <div className="h-2 w-28 overflow-hidden rounded-full bg-[linear-gradient(110deg,hsl(var(--muted)),45%,hsl(var(--primary)/0.28),55%,hsl(var(--muted)))] bg-[length:200%_100%] motion-safe:animate-shimmer" />
    </div>
  );
}
