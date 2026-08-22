import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "@jest/globals";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";

describe("AlertDialog", () => {
  it("renders an alertdialog role with the ink action + z-[100]", () => {
    render(
      <AlertDialog open>
        <AlertDialogContent aria-label="Discard your edits?">
          <AlertDialogTitle>Discard your edits?</AlertDialogTitle>
          <AlertDialogCancel>Keep editing</AlertDialogCancel>
          <AlertDialogAction>Discard</AlertDialogAction>
        </AlertDialogContent>
      </AlertDialog>
    );
    const dialog = screen.getByRole("alertdialog", { name: "Discard your edits?" });
    expect(dialog.className).toContain("z-[100]");
    expect(dialog.className).toContain("rounded-2xl");

    const action = screen.getByRole("button", { name: "Discard" });
    expect(action.className.toLowerCase()).not.toContain("red");
    expect(action.className).toContain("bg-[#0c0c0e]");
  });
});
