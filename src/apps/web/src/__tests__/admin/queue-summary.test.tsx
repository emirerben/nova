/**
 * Tests for the queue-summary panel on /admin/jobs.
 *
 * The panel surfaces broker-level state: active worker count, queue
 * depth per queue, oldest queued job. Critical render path: when the
 * broker is unreachable (ok=false), the panel must distinguish itself
 * from "0 queued, no jobs" so an operator doesn't make decisions on
 * stale data.
 */

import { render, screen, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom";

jest.mock("next/link", () => {
  const Mock = ({ children, href }: { children: React.ReactNode; href: string }) => (
    <a href={href}>{children}</a>
  );
  Mock.displayName = "NextLinkMock";
  return Mock;
});

const mockListJobs = jest.fn();
const mockQueueState = jest.fn();
jest.mock("@/lib/admin-jobs-api", () => ({
  adminListJobs: (...args: unknown[]) => mockListJobs(...args),
  adminGetQueueState: (...args: unknown[]) => mockQueueState(...args),
}));

import AdminJobsPage from "@/app/admin/jobs/page";

beforeEach(() => {
  jest.clearAllMocks();
  // Default: empty job list so we don't error on the table.
  mockListJobs.mockResolvedValue({ items: [], total: 0, limit: 50, offset: 0 });
});

describe("QueueSummary", () => {
  it("renders active worker count, total queued, and oldest queued link", async () => {
    mockQueueState.mockResolvedValue({
      ok: true,
      active_workers: ["celery@worker-1", "celery@worker-2"],
      queues: [
        { name: "celery", depth: 3, oldest_pending_job_id: "abcd1234-job" },
      ],
    });

    render(<AdminJobsPage />);

    await waitFor(() => {
      expect(screen.getByText("Active workers:")).toBeInTheDocument();
    });

    // 2 workers + 3 queued render somewhere in the panel.
    expect(screen.getByText("Queued:")).toBeInTheDocument();
    // The number 3 appears twice (total queued + per-queue depth);
    // assert at least one occurrence rather than uniqueness.
    expect(screen.getAllByText("3").length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText("2").length).toBeGreaterThanOrEqual(1);
    // Per-queue label format: `celery: 3`
    expect(screen.getByText("celery:")).toBeInTheDocument();
    // Oldest job link shows first 8 chars of the job id
    const link = screen.getByText(/oldest: abcd1234/);
    expect(link).toBeInTheDocument();
    expect(link.closest("a")).toHaveAttribute(
      "href",
      "/admin/jobs/abcd1234-job",
    );
  });

  it("renders the broker-unreachable banner when ok=false (NOT zeros)", async () => {
    // Load-bearing assertion: ok=false MUST NOT render as "0 queued".
    // An operator who reads "0 queued" assumes the queue is empty and
    // may cancel a healthy job. The banner is the only signal that the
    // numbers are unknown.
    mockQueueState.mockResolvedValue({
      ok: false,
      active_workers: [],
      queues: [],
    });

    render(<AdminJobsPage />);

    await waitFor(() => {
      expect(screen.getByText(/Broker unreachable/i)).toBeInTheDocument();
    });

    // Crucially, the "Queued: 0" header MUST NOT appear in this state.
    expect(screen.queryByText("Active workers:")).not.toBeInTheDocument();
    expect(screen.queryByText("Queued:")).not.toBeInTheDocument();
  });

  it("renders loading state on first paint before the snapshot resolves", () => {
    // Pending promise that never resolves during this synchronous render.
    mockQueueState.mockReturnValue(new Promise(() => {}));

    render(<AdminJobsPage />);

    expect(screen.getByText(/Loading queue state/i)).toBeInTheDocument();
  });

  it("renders the fetch-error banner when adminGetQueueState rejects", async () => {
    mockQueueState.mockRejectedValue(new Error("network blip"));

    render(<AdminJobsPage />);

    await waitFor(() => {
      expect(screen.getByText(/Could not fetch queue state/i)).toBeInTheDocument();
    });
    expect(screen.getByText(/network blip/i)).toBeInTheDocument();
  });
});
