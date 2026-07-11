import { notFound } from "next/navigation";
import ClipsFixture from "./ClipsFixture";

export default function DevQaClipsPage() {
  if (process.env.E2E_FIXTURES !== "true") notFound();
  return <ClipsFixture />;
}
