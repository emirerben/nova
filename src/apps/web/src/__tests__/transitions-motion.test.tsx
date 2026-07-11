/**
 * Transitions-motion feature tests.
 *
 * Covers the new animation state paths introduced by the transitions.dev slice:
 *   1. VariantRenderCard t-skel reveal — 3 logic paths
 *   2. OnboardingShell StepSlide class — 1 smoke path
 *
 * Uses synchronous rAF mock so CSS-class effects settle without fake timers.
 */

// @ts-nocheck
import React from "react";
import { act, render, screen } from "@testing-library/react";
import "@testing-library/jest-dom";

// ── rAF mock (synchronous, so state updates settle in the same tick) ──────────
let rafCallback: ((t: number) => void) | null = null;
beforeEach(() => {
  jest.spyOn(global, "requestAnimationFrame").mockImplementation((cb) => {
    rafCallback = cb;
    return 1;
  });
  jest.spyOn(global, "cancelAnimationFrame").mockImplementation(() => {});
});
afterEach(() => {
  jest.restoreAllMocks();
  rafCallback = null;
});

function flushRaf() {
  if (rafCallback) {
    act(() => { rafCallback!(0); });
  }
}

// ── matchMedia stub (needed by progress components) ──────────────────────────
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

// ============================================================================
// 1. VariantRenderCard — t-skel revealed state
// ============================================================================

import { VariantRenderCard } from "@/components/progress/VariantRenderCard";

const makeVariant = (status: string | null, url: string | null = null) => ({
  variant_id: "song_lyrics",
  render_status: status,
  render_started_at: null,
  render_finished_at: null,
  output_url: url,
});

describe("VariantRenderCard — t-skel revealed state", () => {
  it("test_variant_skel_revealed_init_when_ready: is-revealed class present immediately when render_status='ready' on mount", () => {
    // Path: revealed = useState(render_status === "ready") → true
    const { container } = render(
      <VariantRenderCard
        variant={makeVariant("ready", "https://cdn.example.com/v.mp4")}
        isNewlyReady={false}
      />
    );
    const tSkel = container.querySelector(".t-skel");
    expect(tSkel).not.toBeNull();
    expect(tSkel!.classList).toContain("is-revealed");
  });

  it("test_variant_skel_not_revealed_when_pending: is-revealed class absent on pending mount", () => {
    // Path: revealed = useState(null !== "ready") → false
    const { container } = render(
      <VariantRenderCard variant={makeVariant(null)} isNewlyReady={false} />
    );
    const tSkel = container.querySelector(".t-skel");
    expect(tSkel).not.toBeNull();
    expect(tSkel!.classList).not.toContain("is-revealed");
  });

  it("test_variant_skel_revealed_on_is_newly_ready: is-revealed added after rAF when isNewlyReady fires", () => {
    // Path: isNewlyReady effect → rAF → setRevealed(true)
    const { container, rerender } = render(
      <VariantRenderCard variant={makeVariant("rendering")} isNewlyReady={false} />
    );
    expect(container.querySelector(".t-skel")!.classList).not.toContain("is-revealed");

    rerender(
      <VariantRenderCard variant={makeVariant("ready", "https://cdn.example.com/v.mp4")} isNewlyReady={true} />
    );
    flushRaf(); // rAF fires → setRevealed(true)
    expect(container.querySelector(".t-skel")!.classList).toContain("is-revealed");
  });

  it("test_variant_skel_revealed_via_polling_fallback: is-revealed added when render_status changes to ready without isNewlyReady", () => {
    // Path: fallback useEffect on render_status → setRevealed(true) without rAF
    const { container, rerender } = render(
      <VariantRenderCard variant={makeVariant("rendering")} isNewlyReady={false} />
    );
    expect(container.querySelector(".t-skel")!.classList).not.toContain("is-revealed");

    act(() => {
      rerender(
        <VariantRenderCard variant={makeVariant("ready", "https://cdn.example.com/v.mp4")} isNewlyReady={false} />
      );
    });
    expect(container.querySelector(".t-skel")!.classList).toContain("is-revealed");
  });
});

// ============================================================================
// 2. OnboardingShell — StepSlide class present
// ============================================================================

import OnboardingShell from "@/app/plan/_components/OnboardingShell";

const SHELL_PROPS = {
  onTikTokContinue: async () => {},
  persona: null,
  onSavePersona: async () => {},
  onChatComplete: () => {},
  onContinueToPlan: () => {},
};

describe("OnboardingShell — StepSlide wrapper", () => {
  it("test_onboarding_step_slide_class_present: step-slide class wraps the current step content", () => {
    const { container } = render(<OnboardingShell {...SHELL_PROPS} />);
    // StepSlide renders a div.step-slide on every step — verify it exists
    expect(container.querySelector(".step-slide")).not.toBeNull();
  });
});
