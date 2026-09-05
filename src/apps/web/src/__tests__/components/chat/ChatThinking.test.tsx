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

  it("keeps the first 1.5 seconds quiet but accessible", () => {
    render(<ChatThinking />);
    expect(screen.queryByText("Reading your direction…")).not.toBeInTheDocument();
    expect(screen.getByText("Kria is thinking")).toBeInTheDocument();
    act(() => { now = 1500; jest.advanceTimersByTime(1500); });
    expect(screen.getByText("Reading your direction…")).toBeInTheDocument();
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
  });

  it("marks the thinking dots as reduced-motion safe", () => {
    render(<ChatThinking />);
    const dots = document.querySelectorAll("[aria-hidden='true']");
    expect(dots).toHaveLength(3);
    dots.forEach((dot) => expect(dot).toHaveClass("motion-reduce:animate-none"));
  });
});
