import { fireEvent, render, screen } from "@testing-library/react";

import { MusicAlignmentDialog } from "@/app/plan/items/[id]/_editor/EditorShell";

describe("MusicAlignmentDialog", () => {
  it("offers Preserve, Re-sync, and Cancel with exact alignment values", () => {
    const onChoose = jest.fn();
    const onCancel = jest.fn();
    render(
      <MusicAlignmentDialog
        open
        preserveAvailable
        preserveReason={null}
        onChoose={onChoose}
        onCancel={onCancel}
      />,
    );

    expect(document.activeElement).toBe(
      screen.getByRole("button", { name: /preserve cuts/i }),
    );
    fireEvent.click(screen.getByRole("button", { name: /preserve cuts/i }));
    fireEvent.click(screen.getByRole("button", { name: /re-sync to beats/i }));
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));

    expect(onChoose).toHaveBeenNthCalledWith(1, "preserve_cuts");
    expect(onChoose).toHaveBeenNthCalledWith(2, "resync_beats");
    expect(onCancel).toHaveBeenCalledTimes(1);
  });

  it("disables Preserve for legacy variants without a linear timeline", () => {
    render(
      <MusicAlignmentDialog
        open
        preserveAvailable={false}
        preserveReason="linear_timeline_unavailable"
        onChoose={jest.fn()}
        onCancel={jest.fn()}
      />,
    );

    expect(
      (screen.getByRole("button", { name: /preserve cuts/i }) as HTMLButtonElement)
        .disabled,
    ).toBe(true);
    expect(screen.getByText(/older render/i)).not.toBeNull();
    expect(
      (screen.getByRole("button", { name: /re-sync to beats/i }) as HTMLButtonElement)
        .disabled,
    ).toBe(false);
    expect(document.activeElement).toBe(
      screen.getByRole("button", { name: /re-sync to beats/i }),
    );
  });

  it("stays hidden when no music-window capability is being saved", () => {
    const { container } = render(
      <MusicAlignmentDialog
        open={false}
        preserveAvailable={false}
        preserveReason={null}
        onChoose={jest.fn()}
        onCancel={jest.fn()}
      />,
    );
    expect(container.innerHTML).toBe("");
  });

  it("handles Escape once without leaking to editor shortcuts", () => {
    const onCancel = jest.fn();
    const outer = jest.fn();
    document.addEventListener("keydown", outer);
    render(
      <MusicAlignmentDialog
        open
        preserveAvailable
        preserveReason={null}
        onChoose={jest.fn()}
        onCancel={onCancel}
      />,
    );

    fireEvent.keyDown(document, { key: "Escape" });
    expect(onCancel).toHaveBeenCalledTimes(1);
    expect(outer).not.toHaveBeenCalled();
    document.removeEventListener("keydown", outer);
  });
});
