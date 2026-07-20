import { fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";

import SongWindowSelector, {
  snapSongWindowStart,
  type SongWindowState,
} from "@/app/plan/items/[id]/_editor/SongWindowSelector";

const baseValue: SongWindowState = {
  startS: 3.2,
  videoDurationS: 5,
  trackDurationS: 20,
  recommendedStartS: 7.8,
  beatTimestampsS: [0, 2, 4, 6, 8, 10, 12, 14],
  editable: true,
  reason: null,
};

function Harness({
  initial = baseValue,
  onCommit,
}: {
  initial?: SongWindowState;
  onCommit: (startS: number) => void;
}) {
  const [value, setValue] = useState(initial);
  return (
    <SongWindowSelector
      value={value}
      onBegin={() => undefined}
      onPreview={(startS) => setValue((current) => ({ ...current, startS }))}
      onChange={(startS) => {
        setValue((current) => ({ ...current, startS }));
        onCommit(startS);
      }}
    />
  );
}

describe("snapSongWindowStart", () => {
  it("clamps and snaps to the nearest usable beat", () => {
    expect(snapSongWindowStart(5.1, 20, 5, [4, 6, 16])).toBe(6);
    expect(snapSongWindowStart(99, 20, 5, [14, 16])).toBe(14);
  });

  it("keeps free movement when no beat timing is available", () => {
    expect(snapSongWindowStart(3.27, 20, 5, [])).toBe(3.27);
  });

  it("never snaps to a beat beyond the latest legal start", () => {
    expect(snapSongWindowStart(15, 20, 5, [14, 15.019])).toBe(14);
  });
});

describe("SongWindowSelector", () => {
  it("previews freely then snaps on keyboard release", () => {
    const onCommit = jest.fn();
    render(<Harness onCommit={onCommit} />);
    const range = screen.getByRole("slider", { name: "Song section start" });

    fireEvent.keyDown(range, { key: "ArrowRight" });
    fireEvent.change(range, { target: { value: "5.1" } });
    expect((range as HTMLInputElement).value).toBe("5.1");
    fireEvent.keyUp(range, { key: "ArrowRight" });

    expect(onCommit).toHaveBeenLastCalledWith(6);
    expect((range as HTMLInputElement).value).toBe("6");
  });

  it("drags the fixed-duration band and snaps on release", () => {
    const onCommit = jest.fn();
    render(<Harness onCommit={onCommit} />);
    const band = screen.getByTestId("song-window-band");
    jest.spyOn(band, "getBoundingClientRect").mockReturnValue({
      x: 0,
      y: 0,
      left: 0,
      top: 0,
      right: 100,
      bottom: 36,
      width: 100,
      height: 36,
      toJSON: () => ({}),
    });

    fireEvent(band, new MouseEvent("pointerdown", { bubbles: true, clientX: 50 }));
    fireEvent(band, new MouseEvent("pointermove", { bubbles: true, clientX: 60 }));
    fireEvent(band, new MouseEvent("pointerup", { bubbles: true, clientX: 60 }));

    expect(onCommit).toHaveBeenLastCalledWith(10);
  });

  it("resets to the recommended section with beat snapping", () => {
    const onCommit = jest.fn();
    render(<Harness onCommit={onCommit} />);

    fireEvent.click(screen.getByRole("button", { name: "Reset to recommended section" }));
    expect(onCommit).toHaveBeenLastCalledWith(8);
  });

  it("disables editing with a clear short-song reason", () => {
    render(
      <Harness
        initial={{
          ...baseValue,
          editable: false,
          reason: "song_shorter_than_video",
        }}
        onCommit={jest.fn()}
      />,
    );

    expect(
      (screen.getByRole("slider", { name: "Song section start" }) as HTMLInputElement)
        .disabled,
    ).toBe(true);
    expect(screen.getByText(/shorter than your video/i)).not.toBeNull();
  });
});
