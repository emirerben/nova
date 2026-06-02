// @ts-nocheck
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom";

import AdminReviewPage from "@/app/admin/review/page";
import { adminLabelReview, adminListReview } from "@/lib/admin-review-api";

jest.mock("@/lib/admin-review-api", () => ({
  adminListReview: jest.fn(),
  adminLabelReview: jest.fn(),
}));

const mockList = adminListReview as jest.MockedFunction<typeof adminListReview>;
const mockLabel = adminLabelReview as jest.MockedFunction<typeof adminLabelReview>;

const ESCALATION = {
  run_id: "run-1111",
  job_id: "abcdef12-0000-4000-8000-000000000000",
  band: "escalate",
  avg: 3.2,
  confidence: 0.55,
  risk_tag: "borderline",
  reasoning: "Hook is slow to land in the first 3 seconds.",
  summary_line: "[escalate] avg=3.20",
  scores: { hook: 3.0, legibility: 4.0, filmed_not_templated: 2.5 },
  thumbnail_url: "https://signed/thumb.jpg",
  video_url: "https://signed/final.mp4",
  created_at: "2026-06-02T12:00:00Z",
  labeled: false,
};

describe("AdminReviewPage", () => {
  beforeEach(() => {
    jest.clearAllMocks();
  });

  afterEach(() => {
    jest.useRealTimers();
  });

  it("renders escalations with rationale, scores, and risk tag", async () => {
    mockList.mockResolvedValue({ items: [ESCALATION], total: 1 });

    render(<AdminReviewPage />);

    // rationale text
    expect(
      await screen.findByText("Hook is slow to land in the first 3 seconds."),
    ).toBeInTheDocument();
    // per-dimension scores rendered as labelled bars
    expect(screen.getByText("hook")).toBeInTheDocument();
    expect(screen.getByText("legibility")).toBeInTheDocument();
    expect(screen.getByText("filmed_not_templated")).toBeInTheDocument();
    // risk tag
    expect(screen.getByText("borderline")).toBeInTheDocument();
    // avg / confidence summary
    expect(screen.getByText(/avg 3\.20/)).toBeInTheDocument();
    // count header
    expect(screen.getByText(/1 video the grader escalated/)).toBeInTheDocument();
  });

  it("shows the empty state when nothing is escalated", async () => {
    mockList.mockResolvedValue({ items: [], total: 0 });

    render(<AdminReviewPage />);

    expect(
      await screen.findByText(/Nothing to review/),
    ).toBeInTheDocument();
  });

  it("surfaces a fetch error", async () => {
    mockList.mockRejectedValue(new Error("Backend unavailable"));

    render(<AdminReviewPage />);

    expect(await screen.findByText("Backend unavailable")).toBeInTheDocument();
  });

  it("writes a calibration label on 👍 and shows confirmation", async () => {
    mockList.mockResolvedValue({ items: [ESCALATION], total: 1 });
    mockLabel.mockResolvedValue({
      run_id: "run-1111",
      job_id: ESCALATION.job_id,
      verdict: "auto_pass",
      ok: true,
    });

    render(<AdminReviewPage />);

    const pass = await screen.findByText(/Looks good/);
    await act(async () => {
      fireEvent.click(pass);
    });

    await waitFor(() => {
      expect(mockLabel).toHaveBeenCalledWith("run-1111", "auto_pass");
    });
    expect(await screen.findByText(/Calibration recorded/)).toBeInTheDocument();
  });

  it("writes auto_reject on 👎", async () => {
    mockList.mockResolvedValue({ items: [ESCALATION], total: 1 });
    mockLabel.mockResolvedValue({
      run_id: "run-1111",
      job_id: ESCALATION.job_id,
      verdict: "auto_reject",
      ok: true,
    });

    render(<AdminReviewPage />);

    const reject = await screen.findByText(/Reject/);
    await act(async () => {
      fireEvent.click(reject);
    });

    await waitFor(() => {
      expect(mockLabel).toHaveBeenCalledWith("run-1111", "auto_reject");
    });
  });

  it("renders an already-labeled item as recorded (no buttons)", async () => {
    mockList.mockResolvedValue({
      items: [{ ...ESCALATION, labeled: true }],
      total: 1,
    });

    render(<AdminReviewPage />);

    expect(await screen.findByText(/Calibration recorded/)).toBeInTheDocument();
    expect(screen.queryByText(/Looks good/)).not.toBeInTheDocument();
  });
});
