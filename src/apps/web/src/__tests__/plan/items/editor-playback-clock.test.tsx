import { act, render, screen } from "@testing-library/react";

import {
  createEditorPlaybackClock,
  useEditorPlaybackTime,
} from "@/app/plan/items/[id]/_editor/editor-playback-clock";

function Subscriber({
  clock,
  fallback,
}: {
  clock: ReturnType<typeof createEditorPlaybackClock> | null;
  fallback: number;
}) {
  const timeS = useEditorPlaybackTime(clock, fallback);
  return <output>{timeS.toFixed(3)}</output>;
}

describe("editor playback clock", () => {
  it("updates only subscribed playback layers", () => {
    const clock = createEditorPlaybackClock(0);
    let shellRenders = 0;
    function StaticShell() {
      shellRenders += 1;
      return <div>shell</div>;
    }
    render(
      <>
        <StaticShell />
        <Subscriber clock={clock} fallback={9} />
      </>,
    );

    expect(screen.getByText("0.000")).toBeTruthy();
    act(() => clock.publish(1 / 30));
    expect(screen.getByText("0.033")).toBeTruthy();
    expect(shellRenders).toBe(1);
  });

  it("uses committed time unchanged when the feature clock is absent", () => {
    const view = render(<Subscriber clock={null} fallback={1.2} />);
    expect(screen.getByText("1.200")).toBeTruthy();

    view.rerender(<Subscriber clock={null} fallback={2.4} />);
    expect(screen.getByText("2.400")).toBeTruthy();
  });

  it("clamps invalid and negative samples before notifying consumers", () => {
    const clock = createEditorPlaybackClock(2);
    clock.publish(-1);
    expect(clock.getSnapshot()).toBe(0);
    clock.publish(Number.NaN);
    expect(clock.getSnapshot()).toBe(0);
  });
});
