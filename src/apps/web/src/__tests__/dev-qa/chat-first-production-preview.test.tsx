import { render, screen } from "@testing-library/react";
import "@testing-library/jest-dom";
import ChatFirstCreationPreview from "@/app/dev-qa/chat-first-creation/ChatFirstCreationPreview";

let params = new URLSearchParams("live=1&project=production-library%3Ajob-1");
let sessionStatus: "authenticated" | "unauthenticated" | "loading" = "authenticated";

jest.mock("next/navigation", () => ({
  useSearchParams: () => params,
}));
jest.mock("next-auth/react", () => ({
  useSession: () => ({ status: sessionStatus }),
}));
jest.mock("@/app/plan/_components/workspace/ChatCreationWorkspace", () => ({
  __esModule: true,
  default: ({ productionPreview, initialThreadId }: { productionPreview?: boolean; initialThreadId?: string }) => (
    <div
      data-testid="live-production-workspace"
      data-read-only={productionPreview || undefined}
      data-project={initialThreadId}
    />
  ),
}));
jest.mock("@/app/plan/_components/SignInPrompt", () => ({
  __esModule: true,
  default: ({ callbackUrl }: { callbackUrl: string }) => <div data-testid="sign-in" data-callback={callbackUrl} />,
}));
jest.mock("@/app/dev-qa/chat-first-creation/ChatFirstCreationFixture", () => ({
  __esModule: true,
  default: () => <div data-testid="fixture-preview" />,
}));

describe("chat-first production preview routing", () => {
  beforeEach(() => {
    params = new URLSearchParams("live=1&project=production-library%3Ajob-1");
    sessionStatus = "authenticated";
  });

  it("uses the authenticated, read-only production workspace for live proof", () => {
    render(<ChatFirstCreationPreview />);
    expect(screen.getByTestId("live-production-workspace")).toHaveAttribute("data-read-only", "true");
    expect(screen.getByTestId("live-production-workspace")).toHaveAttribute("data-project", "production-library:job-1");
  });

  it("requires the creator to sign in before production data is requested", () => {
    sessionStatus = "unauthenticated";
    render(<ChatFirstCreationPreview />);
    expect(screen.getByTestId("sign-in")).toHaveAttribute("data-callback", "/dev-qa/chat-first-creation?live=1");
    expect(screen.queryByTestId("live-production-workspace")).not.toBeInTheDocument();
  });

  it("keeps the deterministic fixture as the default QA mode", () => {
    params = new URLSearchParams();
    render(<ChatFirstCreationPreview />);
    expect(screen.getByTestId("fixture-preview")).toBeInTheDocument();
  });
});
