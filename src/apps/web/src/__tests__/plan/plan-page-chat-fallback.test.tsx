import { useEffect } from "react";
import { render, screen, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom";

import PlanPage from "@/app/plan/page";
import { getContentPlan, getPersona } from "@/lib/plan-api";

jest.mock("next-auth/react", () => ({
  useSession: () => ({ status: "authenticated" }),
}));

jest.mock("next/navigation", () => ({
  useRouter: () => ({ replace: jest.fn() }),
  useSearchParams: () => ({ get: () => null }),
}));

jest.mock("@/lib/chat-first", () => ({ CHAT_FIRST_CREATION_ENABLED: true }));
jest.mock("@/lib/plan-api", () => ({
  getContentPlan: jest.fn(),
  getPersona: jest.fn(),
  createContentPlan: jest.fn(),
  retunePersonaFromFeedback: jest.fn(),
  tiktokScrape: jest.fn(),
  updatePersona: jest.fn(),
  recordOnboardingFork: jest.fn(),
  NotAuthenticatedError: class NotAuthenticatedError extends Error {},
}));
jest.mock("@/lib/generative-api", () => ({ createGenerativeJob: jest.fn() }));
jest.mock("@/app/plan/_lib/route", () => ({ resolvePlanMode: () => "workspace" }));
jest.mock("@/app/plan/_components/workspace/WorkspaceHome", () => ({
  WorkspaceHome: () => <div>Legacy plan workspace</div>,
}));
jest.mock("@/app/plan/_components/workspace/ChatCreationWorkspace", () => ({
  __esModule: true,
  default: function MockChatCreationWorkspace({ onLegacyFallback }: { onLegacyFallback: () => void }) {
    useEffect(() => onLegacyFallback(), [onLegacyFallback]);
    return <div>Chat capability check</div>;
  },
}));

describe("PlanPage account rollout fallback", () => {
  beforeEach(() => {
    jest.mocked(getPersona).mockReset().mockResolvedValue({ persona_status: "ready" } as never);
    jest.mocked(getContentPlan).mockReset().mockResolvedValue({
      id: "plan-1",
      plan_status: "ready",
      items: [],
    } as never);
  });

  it("loads the existing plan experience after the chat capability returns 404", async () => {
    render(<PlanPage />);

    expect(await screen.findByText("Legacy plan workspace")).toBeInTheDocument();
    await waitFor(() => expect(getPersona).toHaveBeenCalledTimes(1));
    expect(getContentPlan).toHaveBeenCalledTimes(1);
  });
});
