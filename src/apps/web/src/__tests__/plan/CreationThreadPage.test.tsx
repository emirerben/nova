import React from "react";
import { render, screen } from "@testing-library/react";
import "@testing-library/jest-dom";

const mockUseParams = jest.fn();
const mockUseSession = jest.fn();

jest.mock("next/navigation", () => ({
  useParams: () => mockUseParams(),
}));

jest.mock("next-auth/react", () => ({
  useSession: () => mockUseSession(),
}));

jest.mock("@/app/plan/_components/workspace/ChatCreationWorkspace", () => ({
  __esModule: true,
  default: ({ initialThreadId }: { initialThreadId: string }) => (
    <div data-testid="workspace-thread-id">{initialThreadId}</div>
  ),
}));

jest.mock("@/app/plan/_components/SignInPrompt", () => ({
  __esModule: true,
  default: ({ callbackUrl }: { callbackUrl: string }) => (
    <a href={callbackUrl}>Sign in to continue</a>
  ),
}));

import CreationThreadPage from "@/app/plan/[threadId]/page";

describe("canonical creation thread route", () => {
  beforeEach(() => {
    mockUseParams.mockReturnValue({ threadId: "thread-42" });
    mockUseSession.mockReturnValue({ status: "authenticated" });
  });

  it("passes the URL thread id to the real workspace", () => {
    render(<CreationThreadPage />);
    expect(screen.getByTestId("workspace-thread-id")).toHaveTextContent("thread-42");
  });

  it("preserves the canonical URL for the sign-in callback", () => {
    mockUseSession.mockReturnValue({ status: "unauthenticated" });
    render(<CreationThreadPage />);
    expect(screen.getByRole("link", { name: "Sign in to continue" })).toHaveAttribute(
      "href",
      "/plan/thread-42",
    );
  });

  it("keeps an explicit loading state while auth is unresolved", () => {
    mockUseSession.mockReturnValue({ status: "loading" });
    render(<CreationThreadPage />);
    expect(screen.getByRole("status")).toHaveTextContent("Loading project…");
  });
});
