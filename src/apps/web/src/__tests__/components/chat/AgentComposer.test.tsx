import { createRef, useState } from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import "@testing-library/jest-dom";

import { AgentComposer } from "@/components/chat/AgentComposer";

describe("AgentComposer", () => {
  it("is controlled and forwards its input ref", () => {
    const onValueChange = jest.fn();
    const ref = createRef<HTMLInputElement | HTMLTextAreaElement>();
    render(
      <AgentComposer
        ref={ref}
        value="Draft"
        onValueChange={onValueChange}
        onSubmit={jest.fn()}
        inputLabel="Message Kria"
      />,
    );
    fireEvent.change(screen.getByLabelText("Message Kria"), { target: { value: "New draft" } });
    expect(onValueChange).toHaveBeenCalledWith("New draft");
    expect(ref.current).toBe(screen.getByLabelText("Message Kria"));
  });

  it("submits on Enter, preserves Shift+Enter, and ignores IME composition", () => {
    const onSubmit = jest.fn();
    render(
      <AgentComposer value="Hello" onValueChange={jest.fn()} onSubmit={onSubmit} inputLabel="Prompt" />,
    );
    const input = screen.getByLabelText("Prompt");
    fireEvent.keyDown(input, { key: "Enter", shiftKey: true });
    fireEvent.keyDown(input, { key: "Enter", isComposing: true });
    fireEvent.keyDown(input, { key: "Enter", keyCode: 229 });
    expect(onSubmit).not.toHaveBeenCalled();
    fireEvent.keyDown(input, { key: "Enter" });
    expect(onSubmit).toHaveBeenCalledTimes(1);
  });

  it("separates input disabled from submit disabled", () => {
    const onSubmit = jest.fn();
    const { rerender } = render(
      <AgentComposer
        value="Queued thought"
        onValueChange={jest.fn()}
        onSubmit={onSubmit}
        inputLabel="Prompt"
        submitDisabled
      />,
    );
    expect(screen.getByLabelText("Prompt")).toBeEnabled();
    expect(screen.getByRole("button", { name: "Send message" })).toBeDisabled();
    fireEvent.keyDown(screen.getByLabelText("Prompt"), { key: "Enter" });
    fireEvent.submit(screen.getByLabelText("Prompt").closest("form")!);
    expect(onSubmit).not.toHaveBeenCalled();

    rerender(
      <AgentComposer
        value="Queued thought"
        onValueChange={jest.fn()}
        onSubmit={jest.fn()}
        inputLabel="Prompt"
        disabled
      />,
    );
    expect(screen.getByLabelText("Prompt")).toBeDisabled();
  });

  it("blocks whitespace-only submission from the keyboard and form", () => {
    const onSubmit = jest.fn();
    render(
      <AgentComposer
        value="   "
        onValueChange={jest.fn()}
        onSubmit={onSubmit}
        inputLabel="Prompt"
      />,
    );
    const input = screen.getByLabelText("Prompt");
    fireEvent.keyDown(input, { key: "Enter" });
    fireEvent.submit(input.closest("form")!);
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("renders leading actions, status, character limits, and queue labels", () => {
    render(
      <AgentComposer
        value="Queue this"
        onValueChange={jest.fn()}
        onSubmit={jest.fn()}
        inputLabel="Prompt"
        submitLabel="Queue message"
        maxLength={12}
        leadingAction={<button type="button">Attach</button>}
        status={<p>10/12</p>}
      />,
    );
    expect(screen.getByRole("button", { name: "Attach" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Queue message" })).toBeInTheDocument();
    expect(screen.getByLabelText("Prompt")).toHaveAttribute("maxlength", "12");
    expect(screen.getByText("10/12")).toBeInTheDocument();
  });

  it("switches between multiline and single-line inputs", () => {
    const props = {
      value: "Draft",
      onValueChange: jest.fn(),
      onSubmit: jest.fn(),
      inputLabel: "Prompt",
    };
    const { rerender } = render(<AgentComposer {...props} />);
    expect(screen.getByLabelText("Prompt").tagName).toBe("TEXTAREA");

    rerender(<AgentComposer {...props} multiline={false} />);
    expect(screen.getByLabelText("Prompt").tagName).toBe("INPUT");
  });

  it("honors a consumer's synchronous in-flight lock to prevent duplicate submission", () => {
    const onSubmit = jest.fn();
    function Harness() {
      const [submitting, setSubmitting] = useState(false);
      return (
        <AgentComposer
          value="Send once"
          onValueChange={jest.fn()}
          onSubmit={() => {
            onSubmit();
            setSubmitting(true);
          }}
          submitDisabled={submitting}
          inputLabel="Prompt"
        />
      );
    }

    render(<Harness />);
    const submit = screen.getByRole("button", { name: "Send message" });
    fireEvent.click(submit);
    fireEvent.click(submit);
    expect(onSubmit).toHaveBeenCalledTimes(1);
  });
});
