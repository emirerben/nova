import { fireEvent, render, screen } from "@testing-library/react";
import "@testing-library/jest-dom";

import { AgentApprovalCard } from "@/components/chat/AgentApprovalCard";
import { Button } from "@/components/ui/button";

describe("AgentApprovalCard", () => {
  it("renders optional structure in compact and default densities", () => {
    const { rerender } = render(
      <AgentApprovalCard
        icon={<span aria-label="Approval icon">!</span>}
        title="Approve the plan"
        badge={<span>Draft</span>}
        description="Nothing renders yet"
      >
        <p>Plan details</p>
      </AgentApprovalCard>,
    );
    expect(screen.getByLabelText("Approval icon")).toBeInTheDocument();
    expect(screen.getByText("Approve the plan")).toBeInTheDocument();
    expect(screen.getByText("Plan details")).toBeInTheDocument();

    rerender(<AgentApprovalCard density="compact" title="Accept suggestion" />);
    expect(screen.getByText("Accept suggestion").closest(".rounded-xl")).toBeInTheDocument();
  });

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
