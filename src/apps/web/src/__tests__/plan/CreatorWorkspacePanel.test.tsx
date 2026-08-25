import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { CreatorWorkspacePanel } from "@/app/plan/_components/workspace/CreatorWorkspacePanel";
import {
  decideCreatorWorkspaceRelevanceProposal,
  getCreatorWorkspaceRelevanceProposal,
  pollLatestCreatorWorkspaceReceipt,
  recordCreatorWorkspacePreferenceSignal,
  PlanApiError,
} from "@/lib/plan-api";

jest.mock("@/lib/plan-api", () => ({
  pollLatestCreatorWorkspaceReceipt: jest.fn(),
  recordCreatorWorkspacePreferenceSignal: jest.fn(),
  decideCreatorWorkspaceRelevanceProposal: jest.fn(),
  getCreatorWorkspaceRelevanceProposal: jest.fn(),
  PlanApiError: jest.requireActual("@/lib/plan-api").PlanApiError,
}));

const pollReceipt = pollLatestCreatorWorkspaceReceipt as jest.MockedFunction<typeof pollLatestCreatorWorkspaceReceipt>;
const savePreference = recordCreatorWorkspacePreferenceSignal as jest.MockedFunction<typeof recordCreatorWorkspacePreferenceSignal>;
const decideProposal = decideCreatorWorkspaceRelevanceProposal as jest.MockedFunction<typeof decideCreatorWorkspaceRelevanceProposal>;
const getProposal = getCreatorWorkspaceRelevanceProposal as jest.MockedFunction<typeof getCreatorWorkspaceRelevanceProposal>;

beforeEach(() => {
  jest.clearAllMocks();
  getProposal.mockReset();
  pollReceipt.mockResolvedValue({
    receipt_id: "receipt-1",
    creator_id: "creator-1",
    plan_id: "plan-1",
    ownership_epoch: 1,
    idempotency_key: "key-1",
    request_digest: "digest-1",
    status: "processing",
    deliverables: [
      {
        deliverable_id: "deliverable-1",
        plan_item_id: "item-1",
        creator_session_id: "session-1",
        ownership_epoch: 1,
        session_revision: 1,
        status: "ready",
        job_id: "job-1",
        variant_id: "variant-1",
        render_generation_id: "generation-1",
        generation_receipt: null,
      },
    ],
    preference_summary: null,
    style: null,
  });
});

it("does not save a preference until the creator confirms it", async () => {
  render(<CreatorWorkspacePanel planId="plan-1" />);
  await screen.findByText("Workspace progress");
  expect(screen.getByText("Deliverable 1")).not.toBeNull();
  expect(screen.getByText("Deliverable 1").parentElement?.parentElement?.textContent).toContain("Ready");
  expect(screen.getByText(/In progress/)).not.toBeNull();

  fireEvent.change(screen.getByLabelText("Preference note"), { target: { value: "Keep openings quiet" } });
  expect(screen.getByRole("button", { name: "Review" }).className).toContain("bg-secondary");
  fireEvent.click(screen.getByRole("button", { name: "Review" }));

  expect(screen.getByText(/Keep openings quiet/)).not.toBeNull();
  expect(savePreference).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "Confirm preference" }));
  await waitFor(() => expect(savePreference).toHaveBeenCalledTimes(1));
});

it("requires an explicit decision for an off-plan proposal", async () => {
  const proposal = {
    id: "proposal-1",
    proposal_id: "proposal-1",
    creator_id: "creator-1",
    plan_id: "plan-1",
    ownership_epoch: 1,
    idempotency_key: "key-2",
    request_digest: "digest-2",
    media_ids: ["upload-1"],
    status: "ready" as const,
    relevance: "new_topic" as const,
    target_plan_item_id: null,
    topic: "A new walk",
    rationale: "The footage introduces a new topic.",
    confidence: 0.9,
    proposal_hash: "a".repeat(64),
    error_code: null,
    decision: null,
    result_plan_item_id: null,
  };
  decideProposal.mockResolvedValue({ ...proposal, status: "approved", decision: "accept_new_topic" });
  render(<CreatorWorkspacePanel planId="plan-1" proposal={proposal} />);

  fireEvent.click(await screen.findByRole("button", { name: "Review suggested choice" }));
  expect(decideProposal).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "Confirm decision" }));
  await waitFor(() => expect(decideProposal).toHaveBeenCalledTimes(1));
});

it("polls an uploaded proposal and passes the terminal decision surface to the creator", async () => {
  const proposal = {
    id: "proposal-1",
    proposal_id: "proposal-1",
    creator_id: "creator-1",
    plan_id: "plan-1",
    ownership_epoch: 1,
    idempotency_key: "key-2",
    request_digest: "digest-2",
    media_ids: ["upload-1"],
    status: "processing" as const,
    relevance: null,
    target_plan_item_id: null,
    topic: null,
    rationale: null,
    confidence: null,
    proposal_hash: null,
    error_code: null,
    decision: null,
    result_plan_item_id: null,
  };
  getProposal.mockResolvedValue({
    ...proposal,
    status: "ready",
    relevance: "new_topic",
    topic: "A new walk",
    rationale: "The footage introduces a new topic.",
    confidence: 0.9,
    proposal_hash: "b".repeat(64),
  });
  const onProposalChange = jest.fn();
  render(<CreatorWorkspacePanel planId="plan-1" proposal={proposal} onProposalChange={onProposalChange} />);

  await waitFor(() => expect(getProposal).toHaveBeenCalledWith("plan-1", "proposal-1"));
  expect(onProposalChange).toHaveBeenCalledWith(expect.objectContaining({ status: "ready" }));
});

it("stops polling when the parent advances the proposal to a terminal status", async () => {
  jest.useFakeTimers();
  const proposal = {
    id: "proposal-1",
    proposal_id: "proposal-1",
    creator_id: "creator-1",
    plan_id: "plan-1",
    ownership_epoch: 1,
    idempotency_key: "key-2",
    request_digest: "digest-2",
    media_ids: ["upload-1"],
    status: "processing" as const,
    relevance: null,
    target_plan_item_id: null,
    topic: null,
    rationale: null,
    confidence: null,
    proposal_hash: null,
    error_code: null,
    decision: null,
    result_plan_item_id: null,
  };
  getProposal.mockResolvedValue({ ...proposal, status: "ready", relevance: "unmatched" });
  const onProposalChange = jest.fn();
  const view = render(<CreatorWorkspacePanel planId="plan-1" proposal={proposal} onProposalChange={onProposalChange} />);
  await waitFor(() => expect(getProposal).toHaveBeenCalledTimes(1));
  getProposal.mockClear();
  view.rerender(<CreatorWorkspacePanel planId="plan-1" proposal={{ ...proposal, status: "ready" }} onProposalChange={onProposalChange} />);
  await act(async () => {
    jest.advanceTimersByTime(5000);
  });
  expect(getProposal).not.toHaveBeenCalled();
  jest.useRealTimers();
});

it("does not poll or render a proposal from another plan", async () => {
  const foreignProposal = {
    id: "proposal-1",
    proposal_id: "proposal-1",
    creator_id: "creator-1",
    plan_id: "other-plan",
    ownership_epoch: 1,
    idempotency_key: "key-2",
    request_digest: "digest-2",
    media_ids: ["upload-1"],
    status: "processing" as const,
    relevance: null,
    target_plan_item_id: null,
    topic: null,
    rationale: null,
    confidence: null,
    proposal_hash: null,
    error_code: null,
    decision: null,
    result_plan_item_id: null,
  };
  const { container } = render(<CreatorWorkspacePanel planId="plan-1" proposal={foreignProposal} onProposalChange={jest.fn()} />);
  await act(async () => Promise.resolve());
  expect(getProposal).not.toHaveBeenCalled();
  expect(container.textContent).not.toContain("Checking where this footage belongs");
  expect(container.textContent).not.toContain("new topic");
});

it("stays absent when workspace coordination is not advertised", async () => {
  pollReceipt.mockRejectedValue(new PlanApiError({ message: "off", status: 404 }));
  const { container } = render(<CreatorWorkspacePanel planId="plan-1" />);
  await waitFor(() => expect(container.innerHTML).toBe(""));
});

it("surfaces non-404 workspace polling failures even when capability is unavailable", async () => {
  pollReceipt.mockRejectedValue(new PlanApiError({ message: "temporarily unavailable", status: 503 }));
  render(<CreatorWorkspacePanel planId="plan-1" />);

  expect((await screen.findByRole("status")).textContent).toContain("Workspace progress is temporarily unavailable.");
  expect(screen.getByRole("button", { name: "Retry" })).not.toBeNull();
});

it("retries a quiet workspace error and restores progress", async () => {
  pollReceipt.mockReset();
  // Keep both responses explicit so this test verifies the user action,
  // rather than relying on the default configured in beforeEach.
  pollReceipt
    .mockRejectedValueOnce(new PlanApiError({ message: "temporarily unavailable", status: 503 }))
    .mockResolvedValueOnce({
      receipt_id: "receipt-1",
      creator_id: "creator-1",
      plan_id: "plan-1",
      ownership_epoch: 1,
      idempotency_key: "key-1",
      request_digest: "digest-1",
      status: "ready",
      deliverables: [],
      preference_summary: null,
      style: null,
    });
  render(<CreatorWorkspacePanel planId="plan-1" />);
  await screen.findByRole("button", { name: "Retry" });
  fireEvent.click(screen.getByRole("button", { name: "Retry" }));
  await waitFor(() => expect(screen.getByText("Workspace progress")).not.toBeNull());
  expect(screen.getByText(/Ready/)).not.toBeNull();
  expect(pollReceipt).toHaveBeenCalledTimes(2);
});
