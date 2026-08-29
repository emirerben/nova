import { notFound } from "next/navigation";
import MobileEditorFixture from "./MobileEditorFixture";

export default function MobileEditorQaPage() {
  if (process.env.E2E_FIXTURES !== "true") notFound();
  return <MobileEditorFixture />;
}
