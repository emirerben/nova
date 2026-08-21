import { render, screen } from "@testing-library/react";
import "@testing-library/jest-dom";

import Header from "@/components/Header";

jest.mock("next/navigation", () => ({
  usePathname: () => "/plan",
}));

jest.mock("next-auth/react", () => ({
  useSession: () => ({ status: "authenticated", data: { user: { id: "user-1" } } }),
  signIn: jest.fn(),
  signOut: jest.fn(),
}));

describe("Header creation-hub rollout label", () => {
  afterEach(() => {
    delete process.env.NEXT_PUBLIC_CREATION_HUB_ENABLED;
  });

  it("keeps Plan while the creation hub is disabled", () => {
    delete process.env.NEXT_PUBLIC_CREATION_HUB_ENABLED;
    render(<Header />);
    expect(screen.getByRole("link", { name: "Plan" })).toHaveAttribute("href", "/plan");
  });

  it("renames Plan to Create atomically with the creation hub", () => {
    process.env.NEXT_PUBLIC_CREATION_HUB_ENABLED = "true";
    render(<Header />);
    expect(screen.getByRole("link", { name: "Create" })).toHaveAttribute("href", "/plan");
  });
});
