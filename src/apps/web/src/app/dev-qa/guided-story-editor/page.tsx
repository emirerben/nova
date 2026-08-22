import { notFound } from "next/navigation";
import GuidedStoryEditorFixture from "./GuidedStoryEditorFixture";

export default function DevQaGuidedStoryEditorPage() {
  if (process.env.E2E_FIXTURES !== "true") notFound();
  return <GuidedStoryEditorFixture />;
}
