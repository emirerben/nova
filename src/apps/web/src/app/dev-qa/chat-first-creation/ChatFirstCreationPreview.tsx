"use client";

import { useSession } from "next-auth/react";
import { useSearchParams } from "next/navigation";
import ChatCreationWorkspace from "@/app/plan/_components/workspace/ChatCreationWorkspace";
import SignInPrompt from "@/app/plan/_components/SignInPrompt";
import ChatFirstCreationFixture from "./ChatFirstCreationFixture";

const PREVIEW_PATH = "/dev-qa/chat-first-creation?live=1";

export default function ChatFirstCreationPreview() {
  const searchParams = useSearchParams();
  const { status } = useSession();
  const live = searchParams.get("live") === "1";

  if (!live) return <ChatFirstCreationFixture />;

  if (status === "unauthenticated") {
    return <SignInPrompt callbackUrl={PREVIEW_PATH} />;
  }
  if (status !== "authenticated") {
    return <div className="flex h-dvh items-center justify-center bg-background" role="status">Opening your production projects…</div>;
  }

  return (
    <ChatCreationWorkspace
      productionPreview
      initialThreadId={searchParams.get("project") ?? undefined}
    />
  );
}
