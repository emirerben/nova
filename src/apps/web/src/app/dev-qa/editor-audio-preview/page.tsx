import { notFound } from "next/navigation";

import EditorAudioPreviewFixture from "./EditorAudioPreviewFixture";

export default function DevQaEditorAudioPreviewPage() {
  if (process.env.E2E_FIXTURES !== "true") notFound();
  return <EditorAudioPreviewFixture />;
}
