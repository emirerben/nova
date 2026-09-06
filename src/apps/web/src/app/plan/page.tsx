"use client";

import { Suspense } from "react";
import { useSession } from "next-auth/react";
import { useSearchParams } from "next/navigation";

import SignInPrompt from "./_components/SignInPrompt";
import { LightShell } from "./_components/ui/LightShell";
import ChatCreationWorkspace from "./_components/workspace/ChatCreationWorkspace";

export default function PlanPage() {
  return (
    <Suspense fallback={<PlanLoadingState />}>
      <PlanPageInner />
    </Suspense>
  );
}

function PlanPageInner() {
  const { status } = useSession();
  const searchParams = useSearchParams();
  const query = searchParams.toString();
  const callbackUrl = query ? `/plan?${query}` : "/plan";

  if (status === "loading") return <PlanLoadingState />;

  if (status === "unauthenticated") {
    return (
      <LightShell>
        <SignInPrompt callbackUrl={callbackUrl} />
      </LightShell>
    );
  }

  return <ChatCreationWorkspace />;
}

function PlanLoadingState() {
  return (
    <div
      className="flex h-dvh items-center justify-center bg-background"
      role="status"
      aria-label="Opening Kria"
    >
      <div className="h-2 w-28 overflow-hidden rounded-full bg-[linear-gradient(110deg,hsl(var(--muted)),45%,hsl(var(--primary)/0.28),55%,hsl(var(--muted)))] bg-[length:200%_100%] motion-safe:animate-shimmer" />
    </div>
  );
}
