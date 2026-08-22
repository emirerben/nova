import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "@jest/globals";
import {
  Dialog,
  DialogContent,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";

describe("Dialog", () => {
  it("renders a dialog role with the Kria z-[100] + card treatment when open", () => {
    render(
      <Dialog open>
        <DialogTrigger>Open</DialogTrigger>
        <DialogContent className="extra-class">
          <DialogTitle>Discard your edits?</DialogTitle>
        </DialogContent>
      </Dialog>
    );
    const dialog = screen.getByRole("dialog", { name: "Discard your edits?" });
    expect(dialog.className).toContain("z-[100]");
    expect(dialog.className).toContain("rounded-2xl");
    expect(dialog.className).toContain("extra-class");
  });
});
