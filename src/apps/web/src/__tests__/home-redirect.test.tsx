/**
 * Tests that page.tsx redirects signed-in users to /plan and renders the
 * landing page for anonymous visitors.
 */
import "@testing-library/jest-dom";
import { render, screen } from "@testing-library/react";
import { redirect } from "next/navigation";
import { getServerSession } from "next-auth";

// Mock the modules that are unavailable in the Jest environment.
jest.mock("next-auth", () => ({
  getServerSession: jest.fn(),
}));

jest.mock("next/navigation", () => ({
  redirect: jest.fn(),
}));

// FadeInOnScroll uses IntersectionObserver (browser-only). Stub it so
// children render immediately in Jest (jsdom has no IO).
jest.mock("@/components/FadeInOnScroll", () => ({
  __esModule: true,
  default: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));

// ShowcaseMarquee also uses IntersectionObserver + HTMLMediaElement.play(),
// neither of which exists in jsdom. Stub it to render a labelled region so
// the page still mounts cleanly.
jest.mock("@/components/ShowcaseMarquee", () => ({
  __esModule: true,
  default: () => <section aria-label="Videos created by Nova" />,
}));

const mockGetServerSession = getServerSession as jest.MockedFunction<
  typeof getServerSession
>;
const mockRedirect = redirect as jest.MockedFunction<typeof redirect>;

describe("HomePage", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    // redirect() normally throws a special Next.js error to halt rendering.
    // In tests we just want to assert it was called, so we throw a plain error.
    mockRedirect.mockImplementation(() => {
      throw new Error("REDIRECT");
    });
  });

  it("redirects to /plan when a session is present", async () => {
    mockGetServerSession.mockResolvedValue({
      user: { name: "Test", email: "test@example.com" },
      expires: "2099-01-01",
    } as Awaited<ReturnType<typeof getServerSession>>);

    const { default: HomePage } = await import("../app/page");

    await expect(HomePage()).rejects.toThrow("REDIRECT");
    expect(mockRedirect).toHaveBeenCalledWith("/plan");
  });

  it("renders the landing page when no session is present", async () => {
    mockGetServerSession.mockResolvedValue(null);

    const { default: HomePage } = await import("../app/page");

    // Need to isolate the module between tests since we import it dynamically.
    const jsx = await HomePage();
    render(jsx);

    expect(mockRedirect).not.toHaveBeenCalled();
    expect(screen.getByRole("heading", { level: 1 })).toBeInTheDocument();
    // Two CTA links exist — hero + closing section.
    const ctaLinks = screen.getAllByRole("link", { name: /build my plan/i });
    expect(ctaLinks.length).toBeGreaterThanOrEqual(1);
  });
});
