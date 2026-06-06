/**
 * Tone contract tests for the ProgressTheater family (D20).
 *
 * Each component gets:
 *   - one light-mode class assertion
 *   - one dark-default regression pin (so template/admin can't silently flip)
 *
 * These are intentionally minimal — they pin the public API contract, not
 * exhaustive class names (which can change as long as visual meaning is preserved).
 */

// @ts-nocheck
import React from "react";
import { render } from "@testing-library/react";
import "@testing-library/jest-dom";

// Components use matchMedia for prefers-reduced-motion checks.
Object.defineProperty(window, "matchMedia", {
  writable: true,
  value: jest.fn().mockImplementation((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: jest.fn(),
    removeListener: jest.fn(),
    addEventListener: jest.fn(),
    removeEventListener: jest.fn(),
    dispatchEvent: jest.fn(),
  })),
});

// ── StatusHeadline ─────────────────────────────────────────────────────────

import { StatusHeadline } from "@/components/progress/StatusHeadline";

describe("StatusHeadline tone contract", () => {
  it("test_status_headline_light_uses_ink_text: renders ink class in light mode", () => {
    const { container } = render(<StatusHeadline text="Rendering…" tone="light" />);
    const h2 = container.querySelector("h2");
    expect(h2).not.toBeNull();
    expect(h2!.className).toContain("text-[#0c0c0e]");
  });

  it("test_status_headline_dark_default_uses_white: dark default preserves white text", () => {
    // Omitting tone must stay dark — template flow regression pin.
    const { container } = render(<StatusHeadline text="Rendering…" />);
    const h2 = container.querySelector("h2");
    expect(h2).not.toBeNull();
    expect(h2!.className).toContain("text-white");
  });
});

// ── EtaBar ─────────────────────────────────────────────────────────────────

import { EtaBar } from "@/components/progress/EtaBar";

describe("EtaBar tone contract", () => {
  it("test_eta_bar_light_uses_lime_fill: fill track is lime-600 in light mode", () => {
    const { container } = render(
      <EtaBar barPosition={0.4} elapsedMs={12000} etaText="~30s" tone="light" />
    );
    const fills = Array.from(container.querySelectorAll("div"));
    expect(fills.some((el) => el.className.includes("bg-lime-600"))).toBe(true);
  });

  it("test_eta_bar_dark_default_uses_amber_fill: fill track is amber-400 in dark default", () => {
    const { container } = render(
      <EtaBar barPosition={0.4} elapsedMs={12000} etaText="~30s" />
    );
    const fills = Array.from(container.querySelectorAll("div"));
    expect(fills.some((el) => el.className.includes("bg-amber-400"))).toBe(true);
  });
});

// ── PayoffField ────────────────────────────────────────────────────────────

import { PayoffField } from "@/components/progress/PayoffField";

describe("PayoffField tone contract", () => {
  it("test_payoff_field_light_uses_zinc300_border: empty state border is zinc-300 in light mode", () => {
    const { container } = render(
      <PayoffField variants={[]} renderCard={() => null} tone="light" />
    );
    const allEls = Array.from(container.querySelectorAll("*"));
    expect(
      allEls.some((el) => (el as HTMLElement).className?.includes("border-zinc-300"))
    ).toBe(true);
  });

  it("test_payoff_field_dark_default_uses_zinc800_border: empty state border is zinc-800 in dark default", () => {
    const { container } = render(
      <PayoffField variants={[]} renderCard={() => null} />
    );
    const allEls = Array.from(container.querySelectorAll("*"));
    expect(
      allEls.some((el) => (el as HTMLElement).className?.includes("border-zinc-800"))
    ).toBe(true);
  });
});

// ── PhaseChipRow ───────────────────────────────────────────────────────────

import { PhaseChipRow } from "@/components/progress/PhaseChipRow";

describe("PhaseChipRow tone contract", () => {
  it("test_phase_chip_row_light_uses_cream_fade_mask: fade masks use cream bg in light mode", () => {
    const { container } = render(
      <PhaseChipRow
        phases={["analyze", "render"]}
        phaseLabels={{ analyze: "Analyzing", render: "Rendering" }}
        currentPhase="analyze"
        tone="light"
      />
    );
    const divs = Array.from(container.querySelectorAll("div"));
    expect(divs.some((el) => el.className.includes("from-[#fafaf8]"))).toBe(true);
  });

  it("test_phase_chip_row_dark_default_uses_black_fade_mask: dark default preserves black fade mask", () => {
    const { container } = render(
      <PhaseChipRow
        phases={["analyze", "render"]}
        phaseLabels={{ analyze: "Analyzing", render: "Rendering" }}
        currentPhase="analyze"
      />
    );
    const divs = Array.from(container.querySelectorAll("div"));
    expect(divs.some((el) => el.className.includes("from-black"))).toBe(true);
  });
});

// ── VariantRenderCard ──────────────────────────────────────────────────────

import { VariantRenderCard } from "@/components/progress/VariantRenderCard";

const PENDING_VARIANT = { variant_id: "song_lyrics", render_status: null };

describe("VariantRenderCard tone contract", () => {
  it("test_variant_render_card_light_uses_zinc100_body: pending card body is zinc-100 in light mode", () => {
    const { container } = render(
      <VariantRenderCard variant={PENDING_VARIANT} isNewlyReady={false} tone="light" />
    );
    const divs = Array.from(container.querySelectorAll("div"));
    expect(divs.some((el) => el.className.includes("bg-zinc-100"))).toBe(true);
  });

  it("test_variant_render_card_dark_default_uses_zinc900_body: dark default preserves zinc-900 body", () => {
    const { container } = render(
      <VariantRenderCard variant={PENDING_VARIANT} isNewlyReady={false} />
    );
    const divs = Array.from(container.querySelectorAll("div"));
    expect(divs.some((el) => el.className.includes("bg-zinc-900"))).toBe(true);
  });
});
