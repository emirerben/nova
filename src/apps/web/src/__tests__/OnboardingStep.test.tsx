import "@testing-library/jest-dom";
import { fireEvent, render, screen } from "@testing-library/react";
import OnboardingStep from "@/app/plan/_components/OnboardingStep";
import type { PersonaQuestionnaire } from "@/lib/plan-api";

const SAVED: PersonaQuestionnaire = {
  work: "founder",
  school: "",
  social: "founder friends",
  location: "London",
  hobbies: "watching football",
  travels: "",
  passions: "building products",
  tiktok_handle: "",
};

function noop() {}

describe("OnboardingStep", () => {
  it("starts blank when there are no saved answers", () => {
    render(<OnboardingStep onSubmit={noop} submitting={false} />);
    // First card is the "work" question — its textarea should be empty.
    expect(screen.getByRole("textbox")).toHaveValue("");
  });

  it("pre-fills from a previously-saved questionnaire so the user doesn't retype", () => {
    render(<OnboardingStep onSubmit={noop} submitting={false} initialAnswers={SAVED} />);
    // The first field ("work") shows the saved value immediately on mount.
    expect(screen.getByRole("textbox")).toHaveValue("founder");
  });

  it("treats a null saved questionnaire as empty (no crash)", () => {
    render(<OnboardingStep onSubmit={noop} submitting={false} initialAnswers={null} />);
    expect(screen.getByRole("textbox")).toHaveValue("");
  });

  it("submits the pre-filled answers, preserving untouched saved fields", () => {
    const onSubmit = jest.fn();
    render(<OnboardingStep onSubmit={onSubmit} submitting={false} initialAnswers={SAVED} />);
    // Walk to the last card, touching nothing. The advance control is the only
    // button carrying a "→" (Next/Skip); Back uses "←". 7 advances over 8 fields.
    for (let i = 0; i < 7; i++) {
      fireEvent.click(screen.getByRole("button", { name: /→/ }));
    }
    fireEvent.click(screen.getByRole("button", { name: /build my persona/i }));
    expect(onSubmit).toHaveBeenCalledWith(SAVED);
  });
});
