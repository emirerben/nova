import { notFound } from "next/navigation";
import EditorTimelineFixture from "./EditorTimelineFixture";

export default function DevQaEditorTimelinePage() {
  if (process.env.E2E_FIXTURES !== "true") notFound();
  return <EditorTimelineFixture />;
}
