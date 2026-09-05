import { redirect } from "next/navigation";

/** Legacy footage-first entry point; creation now starts in the Kria chat. */
export default function CreateRedirect() {
  redirect("/plan");
}
