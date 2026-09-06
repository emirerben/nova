import { render, screen } from "@testing-library/react";
import "@testing-library/jest-dom";

import TikTokConnectionPage from "@/app/plan/tiktok/page";

let authStatus: "loading" | "authenticated" | "unauthenticated" = "authenticated";
let mockSearchParams = new URLSearchParams();

jest.mock("next/navigation", () => ({
  useSearchParams: () => mockSearchParams,
}));

jest.mock("next-auth/react", () => ({
  useSession: () => ({ status: authStatus }),
}));

jest.mock("@/app/plan/_components/TikTokConnectionPanel", () => ({
  __esModule: true,
  default: () => <div>TikTok connection panel</div>,
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

describe("TikTok connection page auth boundary", () => {
  beforeEach(() => {
    authStatus = "authenticated";
    mockSearchParams = new URLSearchParams();
  });

  it("renders the connection panel for authenticated visitors", () => {
    render(<TikTokConnectionPage />);
    expect(screen.getByText("TikTok connection panel")).toBeInTheDocument();
  });

  it("shows the canonical sign-in prompt for unauthenticated visitors", () => {
    authStatus = "unauthenticated";
    render(<TikTokConnectionPage />);

    expect(screen.getByText("Sign in to Kria")).toBeInTheDocument();
    expect(screen.getByTestId("sign-in-callback")).toHaveTextContent("/plan/tiktok");
    expect(screen.queryByText("TikTok connection panel")).not.toBeInTheDocument();
  });

  it("preserves TikTok query parameters through sign-in", () => {
    authStatus = "unauthenticated";
    mockSearchParams = new URLSearchParams("tiktok=connected");
    render(<TikTokConnectionPage />);

    expect(screen.getByTestId("sign-in-callback")).toHaveTextContent(
      "/plan/tiktok?tiktok=connected",
    );
  });

  it("does not mount the connection panel while the session is resolving", () => {
    authStatus = "loading";
    render(<TikTokConnectionPage />);

    expect(screen.getByRole("status", { name: "Opening TikTok" })).toBeInTheDocument();
    expect(screen.queryByText("TikTok connection panel")).not.toBeInTheDocument();
  });
});
