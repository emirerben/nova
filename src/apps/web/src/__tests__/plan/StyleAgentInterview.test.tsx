/**
 * Tests for plan/_components/StyleAgentInterview.tsx (Creator Agent M2).
 *
 * Covers:
 *   1. On load: calls styleAgentStart, renders reply as heading
 *   2. Submit: calls styleAgentTurn, shows applied confirmation when applied=true
 *   3. Clarify turn: applied=false, no confirmation shown
 *   4. Error state: renders error + retry button
 *   5. Suggestion chip click: submits the chip text
 */

// @ts-nocheck
import React from "react";

Object.defineProperty(window, "matchMedia", {
  writable: true,
  value: jest.fn().mockImplementation((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: jest.fn(),
    removeListener: jest.fn(),
    addEventListener: jest.fn(),
    removeEventListener: jest.fn(),
    dispatchEvent: jest.fn(),
  })),
});

import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom";

// Mock plan-api — spread actual to preserve types, override what we need
jest.mock("@/lib/plan-api", () => ({
  ...jest.requireActual("@/lib/plan-api"),
  styleAgentStart: jest.fn(),
  styleAgentTurn: jest.fn(),
}));

import { styleAgentStart, styleAgentTurn } from "@/lib/plan-api";
const mockStart = styleAgentStart as jest.MockedFunction<typeof styleAgentStart>;
const mockTurn = styleAgentTurn as jest.MockedFunction<typeof styleAgentTurn>;

import StyleAgentInterview from "@/app/plan/_components/StyleAgentInterview";

function makeStartResponse(overrides = {}) {
  return {
    reply: "Tell me how you'd like your videos to look.",
    suggestions: ["Make font bigger", "I film outdoors"],
    applied: false,
    intent: "greeting",
    persona_status: "ready",
    ...overrides,
  };
}

function makeTurnResponse(overrides = {}) {
  return {
    reply: "Done — your font is now larger.",
    suggestions: ["Adjust further", "Change color"],
    applied: true,
    intent: "style_edit",
    persona_status: "ready",
    ...overrides,
  };
}

describe("StyleAgentInterview — load and greeting", () => {
  it("test_renders_greeting_on_load: shows agent reply after start resolves", async () => {
    mockStart.mockResolvedValue(makeStartResponse());

    await act(async () => {
      render(<StyleAgentInterview />);
    });

    expect(screen.getByRole("status")).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByText("Tell me how you'd like your videos to look.")).toBeInTheDocument();
    });
  });

  it("test_shows_loading_state_before_start_resolves: loading indicator present initially", () => {
    mockStart.mockResolvedValue(
      new Promise((resolve) => setTimeout(() => resolve(makeStartResponse()), 9999)) as never,
    );

    const { container } = render(<StyleAgentInterview />);
    expect(screen.getByText(/Loading your style/)).toBeInTheDocument();
    expect(screen.getByRole("status", { name: /Loading your style/i })).toBeInTheDocument();
    expect(container.querySelector(".beam-loader")).toHaveAttribute("data-mode", "line");
  });

  it("test_thinking_state_is_accessible: pending turn announces Kria is thinking", async () => {
    mockStart.mockResolvedValue(makeStartResponse());
    mockTurn.mockResolvedValue(new Promise(() => {}) as never);

    await act(async () => {
      render(<StyleAgentInterview />);
    });

    const textarea = screen.getByRole("textbox");
    fireEvent.change(textarea, { target: { value: "make it calmer" } });

    await act(async () => {
      fireEvent.keyDown(textarea, { key: "Enter", shiftKey: false });
    });

    expect(screen.getByRole("status", { name: /Kria is thinking/i })).toBeInTheDocument();
    expect(screen.getByText(/Thinking/i)).toBeInTheDocument();
  });
});

describe("StyleAgentInterview — applied confirmation", () => {
  it("test_applied_confirmation_shown_when_applied_true: lime confirmation line appears after applied turn", async () => {
    mockStart.mockResolvedValue(makeStartResponse());
    mockTurn.mockResolvedValue(
      makeTurnResponse({ applied: true, reply: "Done — your text is now larger." }),
    );

    await act(async () => {
      render(<StyleAgentInterview />);
    });

    await waitFor(() => screen.getByRole("status"));

    const textarea = screen.getByRole("textbox");
    fireEvent.change(textarea, { target: { value: "make text bigger" } });

    await act(async () => {
      fireEvent.keyDown(textarea, { key: "Enter", shiftKey: false });
    });

    await waitFor(() => {
      expect(
        screen.getByText("Done — your next render will use this style."),
      ).toBeInTheDocument();
    });
  });

  it("test_no_confirmation_when_applied_false: clarify turn shows no confirmation", async () => {
    mockStart.mockResolvedValue(makeStartResponse());
    mockTurn.mockResolvedValue(
      makeTurnResponse({
        applied: false,
        intent: "clarify",
        reply: "Did you mean font size or font style?",
      }),
    );

    await act(async () => {
      render(<StyleAgentInterview />);
    });

    await waitFor(() => screen.getByRole("status"));

    const textarea = screen.getByRole("textbox");
    fireEvent.change(textarea, { target: { value: "change my font" } });

    await act(async () => {
      fireEvent.keyDown(textarea, { key: "Enter", shiftKey: false });
    });

    await waitFor(() => {
      expect(screen.queryByText(/Done — your next render/)).not.toBeInTheDocument();
    });
  });
});

describe("StyleAgentInterview — error retry", () => {
  it("test_error_state_shows_retry: network error surfaces error message and retry button", async () => {
    mockStart.mockResolvedValue(makeStartResponse());
    mockTurn.mockRejectedValue(new Error("network error"));

    await act(async () => {
      render(<StyleAgentInterview />);
    });

    await waitFor(() => screen.getByRole("status"));

    const textarea = screen.getByRole("textbox");
    fireEvent.change(textarea, { target: { value: "make text bigger" } });

    await act(async () => {
      fireEvent.keyDown(textarea, { key: "Enter", shiftKey: false });
    });

    await waitFor(() => {
      expect(screen.getByText(/Something went wrong/)).toBeInTheDocument();
      expect(screen.getByText("Try again")).toBeInTheDocument();
    });
  });
});

describe("StyleAgentInterview — suggestion chips", () => {
  it("test_chip_submits_text: clicking a suggestion chip submits its text", async () => {
    mockStart.mockResolvedValue(makeStartResponse({ suggestions: ["Make font bigger"] }));
    mockTurn.mockResolvedValue(makeTurnResponse({ applied: true }));

    await act(async () => {
      render(<StyleAgentInterview />);
    });

    await waitFor(() => screen.getByText("Make font bigger"));

    await act(async () => {
      fireEvent.click(screen.getByText("Make font bigger"));
    });

    await waitFor(() => {
      expect(mockTurn).toHaveBeenCalledWith("Make font bigger", expect.any(Array));
    });
  });
});
