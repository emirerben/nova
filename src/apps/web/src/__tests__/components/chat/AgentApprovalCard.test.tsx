import { fireEvent, render, screen } from "@testing-library/react";
import "@testing-library/jest-dom";

import { AgentApprovalCard } from "@/components/chat/AgentApprovalCard";
import { Button } from "@/components/ui/button";

describe("AgentApprovalCard", () => {

  it("never invokes an action without an explicit click", () => {
    jest.useFakeTimers();
    const onApprove = jest.fn();
    render(
      <AgentApprovalCard
        title="Render this"
        actions={<Button onClick={onApprove}>Approve</Button>}
      />,
    );
    jest.advanceTimersByTime(60_000);
    expect(onApprove).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Approve" }));
    expect(onApprove).toHaveBeenCalledTimes(1);
    jest.useRealTimers();
  });
});
