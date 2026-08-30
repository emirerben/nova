import { redirect } from "next/navigation";

/** The chat workspace owns new-video creation. Keep old bookmarks working. */
export default function NewVideoRedirect() {
  redirect("/plan");
}
