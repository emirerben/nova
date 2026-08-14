import { notFound } from "next/navigation";
import TextMotionFixture from "./TextMotionFixture";

export default function DevQaTextMotionPage() {
  if (process.env.E2E_FIXTURES !== "true") notFound();
  return <TextMotionFixture />;
}
