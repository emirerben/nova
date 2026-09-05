"use client";

import { Suspense } from "react";
import { useParams } from "next/navigation";
import { useSession } from "next-auth/react";
import ChatCreationWorkspace from "../_components/workspace/ChatCreationWorkspace";
import SignInPrompt from "../_components/SignInPrompt";

function CreationThreadPageInner() {
  const { status } = useSession();
  const params = useParams<{ threadId: string }>();
  const callbackUrl = `/plan/${params.threadId}`;

  if (status === "unauthenticated") {
    return <SignInPrompt callbackUrl={callbackUrl} />;
  }
  if (status !== "authenticated") {
    return <div className="flex h-dvh items-center justify-center bg-background" role="status">Loading project…</div>;
  }
  return <ChatCreationWorkspace initialThreadId={params.threadId} />;
}

export default function CreationThreadPage() {
  return (
    <Suspense fallback={<div className="flex h-dvh items-center justify-center bg-background" role="status">Loading project…</div>}>
      <CreationThreadPageInner />
    </Suspense>
  );
}
