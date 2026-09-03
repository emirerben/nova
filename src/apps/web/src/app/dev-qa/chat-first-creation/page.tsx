import { notFound } from "next/navigation";
import ChatFirstCreationFixture from "./ChatFirstCreationFixture";

export default function DevQaChatFirstCreationPage() {
  if (process.env.E2E_FIXTURES !== "true") notFound();
  return <ChatFirstCreationFixture />;
}
