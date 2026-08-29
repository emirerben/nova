import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";

import MainCreatorAgentPanel from "@/app/plan/items/[id]/components/MainCreatorAgentPanel";
import {
  confirmCreatorAgentPlan,
  getCreatorAgentSession,
  PlanApiError,
  requestCreatorAutoIteration,
  startCreatorAgentSession,
  turnCreatorAgentSession,
} from "@/lib/plan-api";

jest.mock("@/lib/plan-api", () => ({
  getCreatorAgentSession: jest.fn(),
  startCreatorAgentSession: jest.fn(),
  turnCreatorAgentSession: jest.fn(),
  confirmCreatorAgentPlan: jest.fn(),
  cancelCreatorAgentSession: jest.fn(),
  requestCreatorAutoIteration: jest.fn(),
  PlanApiError: jest.requireActual("@/lib/plan-api").PlanApiError,
}));

const getSession = getCreatorAgentSession as jest.MockedFunction<typeof getCreatorAgentSession>;
const startSession = startCreatorAgentSession as jest.MockedFunction<
  typeof startCreatorAgentSession
>;
const confirmPlan = confirmCreatorAgentPlan as jest.MockedFunction<typeof confirmCreatorAgentPlan>;
const requestAuto = requestCreatorAutoIteration as jest.MockedFunction<
  typeof requestCreatorAutoIteration
>;
const turnSession = turnCreatorAgentSession as jest.MockedFunction<
  typeof turnCreatorAgentSession
>;

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

it("shows a compact status placeholder while checking creator-session eligibility", async () => {
  let resolveSession!: (value: null) => void;
  getSession.mockReturnValueOnce(
    new Promise((resolve) => {
      resolveSession = resolve;
    }),
  );

  render(<MainCreatorAgentPanel itemId="item-1" />);

  expect(
    screen.getByRole("region", { name: "Create with Kria" }).getAttribute("aria-busy"),
  ).toBe("true");
  expect(screen.getByRole("status").textContent).toContain("Checking your saved Kria plan…");

  await act(async () => resolveSession(null));
  await screen.findByLabelText("Message Kria");
});

it("asks for intent and never renders before explicit confirmation", async () => {
  startSession.mockResolvedValue(proposed);
  render(<MainCreatorAgentPanel itemId="item-1" />);

  const creatorMessage = await screen.findByLabelText("Message Kria");
  fireEvent.change(creatorMessage, {
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

it("submits the recommended clarification chip as the next creator turn", async () => {
  const clarification = {
    ...proposed,
    status: "briefing" as const,
    revision: 3,
    pending_plan: null,
    events: [
      {
        id: "event-1",
        role: "assistant" as const,
        event_type: "assistant_question",
        payload: {
          message: "I can make a balanced 12-second edit without repeating footage.",
          options: ["Use the best 12 seconds", "Repeat footage to keep 24 seconds"],
          recommended_option: "Use the best 12 seconds",
        },
        created_at: "2026-08-29T00:00:00Z",
      },
    ],
  };
  getSession.mockResolvedValue(clarification);
  turnSession.mockResolvedValue({
    ...proposed,
    pending_plan: { ...proposed.pending_plan, summary: "Twelve exact alternating cuts." },
  });

  render(<MainCreatorAgentPanel itemId="item-1" />);

  const recommendation = await screen.findByRole("button", {
    name: "Use the best 12 seconds (recommended)",
  });
  fireEvent.click(recommendation);

  await waitFor(() =>
    expect(turnSession).toHaveBeenCalledWith(
      "item-1",
      "session-1",
      "Use the best 12 seconds",
      3,
      expect.any(String),
    ),
  );
  expect((screen.getByLabelText("Message Kria") as HTMLTextAreaElement).value).toBe("");
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

it("recovers parent availability after a transient refresh failure and successful action", async () => {
  const onAvailabilityChange = jest.fn();
  getSession.mockRejectedValueOnce(new Error("temporary upstream failure"));
  startSession.mockResolvedValue(proposed);

  render(
    <MainCreatorAgentPanel
      itemId="item-1"
      onAvailabilityChange={onAvailabilityChange}
    />,
  );

  expect(await screen.findByText("Kria couldn't refresh this plan. Your work is safe.")).not.toBeNull();
  expect(onAvailabilityChange).toHaveBeenLastCalledWith("error");

  fireEvent.change(screen.getByLabelText("Message Kria"), {
    target: { value: "Alternate every second" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Start" }));

  await screen.findByText("A fast arrival-to-sunset story.");
  expect(onAvailabilityChange).toHaveBeenLastCalledWith("available");
  expect(screen.queryByText("Kria couldn't refresh this plan. Your work is safe.")).toBeNull();
});

it("offers an explicit retry after a transient refresh failure", async () => {
  getSession
    .mockRejectedValueOnce(new Error("temporary upstream failure"))
    .mockResolvedValueOnce(null);

  render(<MainCreatorAgentPanel itemId="item-1" />);

  await screen.findByText("Kria couldn't refresh this plan. Your work is safe.");
  fireEvent.click(screen.getByRole("button", { name: "Retry" }));

  await waitFor(() => expect(getSession).toHaveBeenCalledTimes(2));
  await waitFor(() =>
    expect(screen.queryByText("Kria couldn't refresh this plan. Your work is safe.")).toBeNull(),
  );
});

it("shows a strategy preview without a dead render action during planning-only rollout", async () => {
  getSession.mockResolvedValue({ ...proposed, can_render: false });

  render(<MainCreatorAgentPanel itemId="item-1" />);

  expect(await screen.findByText("A fast arrival-to-sunset story.")).not.toBeNull();
  expect(screen.queryByRole("button", { name: "Render this" })).toBeNull();
  expect(screen.getByText("Rendering is not enabled for this preview yet.")).not.toBeNull();
  expect(screen.queryByText("Photos 0.5–0.8s · Videos 1.5–3s · hard cuts")).toBeNull();
});

it("shows the normalized mixed-media timing profile in the confirmation preview", async () => {
  getSession.mockResolvedValue({
    ...proposed,
    pending_plan: {
      ...proposed.pending_plan,
      mixed_media_timing: {
        image_hold: "very_fast",
        video_hold: "longer",
        boundary_style: "cut",
      },
    },
  });

  render(<MainCreatorAgentPanel itemId="item-1" />);

  expect(
    await screen.findByText("Photos 0.5–0.8s · Videos 1.5–3s · hard cuts"),
  ).not.toBeNull();
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

it("shows a pending review as in progress and polls through awaiting feedback until it is terminal", async () => {
  jest.useFakeTimers();
  getSession
    .mockResolvedValueOnce({
      ...proposed,
      status: "awaiting_feedback",
      pending_plan: null,
      last_review: { status: "pending" },
    })
    .mockResolvedValueOnce({
      ...proposed,
      status: "awaiting_feedback",
      pending_plan: null,
      last_review: { status: "running" },
    })
    .mockResolvedValueOnce({
      ...proposed,
      status: "awaiting_feedback",
      pending_plan: null,
      last_review: { status: "complete", quality_score: 4.2, evidence: [] },
    });

  try {
    render(<MainCreatorAgentPanel itemId="item-1" />);
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(screen.getByText("Reviewing the exact render…")).not.toBeNull();
    expect(getSession).toHaveBeenCalledTimes(1);

    await act(async () => {
      jest.advanceTimersByTime(4_000);
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(getSession).toHaveBeenCalledTimes(2);
    expect(screen.getByText("Reviewing the exact render…")).not.toBeNull();

    await act(async () => {
      jest.advanceTimersByTime(4_000);
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(getSession).toHaveBeenCalledTimes(3);
    expect(screen.getByText("No evidence-linked issues were found.")).not.toBeNull();

    await act(async () => {
      jest.advanceTimersByTime(12_000);
      await Promise.resolve();
    });
    expect(getSession).toHaveBeenCalledTimes(3);
  } finally {
    jest.useRealTimers();
  }
});

it("requires the explicit checkbox and button before requesting automatic iteration", async () => {
  getSession.mockResolvedValue({
    ...proposed,
    status: "awaiting_feedback",
    pending_plan: null,
    auto_iteration: { available: true, label: "One objective revision, if eligible" },
  });
  requestAuto.mockResolvedValue({
    ...proposed,
    status: "rendering",
    pending_plan: null,
    auto_iteration: { available: true },
  });

  render(<MainCreatorAgentPanel itemId="item-1" />);

  expect(screen.queryByRole("button", { name: "Confirm automatic revision" })).toBeNull();
  fireEvent.click(await screen.findByRole("checkbox"));
  fireEvent.click(screen.getByRole("button", { name: "Confirm automatic revision" }));

  await waitFor(() => expect(requestAuto).toHaveBeenCalledTimes(1));
  expect(requestAuto.mock.calls[0][0]).toBe("item-1");
  expect(requestAuto.mock.calls[0][1]).toMatchObject({ opt_in: true, session_id: "session-1" });
});

it("hides automatic iteration when the server does not advertise capability", async () => {
  getSession.mockResolvedValue({
    ...proposed,
    status: "awaiting_feedback",
    pending_plan: null,
    auto_iteration: { available: false },
  });

  render(<MainCreatorAgentPanel itemId="item-1" />);

  await screen.findByText("Create with Kria");
  expect(screen.queryByRole("checkbox")).toBeNull();
  expect(requestAuto).not.toHaveBeenCalled();
});
