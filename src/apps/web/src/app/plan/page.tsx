"use client";

import { Suspense } from "react";
import { useSession } from "next-auth/react";

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

  if (status === "loading") return <PlanLoadingState />;

  if (status === "unauthenticated") {
    return (
      <LightShell>
        <SignInPrompt callbackUrl="/plan" />
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
      <div className="h-2 w-28 overflow-hidden rounded-full bg-muted">
        <div className="h-full w-1/2 motion-safe:animate-pulse rounded-full bg-primary" />
      </div>
    </div>
  );
}
