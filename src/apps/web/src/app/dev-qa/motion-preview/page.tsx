import { notFound } from "next/navigation";
import MotionPreviewPerformanceFixture from "./MotionPreviewPerformanceFixture";

export default function DevQaMotionPreviewPage() {
  if (process.env.E2E_FIXTURES !== "true") notFound();
  return <MotionPreviewPerformanceFixture />;
}
