import { redirect } from "next/navigation";

/**
 * /library is gone — past edits live on the /plan home now (v0.44 redesign).
 * Kept as a redirect so old bookmarks and external links keep working.
 */
export default function LibraryRedirect() {
  redirect("/plan");
}
