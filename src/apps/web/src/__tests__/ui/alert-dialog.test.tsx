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
  it("renders an alertdialog role with the stock default action + z-[100]", () => {
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
    expect(dialog.className).toContain("sm:rounded-lg");

    const action = screen.getByRole("button", { name: "Discard" });
    expect(action.className).toContain("bg-primary");

    const cancel = screen.getByRole("button", { name: "Keep editing" });
    expect(cancel.className).toContain("border");
    expect(cancel.className).toContain("bg-background");
  });
});
