import { notFound } from "next/navigation";
import { Suspense } from "react";
import ChatFirstCreationPreview from "./ChatFirstCreationPreview";

export default function DevQaChatFirstCreationPage() {
  // Keep the deterministic fixture available to local E2E and preview deploys,
  // but never expose it on production builds.
  if (process.env.E2E_FIXTURES !== "true" && process.env.VERCEL_ENV !== "preview") notFound();
  return (
    <Suspense fallback={<div className="flex h-dvh items-center justify-center bg-background" role="status">Opening preview…</div>}>
      <ChatFirstCreationPreview />
    </Suspense>
  );
}
