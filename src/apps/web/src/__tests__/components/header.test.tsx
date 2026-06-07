/**
 * Header isLight predicate tests (D21 — light unification).
 *
 * Verifies that the light editorial design system is applied to all user-facing
 * routes (/plan, /library, /generative) and NOT to dark render routes (/template).
 */

// @ts-nocheck
import React from "react";
import { render } from "@testing-library/react";
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

  it("test_header_dark_on_template: /template/[id] is NOT light (no cream bg class)", () => {
    const { container } = renderWithPathname("/template/abc123");
    const header = container.querySelector("header");
    // Dark header has no static background class — it's applied via inline style.
    expect(header!.className).not.toContain("bg-[#fafaf8]");
  });

  it("test_header_dark_on_template_jobs: /template-jobs/x is NOT light", () => {
    const { container } = renderWithPathname("/template-jobs/abc123");
    const header = container.querySelector("header");
    expect(header!.className).not.toContain("bg-[#fafaf8]");
  });
});
