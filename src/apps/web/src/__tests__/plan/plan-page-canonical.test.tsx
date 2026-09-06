import { render, screen } from "@testing-library/react";
import "@testing-library/jest-dom";

import PlanPage from "@/app/plan/page";

let authStatus: "loading" | "authenticated" | "unauthenticated" =
  "authenticated";
let mockSearchParams = new URLSearchParams();

jest.mock("next/navigation", () => ({
  useSearchParams: () => mockSearchParams,
}));

jest.mock("next-auth/react", () => ({
  useSession: () => ({ status: authStatus }),
}));

jest.mock("@/app/plan/_components/workspace/ChatCreationWorkspace", () => ({
  __esModule: true,
  default: () => <div>Canonical creation chat</div>,
}));

jest.mock("@/app/plan/_components/SignInPrompt", () => ({
  __esModule: true,
  default: ({ callbackUrl }: { callbackUrl: string }) => (
    <div>
      <span>Sign in to Kria</span>
      <output data-testid="sign-in-callback">{callbackUrl}</output>
    </div>
  ),
}));

describe("PlanPage canonical experience", () => {
  beforeEach(() => {
    mockSearchParams = new URLSearchParams();
  });

  it("always renders chat-first creation for an authenticated account", () => {
    authStatus = "authenticated";
    render(<PlanPage />);
    expect(screen.getByText("Canonical creation chat")).toBeInTheDocument();
  });

  it("keeps authentication at the canonical route boundary", () => {
    authStatus = "unauthenticated";
    render(<PlanPage />);
    expect(screen.getByText("Sign in to Kria")).toBeInTheDocument();
    expect(
      screen.queryByText("Canonical creation chat"),
    ).not.toBeInTheDocument();
  });

  it("preserves the Gallery deep link through sign-in", () => {
    authStatus = "unauthenticated";
    mockSearchParams = new URLSearchParams("view=gallery");
    render(<PlanPage />);

    expect(screen.getByTestId("sign-in-callback")).toHaveTextContent(
      "/plan?view=gallery",
    );
  });

  it("shows indeterminate progress and does not mount the API workspace before session resolution", () => {
    authStatus = "loading";
    const { container } = render(<PlanPage />);
    expect(
      screen.getByRole("status", { name: "Opening Kria" }),
    ).toBeInTheDocument();
    expect(
      container.querySelector(".motion-safe\\:animate-shimmer"),
    ).toBeInTheDocument();
    expect(container.querySelector(".w-1\\/2")).not.toBeInTheDocument();
    expect(
      screen.queryByText("Canonical creation chat"),
    ).not.toBeInTheDocument();
  });
});
