/**
 * Header isLight predicate + account menu tests (D21 — light unification;
 * Kria Design System Lane A — borderless header, shadcn DropdownMenu account
 * menu).
 *
 * Verifies that the light editorial design system is applied to all user-facing
 * routes (/plan, /library, /generative) and NOT to dark render job routes.
 */

// @ts-nocheck
import React from "react";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import "@testing-library/jest-dom";

let mockPathname = "/";

jest.mock("next/navigation", () => ({
  usePathname: jest.fn(() => mockPathname),
}));

const mockSignOut = jest.fn();
jest.mock("next-auth/react", () => ({
  useSession: jest.fn(() => ({ data: null, status: "unauthenticated" })),
  signIn: jest.fn(),
  signOut: (...args: unknown[]) => mockSignOut(...args),
}));

import Header from "@/components/Header";

function renderWithPathname(pathname: string) {
  mockPathname = pathname;
  return render(<Header />);
}

describe("Header — isLight predicate", () => {
  const { useSession } = require("next-auth/react");

  beforeEach(() => {
    useSession.mockReturnValue({ data: null, status: "unauthenticated" });
  });

  it("test_header_light_on_landing: / is light, borderless, and leaves actions to the story", () => {
    const { container } = renderWithPathname("/");
    const header = container.querySelector("header");
    expect(header!.className).toContain("bg-[#ffffff]");
    expect(header!.className).not.toContain("border-b");
    expect(screen.queryByRole("link", { name: /create my first edit/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /sign in/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Terms" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Privacy" })).not.toBeInTheDocument();
  });

  it("test_header_light_on_auto_story: /auto-story is light and borderless", () => {
    const { container } = renderWithPathname("/auto-story");
    const header = container.querySelector("header");
    expect(header!.className).toContain("bg-[#ffffff]");
    expect(header!.className).not.toContain("border-b");
    expect(screen.queryByRole("link", { name: /create my first edit/i })).not.toBeInTheDocument();
  });

  it("test_header_light_on_plan: /plan is light and borderless, with the standard auth control", () => {
    const { container } = renderWithPathname("/plan");
    const header = container.querySelector("header");
    expect(header!.className).toContain("bg-[#ffffff]");
    expect(header!.className).not.toContain("border-b");
    expect(screen.getByRole("button", { name: /sign in/i })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Terms of Service" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Privacy Policy" })).toBeInTheDocument();
  });

  it("test_header_light_on_plan_items: /plan/items/x is light", () => {
    const { container } = renderWithPathname("/plan/items/abc123");
    const header = container.querySelector("header");
    expect(header!.className).toContain("bg-[#ffffff]");
  });

  it("test_header_light_on_library: /library is light", () => {
    const { container } = renderWithPathname("/library");
    const header = container.querySelector("header");
    expect(header!.className).toContain("bg-[#ffffff]");
  });

  it("test_header_light_on_generative: /generative is light", () => {
    const { container } = renderWithPathname("/generative");
    const header = container.querySelector("header");
    expect(header!.className).toContain("bg-[#ffffff]");
  });

  it("test_header_light_on_tiktok: /tiktok is light", () => {
    const { container } = renderWithPathname("/tiktok");
    const header = container.querySelector("header");
    expect(header!.className).toContain("bg-[#ffffff]");
  });

  it("test_header_dark_on_template_jobs: /template-jobs/x is NOT light", () => {
    const { container } = renderWithPathname("/template-jobs/abc123");
    const header = container.querySelector("header");
    expect(header!.className).not.toContain("bg-[#ffffff]");
  });

  it("test_header_no_plan_link: authenticated header has no Plan/Create nav link, on any route", () => {
    useSession.mockReturnValue({
      data: { user: { name: "Test User", email: "test@example.com", image: null } },
      status: "authenticated",
    });
    renderWithPathname("/plan");
    expect(screen.queryByRole("link", { name: /^plan$/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /^create$/i })).not.toBeInTheDocument();
  });
});

describe("Header — account menu (authenticated)", () => {
  const { useSession } = require("next-auth/react");

  beforeEach(() => {
    mockPathname = "/plan";
    mockSignOut.mockReset();
    useSession.mockReturnValue({
      data: { user: { name: "Test User", email: "test@example.com", image: null } },
      status: "authenticated",
    });
  });

  async function openMenu() {
    const user = userEvent.setup({ delay: null });
    const avatar = screen.getByRole("button", { name: /account menu/i });
    await user.click(avatar);
    return user;
  }

  it("test_account_menu_opens_with_my_videos_and_sign_out", async () => {
    render(<Header />);
    await openMenu();
    expect(await screen.findByRole("menuitem", { name: /my videos/i })).toBeInTheDocument();
    expect(screen.getByRole("menuitem", { name: /sign out/i })).toBeInTheDocument();
  });

  it("test_account_menu_shows_name_label", async () => {
    render(<Header />);
    await openMenu();
    await screen.findByRole("menu");
    expect(screen.getByText("Test User")).toBeInTheDocument();
  });

  it("test_account_menu_my_videos_links_to_plan", async () => {
    render(<Header />);
    await openMenu();
    const myVideos = await screen.findByRole("menuitem", { name: /my videos/i });
    expect(myVideos.getAttribute("href")).toBe("/plan");
  });

  it("test_account_menu_sign_out_calls_signOut", async () => {
    render(<Header />);
    const user = await openMenu();
    const signOutItem = await screen.findByRole("menuitem", { name: /sign out/i });
    await user.click(signOutItem);
    await waitFor(() => {
      expect(mockSignOut).toHaveBeenCalledWith({ callbackUrl: "/" });
    });
  });

  it("test_account_menu_has_no_persona_or_start_over", async () => {
    render(<Header />);
    await openMenu();
    await screen.findByRole("menu");
    expect(screen.queryByRole("menuitem", { name: /your persona/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("menuitem", { name: /start over/i })).not.toBeInTheDocument();
    expect(screen.queryByText(/deletes your plan/i)).not.toBeInTheDocument();
  });
});
