/**
 * Transitions-motion feature tests.
 *
 * Covers the new animation state paths introduced by the transitions.dev slice:
 *   1. VariantRenderCard t-skel reveal — 3 logic paths
 *   2. TemplatePreviewModal t-modal animState — 2 logic paths
 *   3. OnboardingShell StepSlide class — 1 smoke path
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
// 2. TemplatePreviewModal — t-modal animState machine
// ============================================================================

// Mock next/navigation (useRouter is called inside the modal)
jest.mock("next/navigation", () => ({
  useRouter: () => ({ push: jest.fn() }),
}));

// Mock template-playback to avoid network calls
jest.mock("@/lib/template-playback", () => ({
  activeTileStore: { setActive: jest.fn() },
  getCachedPlaybackUrl: jest.fn(() => new Promise(() => {})), // never resolves
  invalidatePlaybackUrl: jest.fn(),
}));

import TemplatePreviewModal from "@/app/TemplatePreviewModal";

const TEMPLATE = {
  id: "tmpl-1",
  name: "Test Template",
  total_duration_s: 30,
  slot_count: 3,
  thumbnail_url: null,
  published: true,
};

describe("TemplatePreviewModal — t-modal animState", () => {
  it("test_modal_closed_renders_null_when_no_template: returns null (no DOM) when template=null", () => {
    const { container } = render(
      <TemplatePreviewModal template={null} returnFocusTo={null} onClose={() => {}} />
    );
    // animState stays "closed" → return null
    expect(container.firstChild).toBeNull();
  });

  it("test_modal_open_adds_is_open_class_after_raf: .is-open class on .t-modal after rAF when template provided", () => {
    const { container } = render(
      <TemplatePreviewModal template={TEMPLATE} returnFocusTo={null} onClose={() => {}} />
    );
    // The useEffect fires after the render, then schedules a rAF → flush it:
    flushRaf();
    const tModal = container.querySelector(".t-modal");
    expect(tModal).not.toBeNull();
    expect(tModal!.classList).toContain("is-open");
  });

  it("test_modal_closing_then_closed: template→null while open plays closing then unmounts after MODAL_CLOSE_MS", () => {
    // 1. Open the modal using the spyOn rAF mock (from beforeEach).
    const { container, rerender } = render(
      <TemplatePreviewModal template={TEMPLATE} returnFocusTo={null} onClose={() => {}} />
    );
    flushRaf(); // → animState="open"
    expect(container.querySelector(".t-modal.is-open")).not.toBeNull();

    // 2. Switch to fake timers AFTER the open rAF is done, so setTimeout is now fake.
    jest.useFakeTimers();
    try {
      // Clear template → animState transitions to "closing"
      rerender(<TemplatePreviewModal template={null} returnFocusTo={null} onClose={() => {}} />);
      expect(container.querySelector(".t-modal.is-closing")).not.toBeNull();

      // After MODAL_CLOSE_MS the component should unmount (animState="closed" → return null)
      act(() => { jest.advanceTimersByTime(150); });
      expect(container.firstChild).toBeNull();
    } finally {
      jest.useRealTimers();
    }
  });
});

// ============================================================================
// 3. OnboardingShell — StepSlide class present
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
