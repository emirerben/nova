import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "@jest/globals";
import { InkButton } from "../../components/ui/InkButton";

describe("InkButton", () => {
  it("keeps the default solid CTA sizing", () => {
    render(<InkButton>Save</InkButton>);

    const className = screen.getByRole("button", { name: "Save" }).className;
    expect(className).toContain("px-9");
    expect(className).toContain("py-[15px]");
    expect(className).toContain("text-[15px]");
  });

  it("supports compact dense-surface sizing", () => {
    render(<InkButton size="compact">Save</InkButton>);

    const className = screen.getByRole("button", { name: "Save" }).className;
    expect(className).toContain("h-9");
    expect(className).toContain("px-5");
    expect(className).toContain("text-[13px]");
  });
});
