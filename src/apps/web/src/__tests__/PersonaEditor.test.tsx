import "@testing-library/jest-dom";
import { fireEvent, render, screen } from "@testing-library/react";
import PersonaEditor from "@/app/plan/_components/PersonaEditor";
import type { PersonaContent } from "@/lib/plan-api";

const persona: PersonaContent = {
  summary: "A thoughtful creator sharing practical city-living ideas.",
  content_pillars: ["City guides", "Simple routines", "Local food", "Creative work"],
  tone: "Warm and observant",
  audience: "Curious urban creatives",
  posting_cadence: "Three times a week",
  posts_per_week: 3,
  sample_topics: [
    "A perfect Sunday route",
    "Small-space rituals",
    "Hidden neighborhood spots",
    "Easy hosting ideas",
    "Creative morning routines",
    "What I learned this week",
  ],
  rationale: "Your strongest lane combines useful local perspective with a personal point of view.",
};

const baseProps = {
  persona,
  status: "ready" as const,
  onSave: async () => {},
  onContinue: jest.fn(),
  continueLabel: "Get my ideas →",
};

describe("PersonaEditor", () => {
  it("reveal puts the CTA before the supporting detail", () => {
    render(<PersonaEditor {...baseProps} variant="reveal" />);

    const cta = screen.getByRole("button", { name: "Get my ideas →" });
    const pillars = screen.getByText("Content pillars");

    expect(cta.compareDocumentPosition(pillars) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("reveal hides manage-only chrome", () => {
    render(
      <PersonaEditor
        {...baseProps}
        variant="reveal"
        status="ready"
        onRetuneFromFeedback={async () => {}}
      />,
    );

    expect(screen.queryByText("AI-generated")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Update from feedback/i })).not.toBeInTheDocument();
    expect(screen.queryByText("This is who we think you are.")).not.toBeInTheDocument();
  });

  it("manage is unchanged", () => {
    render(<PersonaEditor {...baseProps} status="ready" />);

    expect(screen.getByText("AI-generated")).toBeInTheDocument();
    expect(screen.getByText(/This is who we think you are\./)).toBeInTheDocument();
    const rationale = screen.getByText("Why this lane");
    const summary = screen.getByText(persona.summary);
    expect(rationale.compareDocumentPosition(summary) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("reveal renders the rationale last", () => {
    render(<PersonaEditor {...baseProps} variant="reveal" />);

    const rationale = screen.getByText("Why this lane");
    const topics = screen.getByText("Sample topics");
    expect(topics.compareDocumentPosition(rationale) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("continue still flushes edits", () => {
    const onContinue = jest.fn();
    render(<PersonaEditor {...baseProps} variant="reveal" onContinue={onContinue} />);

    fireEvent.click(screen.getByRole("button", { name: "Get my ideas →" }));

    expect(onContinue).toHaveBeenCalledTimes(1);
  });
});

/**
 * A failed generation leaves a truthy-but-empty persona (only footage_type_bias),
 * which OnboardingShell still routes into the reveal. Observed live: status
 * "failed", persona {footage_type_bias: [...]}, summary null.
 */
describe("PersonaEditor — reveal with an empty persona", () => {
  const empty = { footage_type_bias: ["talking_head"] } as unknown as PersonaContent;
  const emptyProps = { ...baseProps, persona: empty, status: "failed" as const };

  it("offers a way to retry instead of stranding the user", () => {
    render(
      <PersonaEditor {...emptyProps} variant="reveal" onRetuneFromFeedback={async () => {}} />,
    );
    expect(screen.getByRole("button", { name: /Generate persona/i })).toBeInTheDocument();
  });

  it("withholds the continue CTA, which would 409 with no persona", () => {
    render(
      <PersonaEditor {...emptyProps} variant="reveal" onRetuneFromFeedback={async () => {}} />,
    );
    expect(screen.queryByRole("button", { name: "Get my ideas →" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Write it myself/i })).toBeInTheDocument();
  });

  it("does not show the retry button once there is a real persona", () => {
    render(<PersonaEditor {...baseProps} variant="reveal" onRetuneFromFeedback={async () => {}} />);
    expect(screen.queryByRole("button", { name: /Generate persona/i })).not.toBeInTheDocument();
  });

  it("omits the supporting-detail section when there is nothing to support", () => {
    render(<PersonaEditor {...emptyProps} variant="reveal" />);
    expect(screen.queryByText("What we based it on")).not.toBeInTheDocument();
    expect(screen.queryByText("Content pillars")).not.toBeInTheDocument();
  });

  it("still renders the supporting-detail section for a real persona", () => {
    render(<PersonaEditor {...baseProps} variant="reveal" />);
    expect(screen.getByText("What we based it on")).toBeInTheDocument();
  });
});
