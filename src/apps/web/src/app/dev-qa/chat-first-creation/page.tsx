import { notFound } from "next/navigation";
import ChatFirstCreationFixture from "./ChatFirstCreationFixture";

export default function DevQaChatFirstCreationPage() {
  // Keep the deterministic fixture available to local E2E and preview deploys,
  // but never expose it on production builds.
  if (process.env.E2E_FIXTURES !== "true" && process.env.VERCEL_ENV !== "preview") notFound();
  return <ChatFirstCreationFixture />;
}
