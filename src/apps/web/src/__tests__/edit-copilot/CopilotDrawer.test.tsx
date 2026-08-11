import "@testing-library/jest-dom";
import { fireEvent, render, screen, within } from "@testing-library/react";
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

/**
 * Chat steps feed (PR4, NEXT_PUBLIC_NOVA_STEPS_FEED_ENABLED): retires the
 * lime ChangeChip pills in favor of compact NovaStepRows for local-op
 * turns, gives server-render turns a disclosure + live feed instead, and
 * swaps the starter chips for contextual "Undo that" / "What else changed?"
 * chips right after an applied, locally-undoable turn.
 */
describe("CopilotDrawer — steps feed flag gate", () => {
  afterEach(() => {
    delete process.env.NEXT_PUBLIC_NOVA_STEPS_FEED_ENABLED;
  });

  const localOpMessages: CopilotMessage[] = [
    { id: "u1", role: "user", text: "make the hook bigger and switch it to lime" },
    {
      id: "a1",
      role: "assistant",
      text: "Done — sized up your hook and switched it to lime.",
      applied: ["Hook size: 32px → 44px", "Hook color: Ink → Lime"],
      rejected: ["Caption color: no caption selected"],
      undoVersion: 3,
    },
  ];

  it("flag off: renders the lime pills exactly as before, never NovaStepRow rows", () => {
    delete process.env.NEXT_PUBLIC_NOVA_STEPS_FEED_ENABLED;
    render(
      <CopilotDrawer
        {...baseProps}
        layoutMode="full"
        messages={localOpMessages}
        historyVersion={3}
      />,
    );
    expect(screen.getByText("32px → 44px")).toBeInTheDocument();
    expect(screen.queryByRole("list", { name: "Nova AI changes" })).not.toBeInTheDocument();
  });

  it("flag on: applied ops render as compact rows, rejected ops as dashed/failed rows", () => {
    process.env.NEXT_PUBLIC_NOVA_STEPS_FEED_ENABLED = "true";
    render(
      <CopilotDrawer
        {...baseProps}
        layoutMode="full"
        messages={localOpMessages}
        historyVersion={3}
      />,
    );
    expect(screen.queryByText("32px → 44px")).not.toBeInTheDocument();
    const list = screen.getByRole("list", { name: "Nova AI changes" });
    expect(within(list).getByText("Hook size 32px → 44px")).toBeInTheDocument();
    expect(within(list).getByText("Hook color Ink → Lime")).toBeInTheDocument();
    expect(
      within(list).getByText("Couldn't apply: Caption color: no caption selected"),
    ).toBeInTheDocument();
  });

  it("flag on: collapses beyond 3 rows behind a +N more toggle", () => {
    process.env.NEXT_PUBLIC_NOVA_STEPS_FEED_ENABLED = "true";
    const messages: CopilotMessage[] = [
      {
        id: "a1",
        role: "assistant",
        text: "Done.",
        applied: ["A: 1 → 2", "B: 1 → 2", "C: 1 → 2"],
        rejected: ["D: missing"],
      },
    ];
    render(<CopilotDrawer {...baseProps} layoutMode="full" messages={messages} />);
    expect(screen.queryByText("Couldn't apply: D: missing")).not.toBeInTheDocument();
    const more = screen.getByRole("button", { name: "+1 more" });
    fireEvent.click(more);
    expect(screen.getByText("Couldn't apply: D: missing")).toBeInTheDocument();
  });

  it("flag on: the Undo link still honors the undoVersion staleness guard", () => {
    process.env.NEXT_PUBLIC_NOVA_STEPS_FEED_ENABLED = "true";
    const { rerender } = render(
      <CopilotDrawer
        {...baseProps}
        layoutMode="full"
        messages={localOpMessages}
        historyVersion={3}
      />,
    );
    expect(screen.getByRole("button", { name: "Undo" })).toBeInTheDocument();

    rerender(
      <CopilotDrawer
        {...baseProps}
        layoutMode="full"
        messages={localOpMessages}
        historyVersion={4}
      />,
    );
    expect(screen.queryByRole("button", { name: "Undo" })).toBeNull();

    rerender(
      <CopilotDrawer
        {...baseProps}
        layoutMode="full"
        messages={localOpMessages}
        historyVersion={3}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Undo" }));
    expect(baseProps.onUndo).toHaveBeenCalledTimes(1);
  });

  it("flag on: contextual chips replace the starters after an applied, locally-undoable turn", () => {
    process.env.NEXT_PUBLIC_NOVA_STEPS_FEED_ENABLED = "true";
    render(
      <CopilotDrawer
        {...baseProps}
        layoutMode="full"
        messages={localOpMessages}
        historyVersion={3}
      />,
    );
    expect(screen.getByRole("button", { name: "Undo that" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Do that again" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "What else changed?" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Make the hook punchier" })).not.toBeInTheDocument();
  });

  it("flag on: contextual chips disappear once the undo staleness guard trips", () => {
    process.env.NEXT_PUBLIC_NOVA_STEPS_FEED_ENABLED = "true";
    render(
      <CopilotDrawer
        {...baseProps}
        layoutMode="full"
        messages={localOpMessages}
        historyVersion={4}
      />,
    );
    expect(screen.queryByRole("button", { name: "Undo that" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Do that again" })).not.toBeInTheDocument();
  });

  it("'Undo that' fires the same handler as the Undo link; 'Do that again' and 'What else changed?' send chat messages", () => {
    process.env.NEXT_PUBLIC_NOVA_STEPS_FEED_ENABLED = "true";
    render(
      <CopilotDrawer
        {...baseProps}
        layoutMode="full"
        messages={localOpMessages}
        historyVersion={3}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Undo that" }));
    expect(baseProps.onUndo).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole("button", { name: "Do that again" }));
    expect(baseProps.onSend).toHaveBeenCalledWith("Do that again");

    fireEvent.click(screen.getByRole("button", { name: "What else changed?" }));
    expect(baseProps.onSend).toHaveBeenCalledWith("What else changed?");
  });

  it("flag on, no applied/undoable turn: falls back to the generic starter chips", () => {
    process.env.NEXT_PUBLIC_NOVA_STEPS_FEED_ENABLED = "true";
    render(<CopilotDrawer {...baseProps} layoutMode="full" messages={[]} />);
    expect(screen.getByRole("button", { name: "Make the hook punchier" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Undo that" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Do that again" })).not.toBeInTheDocument();
  });

  describe("server-render turn (artboard 03)", () => {
    const renderTurnMessages: CopilotMessage[] = [
      { id: "u1", role: "user", text: "give the intro a full-screen layout" },
      {
        id: "a1",
        role: "assistant",
        text: "That's a re-render, not an instant edit — starting it now.",
        applied: ["Intro layout: Classic → Editorial (re-rendering)"],
        isRenderTurn: true,
      },
    ];

    it("shows the disclosure copy instead of chips/rows, and never an Undo affordance", () => {
      process.env.NEXT_PUBLIC_NOVA_STEPS_FEED_ENABLED = "true";
      render(
        <CopilotDrawer
          {...baseProps}
          layoutMode="full"
          messages={renderTurnMessages}
          renderTurnActive
          renderTurnSteps={null}
        />,
      );
      expect(
        screen.getByText("That's a re-render, not an instant edit — starting it now."),
      ).toBeInTheDocument();
      expect(
        screen.getByText(/can't be undone from chat/i),
      ).toBeInTheDocument();
      expect(screen.queryByText("Intro layout")).not.toBeInTheDocument();
      expect(screen.queryByRole("button", { name: "Undo" })).not.toBeInTheDocument();
      expect(screen.queryByRole("list", { name: "Nova AI changes" })).not.toBeInTheDocument();
    });

    it("renders the live compact feed once steps arrive from the poll", () => {
      process.env.NEXT_PUBLIC_NOVA_STEPS_FEED_ENABLED = "true";
      render(
        <CopilotDrawer
          {...baseProps}
          layoutMode="full"
          messages={renderTurnMessages}
          renderTurnActive
          renderTurnSteps={[
            { id: "s1", ts: "", kind: "decision", label: "Read your current cut", detail: null, status: "done" },
            { id: "s2", ts: "", kind: "render", label: "Rendering your new intro", detail: null, status: "active" },
          ]}
        />,
      );
      const list = screen.getByRole("list", { name: /nova ai steps/i });
      expect(within(list).getByText("Read your current cut")).toBeInTheDocument();
      expect(within(list).getByText("Rendering your new intro")).toBeInTheDocument();
      expect(screen.queryByText(/can't be undone from chat/i)).not.toBeInTheDocument();
    });

    it("suggestion chips are absent after a server-render turn", () => {
      process.env.NEXT_PUBLIC_NOVA_STEPS_FEED_ENABLED = "true";
      render(
        <CopilotDrawer
          {...baseProps}
          layoutMode="full"
          messages={renderTurnMessages}
          renderTurnActive
          renderTurnSteps={null}
        />,
      );
      expect(screen.queryByRole("button", { name: "Undo that" })).not.toBeInTheDocument();
      expect(screen.queryByRole("button", { name: "Do that again" })).not.toBeInTheDocument();
      expect(screen.queryByRole("button", { name: "What else changed?" })).not.toBeInTheDocument();
    });

    it("a historical (inactive) render turn shows only the bubble text — no stale spinner", () => {
      process.env.NEXT_PUBLIC_NOVA_STEPS_FEED_ENABLED = "true";
      render(
        <CopilotDrawer
          {...baseProps}
          layoutMode="full"
          messages={renderTurnMessages}
          renderTurnActive={false}
          renderTurnSteps={null}
        />,
      );
      expect(
        screen.getByText("That's a re-render, not an instant edit — starting it now."),
      ).toBeInTheDocument();
      expect(screen.queryByText(/can't be undone from chat/i)).not.toBeInTheDocument();
    });
  });
});
