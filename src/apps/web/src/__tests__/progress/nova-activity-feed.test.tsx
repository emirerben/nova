/**
 * NovaActivityFeed — the render-progress "Nova AI steps" feed.
 * Covers: empty fallback (state a), live list + pending derived rows
 * (state b), success-receipt collapse/toggle (state c), and the D10
 * failure context line (state d). Tone variants pinned per D20.
 */
import "@testing-library/jest-dom";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { NovaActivityFeed } from "@/components/progress/NovaActivityFeed";
import type { NovaStep } from "@/lib/job-phases";

const liveSteps: NovaStep[] = [
  { id: "s1", ts: "t1", kind: "phase", label: "Analyzed your clips", detail: null, status: "done" },
  { id: "s2", ts: "t2", kind: "agent", label: "Matched a song — Golden Hour", detail: null, status: "done" },
  { id: "s3", ts: "t3", kind: "agent", label: "Wrote your intro line", detail: null, status: "done" },
  {
    id: "s4",
    ts: "t4",
    kind: "render",
    label: "Rendering variant 1 of 3",
    detail: ["Encoding at 1080x1920, 30fps"],
    status: "active",
  },
];

const failedSteps: NovaStep[] = [
  ...liveSteps.slice(0, 3),
  { id: "s4f", ts: "t4", kind: "render", label: "This one didn't render", detail: null, status: "failed" },
];

describe("NovaActivityFeed — empty fallback (state a)", () => {
  it("renders nothing when steps is null", () => {
    const { container } = render(
      <NovaActivityFeed steps={null} isTerminal={false} isSuccess={false} />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("renders nothing when steps is an empty array", () => {
    const { container } = render(
      <NovaActivityFeed steps={[]} isTerminal={false} isSuccess={false} />,
    );
    expect(container).toBeEmptyDOMElement();
  });
});

describe("NovaActivityFeed — live feed (state b)", () => {
  it("renders a role=list with one listitem per step", () => {
    render(<NovaActivityFeed steps={liveSteps} isTerminal={false} isSuccess={false} />);
    const list = screen.getByRole("list", { name: /nova ai steps/i });
    expect(list).toBeInTheDocument();
    expect(screen.getAllByRole("listitem")).toHaveLength(4);
    expect(within(list).getByText("Rendering variant 1 of 3")).toBeInTheDocument();
  });

  it("appends dimmed pending rows derived from the phase order, only while not terminal", () => {
    const { rerender } = render(
      <NovaActivityFeed
        steps={liveSteps}
        isTerminal={false}
        isSuccess={false}
        pendingLabels={["Mixing audio", "Finalizing export"]}
      />,
    );
    expect(screen.getByText("Mixing audio")).toBeInTheDocument();
    expect(screen.getByText("Finalizing export")).toBeInTheDocument();

    rerender(
      <NovaActivityFeed
        steps={liveSteps}
        isTerminal={true}
        isSuccess={true}
        receiptText="Ready in 2:41"
        pendingLabels={["Mixing audio", "Finalizing export"]}
      />,
    );
    expect(screen.queryByText("Mixing audio")).not.toBeInTheDocument();
  });

  it("auto-expands the active row's detail and lets a chevron click toggle it", () => {
    render(<NovaActivityFeed steps={liveSteps} isTerminal={false} isSuccess={false} />);
    // Active row (s4) auto-expanded by default.
    expect(screen.getByText("Encoding at 1080x1920, 30fps")).toBeInTheDocument();
    const btn = screen.getByRole("button", { name: /hide details for rendering variant 1 of 3/i });
    expect(btn).toHaveAttribute("aria-expanded", "true");

    fireEvent.click(btn);
    expect(screen.getByRole("button", { name: /show details/i })).toHaveAttribute(
      "aria-expanded",
      "false",
    );
  });

  it("announces each newly-arrived step label once via a single aria-live region", () => {
    const { container, rerender } = render(
      <NovaActivityFeed steps={liveSteps.slice(0, 1)} isTerminal={false} isSuccess={false} />,
    );
    const liveRegions = () => container.querySelectorAll('[role="status"][aria-live="polite"]');
    expect(liveRegions()).toHaveLength(1);
    expect(liveRegions()[0].textContent).toBe("Analyzed your clips");

    rerender(<NovaActivityFeed steps={liveSteps} isTerminal={false} isSuccess={false} />);
    // Only ONE live region on the feed — no per-row aria-live spam.
    expect(liveRegions()).toHaveLength(1);
    expect(liveRegions()[0].textContent).toBe("Rendering variant 1 of 3");

    // Re-rendering with the identical steps array must not re-announce.
    rerender(<NovaActivityFeed steps={liveSteps} isTerminal={false} isSuccess={false} />);
    expect(liveRegions()[0].textContent).toBe("Rendering variant 1 of 3");
  });
});

describe("NovaActivityFeed — success receipt (state c)", () => {
  it("collapses into a single receipt line with step count and a toggle", () => {
    render(
      <NovaActivityFeed
        steps={liveSteps}
        isTerminal={true}
        isSuccess={true}
        receiptText="Ready in 2:41"
      />,
    );
    expect(screen.getByText("Ready in 2:41")).toBeInTheDocument();
    expect(screen.getByText("4 steps")).toBeInTheDocument();
    expect(screen.queryByRole("list")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "See what Nova did" })).toBeInTheDocument();
  });

  it("'See what Nova did' toggles the full list back open, and back closed", () => {
    render(
      <NovaActivityFeed
        steps={liveSteps}
        isTerminal={true}
        isSuccess={true}
        receiptText="Ready in 2:41"
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "See what Nova did" }));
    expect(screen.getByRole("list", { name: /nova ai steps/i })).toBeInTheDocument();
    expect(screen.getAllByRole("listitem")).toHaveLength(4);

    fireEvent.click(screen.getByRole("button", { name: "Hide steps" }));
    expect(screen.queryByRole("list")).not.toBeInTheDocument();
    expect(screen.getByText("Ready in 2:41")).toBeInTheDocument();
  });
});

describe("NovaActivityFeed — failure (state d, D10 no red)", () => {
  it("shows a 'Completed N of M steps before stopping' context line, list stays visible", () => {
    const { container } = render(
      <NovaActivityFeed steps={failedSteps} isTerminal={true} isSuccess={false} />,
    );
    expect(screen.getByRole("list")).toBeInTheDocument();
    expect(screen.getByText("Completed 3 of 4 steps before stopping")).toBeInTheDocument();
    expect(container.innerHTML).not.toMatch(/text-red|bg-red|border-red/);
  });
});

describe("NovaActivityFeed — tone variants (D20)", () => {
  it("light tone uses lime accent classes on the active row", () => {
    const { container } = render(
      <NovaActivityFeed steps={liveSteps} isTerminal={false} isSuccess={false} tone="light" />,
    );
    const divs = Array.from(container.querySelectorAll("li"));
    expect(divs.some((el) => el.className.includes("bg-lime-50"))).toBe(true);
  });

  it("dark tone uses amber accent classes on the active row (never lime)", () => {
    const { container } = render(
      <NovaActivityFeed steps={liveSteps} isTerminal={false} isSuccess={false} tone="dark" />,
    );
    const divs = Array.from(container.querySelectorAll("li"));
    expect(divs.some((el) => el.className.includes("bg-amber-400/10"))).toBe(true);
    expect(container.innerHTML).not.toMatch(/bg-lime-50/);
  });
});
