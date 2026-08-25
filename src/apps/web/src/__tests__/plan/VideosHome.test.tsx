// @ts-nocheck
/**
 * WorkspaceHome (v0.44 "basic home") — create block + PAST EDITS grid.
 * Guards the redesign contract: /plan is openly the create-a-new-video page,
 * past edits render below via listMyJobs, SeedUploadCard still gates on
 * activation, and the ideas ledger is gone.
 */
import React from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom";

import { WorkspaceHome } from "@/app/plan/_components/workspace/WorkspaceHome";
import { listMyJobs } from "@/lib/me-api";

jest.mock("@/lib/me-api", () => ({
  listMyJobs: jest.fn(),
}));

jest.mock("@/components/library/LibraryTile", () => ({
  __esModule: true,
  default: ({ job, onDeleted }) => (
    <>
      <div data-testid={`tile-${job.id}`}>{job.status}</div>
      <button type="button" onClick={() => onDeleted?.(job.id)}>
        Delete {job.id}
      </button>
    </>
  ),
}));

// TikTokConnectionCard reports availability to its parent via onConnection
// (available:false when TikTok isn't reachable for this account, in which
// case the real component itself renders null). WorkspaceHome must mount it
// unconditionally (so the callback ever fires) but hide the surrounding
// "Integrations" heading until availability is confirmed true.
let mockTikTokAvailable = true;
jest.mock("@/components/library/TikTokConnectionCard", () => {
  const React = require("react");
  function MockTikTokConnectionCard({ onConnection }) {
    React.useEffect(() => {
      onConnection?.(mockTikTokAvailable ? { available: true } : { available: false });
    }, []);
    return mockTikTokAvailable ? <div data-testid="tiktok-card" /> : null;
  }
  return {
    __esModule: true,
    default: MockTikTokConnectionCard,
  };
});

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
    mockTikTokAvailable = true;
  });

  it("leads with the create block linking to /plan/new", async () => {
    renderHome();
    expect(
      screen.getByRole("heading", { name: "Create a new video" }),
    ).toBeInTheDocument();
    const cta = screen.getByRole("link", { name: "Create a video" });
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
    expect(screen.getByText("Your videos")).toBeInTheDocument();
  });

  it("shows the quiet empty line with zero jobs — no ideas ledger anywhere", async () => {
    renderHome();
    expect(
      await screen.findByText("Your finished videos will appear here."),
    ).toBeInTheDocument();
    expect(screen.getByText("Create your first video to get started.")).toBeInTheDocument();
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
      await screen.findByText(/content plan is still being prepared/),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Create a video" })).toBeInTheDocument();
  });

  it("pages through more jobs via the cursor", async () => {
    mockListMyJobs
      .mockResolvedValueOnce({ jobs: [job("j1")], next_cursor: "c2" })
      .mockResolvedValueOnce({ jobs: [job("j2")], next_cursor: null });
    renderHome();
    const more = await screen.findByRole("button", { name: "Load more videos" });
    more.click();
    expect(await screen.findByTestId("tile-j2")).toBeInTheDocument();
    expect(mockListMyJobs).toHaveBeenLastCalledWith({ cursor: "c2" });
    expect(
      screen.queryByRole("button", { name: "Load more videos" }),
    ).not.toBeInTheDocument();
  });

  it("removes a deleted video from the local grid without reloading the page", async () => {
    mockListMyJobs.mockResolvedValue({ jobs: [job("j1")], next_cursor: null });
    renderHome();

    expect(await screen.findByTestId("tile-j1")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Delete j1" }));

    await waitFor(() => expect(screen.queryByTestId("tile-j1")).not.toBeInTheDocument());
    expect(screen.getByText("Your finished videos will appear here.")).toBeInTheDocument();
  });

  it("renders an Integrations section with the TikTok card under the grid (release rails target /plan#tiktok)", async () => {
    const { container } = renderHome();
    expect(
      await screen.findByRole("heading", { name: "Connected accounts" }),
    ).toBeInTheDocument();
    expect(container.querySelector("#tiktok")).toBeInTheDocument();
    expect(screen.getByTestId("tiktok-card")).toBeInTheDocument();
    await waitFor(() => expect(mockListMyJobs).toHaveBeenCalled());
  });

  it("hides the Integrations heading entirely when TikTok isn't available (no empty section)", async () => {
    mockTikTokAvailable = false;
    renderHome();
    await waitFor(() => expect(mockListMyJobs).toHaveBeenCalled());
    expect(
      screen.queryByRole("heading", { name: "Connected accounts" }),
    ).not.toBeInTheDocument();
    expect(screen.queryByTestId("tiktok-card")).not.toBeInTheDocument();
  });
});
