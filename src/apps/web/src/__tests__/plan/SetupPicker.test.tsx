import React from "react";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import SetupPicker from "@/app/plan/items/[id]/components/SetupPicker";
import type { SetupPickerProps } from "@/app/plan/items/[id]/components/SetupPicker";

function renderPicker(overrides: Partial<SetupPickerProps> = {}) {
  const onPatch = jest.fn().mockResolvedValue(undefined);
  const props: SetupPickerProps = {
    resolvedFormat: "montage",
    rawEditFormat: "montage",
    montagePreset: "classic",
    subtitledEnabled: true,
    showTalkingHead: false,
    hasGuide: false,
    startCollapsed: false,
    onPatch,
    ...overrides,
  };
  const utils = render(<SetupPicker {...props} />);
  return { onPatch: (props.onPatch as jest.Mock) ?? onPatch, ...utils };
}

const typeReceipt = () => screen.getByRole("button", { name: /^Type/ });

describe("SetupPicker PATCH payloads", () => {
  it("montage without a guide stamps the already-filmed default", async () => {
    const { onPatch } = renderPicker({
      resolvedFormat: "narrated_planned",
      rawEditFormat: "narrated_ready",
    });
    fireEvent.click(screen.getByRole("radio", { name: /Music montage/ }));
    await waitFor(() =>
      expect(onPatch).toHaveBeenCalledWith({
        edit_format: "montage",
        content_mode: "existing_footage",
      }),
    );
  });

  it("montage with an accepted guide leaves content_mode alone", async () => {
    const { onPatch } = renderPicker({
      resolvedFormat: "narrated_planned",
      rawEditFormat: "narrated_ready",
      hasGuide: true,
    });
    fireEvent.click(screen.getByRole("radio", { name: /Music montage/ }));
    await waitFor(() =>
      expect(onPatch).toHaveBeenCalledWith({ edit_format: "montage" }),
    );
  });

  it("voiceover selects the narrated_ready sub-mode", async () => {
    const { onPatch } = renderPicker();
    fireEvent.click(screen.getByRole("radio", { name: /Voiceover/ }));
    await waitFor(() =>
      expect(onPatch).toHaveBeenCalledWith({ edit_format: "narrated_ready" }),
    );
  });

  it("legacy narrated_planned items still upgrade to narrated_ready on click", async () => {
    // resolvedFormat folds all narrated sub-modes onto the Voiceover card, so
    // the no-op guard must compare the raw stored value, not the folded one.
    const { onPatch } = renderPicker({
      resolvedFormat: "narrated_planned",
      rawEditFormat: "narrated_planned",
    });
    fireEvent.click(screen.getByRole("radio", { name: /Voiceover/ }));
    await waitFor(() =>
      expect(onPatch).toHaveBeenCalledWith({ edit_format: "narrated_ready" }),
    );
  });

  it("re-clicking the format the item already has is a no-op", () => {
    const { onPatch } = renderPicker({
      resolvedFormat: "narrated_planned",
      rawEditFormat: "narrated_ready",
    });
    fireEvent.click(screen.getByRole("radio", { name: /Voiceover/ }));
    expect(onPatch).not.toHaveBeenCalled();
  });

  it("re-clicking Montage on a default create_new item still stamps existing_footage", async () => {
    // Default new items land on montage + create_new + no guide with nothing
    // in the DOM to upload clips into (PR #833 removed the mode toggle) — the
    // no-op guard must not swallow this click.
    const { onPatch } = renderPicker({
      resolvedFormat: "montage",
      rawEditFormat: "montage",
      contentMode: "create_new",
    });
    fireEvent.click(screen.getByRole("radio", { name: /Music montage/ }));
    await waitFor(() =>
      expect(onPatch).toHaveBeenCalledWith({
        edit_format: "montage",
        content_mode: "existing_footage",
      }),
    );
  });

  it("re-clicking Montage when already existing_footage is a no-op", () => {
    const { onPatch } = renderPicker({
      resolvedFormat: "montage",
      rawEditFormat: "montage",
      contentMode: "existing_footage",
    });
    fireEvent.click(screen.getByRole("radio", { name: /Music montage/ }));
    expect(onPatch).not.toHaveBeenCalled();
  });
});

describe("SetupPicker optimistic format", () => {
  it("reverts the receipt to the server value when the PATCH fails", async () => {
    let rejectPatch: (err: Error) => void = () => undefined;
    const onPatch = jest.fn(
      () =>
        new Promise<void>((_, reject) => {
          rejectPatch = reject;
        }),
    );
    renderPicker({ onPatch });
    fireEvent.click(screen.getByRole("radio", { name: /Voiceover/ }));
    // Optimistic: receipt follows the click immediately.
    expect(typeReceipt().textContent).toContain("Voiceover");
    rejectPatch(new Error("network"));
    await waitFor(() =>
      expect(typeReceipt().textContent).toContain("Music montage"),
    );
  });
});
