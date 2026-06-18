/**
 * OnboardingShell tests.
 *
 * Verifies:
 *   1. The rail renders all 4 step labels.
 *   2. The "What you make" step shows 4 cards and keeps Continue disabled
 *      until a selection is made.
 *   3. Selecting a card enables Continue.
 */

import "@testing-library/jest-dom";
import { act, fireEvent, render, screen } from "@testing-library/react";
import OnboardingShell from "@/app/plan/_components/OnboardingShell";

// Minimal no-op props so the shell renders at Step 1.
const noopProps = {
  onTikTokContinue: async () => {},
  persona: null,
  onSavePersona: async () => {},
  onChatComplete: () => {},
  onContinueToPlan: () => {},
};

describe("OnboardingShell — rail", () => {
  it("renders all 4 step labels in the left rail", () => {
    render(<OnboardingShell {...noopProps} />);
    expect(screen.getByText("TikTok")).toBeInTheDocument();
    expect(screen.getByText("What you make")).toBeInTheDocument();
    expect(screen.getByText("Style")).toBeInTheDocument();
    expect(screen.getByText("First plan")).toBeInTheDocument();
  });
});

describe("OnboardingShell — What you make step", () => {
  /**
   * Advance past Step 1 (TikTok) by clicking "Skip →".
   * TikTokPreScreen renders a "Skip →" button that calls onContinue("").
   * The handler is async so we wrap in act().
   */
  async function advanceToStep2() {
    render(<OnboardingShell {...noopProps} />);
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /skip/i }));
    });
  }

  it("shows all 4 footage-type cards after skipping TikTok", async () => {
    await advanceToStep2();
    expect(screen.getByText("Talking to camera")).toBeInTheDocument();
    expect(screen.getByText("B-roll & nature")).toBeInTheDocument();
    expect(screen.getByText("Vlogs & daily life")).toBeInTheDocument();
    expect(screen.getByText("Mixed")).toBeInTheDocument();
  });

  it("Continue is disabled until a card is selected", async () => {
    await advanceToStep2();
    const continueBtn = screen.getByRole("button", { name: /continue/i });
    expect(continueBtn).toBeDisabled();
  });

  it("Continue is enabled after selecting a card", async () => {
    await advanceToStep2();
    fireEvent.click(screen.getByText("Talking to camera"));
    const continueBtn = screen.getByRole("button", { name: /continue/i });
    expect(continueBtn).not.toBeDisabled();
  });
});
