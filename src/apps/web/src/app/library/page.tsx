import { redirect } from "next/navigation";

/**
 * Past edits live in the canonical chat workspace Gallery. Keep this redirect
 * so old bookmarks and external links open the correct workspace state.
 */
export default function LibraryRedirect() {
  redirect("/plan?view=gallery");
}
