/**
 * Tests for the Worker state panel on /admin/jobs/[id].
 *
 * Load-bearing operator-safety property:
 *
 *   "NOT FOUND" (worker likely died) must render distinctly from
 *   "UNKNOWN"  (broker unreachable, state is uncertain).
 *
 * If they collapse to the same UI, an operator could cancel a healthy
 * job during a broker hiccup. The two cases call for opposite actions:
 * NOT FOUND → safe to cancel + cleanup; UNKNOWN → wait and verify.
 *
 * Also verifies the Cancel button visibility tracks the DB status (only
 * cancellable statuses get the button).
 */

import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import "@testing-library/jest-dom";

jest.mock("next/link", () => {
  const Mock = ({ children, href }: { children: React.ReactNode; href: string }) => (
    <a href={href}>{children}</a>
  );
  Mock.displayName = "NextLinkMock";
  return Mock;
});

const mockGetDebug = jest.fn();
const mockCancelJob = jest.fn();
jest.mock("@/lib/admin-jobs-api", () => ({
  adminGetJobDebug: (...args: unknown[]) => mockGetDebug(...args),
  adminCancelJob: (...args: unknown[]) => mockCancelJob(...args),
}));

// JsonTreeView pulls in heavy markdown deps in tests; stub it.
jest.mock("@/components/JsonTreeView", () => ({
  JsonTreeView: () => <div data-testid="json-tree-stub" />,
}));

jest.mock("@/app/admin/_shared/AgentSection", () => ({
  AgentSection: () => <div data-testid="agent-section-stub" />,
}));

jest.mock("@/app/admin/jobs/[id]/Timeline", () => ({
  Timeline: () => <div data-testid="timeline-stub" />,
}));

import JobDebugPage from "@/app/admin/jobs/[id]/page";

const baseJob = {
  id: "11111111-2222-3333-4444-555555555555",
  user_id: "user-1",
  status: "processing",
  job_type: "music",
  mode: null,
  template_id: null,
  music_track_id: null,
  failure_reason: null,
  error_detail: null,
  current_phase: "analyze_clips",
  phase_log: [],
  raw_storage_path: null,
  selected_platforms: null,
  probe_metadata: null,
  transcript: null,
  scene_cuts: null,
  all_candidates: null,
  assembly_plan: null,
  pipeline_trace: null,
  started_at: "2026-05-19T10:00:00Z",
  finished_at: null,
  created_at: "2026-05-19T09:59:00Z",
  updated_at: "2026-05-19T10:00:00Z",
  celery_task_id: "11111111-2222-3333-4444-555555555555",
};

function buildDebugResponse(overrides: {
  status?: string;
  runtime: {
    state: "active" | "reserved" | "not_found" | "unknown";
    worker?: string | null;
    task_id?: string | null;
    queue_position?: number | null;
  };
}) {
  return {
    job: { ...baseJob, status: overrides.status ?? "processing" },
    job_clips: [],
    template: null,
    music_track: null,
    agent_runs: [],
    template_agent_runs: [],
    track_agent_runs: [],
    runtime: {
      worker: null,
      task_id: baseJob.celery_task_id,
      queue_position: null,
      ...overrides.runtime,
    },
  };
}

beforeEach(() => {
  jest.clearAllMocks();
});

describe("WorkerStatePanel — operator-safety rendering", () => {
  it("renders ACTIVE with the worker hostname", async () => {
    mockGetDebug.mockResolvedValue(
      buildDebugResponse({
        runtime: { state: "active", worker: "celery@worker-7" },
      }),
    );
    render(<JobDebugPage params={{ id: baseJob.id }} />);

    await waitFor(() => {
      expect(screen.getByText("ACTIVE")).toBeInTheDocument();
    });
    expect(screen.getByText("celery@worker-7")).toBeInTheDocument();
  });

  it("renders RESERVED with queue position", async () => {
    mockGetDebug.mockResolvedValue(
      buildDebugResponse({
        status: "queued",
        runtime: { state: "reserved", worker: "celery@worker-1", queue_position: 2 },
      }),
    );
    render(<JobDebugPage params={{ id: baseJob.id }} />);

    await waitFor(() => {
      expect(screen.getByText("RESERVED")).toBeInTheDocument();
    });
    expect(screen.getByText("queue pos:")).toBeInTheDocument();
    expect(screen.getByText("2")).toBeInTheDocument();
  });

  it("renders NOT FOUND with the worker-died advisory copy", async () => {
    mockGetDebug.mockResolvedValue(
      buildDebugResponse({
        runtime: { state: "not_found", worker: null },
      }),
    );
    render(<JobDebugPage params={{ id: baseJob.id }} />);

    await waitFor(() => {
      expect(screen.getByText("NOT FOUND")).toBeInTheDocument();
    });
    // Operator-safety copy: tells the operator a cancel is safe here.
    expect(
      screen.getByText(/Worker did not report this task/i),
    ).toBeInTheDocument();
  });

  it("renders UNKNOWN distinctly from NOT FOUND, with do-not-cancel copy", async () => {
    // This is THE load-bearing test. If "unknown" rendered the same as
    // "not_found" (or rendered the worker-died copy), an operator could
    // cancel a healthy job during a broker hiccup.
    mockGetDebug.mockResolvedValue(
      buildDebugResponse({
        runtime: { state: "unknown", worker: null },
      }),
    );
    render(<JobDebugPage params={{ id: baseJob.id }} />);

    await waitFor(() => {
      expect(screen.getByText("UNKNOWN")).toBeInTheDocument();
    });
    // UNKNOWN must NOT render the NOT FOUND chip.
    expect(screen.queryByText("NOT FOUND")).not.toBeInTheDocument();
    // UNKNOWN must NOT render the worker-died advisory.
    expect(
      screen.queryByText(/Worker did not report this task/i),
    ).not.toBeInTheDocument();
    // UNKNOWN MUST render the broker-unreachable advisory.
    expect(
      screen.getByText(/Broker unreachable/i),
    ).toBeInTheDocument();
  });
});

describe("Cancel button visibility tracks status", () => {
  it("shows the Cancel button while status is cancellable", async () => {
    mockGetDebug.mockResolvedValue(
      buildDebugResponse({
        status: "processing",
        runtime: { state: "active", worker: "celery@w1" },
      }),
    );
    render(<JobDebugPage params={{ id: baseJob.id }} />);

    await waitFor(() => {
      expect(screen.getByRole("button", { name: /Cancel job/i })).toBeInTheDocument();
    });
  });

  it("hides the Cancel button once status is terminal", async () => {
    mockGetDebug.mockResolvedValue(
      buildDebugResponse({
        status: "music_ready",
        runtime: { state: "unknown", worker: null },
      }),
    );
    render(<JobDebugPage params={{ id: baseJob.id }} />);

    await waitFor(() => {
      // For terminal rows the status itself is rendered (uppercased), not the
      // live worker state chip.
      expect(screen.getByText("MUSIC_READY")).toBeInTheDocument();
    });
    expect(screen.queryByRole("button", { name: /Cancel job/i })).not.toBeInTheDocument();
  });

  it("hides the Cancel button after the row is cancelled", async () => {
    mockGetDebug.mockResolvedValue(
      buildDebugResponse({
        status: "cancelled",
        runtime: { state: "unknown", worker: null },
      }),
    );
    render(<JobDebugPage params={{ id: baseJob.id }} />);

    await waitFor(() => {
      expect(screen.getByText("CANCELLED")).toBeInTheDocument();
    });
    expect(screen.queryByRole("button", { name: /Cancel job/i })).not.toBeInTheDocument();
  });
});

describe("Cancel confirmation flow", () => {
  it("first click reveals confirm; confirm POSTs to adminCancelJob and refetches", async () => {
    mockGetDebug.mockResolvedValue(
      buildDebugResponse({
        status: "processing",
        runtime: { state: "active", worker: "celery@w1" },
      }),
    );
    mockCancelJob.mockResolvedValue({
      job_id: baseJob.id,
      previous_status: "processing",
      status: "cancelled",
      task_id: baseJob.celery_task_id,
      revoke_sent: true,
    });

    render(<JobDebugPage params={{ id: baseJob.id }} />);

    await waitFor(() => {
      expect(screen.getByRole("button", { name: /Cancel job/i })).toBeInTheDocument();
    });

    // First click reveals the confirm pair.
    fireEvent.click(screen.getByRole("button", { name: /Cancel job/i }));
    expect(
      screen.getByRole("button", { name: /Yes, terminate task/i }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /Keep running/i }),
    ).toBeInTheDocument();

    // Confirm fires the POST.
    fireEvent.click(screen.getByRole("button", { name: /Yes, terminate task/i }));

    await waitFor(() => {
      expect(mockCancelJob).toHaveBeenCalledWith(baseJob.id);
    });
    // Refetch happens via onCancelled -> adminGetJobDebug.
    expect(mockGetDebug.mock.calls.length).toBeGreaterThanOrEqual(2);
  });

  it("Keep running aborts the cancel without POSTing", async () => {
    mockGetDebug.mockResolvedValue(
      buildDebugResponse({
        status: "processing",
        runtime: { state: "active", worker: "celery@w1" },
      }),
    );

    render(<JobDebugPage params={{ id: baseJob.id }} />);

    await waitFor(() => {
      expect(screen.getByRole("button", { name: /Cancel job/i })).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole("button", { name: /Cancel job/i }));
    fireEvent.click(screen.getByRole("button", { name: /Keep running/i }));

    // Confirm pair goes away; primary Cancel button returns.
    await waitFor(() => {
      expect(screen.getByRole("button", { name: /Cancel job/i })).toBeInTheDocument();
    });
    expect(mockCancelJob).not.toHaveBeenCalled();
  });
});
