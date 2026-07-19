// @ts-nocheck
import React from "react";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import "@testing-library/jest-dom";
import type { ContentPlan, PlanItem, PlanItemStatus } from "@/lib/plan-api";
import { addIdea, deleteIdea, generateIdeasWithAI } from "@/lib/plan-api";
import { IdeasHome } from "@/app/plan/_components/workspace/IdeasHome";

jest.mock("@/lib/plan-api", () => ({
  ...jest.requireActual("@/lib/plan-api"),
  addIdea: jest.fn(),
  deleteIdea: jest.fn(),
  generateIdeasWithAI: jest.fn(),
}));

const mockAddIdea = addIdea as jest.MockedFunction<typeof addIdea>;
const mockDeleteIdea = deleteIdea as jest.MockedFunction<typeof deleteIdea>;
const mockGenerateIdeasWithAI = generateIdeasWithAI as jest.MockedFunction<typeof generateIdeasWithAI>;

function makeItem(overrides: Partial<PlanItem>): PlanItem {
  return {
    id: "item-1",
    day_index: null,
    theme: null,
    idea: "Idea",
    position: 0,
    scheduled_date: null,
    notes: null,
    scenes: [],
    filming_suggestion: null,
    rationale: null,
    filming_guide: [],
    clip_gcs_paths: [],
    clip_assignments: [],
    status: "idea",
    current_job_id: null,
    user_edited: false,
    landscape_fit: "fit",
    ...overrides,
  };
}

function makePlan(items: PlanItem[] = [], overrides: Partial<ContentPlan> = {}): ContentPlan {
  return {
    id: "plan-1",
    plan_status: "ready",
    horizon_days: 30,
    events: null,
    items,
    activation_status: "none",
    seed_clip_count: 0,
    ...overrides,
  };
}

describe("IdeasHome", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockAddIdea.mockResolvedValue(makeItem({ id: "new-idea", idea: "New idea" }));
    mockDeleteIdea.mockResolvedValue(undefined);
    mockGenerateIdeasWithAI.mockResolvedValue(makePlan());
  });

  it("orders rows newest-first by descending position", () => {
    const plan = makePlan([
      makeItem({ id: "old", idea: "Old idea", position: 1 }),
      makeItem({ id: "new", idea: "Newest idea", position: 3 }),
      makeItem({ id: "mid", idea: "Middle idea", position: 2 }),
    ]);

    render(<IdeasHome plan={plan} onRefresh={jest.fn()} />);

    const ledger = screen.getByRole("list", { name: "Ideas ledger" });
    expect(within(ledger).getAllByRole("link").map((link) => link.textContent)).toEqual([
      "Newest idea",
      "Middle idea",
      "Old idea",
    ]);
  });

  it("disables Generate with AI while one generation is in flight", async () => {
    let resolveGenerate: (plan: ContentPlan) => void = () => {};
    mockGenerateIdeasWithAI.mockImplementationOnce(
      () => new Promise((resolve) => {
        resolveGenerate = resolve;
      }),
    );
    render(<IdeasHome plan={makePlan()} onRefresh={jest.fn()} />);

    const button = screen.getByRole("button", { name: /generate with ai/i });
    fireEvent.click(button);

    expect(button).toBeDisabled();
    fireEvent.click(button);
    expect(mockGenerateIdeasWithAI).toHaveBeenCalledTimes(1);

    resolveGenerate(makePlan());
    await waitFor(() => expect(button).not.toBeDisabled());
  });

  it("hides the stat line when ready and rendering counts are both zero", () => {
    render(<IdeasHome plan={makePlan([makeItem({ status: "idea" })])} onRefresh={jest.fn()} />);

    expect(screen.queryByText(/ready/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/rendering/i)).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /view ready videos/i })).not.toBeInTheDocument();
  });

  it("hides zero-value stat fragments", () => {
    render(
      <IdeasHome
        plan={makePlan([makeItem({ id: "ready", status: "ready", position: 1 })])}
        onRefresh={jest.fn()}
      />,
    );

    expect(screen.getByText("1 ready")).toBeInTheDocument();
    expect(screen.queryByText(/0 rendering/i)).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: /view ready videos in your library/i }))
      .toHaveAttribute("href", "/library");
  });

  it.each<[PlanItemStatus, string]>([
    ["ready", "Ready to post"],
    ["generating", "Rendering…"],
    ["rerolling", "Rendering…"],
    ["failed", "Didn't render — open to retry"],
    ["awaiting_clips", "Needs footage"],
    ["idea", "Plan this →"],
  ])("renders the %s status slot", (status, label) => {
    render(
      <IdeasHome
        plan={makePlan([makeItem({ id: status, status, position: 1 })])}
        onRefresh={jest.fn()}
      />,
    );

    expect(screen.getByText(label)).toBeInTheDocument();
  });

  it("shows the localized production date next to a ready status", () => {
    const finishedAt = "2026-07-19T12:30:00Z";
    render(
      <IdeasHome
        plan={makePlan([
          makeItem({ id: "ready", status: "ready", position: 1, finished_at: finishedAt }),
        ])}
        onRefresh={jest.fn()}
      />,
    );

    expect(screen.getByText(`· ${new Date(finishedAt).toLocaleDateString()}`))
      .toBeInTheDocument();
  });

  it.each([null, undefined, "not-a-date"])(
    "keeps the ready status clean when finished_at is %s",
    (finishedAt) => {
      render(
        <IdeasHome
          plan={makePlan([
            makeItem({ id: "ready", status: "ready", position: 1, finished_at: finishedAt }),
          ])}
          onRefresh={jest.fn()}
        />,
      );

      expect(screen.getByText("Ready to post")).toBeInTheDocument();
      expect(screen.queryByText(/Invalid Date/)).not.toBeInTheDocument();
      expect(screen.queryByText(/^· /)).not.toBeInTheDocument();
    },
  );

  it.each<PlanItemStatus>(["ready", "generating", "rerolling"])(
    "shows delete confirmation for %s rows",
    (status) => {
      render(
        <IdeasHome
          plan={makePlan([makeItem({ id: status, idea: `${status} idea`, status, position: 1 })])}
          onRefresh={jest.fn()}
        />,
      );

      fireEvent.click(screen.getByRole("button", { name: `Remove idea: ${status} idea` }));

      expect(screen.getByText(/Delete idea\? It has a video/)).toBeInTheDocument();
      expect(mockDeleteIdea).not.toHaveBeenCalled();
    },
  );

  it.each<PlanItemStatus>(["idea", "awaiting_clips", "failed"])(
    "deletes %s rows immediately",
    async (status) => {
      render(
        <IdeasHome
          plan={makePlan([makeItem({ id: status, idea: `${status} idea`, status, position: 1 })])}
          onRefresh={jest.fn()}
        />,
      );

      fireEvent.click(screen.getByRole("button", { name: `Remove idea: ${status} idea` }));

      await waitFor(() => expect(mockDeleteIdea).toHaveBeenCalledWith(status));
      expect(screen.queryByText(/Delete idea\? It has a video/)).not.toBeInTheDocument();
    },
  );

  it("renders the empty-state invitation", () => {
    render(<IdeasHome plan={makePlan()} onRefresh={jest.fn()} />);

    expect(screen.getByText("Pitch your first idea.")).toBeInTheDocument();
  });

  it("renders an aria-live shimmer row while the plan is generating", () => {
    render(<IdeasHome plan={makePlan([], { plan_status: "generating" })} onRefresh={jest.fn()} />);

    const status = screen.getByRole("status");
    expect(status).toHaveAttribute("aria-live", "polite");
    expect(within(status).getByText("Kria is writing an idea…")).toBeInTheDocument();
  });

  it("renders Generate with AI failures under the composer", async () => {
    mockGenerateIdeasWithAI.mockRejectedValueOnce(new Error("409 already generating"));

    render(<IdeasHome plan={makePlan()} onRefresh={jest.fn()} />);

    fireEvent.click(screen.getByRole("button", { name: /generate with ai/i }));

    expect(await screen.findByRole("alert")).toHaveTextContent("409 already generating");
  });
});
