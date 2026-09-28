import "@testing-library/jest-dom";
import { render, screen } from "@testing-library/react";
import { UploadBar } from "@/components/progress";

describe("UploadBar", () => {

  it("clamps progress to 0-100%", () => {
    render(<UploadBar progress={1.5} />);
    const bar = screen.getByRole("progressbar");
    expect(bar).toHaveAttribute("aria-valuenow", "100");
  });
});
