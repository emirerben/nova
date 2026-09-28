/**
 * NovaStepRow — expand/collapse affordance, aria-expanded contract, and
 * reduced-motion handling (t-accordion is pure CSS; here we assert the
 * `is-open` class flip).
 */
import "@testing-library/jest-dom";
import { fireEvent, render, screen } from "@testing-library/react";
import { NovaStepRow } from "@/components/progress/NovaStepRow";
import type { NovaStep } from "@/lib/job-phases";

const doneStep: NovaStep = {
  id: "s1",
  ts: "2026-08-11T00:00:00Z",
  kind: "phase",
  label: "Analyzed your clips",
  detail: null,
  status: "done",
};

const activeStepWithDetail: NovaStep = {
  id: "s2",
  ts: "2026-08-11T00:00:05Z",
  kind: "render",
  label: "Rendering variant 1 of 3",
  detail: ["Encoding at 1080x1920, 30fps", "Applying captions and text overlays"],
  status: "active",
};

describe("NovaStepRow", () => {
  it("renders a done row with its label, no expand affordance when detail is null", () => {
    render(
      <NovaStepRow step={doneStep} tone="light" size="full" expanded={false} onToggle={() => {}} />,
    );
    expect(screen.getByText("Analyzed your clips")).toBeInTheDocument();
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });

  it("renders a chevron button with aria-expanded=false when collapsed and detail exists", () => {
    render(
      <NovaStepRow
        step={activeStepWithDetail}
        tone="light"
        size="full"
        expanded={false}
        onToggle={() => {}}
      />,
    );
    const btn = screen.getByRole("button", { name: /show details/i });
    expect(btn).toHaveAttribute("aria-expanded", "false");
    // Detail lines are present in the DOM (grid-rows collapse, not unmount)
    // but the wrapper lacks is-open.
    expect(screen.getByText("Encoding at 1080x1920, 30fps")).toBeInTheDocument();
  });

  it("flips aria-expanded and calls onToggle when the chevron is clicked", () => {
    const onToggle = jest.fn();
    const { rerender } = render(
      <NovaStepRow
        step={activeStepWithDetail}
        tone="light"
        size="full"
        expanded={false}
        onToggle={onToggle}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /show details/i }));
    expect(onToggle).toHaveBeenCalledTimes(1);

    rerender(
      <NovaStepRow
        step={activeStepWithDetail}
        tone="light"
        size="full"
        expanded={true}
        onToggle={onToggle}
      />,
    );
    expect(screen.getByRole("button", { name: /hide details/i })).toHaveAttribute(
      "aria-expanded",
      "true",
    );
  });

  it("applies the is-open class to the .t-accordion wrapper only when expanded", () => {
    const { container, rerender } = render(
      <NovaStepRow
        step={activeStepWithDetail}
        tone="light"
        size="full"
        expanded={false}
        onToggle={() => {}}
      />,
    );
    expect(container.querySelector(".t-accordion")).not.toHaveClass("is-open");

    rerender(
      <NovaStepRow
        step={activeStepWithDetail}
        tone="light"
        size="full"
        expanded={true}
        onToggle={() => {}}
      />,
    );
    expect(container.querySelector(".t-accordion")).toHaveClass("is-open");
  });
});

describe("NovaPendingRow", () => {
});

describe("t-accordion CSS token (DESIGN.md §6)", () => {
});
