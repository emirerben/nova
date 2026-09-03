import { redirect } from "next/navigation";

/**
 * The standalone /generative editor is gone — video creation starts in the
 * canonical /plan chat workspace. Kept as a redirect for old bookmarks.
 *
 * NOTE: the sibling modules in this directory (timeline-math, timeline-reducer,
 * VariantCard, VoiceRecorder) are load-bearing for the /plan item editor and
 * stay put — only the route's page (and its now-orphaned VariantTile) was removed.
 */
export default function GenerativeRedirect() {
  redirect("/plan");
}
