import { render, screen } from "@testing-library/react";
import "@testing-library/jest-dom";

import { ChatMessage } from "@/components/chat/ChatMessage";

describe("ChatMessage", () => {
  it("keeps creator messages as right-aligned ink bubbles", () => {
    render(<ChatMessage role="user" pending>Make the opening faster</ChatMessage>);
    const message = screen.getByText("Make the opening faster");
    expect(message).toHaveClass(
      "ml-auto",
      "bg-[#EBF3FF]",
      "opacity-60",
      "motion-safe:animate-chat-message-in",
      "motion-reduce:animate-chat-fade-in",
    );
  });

  it("renders assistant messages as unboxed wrapping prose", () => {
    render(<ChatMessage role="assistant" animate={false}>First line{"\n"}Second line</ChatMessage>);
    const message = screen.getByText(/First line/);
    expect(message).toHaveClass("whitespace-pre-line", "[overflow-wrap:anywhere]");
    expect(message).not.toHaveClass("bg-muted", "rounded-lg");
    expect(message).not.toHaveClass("motion-safe:animate-chat-message-in");
  });

  it("supports the editorial pull-quote and response treatments", () => {
    const { rerender } = render(
      <ChatMessage role="user" presentation="editorial">Use the candid clip</ChatMessage>,
    );
    expect(screen.getByText("Use the candid clip")).toHaveClass("border-l-2", "italic");

    rerender(
      <ChatMessage role="assistant" presentation="editorial">That clip fits shot one.</ChatMessage>,
    );
    expect(screen.getByText("That clip fits shot one.")).toHaveClass("font-display", "text-xl");
  });
});
