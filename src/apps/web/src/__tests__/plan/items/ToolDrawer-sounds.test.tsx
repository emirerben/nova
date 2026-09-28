import "@testing-library/jest-dom";
import type { ComponentProps } from "react";
import { fireEvent, render, screen, within } from "@testing-library/react";
import ToolDrawer from "@/app/plan/items/[id]/_editor/ToolDrawer";
import SfxPicker from "@/app/plan/_components/SfxPicker";
import type { SoundEffectSummary } from "@/lib/sfx-api";

function effect(
  id: string,
  name: string,
  category: string | null,
  search_terms: string[] = [],
): SoundEffectSummary {
  return {
    id,
    name,
    duration_s: 1.2,
    published_at: "2026-09-23T00:00:00Z",
    archived_at: null,
    status: "ready",
    source_filename: null,
    preview_audio_url: `https://cdn.example.com/${id}.m4a`,
    category,
    search_terms,
  };
}

const LIBRARY: SoundEffectSummary[] = [
  effect("buzz", "Wrong buzzer", "rejection", ["wrong", "fail", "buzzer"]),
  effect("drum", "Drum roll + crash", "suspense", ["drum roll", "reveal"]),
  effect("tape", "Tape rewind", "transition", ["rewind"]),
  effect("whoosh", "Whoosh", "transition", ["whoosh", "swoosh"]),
  effect("tap", "Soft tap", "ui", ["tap", "click"]),
  effect("horn", "Airhorn", null),
];

function renderSounds(overrides: Partial<ComponentProps<typeof ToolDrawer>> = {}) {
  const props: ComponentProps<typeof ToolDrawer> = {
    tool: "sounds",
    sampleWord: null,
    appliedPresetId: null,
    onAddText: jest.fn(),
    onPickPreset: jest.fn(),
    onClose: jest.fn(),
    sfxEffects: LIBRARY,
    onAddSfx: jest.fn(),
    ...overrides,
  };
  return { ...render(<ToolDrawer {...props} />), props };
}

const search = () => screen.getByRole("searchbox", { name: "Search sound effects" });
const effectButtons = () =>
  screen.queryAllByRole("button").filter((b) => b.hasAttribute("data-sfx-option"));
const effectNames = () => effectButtons().map((b) => b.querySelector("span")?.textContent);

describe("ToolDrawer Sounds — effect library", () => {
  it("groups effects under category headings in a fixed order, Other last", () => {
    renderSounds();

    const headings = screen.getAllByRole("heading", { level: 3 }).map((h) => h.textContent);
    expect(headings).toEqual(["Transitions, 2", "Rejection, 1", "Suspense, 1", "Text & UI, 1", "Other, 1"]);

    const transitions = screen.getByRole("group", { name: /Transitions/ });
    expect(within(transitions).getAllByRole("button").map((b) => b.textContent)).toEqual([
      "Tape rewind, 1.2s",
      "Whoosh, 1.2s",
    ]);
    expect(within(screen.getByRole("group", { name: /Other/ })).getByText("Airhorn")).toBeInTheDocument();
  });

  it("filters by whole words in names and search terms, case-insensitively", () => {
    renderSounds();

    fireEvent.change(search(), { target: { value: "WRONG buzzer" } });
    expect(effectNames()).toEqual(["Wrong buzzer"]);

    fireEvent.change(search(), { target: { value: "drum roll" } });
    expect(effectNames()).toEqual(["Drum roll + crash"]);

    fireEvent.change(search(), { target: { value: "swoosh" } });
    expect(effectNames()).toEqual(["Whoosh"]);

    // Whole words only: "tap" is not "Tape rewind".
    fireEvent.change(search(), { target: { value: "tap" } });
    expect(effectNames()).toEqual(["Soft tap"]);
    expect(screen.getAllByRole("heading", { level: 3 }).map((h) => h.textContent)).toEqual([
      "Text & UI, 1",
    ]);
    expect(screen.getByRole("status")).toHaveTextContent("1 effect");
  });

  it("adds the picked effect as-is so live preview keeps its audio URL", () => {
    const onAddSfx = jest.fn();
    renderSounds({ onAddSfx });

    fireEvent.change(search(), { target: { value: "buzzer" } });
    fireEvent.click(screen.getByRole("button", { name: /Wrong buzzer/ }));

    expect(onAddSfx).toHaveBeenCalledTimes(1);
    // Same object: EditorShell reads preview_audio_url off it for useSfxPreview.
    expect(onAddSfx.mock.calls[0][0]).toBe(LIBRARY[0]);
    expect(onAddSfx.mock.calls[0][0].preview_audio_url).toBe("https://cdn.example.com/buzz.m4a");
  });

  it("shows a no-match state with a working Clear search action", () => {
    renderSounds();

    fireEvent.change(search(), { target: { value: "kazoo" } });
    expect(effectButtons()).toHaveLength(0);
    expect(screen.getByText(/No effects match “kazoo”/)).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("0 effects");

    fireEvent.click(screen.getByRole("button", { name: "Clear search" }));
    expect(search()).toHaveValue("");
    expect(search()).toHaveFocus();
    expect(effectButtons()).toHaveLength(LIBRARY.length);
  });

  it("moves focus from the search field into the results with the arrow keys", () => {
    renderSounds();

    fireEvent.keyDown(search(), { key: "ArrowDown" });
    expect(screen.getByRole("button", { name: /Tape rewind/ })).toHaveFocus();

    fireEvent.keyDown(document.activeElement!, { key: "ArrowDown" });
    expect(screen.getByRole("button", { name: /Whoosh/ })).toHaveFocus();

    // Crosses group boundaries in visual order.
    fireEvent.keyDown(document.activeElement!, { key: "ArrowDown" });
    expect(screen.getByRole("button", { name: /Wrong buzzer/ })).toHaveFocus();

    fireEvent.keyDown(document.activeElement!, { key: "ArrowUp" });
    fireEvent.keyDown(document.activeElement!, { key: "ArrowUp" });
    fireEvent.keyDown(document.activeElement!, { key: "ArrowUp" });
    expect(search()).toHaveFocus();
  });

  it("renders a legacy-only library as one flat list without headings", () => {
    renderSounds({
      sfxEffects: [effect("b", "Boom", null), effect("a", "Applause", null)],
    });

    expect(screen.queryAllByRole("heading", { level: 3 })).toHaveLength(0);
    expect(effectNames()).toEqual(["Applause", "Boom"]);
  });

  it("keeps the Other heading when a search in a categorized library only hits legacy effects", () => {
    renderSounds();

    fireEvent.change(search(), { target: { value: "airhorn" } });
    expect(effectNames()).toEqual(["Airhorn"]);
    expect(screen.getAllByRole("heading", { level: 3 }).map((h) => h.textContent)).toEqual([
      "Other, 1",
    ]);
  });

  it("keeps the empty-library and loading states", () => {
    const { rerender, props } = renderSounds({ sfxEffects: [] });
    expect(screen.getByText("No published sound effects found.")).toBeInTheDocument();
    expect(screen.queryByRole("searchbox")).toBeNull();

    rerender(<ToolDrawer {...props} sfxEffects={LIBRARY} sfxLoading />);
    expect(screen.getByText("Loading effects...")).toBeInTheDocument();
    expect(screen.queryByRole("searchbox")).toBeNull();
  });

  it("ignores non-arrow keys and stops at the last result", () => {
    renderSounds();

    // Only ArrowDown leaves the search field.
    search().focus();
    fireEvent.keyDown(search(), { key: "Enter" });
    fireEvent.keyDown(search(), { key: "ArrowUp" });
    expect(search()).toHaveFocus();

    fireEvent.keyDown(search(), { key: "ArrowDown" });
    const first = screen.getByRole("button", { name: /Tape rewind/ });
    expect(first).toHaveFocus();
    fireEvent.keyDown(first, { key: "Tab" });
    fireEvent.keyDown(first, { key: "ArrowLeft" });
    expect(first).toHaveFocus();

    // Walk to the last option ("Airhorn" under Other); ArrowDown there is a no-op.
    for (let i = 0; i < LIBRARY.length - 1; i += 1) {
      fireEvent.keyDown(document.activeElement!, { key: "ArrowDown" });
    }
    const last = screen.getByRole("button", { name: /Airhorn/ });
    expect(last).toHaveFocus();
    fireEvent.keyDown(last, { key: "ArrowDown" });
    expect(last).toHaveFocus();
  });

  it("keeps focus in the search field when ArrowDown has no results to enter", () => {
    renderSounds();

    fireEvent.change(search(), { target: { value: "kazoo" } });
    search().focus();
    fireEvent.keyDown(search(), { key: "ArrowDown" });
    expect(search()).toHaveFocus();

    // Arrow keys on the Clear search action are not treated as result navigation.
    const clear = screen.getByRole("button", { name: "Clear search" });
    clear.focus();
    fireEvent.keyDown(clear, { key: "ArrowUp" });
    expect(clear).toHaveFocus();
  });

  it("labels effects without a duration as SFX and leaves drawer rows unpressed", () => {
    renderSounds({
      sfxEffects: [{ ...effect("pop", "Pop", "ui"), duration_s: null }, LIBRARY[0]],
    });

    expect(screen.getByRole("button", { name: /Pop/ })).toHaveTextContent("Pop, SFX");
    expect(screen.getByRole("button", { name: /Wrong buzzer/ })).toHaveTextContent("Wrong buzzer, 1.2s");
    // One-step drawer: no selection state, so no toggle semantics.
    for (const button of effectButtons()) expect(button).not.toHaveAttribute("aria-pressed");
  });

  it("does not throw when a pick has no onAddSfx handler", () => {
    renderSounds({ onAddSfx: undefined });
    expect(() => fireEvent.click(screen.getByRole("button", { name: /Soft tap/ }))).not.toThrow();
  });
});

// SfxPicker driven directly: behavior the drawer/lane tests don't isolate.
describe("SfxPicker — direct props", () => {
  it("keeps results while a word is half-typed, but whole words still win", () => {
    render(<SfxPicker effects={LIBRARY} onPick={jest.fn()} />);

    // Mid-word: no whole-word hit, so the last word matches word starts.
    fireEvent.change(search(), { target: { value: "whoo" } });
    expect(effectNames()).toEqual(["Whoosh"]);
    fireEvent.change(search(), { target: { value: "wrong buz" } });
    expect(effectNames()).toEqual(["Wrong buzzer"]);

    // A whole-word hit exists, so "tap" never widens to "Tape rewind".
    fireEvent.change(search(), { target: { value: "tap" } });
    expect(effectNames()).toEqual(["Soft tap"]);
  });

  it("marks the selected row in both densities", () => {
    const { rerender } = render(
      <SfxPicker effects={LIBRARY} onPick={jest.fn()} selectedId="tap" density="comfortable" />,
    );
    const selected = () => screen.getByRole("button", { name: /Soft tap/ });
    const other = () => screen.getByRole("button", { name: /Whoosh/ });

    expect(selected()).toHaveAttribute("aria-pressed", "true");
    expect(selected()).toHaveClass("border-sky");
    expect(other()).toHaveAttribute("aria-pressed", "false");
    expect(other()).not.toHaveClass("border-sky");

    rerender(<SfxPicker effects={LIBRARY} onPick={jest.fn()} selectedId="tap" density="compact" />);
    // Sky ring on pale Sky, so selection differs from the pale-Sky hover.
    expect(selected()).toHaveClass("bg-sky-soft", "font-semibold", "ring-sky");
    expect(other()).toHaveAttribute("aria-pressed", "false");
    expect(other()).not.toHaveClass("bg-sky-soft");
  });

  it("clears the query on Escape without letting it close the host sheet", () => {
    const hostKeyDown = jest.fn();
    render(
      <div onKeyDown={hostKeyDown}>
        <SfxPicker effects={LIBRARY} onPick={jest.fn()} />
      </div>,
    );

    fireEvent.change(search(), { target: { value: "buzzer" } });
    fireEvent.keyDown(search(), { key: "Escape" });
    expect(search()).toHaveValue("");
    expect(effectButtons()).toHaveLength(LIBRARY.length);
    expect(hostKeyDown).not.toHaveBeenCalled();

    // With nothing to clear, Escape reaches the host as before.
    fireEvent.keyDown(search(), { key: "Escape" });
    expect(hostKeyDown).toHaveBeenCalledTimes(1);
  });

  it("finds effects from natural phrasing like 'buzzer sound'", () => {
    render(<SfxPicker effects={LIBRARY} onPick={jest.fn()} />);
    fireEvent.change(search(), { target: { value: "wrong buzzer sound effect" } });
    expect(effectNames()).toEqual(["Wrong buzzer"]);
  });

  it("sends typing on a row back to search so Backspace never deletes the new SFX", () => {
    // Stands in for EditorShell's document-level delete-selection shortcut.
    const editorKeyDown = jest.fn();
    render(
      <div onKeyDown={editorKeyDown}>
        <SfxPicker effects={LIBRARY} onPick={jest.fn()} />
      </div>,
    );
    const row = screen.getByRole("button", { name: /Wrong buzzer/ });

    row.focus();
    fireEvent.keyDown(row, { key: "Backspace" });
    expect(search()).toHaveFocus();
    expect(editorKeyDown).not.toHaveBeenCalled();

    row.focus();
    fireEvent.keyDown(row, { key: "Delete" });
    fireEvent.keyDown(row, { key: "b" });
    expect(search()).toHaveFocus();
    expect(editorKeyDown).not.toHaveBeenCalled();

    // Space and shortcuts stay with the row / the editor.
    row.focus();
    fireEvent.keyDown(row, { key: " " });
    fireEvent.keyDown(row, { key: "z", metaKey: true });
    expect(row).toHaveFocus();
    expect(editorKeyDown).toHaveBeenCalledTimes(2);
  });
});
