import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import MainCreatorAgentPanel from "@/app/plan/items/[id]/components/MainCreatorAgentPanel";
import {
  confirmCreatorAgentPlan,
  getCreatorAgentSession,
  PlanApiError,
  startCreatorAgentSession,
} from "@/lib/plan-api";

jest.mock("@/lib/plan-api", () => ({
  getCreatorAgentSession: jest.fn(),
  startCreatorAgentSession: jest.fn(),
  turnCreatorAgentSession: jest.fn(),
  confirmCreatorAgentPlan: jest.fn(),
  cancelCreatorAgentSession: jest.fn(),
  PlanApiError: jest.requireActual("@/lib/plan-api").PlanApiError,
}));

const getSession = getCreatorAgentSession as jest.MockedFunction<typeof getCreatorAgentSession>;
const startSession = startCreatorAgentSession as jest.MockedFunction<
  typeof startCreatorAgentSession
>;
const confirmPlan = confirmCreatorAgentPlan as jest.MockedFunction<typeof confirmCreatorAgentPlan>;

const proposed = {
  id: "session-1",
  status: "awaiting_confirmation" as const,
  revision: 2,
  render_attempts: 0,
  max_render_attempts: 2,
  can_render: true,
  pending_plan: {
    version: 1,
    plan_hash: "a".repeat(64),
    summary: "A fast arrival-to-sunset story.",
    creative_rationale: "Open with motion and end on the emotional payoff.",
    edit_format: "montage",
    audio_strategy: "licensed_music",
  },
  current_job_id: null,
  events: [],
  created_at: "2026-08-24T00:00:00Z",
  updated_at: "2026-08-24T00:00:00Z",
};

beforeEach(() => {
  jest.clearAllMocks();
  getSession.mockResolvedValue(null);
});

it("asks for intent and never renders before explicit confirmation", async () => {
  startSession.mockResolvedValue(proposed);
  render(<MainCreatorAgentPanel itemId="item-1" />);

  await screen.findByText("Create with Kria");
  fireEvent.change(screen.getByLabelText("Message Kria"), {
    target: { value: "Make it feel energetic" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Start" }));

  await screen.findByText("A fast arrival-to-sunset story.");
  expect(startSession).toHaveBeenCalledTimes(1);
  expect(confirmPlan).not.toHaveBeenCalled();
});

it("dispatches only after Render this is clicked", async () => {
  getSession.mockResolvedValue(proposed);
  confirmPlan.mockResolvedValue({
    ...proposed,
    status: "rendering",
    revision: 4,
    pending_plan: null,
    render_attempts: 1,
    current_job_id: "job-1",
  });
  render(<MainCreatorAgentPanel itemId="item-1" />);

  const confirm = await screen.findByRole("button", { name: "Render this" });
  expect(confirmPlan).not.toHaveBeenCalled();
  fireEvent.click(confirm);

  await waitFor(() => expect(confirmPlan).toHaveBeenCalledTimes(1));
  expect(await screen.findByText("Rendering the confirmed direction…")).not.toBeNull();
});

it("stays hidden when the user is outside the backend rollout cohort", async () => {
  getSession.mockRejectedValue(
    new PlanApiError({
      message: "Creator agent unavailable",
      status: 404,
    }),
  );

  const { container } = render(<MainCreatorAgentPanel itemId="item-1" />);

  await waitFor(() => expect(getSession).toHaveBeenCalledTimes(1));
  await waitFor(() => expect(container.innerHTML).toBe(""));
});

it("shows a strategy preview without a dead render action during planning-only rollout", async () => {
  getSession.mockResolvedValue({ ...proposed, can_render: false });

  render(<MainCreatorAgentPanel itemId="item-1" />);

  expect(await screen.findByText("A fast arrival-to-sunset story.")).not.toBeNull();
  expect(screen.queryByRole("button", { name: "Render this" })).toBeNull();
  expect(screen.getByText("Rendering is not enabled for this preview yet.")).not.toBeNull();
});

it("shows bounded review evidence without mutating the confirmed render", async () => {
  getSession.mockResolvedValue({
    ...proposed,
    status: "awaiting_feedback",
    pending_plan: null,
    last_review: {
      status: "complete",
      decision: "revise",
      evidence: [
        {
          evidence_id: "evidence-1",
          kind: "visual",
          severity: "warning",
          start_s: 4,
          end_s: 5,
          observation: "The opening loses momentum.",
        },
      ],
      proposed_revision: {
        revision_id: "revision-1",
        summary: "Tighten the first beat.",
        evidence_ids: ["evidence-1"],
      },
    },
  });

  render(<MainCreatorAgentPanel itemId="item-1" />);

  expect(await screen.findByText("The opening loses momentum.")).not.toBeNull();
  expect(screen.getByText(/Nothing changes without your confirmation/)).not.toBeNull();
  expect(confirmPlan).not.toHaveBeenCalled();
});
