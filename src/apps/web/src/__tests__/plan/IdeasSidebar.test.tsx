// @ts-nocheck
import React from "react";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import "@testing-library/jest-dom";
import type { ContentPlan, PlanItem } from "@/lib/plan-api";
import { addIdea, deleteIdea, generateIdeasWithAI } from "@/lib/plan-api";
import { IdeasSidebar } from "@/app/plan/_components/workspace/IdeasSidebar";

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

describe("IdeasSidebar", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockAddIdea.mockResolvedValue(makeItem({ id: "new-idea", idea: "New idea" }));
    mockDeleteIdea.mockResolvedValue(undefined);
    mockGenerateIdeasWithAI.mockResolvedValue(makePlan());
  });

  it("renders bare ideas under Ideas and scheduled ideas under In your plan with Day chips", () => {
    const plan = makePlan([
      makeItem({ id: "bare-2", idea: "Second bare idea", position: 2 }),
      makeItem({ id: "scheduled-3", idea: "Third planned idea", day_index: 3, position: 3 }),
      makeItem({ id: "bare-1", idea: "First bare idea", position: 1 }),
      makeItem({ id: "scheduled-1", idea: "First planned idea", day_index: 1, position: 4 }),
    ]);

    render(<IdeasSidebar plan={plan} onRefresh={jest.fn()} />);

    const ideasList = screen.getByRole("list", { name: "Ideas" });
    expect(within(ideasList).getAllByRole("link").map((link) => link.textContent)).toEqual([
      "First bare idea",
      "Second bare idea",
    ]);

    const plannedList = screen.getByRole("list", { name: "In your plan" });
    expect(within(plannedList).getAllByRole("link").map((link) => link.textContent)).toEqual([
      "First planned idea",
      "Third planned idea",
    ]);
    expect(within(plannedList).getByText("Day 1")).toBeInTheDocument();
    expect(within(plannedList).getByText("Day 3")).toBeInTheDocument();
  });

  it("does not render In your plan when there are no scheduled items", () => {
    const plan = makePlan([
      makeItem({ id: "bare-1", idea: "Unscheduled idea", position: 1 }),
    ]);

    render(<IdeasSidebar plan={plan} onRefresh={jest.fn()} />);

    expect(screen.queryByText("In your plan")).not.toBeInTheDocument();
    expect(screen.queryByRole("list", { name: "In your plan" })).not.toBeInTheDocument();
  });

  it("renders Generate with AI failures under the button", async () => {
    mockGenerateIdeasWithAI.mockRejectedValueOnce(new Error("409 already generating"));

    render(<IdeasSidebar plan={makePlan()} onRefresh={jest.fn()} />);

    fireEvent.click(screen.getByRole("button", { name: /generate with ai/i }));

    expect(await screen.findByRole("alert")).toHaveTextContent("409 already generating");
    expect(screen.queryByText("Couldn't save")).not.toBeInTheDocument();
  });

  it("add and delete interactions call the plan API client", async () => {
    const onRefresh = jest.fn();
    const plan = makePlan([
      makeItem({ id: "bare-1", idea: "Remove this idea", position: 1 }),
    ]);

    render(<IdeasSidebar plan={plan} onRefresh={onRefresh} />);

    const input = screen.getByLabelText("Add a new idea");
    fireEvent.change(input, { target: { value: "A fresh idea" } });
    fireEvent.keyDown(input, { key: "Enter" });

    await waitFor(() => {
      expect(mockAddIdea).toHaveBeenCalledWith("plan-1", "A fresh idea");
    });

    fireEvent.click(screen.getByRole("button", { name: "Remove idea: Remove this idea" }));

    await waitFor(() => {
      expect(mockDeleteIdea).toHaveBeenCalledWith("bare-1");
    });
    expect(onRefresh).toHaveBeenCalledTimes(2);
  });
});
