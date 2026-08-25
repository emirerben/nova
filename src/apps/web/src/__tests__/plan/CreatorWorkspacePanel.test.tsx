import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { CreatorWorkspacePanel } from "@/app/plan/_components/workspace/CreatorWorkspacePanel";
import {
  decideCreatorWorkspaceRelevanceProposal,
  pollLatestCreatorWorkspaceReceipt,
  recordCreatorWorkspacePreferenceSignal,
  PlanApiError,
} from "@/lib/plan-api";

jest.mock("@/lib/plan-api", () => ({
  pollLatestCreatorWorkspaceReceipt: jest.fn(),
  recordCreatorWorkspacePreferenceSignal: jest.fn(),
  decideCreatorWorkspaceRelevanceProposal: jest.fn(),
  PlanApiError: jest.requireActual("@/lib/plan-api").PlanApiError,
}));

const pollReceipt = pollLatestCreatorWorkspaceReceipt as jest.MockedFunction<typeof pollLatestCreatorWorkspaceReceipt>;
const savePreference = recordCreatorWorkspacePreferenceSignal as jest.MockedFunction<typeof recordCreatorWorkspacePreferenceSignal>;
const decideProposal = decideCreatorWorkspaceRelevanceProposal as jest.MockedFunction<typeof decideCreatorWorkspaceRelevanceProposal>;

beforeEach(() => {
  jest.clearAllMocks();
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

  fireEvent.change(screen.getByLabelText("Preference note"), { target: { value: "Keep openings quiet" } });
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

it("stays absent when workspace coordination is not advertised", async () => {
  pollReceipt.mockRejectedValue(new PlanApiError({ message: "off", status: 404 }));
  const { container } = render(<CreatorWorkspacePanel planId="plan-1" />);
  await waitFor(() => expect(container.innerHTML).toBe(""));
});
