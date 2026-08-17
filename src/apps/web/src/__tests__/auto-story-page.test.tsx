import "@testing-library/jest-dom";
import { render, screen } from "@testing-library/react";
import { getServerSession } from "next-auth";
import { redirect } from "next/navigation";

jest.mock("next-auth", () => ({
  getServerSession: jest.fn(),
}));

jest.mock("next/navigation", () => ({
  redirect: jest.fn(),
}));

const mockStory = jest.fn(({ mode }: { mode?: string }) => (
  <section aria-label="Automatic Kria edit story" data-mode={mode} />
));

jest.mock("@/components/KriaEditStory", () => ({
  __esModule: true,
  default: (props: { mode?: string }) => mockStory(props),
}));

const mockGetServerSession = getServerSession as jest.MockedFunction<
  typeof getServerSession
>;
const mockRedirect = redirect as jest.MockedFunction<typeof redirect>;

describe("AutoStoryPage", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockRedirect.mockImplementation(() => {
      throw new Error("REDIRECT");
    });
  });

  it("redirects authenticated visitors to /plan", async () => {
    mockGetServerSession.mockResolvedValue({
      user: { name: "Test", email: "test@example.com" },
      expires: "2099-01-01",
    } as Awaited<ReturnType<typeof getServerSession>>);
    const { default: AutoStoryPage } = await import("../app/auto-story/page");

    await expect(AutoStoryPage()).rejects.toThrow("REDIRECT");
    expect(mockRedirect).toHaveBeenCalledWith("/plan");
  });

  it("renders the automatic story for anonymous visitors", async () => {
    mockGetServerSession.mockResolvedValue(null);
    const { default: AutoStoryPage } = await import("../app/auto-story/page");

    render(await AutoStoryPage());

    expect(mockRedirect).not.toHaveBeenCalled();
    expect(screen.getByLabelText("Automatic Kria edit story")).toHaveAttribute(
      "data-mode",
      "auto",
    );
    expect(mockStory).toHaveBeenCalledWith({ mode: "auto" });
  });
});
