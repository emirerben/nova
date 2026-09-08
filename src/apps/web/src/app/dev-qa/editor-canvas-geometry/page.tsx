import { notFound } from "next/navigation";
import { Suspense } from "react";
import EditorCanvasGeometryFixture from "./EditorCanvasGeometryFixture";

export default function DevQaEditorCanvasGeometryPage() {
  if (process.env.E2E_FIXTURES !== "true") notFound();
  return (
    <Suspense fallback={null}>
      <EditorCanvasGeometryFixture />
    </Suspense>
  );
}
