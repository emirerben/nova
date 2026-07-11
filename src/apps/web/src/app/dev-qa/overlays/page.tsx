import { notFound } from "next/navigation";
import OverlaysFixture from "./OverlaysFixture";

export default function DevQaOverlaysPage() {
  if (process.env.E2E_FIXTURES !== "true") notFound();
  return <OverlaysFixture />;
}
