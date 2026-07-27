/**
 * CaptionsDrawer — the left-rail Captions tool.
 *
 * Pins the contract the drawer exists to establish: globals reachable WITHOUT a
 * cue selection, find-and-fix as a first-class control, honest subtitles-off and
 * zero-cue states, and an a11y model that survives 40 rows (roving arrows,
 * aria-current, no <input> inside a <button>).
 */

import "@testing-library/jest-dom";
import React from "react";
import { fireEvent, render, screen, within } from "@testing-library/react";

import CaptionsDrawer, {
  type CaptionsDrawerControl,
} from "@/app/plan/items/[id]/_editor/CaptionsDrawer";

const CUES = [
  { id: "caption-0", text: "Geçen hafta bir şey oldu", start_s: 2, end_s: 5 },
  { id: "caption-1", text: "ve hâlâ inanamıyorum", start_s: 5, end_s: 8 },
  { id: "caption-2", text: "çünkü kimse inanamıyorum beklemiyordu", start_s: 8, end_s: 11 },
];

function renderDrawer(overrides: Partial<CaptionsDrawerControl> = {}) {
  const onSelectCue = jest.fn();
  const onEditCueText = jest.fn();
  const onPatchMeta = jest.fn();
  const onReplaceAll = jest.fn(() => 2);
  const onChangeLanguage = jest.fn();
  const onRetranscribe = jest.fn();
  const props: CaptionsDrawerControl = {
    cues: CUES,
    selectedId: null,
    currentTime: 6,
    meta: {
      enabled: true,
      style: "sentence",
      font: null,
      y_frac: 0.8,
      size_px: 78,
      color: "#FFFFFF",
      stroke_width: 4,
      shadow_enabled: true,
    },
    language: null,
    readOnly: false,
    busy: "idle",
    error: null,
    onSelectCue,
    onEditCueText,
    onPatchMeta,
    onReplaceAll,
    onChangeLanguage,
    onRetranscribe,
    ...overrides,
  };
  render(<CaptionsDrawer {...props} />);
  return { onSelectCue, onEditCueText, onPatchMeta, onReplaceAll, onChangeLanguage, onRetranscribe };
}

describe("CaptionsDrawer — cue list", () => {
  it("lists every cue with its timecode", () => {
    renderDrawer();
    expect(screen.getByRole("button", { name: /Caption at 0:02/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Caption at 0:05/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Caption at 0:08/ })).toBeInTheDocument();
  });

  it("marks the cue under the playhead with aria-current, not colour alone", () => {
    renderDrawer({ currentTime: 6 });
    const playing = screen.getByRole("button", { name: /Caption at 0:05/ });
    expect(playing).toHaveAttribute("aria-current", "true");
    // Redundant non-colour signal for the same state.
    expect(playing.className).toContain("border-lime-600");
    expect(
      screen.getByRole("button", { name: /Caption at 0:02/ }),
    ).not.toHaveAttribute("aria-current");
  });

  it("clicking a cue selects it and opens inline editing in the drawer, not across the screen", () => {
    const { onSelectCue } = renderDrawer();
    fireEvent.click(screen.getByRole("button", { name: /Caption at 0:05/ }));
    expect(onSelectCue).toHaveBeenCalledWith("caption-1");
    // Fixing a typo must not require a trip to the right-hand inspector.
    expect(screen.getByLabelText("Edit caption at 0:05")).toBeInTheDocument();
  });

  it("routes edits through onEditCueText so they join the same session state as every other lane", () => {
    const { onEditCueText } = renderDrawer();
    fireEvent.click(screen.getByRole("button", { name: /Caption at 0:05/ }));
    fireEvent.change(screen.getByLabelText("Edit caption at 0:05"), {
      target: { value: "ve hâlâ inanamıyorum!" },
    });
    expect(onEditCueText).toHaveBeenCalledWith("caption-1", "ve hâlâ inanamıyorum!");
  });

  it("keeps the editing input OUT of a button — nesting one swallows clicks and keystrokes", () => {
    renderDrawer();
    fireEvent.click(screen.getByRole("button", { name: /Caption at 0:05/ }));
    const input = screen.getByLabelText("Edit caption at 0:05");
    expect(input.closest("button")).toBeNull();
    expect(input.closest("li")).not.toBeNull();
  });

  it("moves focus between rows with the arrow keys", () => {
    renderDrawer();
    const first = screen.getByRole("button", { name: /Caption at 0:02/ });
    first.focus();
    fireEvent.keyDown(screen.getByRole("list", { name: "Caption lines" }), { key: "ArrowDown" });
    expect(screen.getByRole("button", { name: /Caption at 0:05/ })).toHaveFocus();
    fireEvent.keyDown(screen.getByRole("list", { name: "Caption lines" }), { key: "ArrowUp" });
    expect(first).toHaveFocus();
  });
});

describe("CaptionsDrawer — find and replace", () => {
  it("counts matches and steps through them without filtering the list away", () => {
    const { onSelectCue } = renderDrawer();
    fireEvent.change(screen.getByLabelText("Find in captions"), {
      target: { value: "inanamıyorum" },
    });
    expect(screen.getByText("1 of 2")).toBeInTheDocument();
    // Context is preserved: non-matching rows stay on screen.
    expect(screen.getByRole("button", { name: /Caption at 0:02/ })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Next match" }));
    expect(screen.getByText("2 of 2")).toBeInTheDocument();
    expect(onSelectCue).toHaveBeenCalledWith("caption-2");
  });

  it("wraps backwards from the first match", () => {
    const { onSelectCue } = renderDrawer();
    fireEvent.change(screen.getByLabelText("Find in captions"), {
      target: { value: "inanamıyorum" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Previous match" }));
    expect(screen.getByText("2 of 2")).toBeInTheDocument();
    expect(onSelectCue).toHaveBeenCalledWith("caption-2");
  });

  it("reports zero matches instead of silently showing nothing", () => {
    renderDrawer();
    fireEvent.change(screen.getByLabelText("Find in captions"), {
      target: { value: "zzzznotinthere" },
    });
    expect(screen.getByText("0")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Replace all" })).not.toBeInTheDocument();
  });

  it("offers Replace all only when there are matches, and says how to undo it", () => {
    const { onReplaceAll } = renderDrawer();
    fireEvent.change(screen.getByLabelText("Find in captions"), {
      target: { value: "inanamıyorum" },
    });
    fireEvent.change(screen.getByLabelText("Replace matches with"), {
      target: { value: "inanmıyorum" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Replace all" }));
    expect(onReplaceAll).toHaveBeenCalledWith("inanamıyorum", "inanmıyorum");
    // A bulk edit is only safe to offer if the way back is stated.
    expect(screen.getByText("Replaced 2 lines. Cmd+Z to undo.")).toBeInTheDocument();
  });

  it("uses a visible label for Find, never placeholder-as-label", () => {
    renderDrawer();
    const field = screen.getByLabelText("Find in captions");
    expect(field).toHaveAttribute("id", "captions-find");
    expect(screen.getByText("Find in captions").tagName).toBe("LABEL");
  });
});

describe("CaptionsDrawer — globals", () => {
  it("reaches the variant-wide styling with NO cue selected — the whole point of moving it here", () => {
    const { onPatchMeta } = renderDrawer({ selectedId: null });
    fireEvent.click(screen.getByRole("button", { name: /All captions/ }));
    fireEvent.change(screen.getByLabelText("All captions font size"), {
      target: { value: "96" },
    });
    expect(onPatchMeta).toHaveBeenCalledWith({ size_px: 96 });
  });

  it("summarises the current styling while collapsed, so it informs without being opened", () => {
    renderDrawer({
      meta: {
        enabled: true,
        style: "sentence",
        font: "Montserrat Bold",
        y_frac: 0.8,
        size_px: 96,
      },
    });
    const summary = screen.getByRole("button", { name: /All captions/ });
    expect(within(summary).getByText("Montserrat Bold · 96")).toBeInTheDocument();
  });

  it("patches stroke, colour and shadow through the same meta channel", () => {
    const { onPatchMeta } = renderDrawer();
    fireEvent.click(screen.getByRole("button", { name: /All captions/ }));
    fireEvent.change(screen.getByLabelText("All captions stroke width"), {
      target: { value: "8" },
    });
    expect(onPatchMeta).toHaveBeenCalledWith({ stroke_width: 8 });
    fireEvent.click(screen.getByRole("switch", { name: "All captions shadow" }));
    expect(onPatchMeta).toHaveBeenCalledWith({ shadow_enabled: false });
  });
});

describe("CaptionsDrawer — states", () => {
  it("toggles subtitles through the meta channel so Save carries it", () => {
    const { onPatchMeta } = renderDrawer();
    fireEvent.click(screen.getByRole("switch", { name: "Subtitles" }));
    expect(onPatchMeta).toHaveBeenCalledWith({ enabled: false });
  });

  it("with subtitles off, reassures that cues are kept and leaves the switch reachable", () => {
    renderDrawer({
      meta: { enabled: false, style: "sentence", font: null, y_frac: 0.8 },
    });
    expect(
      screen.getByText(/Your caption lines are saved — turn subtitles on to edit them/),
    ).toBeInTheDocument();
    // Never trap the user: the way back must stay on screen.
    expect(screen.getByRole("switch", { name: "Subtitles" })).toBeInTheDocument();
    expect(screen.queryByRole("list", { name: "Caption lines" })).not.toBeInTheDocument();
  });

  it("gives the zero-cue empty state a real recovery action, not just a dead sentence", () => {
    const { onRetranscribe } = renderDrawer({ cues: [] });
    expect(screen.getByText("No caption lines yet.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Re-transcribe" }));
    expect(onRetranscribe).toHaveBeenCalled();
  });

  it("names which stage of the language switch is running, not one anonymous spinner", () => {
    renderDrawer({ busy: "saving" });
    expect(screen.getByText("Saving your edits…")).toBeInTheDocument();
  });

  it("distinguishes the transcribing stage from the saving stage", () => {
    renderDrawer({ busy: "transcribing" });
    expect(screen.getByText("Re-transcribing…")).toBeInTheDocument();
  });

  it("surfaces an error without destroying the list", () => {
    renderDrawer({ error: "Couldn't re-transcribe. Your edits were saved." });
    expect(
      screen.getByText("Couldn't re-transcribe. Your edits were saved."),
    ).toBeInTheDocument();
    expect(screen.getByRole("list", { name: "Caption lines" })).toBeInTheDocument();
  });

  it("locks every write while read-only", () => {
    const { onPatchMeta } = renderDrawer({ readOnly: true });
    expect(screen.getByRole("switch", { name: "Subtitles" })).toBeDisabled();
    fireEvent.click(screen.getByRole("switch", { name: "Subtitles" }));
    expect(onPatchMeta).not.toHaveBeenCalled();
  });
});

describe("CaptionsDrawer — language switch", () => {
  it("hides the language row when the variant has no language override (narrated)", () => {
    renderDrawer({ language: null });
    expect(screen.queryByRole("button", { name: "Change caption language" })).not.toBeInTheDocument();
  });

  it("states BOTH consequences before re-transcribing — the save and the rewrite", () => {
    renderDrawer({ language: "tr" });
    fireEvent.click(screen.getByRole("button", { name: "Change caption language" }));
    expect(
      screen.getByText(/saves your current\s+edits, then re-transcribes/),
    ).toBeInTheDocument();
    expect(screen.getByText(/your caption text\s+edits are replaced/)).toBeInTheDocument();
  });

  it("requires the explicit confirm — cancelling changes nothing", () => {
    const { onChangeLanguage } = renderDrawer({ language: "tr" });
    fireEvent.click(screen.getByRole("button", { name: "Change caption language" }));
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(onChangeLanguage).not.toHaveBeenCalled();
  });

  it("switches to the other language on confirm", () => {
    const { onChangeLanguage } = renderDrawer({ language: "tr" });
    fireEvent.click(screen.getByRole("button", { name: "Change caption language" }));
    fireEvent.click(screen.getByRole("button", { name: "Save & re-transcribe" }));
    expect(onChangeLanguage).toHaveBeenCalledWith("en");
  });
});
