import "@testing-library/jest-dom";
import { fireEvent, render, screen } from "@testing-library/react";
import CopilotDrawer from "@/app/plan/items/[id]/_editor/CopilotDrawer";
import type { CopilotMessage } from "@/lib/edit-copilot/useEditCopilot";

const baseProps = {
  open: true,
  messages: [] as CopilotMessage[],
  sending: false,
  queued: null,
  error: null,
  restoredInput: "",
  suggestions: [],
  historyVersion: 0,
  canUndo: true,
  onSend: jest.fn(),
  onCancelQueued: jest.fn(),
  onEditQueued: jest.fn(),
  onStop: jest.fn(),
  onUndo: jest.fn(),
  onClose: jest.fn(),
  onClearRestoredInput: jest.fn(),
};

afterEach(() => {
  jest.clearAllMocks();
});

describe("CopilotDrawer layout modes", () => {
  it("renders the full drawer, overlay strip, and light sheet variants", () => {
    const { rerender } = render(<CopilotDrawer {...baseProps} layoutMode="full" />);
    expect(screen.getByTestId("copilot-full")).toBeInTheDocument();

    rerender(<CopilotDrawer {...baseProps} layoutMode="overlay" />);
    expect(screen.getByTestId("copilot-overlay")).toBeInTheDocument();

    rerender(<CopilotDrawer {...baseProps} layoutMode="light" />);
    expect(screen.getByTestId("copilot-light")).toBeInTheDocument();
  });
});

describe("CopilotDrawer undo chip", () => {
  const messages: CopilotMessage[] = [
    { id: "u1", role: "user", text: "make it smaller" },
    {
      id: "a1",
      role: "assistant",
      text: "Done",
      applied: ["Size: 64 -> 54"],
      undoVersion: 3,
    },
  ];

  it("renders only while the latest applied turn matches history.version", () => {
    const { rerender } = render(
      <CopilotDrawer {...baseProps} layoutMode="full" messages={messages} historyVersion={3} />,
    );
    expect(screen.getByRole("button", { name: "Undo" })).toBeInTheDocument();

    rerender(
      <CopilotDrawer {...baseProps} layoutMode="full" messages={messages} historyVersion={4} />,
    );
    expect(screen.queryByRole("button", { name: "Undo" })).toBeNull();
  });

  it("calls the undo handler from the latest chip", () => {
    render(
      <CopilotDrawer {...baseProps} layoutMode="full" messages={messages} historyVersion={3} />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Undo" }));
    expect(baseProps.onUndo).toHaveBeenCalledTimes(1);
  });
});
