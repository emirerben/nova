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

/**
 * The reveal variant is the default layout with ONE change: the action row sits
 * above the supporting detail instead of below it, so the CTA lands in the first
 * viewport. Everything else must stay identical — these tests pin both halves of
 * that contract, because an earlier version of this change quietly removed the
 * status badge, the subline, and the retune button along with the reorder.
 */
describe("PersonaEditor — reveal moves the CTA, and changes nothing else", () => {
  it("puts the CTA before the supporting detail", () => {
    render(<PersonaEditor {...baseProps} variant="reveal" />);

    const cta = screen.getByRole("button", { name: "Get my ideas →" });
    const pillars = screen.getByText("Content pillars");

    expect(cta.compareDocumentPosition(pillars) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("keeps the status badge, the subline and the rationale card", () => {
    render(<PersonaEditor {...baseProps} variant="reveal" />);

    expect(screen.getByText("AI-generated")).toBeInTheDocument();
    expect(screen.getByText(/This is who we think you are\./)).toBeInTheDocument();
    expect(screen.getByText("Why this lane")).toBeInTheDocument();
  });

  it("keeps the rationale card above the summary, as in the default layout", () => {
    render(<PersonaEditor {...baseProps} variant="reveal" />);

    const rationale = screen.getByText("Why this lane");
    const summary = screen.getByText(persona.summary!);
    expect(
      rationale.compareDocumentPosition(summary) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  it("keeps the retune button", () => {
    render(<PersonaEditor {...baseProps} variant="reveal" onRetuneFromFeedback={async () => {}} />);
    expect(screen.getByRole("button", { name: /Update from feedback/i })).toBeInTheDocument();
  });

  it("continue still flushes edits", () => {
    const onContinue = jest.fn();
    render(<PersonaEditor {...baseProps} variant="reveal" onContinue={onContinue} />);

    fireEvent.click(screen.getByRole("button", { name: "Get my ideas →" }));

    expect(onContinue).toHaveBeenCalledTimes(1);
  });

  it("default variant is unchanged — detail still follows the summary directly", () => {
    render(<PersonaEditor {...baseProps} status="ready" />);

    const cta = screen.getByRole("button", { name: "Get my ideas →" });
    const pillars = screen.getByText("Content pillars");
    // In the default layout the CTA comes AFTER the detail.
    expect(pillars.compareDocumentPosition(cta) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });
});

/**
 * A failed generation leaves a truthy-but-empty persona (only footage_type_bias),
 * which OnboardingShell still routes into the reveal. Observed live: status
 * "failed", persona {footage_type_bias: [...]}, summary null. The recovery path
 * is the existing morphing retune button ("Generate persona").
 */
describe("PersonaEditor — reveal with an empty persona", () => {
  const empty = { footage_type_bias: ["talking_head"] } as unknown as PersonaContent;
  const emptyProps = { ...baseProps, persona: empty, status: "failed" as const };

  it("offers the Generate persona recovery path", () => {
    render(
      <PersonaEditor {...emptyProps} variant="reveal" onRetuneFromFeedback={async () => {}} />,
    );
    expect(screen.getByRole("button", { name: /Generate persona/i })).toBeInTheDocument();
  });

  it("omits the supporting-detail section rather than rendering a bare divider", () => {
    render(<PersonaEditor {...emptyProps} variant="reveal" />);
    expect(screen.queryByText("Content pillars")).not.toBeInTheDocument();
    expect(screen.queryByText("Sample topics")).not.toBeInTheDocument();
  });

  it("still renders the supporting detail for a real persona", () => {
    render(<PersonaEditor {...baseProps} variant="reveal" />);
    expect(screen.getByText("Content pillars")).toBeInTheDocument();
  });
});
