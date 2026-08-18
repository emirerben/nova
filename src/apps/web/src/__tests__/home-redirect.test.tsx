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

// KriaEditStory uses browser media and animation APIs. Stub it so this server-page
// test stays focused on authentication and the landing composition boundary.
const mockStory = jest.fn(({ mode }: { mode?: string }) => (
  <section
    aria-label="How Kria turns raw videos into a finished edit"
    data-mode={mode}
  >
    <h1>Save time. Let AI edit your videos. Create more.</h1>
    <a href="/plan">Create my first edit</a>
    <a href="/terms">Terms</a>
    <a href="/privacy">Privacy</a>
  </section>
));

jest.mock("@/components/KriaEditStory", () => ({
  __esModule: true,
  default: (props: { mode?: string }) => mockStory(props),
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

    await expect(HomePage({})).rejects.toThrow("REDIRECT");
    expect(mockRedirect).toHaveBeenCalledWith("/plan");
  });

  it("renders the automatic landing story by default", async () => {
    mockGetServerSession.mockResolvedValue(null);

    const { default: HomePage } = await import("../app/page");

    // Need to isolate the module between tests since we import it dynamically.
    const jsx = await HomePage({});
    render(jsx);

    expect(mockRedirect).not.toHaveBeenCalled();
    expect(screen.getByRole("heading", { level: 1 })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /create my first edit/i })).toHaveAttribute(
      "href",
      "/plan",
    );
    expect(
      screen.getByLabelText("How Kria turns raw videos into a finished edit"),
    ).toHaveAttribute("data-mode", "auto");
    expect(mockStory).toHaveBeenCalledWith({ mode: "auto" });
    expect(screen.queryByText(/how your agent works/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/it learns about you/i)).not.toBeInTheDocument();
  });

  it("keeps the scroll comparison available through the mode query", async () => {
    mockGetServerSession.mockResolvedValue(null);

    const { default: HomePage } = await import("../app/page");
    render(await HomePage({ searchParams: { mode: "scroll" } }));

    expect(
      screen.getByLabelText("How Kria turns raw videos into a finished edit"),
    ).toHaveAttribute("data-mode", "scroll");
    expect(mockStory).toHaveBeenCalledWith({ mode: "scroll" });
  });
});
