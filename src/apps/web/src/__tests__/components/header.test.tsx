/**
 * Header isLight predicate tests (D21 — light unification).
 *
 * Verifies that the light editorial design system is applied to all user-facing
 * routes (/plan, /library, /generative) and NOT to dark render job routes.
 */

// @ts-nocheck
import React from "react";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom";

let mockPathname = "/";

jest.mock("next/navigation", () => ({
  usePathname: jest.fn(() => mockPathname),
}));

jest.mock("next-auth/react", () => ({
  useSession: jest.fn(() => ({ data: null, status: "unauthenticated" })),
  signIn: jest.fn(),
  signOut: jest.fn(),
}));

const mockResetPersona = jest.fn();
jest.mock("@/lib/plan-api", () => ({
  resetPersona: (...args: unknown[]) => mockResetPersona(...args),
}));

import Header from "@/components/Header";

function renderWithPathname(pathname: string) {
  mockPathname = pathname;
  return render(<Header />);
}

describe("Header — isLight predicate", () => {
  it("test_header_light_on_landing: / is light (cream bg)", () => {
    const { container } = renderWithPathname("/");
    const header = container.querySelector("header");
    expect(header!.className).toContain("bg-[#fafaf8]");
  });

  it("test_header_light_on_plan: /plan is light", () => {
    const { container } = renderWithPathname("/plan");
    const header = container.querySelector("header");
    expect(header!.className).toContain("bg-[#fafaf8]");
  });

  it("test_header_light_on_plan_items: /plan/items/x is light", () => {
    const { container } = renderWithPathname("/plan/items/abc123");
    const header = container.querySelector("header");
    expect(header!.className).toContain("bg-[#fafaf8]");
  });

  it("test_header_light_on_library: /library is light", () => {
    const { container } = renderWithPathname("/library");
    const header = container.querySelector("header");
    expect(header!.className).toContain("bg-[#fafaf8]");
  });

  it("test_header_light_on_generative: /generative is light", () => {
    const { container } = renderWithPathname("/generative");
    const header = container.querySelector("header");
    expect(header!.className).toContain("bg-[#fafaf8]");
  });

  it("test_header_dark_on_template_jobs: /template-jobs/x is NOT light", () => {
    const { container } = renderWithPathname("/template-jobs/abc123");
    const header = container.querySelector("header");
    expect(header!.className).not.toContain("bg-[#fafaf8]");
  });
});

describe("Header — Start over (authenticated)", () => {
  const { useSession } = require("next-auth/react");

  beforeEach(() => {
    mockPathname = "/plan";
    mockResetPersona.mockReset();
    useSession.mockReturnValue({
      data: { user: { name: "Test User", email: "test@example.com", image: null } },
      status: "authenticated",
    });
    // jsdom doesn't implement window.location.assign; replace with a spy.
    Object.defineProperty(window, "location", {
      value: { assign: jest.fn() },
      writable: true,
    });
  });

  function openMenu() {
    const avatar = screen.getByRole("button", { name: /account menu/i });
    fireEvent.click(avatar);
  }

  it("test_start_over_visible_in_dropdown", () => {
    render(<Header />);
    openMenu();
    expect(screen.getByRole("button", { name: /start over/i })).toBeInTheDocument();
  });

  it("test_start_over_shows_confirm_on_click", () => {
    render(<Header />);
    openMenu();
    fireEvent.click(screen.getByRole("button", { name: /^start over$/i }));
    expect(
      screen.getByText(/deletes your plan/i),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /yes, start over/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /cancel/i })).toBeInTheDocument();
  });

  it("test_start_over_cancel_restores_menu", () => {
    render(<Header />);
    openMenu();
    fireEvent.click(screen.getByRole("button", { name: /^start over$/i }));
    fireEvent.click(screen.getByRole("button", { name: /cancel/i }));
    // "Start over" button is back; confirm text is gone.
    expect(screen.getByRole("button", { name: /^start over$/i })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /yes, start over/i })).not.toBeInTheDocument();
    expect(mockResetPersona).not.toHaveBeenCalled();
  });

  it("test_start_over_confirm_calls_reset_and_navigates", async () => {
    mockResetPersona.mockResolvedValue({ reset: true });
    render(<Header />);
    openMenu();
    fireEvent.click(screen.getByRole("button", { name: /^start over$/i }));
    fireEvent.click(screen.getByRole("button", { name: /yes, start over/i }));
    await waitFor(() => {
      expect(mockResetPersona).toHaveBeenCalledTimes(1);
      expect(window.location.assign).toHaveBeenCalledWith("/plan");
    });
  });
});
