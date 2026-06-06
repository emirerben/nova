/**
 * Tests PR6: ProgressTheater renders for in-progress template jobs.
 *
 * Regression invariant:
 * - Old PhaseChips (flex-wrap) is gone; ProgressTheater renders instead.
 * - Old ElapsedTimer is gone.
 * - template-job-phases.ts is untouched (covered by job-phases.test.ts).
 * - useJobStream (SSE) preserved — mock controls data; page does not crash.
 */

// @ts-nocheck
import React from "react";

// jsdom doesn't implement window.matchMedia — mock it globally for all tests
// (used by StatusHeadline to read prefers-reduced-motion).
Object.defineProperty(window, "matchMedia", {
  writable: true,
  value: jest.fn().mockImplementation((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: jest.fn(),
    removeListener: jest.fn(),
    addEventListener: jest.fn(),
    removeEventListener: jest.fn(),
    dispatchEvent: jest.fn(),
  })),
});

import { render, screen } from "@testing-library/react";
import "@testing-library/jest-dom";

// Mock useJobStream so tests control the data flow.
jest.mock("@/hooks/useJobStream", () => ({
  useJobStream: jest.fn(),
}));

// Mock next/navigation — page uses useParams and useRouter.
const mockPush = jest.fn();
jest.mock("next/navigation", () => ({
  useParams: () => ({ id: "test-job-123" }),
  useRouter: () => ({ push: mockPush }),
}));

// Mock API calls to prevent real network calls.
jest.mock("@/lib/api", () => ({
  ...jest.requireActual("@/lib/api"),
  getTemplatePlaybackUrl: jest.fn(),
  rerollTemplateJob: jest.fn(),
}));

import { useJobStream } from "@/hooks/useJobStream";
import TemplateJobPage from "@/app/template-jobs/[id]/page";

const mockUseJobStream = useJobStream as jest.MockedFunction<typeof useJobStream>;

// ===== Factory helpers =====

function makeJob(overrides: Record<string, unknown> = {}) {
  return {
    job_id: "test-job-123",
    status: "analyzing_clips",
    template_id: "tmpl-abc",
    current_phase: "analyze_clips",
    phase_log: [],
    started_at: new Date(Date.now() - 30_000).toISOString(),
    created_at: new Date(Date.now() - 32_000).toISOString(),
    finished_at: null,
    output_url: null,
    assembly_plan: null,
    error_detail: null,
    failure_reason: null,
    expected_phase_durations: { download_clips: 8000, analyze_clips: 25000 },
    updated_at: new Date().toISOString(),
    ...overrides,
  };
}

// ===== Tests =====

describe("TemplateJobPage — PR6 ProgressTheater", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockPush.mockReset();
  });

  it("renders ProgressTheater for in-progress job (not the old spinner)", () => {
    mockUseJobStream.mockReturnValue({
      data: makeJob(),
      error: null,
      streaming: true,
    });
    const { container } = render(<TemplateJobPage />);
    // ProgressTheater renders a div.w-full as its root (size="full").
    // Old ProgressScreen used <main>; ProgressTheater is just a div.
    // Assert no old spinner circle (animate-spin border circle).
    expect(container.querySelector(".animate-spin")).toBeNull();
    // Assert ProgressTheater rendered (it has a div.space-y-8 wrapper).
    expect(container.querySelector("div.space-y-8")).toBeInTheDocument();
    // Page must not crash.
    expect(screen.queryByText(/exception/i)).toBeNull();
  });

  it("renders without crash when job is null (initial loading state)", () => {
    mockUseJobStream.mockReturnValue({
      data: null,
      error: null,
      streaming: false,
    });
    const { container } = render(<TemplateJobPage />);
    // Should render ProgressTheater in loading state, not crash.
    expect(container.querySelector("div.space-y-8")).toBeInTheDocument();
    expect(screen.queryByText(/something went wrong/i)).toBeNull();
  });

  it("renders with null expected_phase_durations (deploy-skew — no crash)", () => {
    mockUseJobStream.mockReturnValue({
      data: makeJob({ expected_phase_durations: null }),
      error: null,
      streaming: false,
    });
    const { container } = render(<TemplateJobPage />);
    // Should not throw or show a crash banner.
    expect(screen.queryByText(/something went wrong/i)).toBeNull();
    expect(container.querySelector("div.space-y-8")).toBeInTheDocument();
  });

  it("shows progress theater when status is queued (D9)", () => {
    mockUseJobStream.mockReturnValue({
      data: makeJob({ status: "queued", current_phase: null, started_at: null }),
      error: null,
      streaming: false,
    });
    const { container } = render(<TemplateJobPage />);
    // Theater renders; no crash.
    expect(container.querySelector("div.space-y-8")).toBeInTheDocument();
    expect(screen.queryByText(/exception/i)).toBeNull();
  });

  it("renders ErrorScreen on stream error", () => {
    mockUseJobStream.mockReturnValue({
      data: null,
      error: "Connection failed",
      streaming: false,
    });
    render(<TemplateJobPage />);
    expect(screen.getByText("Connection failed")).toBeInTheDocument();
  });

  it("renders ErrorScreen on processing_failed status", () => {
    mockUseJobStream.mockReturnValue({
      data: makeJob({
        status: "processing_failed",
        failure_reason: "timeout",
        error_detail: null,
      }),
      error: null,
      streaming: false,
    });
    render(<TemplateJobPage />);
    // FAILURE_MESSAGES.timeout copy
    expect(
      screen.getByText(/processing took too long/i),
    ).toBeInTheDocument();
  });

  it("renders ErrorScreen on cancelled status", () => {
    mockUseJobStream.mockReturnValue({
      data: makeJob({ status: "cancelled" }),
      error: null,
      streaming: false,
    });
    render(<TemplateJobPage />);
    expect(screen.getByText(/cancelled by an administrator/i)).toBeInTheDocument();
  });

  it("old PhaseChips flex-wrap is gone (regression invariant)", () => {
    mockUseJobStream.mockReturnValue({
      data: makeJob(),
      error: null,
      streaming: true,
    });
    const { container } = render(<TemplateJobPage />);
    // The old PhaseChips used a div.flex-wrap for the chip container.
    // ProgressTheater does NOT use flex-wrap on the chip row.
    // This assertion guards against accidentally re-adding the old component.
    const flexWrapDivs = container.querySelectorAll("div.flex-wrap");
    expect(flexWrapDivs.length).toBe(0);
  });
});
