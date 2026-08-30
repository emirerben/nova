import { redirect } from "next/navigation";

/** Manual creation remains available from the editor, not as a second flow. */
export default function ManualCreateRedirect() {
  redirect("/plan");
}
