import "@testing-library/jest-dom";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
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

  it("keeps the status badge, the persona info dot and the rationale card", () => {
    render(<PersonaEditor {...baseProps} variant="reveal" />);

    expect(screen.getByText("AI-generated")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "About Your creator profile" })).toBeInTheDocument();
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
 * A failed generation can leave a truthy-but-empty persona (only
 * footage_type_bias). The profile route must still offer its recovery action.
 */
describe("PersonaEditor — reveal with an empty persona", () => {
  const empty = { footage_type_bias: ["talking_head"] } as unknown as PersonaContent;
  const emptyProps = { ...baseProps, persona: empty, status: "failed" as const };

  it("offers the Generate profile recovery path", () => {
    render(
      <PersonaEditor {...emptyProps} variant="reveal" onRetuneFromFeedback={async () => {}} />,
    );
    expect(screen.getByRole("button", { name: /Generate profile/i })).toBeInTheDocument();
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

describe("PersonaEditor — posts-per-week field after the declutter refactor", () => {
  beforeAll(() => {
    if (typeof globalThis.ResizeObserver === "undefined") {
      class RO {
        observe() {}
        unobserve() {}
        disconnect() {}
      }
      (globalThis as unknown as Record<string, unknown>).ResizeObserver = RO;
    }
  });

  it("keeps the input labelled and exposes the cadence explainer via InfoDot", () => {
    render(<PersonaEditor {...baseProps} startInEdit />);
    expect(screen.getByLabelText("Posts per week")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "About Posts per week" }));
    expect(screen.getByText(/infers it from your cadence/)).toBeInTheDocument();
  });
});

describe("PersonaEditor — explicit TikTok visual-style consent", () => {
  const tiktokProfile = { handle: "kria_creator", video_count: 12 };

  it("explains the bounded review before starting it", async () => {
    const onAnalyzeTikTokStyle = jest.fn().mockResolvedValue(undefined);
    render(
      <PersonaEditor
        {...baseProps}
        tiktokProfile={tiktokProfile}
        tiktokStyleAnalysisAvailable
        onAnalyzeTikTokStyle={onAnalyzeTikTokStyle}
      />,
    );

    expect(screen.getByText(/up to eight representative public TikTok videos/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Review my TikTok visual style" }));

    await waitFor(() => expect(onAnalyzeTikTokStyle).toHaveBeenCalledTimes(1));
  });

  it("keeps the consent action retryable when dispatch fails", async () => {
    const onAnalyzeTikTokStyle = jest.fn().mockRejectedValue(new Error("offline"));
    render(
      <PersonaEditor
        {...baseProps}
        tiktokProfile={tiktokProfile}
        tiktokStyleAnalysisAvailable
        onAnalyzeTikTokStyle={onAnalyzeTikTokStyle}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Review my TikTok visual style" }));

    expect(await screen.findByText(/couldn’t start the TikTok visual-style review/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Review my TikTok visual style" })).toBeEnabled();
  });

  it("shows the 90-day reuse state without another paid action", () => {
    render(
      <PersonaEditor
        {...baseProps}
        tiktokProfile={tiktokProfile}
        tiktokStyleAnalysisAvailable
        onAnalyzeTikTokStyle={jest.fn()}
        tiktokStyleAnalysisStatus="ready"
      />,
    );

    expect(screen.getByText(/reuse this analysis for 90 days/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Review my TikTok visual style" })).not.toBeInTheDocument();
  });

  it("hides the paid action when the server capability is off", () => {
    render(
      <PersonaEditor
        {...baseProps}
        tiktokProfile={tiktokProfile}
        onAnalyzeTikTokStyle={jest.fn()}
      />,
    );

    expect(screen.queryByText(/up to eight representative public TikTok videos/i)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Review my TikTok visual style" })).not.toBeInTheDocument();
  });

  it("shows a deliberate retry after a terminal analysis failure", () => {
    render(
      <PersonaEditor
        {...baseProps}
        tiktokProfile={tiktokProfile}
        tiktokStyleAnalysisAvailable
        tiktokStyleAnalysisStatus="failed"
        onAnalyzeTikTokStyle={jest.fn()}
      />,
    );

    expect(screen.getByText(/could not finish/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Retry TikTok visual-style review" })).toBeEnabled();
  });
});
