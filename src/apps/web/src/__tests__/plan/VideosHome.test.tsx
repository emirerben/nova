// @ts-nocheck
/**
 * WorkspaceHome (v0.44 "basic home") — create block + PAST EDITS grid.
 * Guards the redesign contract: /plan is openly the create-a-new-video page,
 * past edits render below via listMyJobs, SeedUploadCard still gates on
 * activation, and the ideas ledger is gone.
 */
import React from "react";
import { render, screen, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom";

import { WorkspaceHome } from "@/app/plan/_components/workspace/WorkspaceHome";
import { listMyJobs } from "@/lib/me-api";

jest.mock("@/lib/me-api", () => ({
  listMyJobs: jest.fn(),
}));

jest.mock("@/components/library/LibraryTile", () => ({
  __esModule: true,
  default: ({ job }) => <div data-testid={`tile-${job.id}`}>{job.status}</div>,
}));

jest.mock("@/components/library/TikTokConnectionCard", () => ({
  __esModule: true,
  default: () => <div data-testid="tiktok-card" />,
}));

jest.mock("@/app/plan/_components/SeedUploadCard", () => ({
  __esModule: true,
  default: () => <div data-testid="seed-upload-card" />,
}));

const mockListMyJobs = listMyJobs as jest.MockedFunction<typeof listMyJobs>;

function makePlan(overrides = {}) {
  return {
    id: "plan-1",
    plan_status: "ready",
    activation_status: "none",
    items: [],
    ...overrides,
  };
}

function job(id, status = "ready") {
  return { id, status, content_plan_item_id: null };
}

function renderHome(plan = makePlan()) {
  return render(
    <WorkspaceHome
      plan={plan}
      onRefresh={jest.fn()}
      onPlanChange={jest.fn()}
      onError={jest.fn()}
    />,
  );
}

describe("WorkspaceHome (basic home)", () => {
  beforeEach(() => {
    mockListMyJobs.mockReset().mockResolvedValue({ jobs: [], next_cursor: null });
  });

  it("leads with the create block linking to /plan/new", async () => {
    renderHome();
    expect(
      screen.getByRole("heading", { name: "Make a new video." }),
    ).toBeInTheDocument();
    const cta = screen.getByRole("link", { name: "New video" });
    expect(cta).toHaveAttribute("href", "/plan/new");
    await waitFor(() => expect(mockListMyJobs).toHaveBeenCalled());
  });

  it("renders past edits from listMyJobs, newest layout intact", async () => {
    mockListMyJobs.mockResolvedValue({
      jobs: [job("j1", "ready"), job("j2", "generating")],
      next_cursor: null,
    });
    renderHome();
    expect(await screen.findByTestId("tile-j1")).toBeInTheDocument();
    expect(screen.getByTestId("tile-j2")).toBeInTheDocument();
    expect(screen.getByText("Past edits")).toBeInTheDocument();
  });

  it("shows the quiet empty line with zero jobs — no ideas ledger anywhere", async () => {
    renderHome();
    expect(await screen.findByText("Your edits will live here.")).toBeInTheDocument();
    expect(screen.queryByText(/Pitch your first idea/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Generate with AI/)).not.toBeInTheDocument();
  });

  it("mounts SeedUploadCard only while the plan is activating", async () => {
    const { unmount } = renderHome(makePlan({ activation_status: "seeding" }));
    expect(screen.getByTestId("seed-upload-card")).toBeInTheDocument();
    unmount();
    renderHome(makePlan({ activation_status: "activated" }));
    expect(screen.queryByTestId("seed-upload-card")).not.toBeInTheDocument();
    await waitFor(() => expect(mockListMyJobs).toHaveBeenCalled());
  });

  it("notes a still-generating plan without blocking creation", async () => {
    renderHome(makePlan({ plan_status: "generating" }));
    expect(
      await screen.findByText(/still setting up your plan/),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "New video" })).toBeInTheDocument();
  });

  it("pages through more jobs via the cursor", async () => {
    mockListMyJobs
      .mockResolvedValueOnce({ jobs: [job("j1")], next_cursor: "c2" })
      .mockResolvedValueOnce({ jobs: [job("j2")], next_cursor: null });
    renderHome();
    const more = await screen.findByRole("button", { name: "Load more" });
    more.click();
    expect(await screen.findByTestId("tile-j2")).toBeInTheDocument();
    expect(mockListMyJobs).toHaveBeenLastCalledWith({ cursor: "c2" });
    expect(screen.queryByRole("button", { name: "Load more" })).not.toBeInTheDocument();
  });

  it("renders an Integrations section with the TikTok card under the grid (release rails target /plan#tiktok)", async () => {
    const { container } = renderHome();
    expect(screen.getByRole("heading", { name: "Integrations" })).toBeInTheDocument();
    expect(container.querySelector("#tiktok")).toBeInTheDocument();
    expect(screen.getByTestId("tiktok-card")).toBeInTheDocument();
    await waitFor(() => expect(mockListMyJobs).toHaveBeenCalled());
  });
});
