/**
 * /plan/style — conversational style editor (Creator Agent M2).
 *
 * Renders the StyleAgentInterview component. The "Tweak your style" entry
 * link from StyleCard → this page is added in the post-merge integration
 * commit (after Lane A + Lane C both merge).
 *
 * Route is unreachable when style status is "absent" (the StyleCard caller
 * should check status before linking here). If a user lands here without
 * a style entity, the agent start call returns 404 and the interview shows
 * a friendly error.
 */
import StyleAgentInterview from "@/app/plan/_components/StyleAgentInterview";

export const metadata = {
  title: "Your Style — Kria",
};

export default function StylePage() {
  return (
    <main className="min-h-screen bg-[#fafaf8] px-4 py-12">
      <div className="mx-auto max-w-xl">
        <StyleAgentInterview />
      </div>
    </main>
  );
}
