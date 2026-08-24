import { notFound } from "next/navigation";
import NewVideoFlowFixture from "./NewVideoFlowFixture";

export default function DevQaNewVideoFlowPage() {
  if (process.env.E2E_FIXTURES !== "true") notFound();
  return <NewVideoFlowFixture />;
}
