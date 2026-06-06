import "@testing-library/jest-dom";
import { render, screen } from "@testing-library/react";
import { UploadBar } from "@/components/progress";

describe("UploadBar", () => {
  it("renders a progressbar with correct aria attributes", () => {
    render(<UploadBar progress={0.4} />);
    const bar = screen.getByRole("progressbar");
    expect(bar).toHaveAttribute("aria-valuenow", "40");
    expect(bar).toHaveAttribute("aria-valuemin", "0");
    expect(bar).toHaveAttribute("aria-valuemax", "100");
    expect(bar).toHaveAttribute("aria-label", "Upload progress");
  });

  it("renders custom ariaLabel", () => {
    render(<UploadBar progress={0.5} ariaLabel="Importing files" />);
    expect(screen.getByRole("progressbar")).toHaveAttribute("aria-label", "Importing files");
  });

  it("renders label text when provided", () => {
    render(<UploadBar progress={0.6} label="3 of 5 MB" />);
    expect(screen.getByText("3 of 5 MB")).toBeInTheDocument();
  });

  it("does not render label text when omitted (no ETA)", () => {
    const { container } = render(<UploadBar progress={0.5} />);
    expect(container.querySelector("p")).toBeNull();
  });

  it("clamps progress to 0-100%", () => {
    render(<UploadBar progress={1.5} />);
    const bar = screen.getByRole("progressbar");
    expect(bar).toHaveAttribute("aria-valuenow", "100");
  });
});
