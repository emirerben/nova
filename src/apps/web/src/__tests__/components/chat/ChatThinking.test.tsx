import { act, render, screen } from "@testing-library/react";
import "@testing-library/jest-dom";
import { ChatThinking } from "@/components/chat/ChatThinking";

describe("ChatThinking", () => {
  let now = 0;

  beforeEach(() => {
    jest.useFakeTimers();
    now = 0;
    jest.spyOn(Date, "now").mockImplementation(() => now);
  });

  afterEach(() => {
    jest.restoreAllMocks();
    jest.useRealTimers();
  });

  it("shows an immediate accessible shimmer before progressive copy", () => {
    render(<ChatThinking />);
    expect(screen.queryByText("Reading your direction…")).not.toBeInTheDocument();
    expect(screen.getByText("Kria is thinking")).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("Kria is thinking");
    act(() => { now = 1500; jest.advanceTimersByTime(1500); });
    expect(screen.getByText("Reading your direction…")).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("Reading your direction…");
  });

  it.each([
    [1499, "Kria is thinking"],
    [8000, "Shaping the edit around your clips…"],
    [7999, "Reading your direction…"],
    [19999, "Shaping the edit around your clips…"],
    [20000, "Still working — your direction is saved."],
  ])("uses meaningful elapsed-time copy at %sms", (elapsed, copy) => {
    render(<ChatThinking />);
    act(() => { now = elapsed; jest.advanceTimersByTime(elapsed); });
    expect(screen.getByText(copy)).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent(copy);
  });

  it("marks the shimmer as reduced-motion safe", () => {
    render(<ChatThinking />);
    expect(screen.getByText("Kria is thinking")).toHaveClass("motion-safe:animate-shimmer", "motion-reduce:text-muted-foreground");
  });

  it("renders nothing and schedules no timers while inactive", () => {
    const { container } = render(<ChatThinking active={false} />);
    expect(container).toBeEmptyDOMElement();
    expect(jest.getTimerCount()).toBe(0);
  });

  it("reveals an optional stop action at its configured threshold and cleans up", () => {
    const onStop = jest.fn();
    const { unmount } = render(<ChatThinking onStop={onStop} stopAfterMs={5000} />);
    expect(screen.queryByRole("button", { name: "Stop" })).not.toBeInTheDocument();
    act(() => { now = 5000; jest.advanceTimersByTime(5000); });
    screen.getByRole("button", { name: "Stop" }).click();
    expect(onStop).toHaveBeenCalledTimes(1);
    unmount();
    expect(jest.getTimerCount()).toBe(0);
  });
});
