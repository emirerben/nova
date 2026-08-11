/**
 * NovaStepRow — expand/collapse affordance, aria-expanded contract, and
 * reduced-motion handling (t-accordion is pure CSS; here we assert the
 * `is-open` class flip and the @media guard's presence in globals.css,
 * plus that the component itself does not gate on JS reduced-motion state
 * — CSS alone must zero the animation).
 */
import "@testing-library/jest-dom";
import { fireEvent, render, screen } from "@testing-library/react";
import fs from "fs";
import path from "path";
import { NovaPendingRow, NovaStepRow } from "@/components/progress/NovaStepRow";
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

const failedStep: NovaStep = {
  id: "s3",
  ts: "2026-08-11T00:00:10Z",
  kind: "render",
  label: "This one didn't render",
  detail: null,
  status: "failed",
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

  it("failed rows use a zinc dash icon, never a red class", () => {
    const { container } = render(
      <NovaStepRow step={failedStep} tone="light" size="full" expanded={false} onToggle={() => {}} />,
    );
    expect(container.innerHTML).not.toMatch(/text-red|bg-red|border-red/);
  });
});

describe("NovaPendingRow", () => {
  it("renders a dimmed, non-interactive placeholder row", () => {
    render(<NovaPendingRow label="Mixing audio" tone="light" size="full" />);
    expect(screen.getByText("Mixing audio")).toBeInTheDocument();
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });
});

describe("t-accordion CSS token (DESIGN.md §6)", () => {
  it("globals.css defines --t-accordion-dur/--t-accordion-ease and a reduced-motion guard", () => {
    const css = fs.readFileSync(
      path.join(__dirname, "../../app/globals.css"),
      "utf-8",
    );
    expect(css).toMatch(/--t-accordion-dur:\s*300ms/);
    expect(css).toMatch(/--t-accordion-ease:\s*cubic-bezier\(0\.23,\s*1,\s*0\.32,\s*1\)/);
    // Reduced-motion zeroes the accordion transition specifically.
    const guardMatch = css.match(
      /@media \(prefers-reduced-motion: reduce\) \{\s*\.t-accordion \{ transition: none !important; \}\s*\}/,
    );
    expect(guardMatch).not.toBeNull();
  });
});
