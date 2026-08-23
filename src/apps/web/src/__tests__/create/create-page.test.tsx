// @ts-nocheck
process.env.NEXT_PUBLIC_CREATION_HUB_ENABLED = "true";

import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom";

const mockReplace = jest.fn();
jest.mock("next/navigation", () => ({
  useRouter: () => ({ replace: mockReplace }),
}));

jest.mock("next-auth/react", () => ({
  useSession: () => ({ status: "authenticated", data: { user: { id: "user-1" } } }),
}));

jest.mock("@/app/generative/VoiceRecorder", () => ({
  VoiceRecorder: () => <div data-testid="voice-recorder" />,
}));

jest.mock("@/components/progress", () => ({
  ProgressTheater: ({ children }) => <div data-testid="progress-theater">{children}</div>,
}));

jest.mock("@/components/progress/logic", () => ({
  deriveReceiptText: () => "Ready",
}));

const mockRefetch = jest.fn();
const mockUsePolledJobStatus = jest.fn();
jest.mock("@/hooks/usePolledJobStatus", () => ({
  usePolledJobStatus: (...args) => mockUsePolledJobStatus(...args),
}));

jest.mock("@/lib/generative-api", () => ({
  ...jest.requireActual("@/lib/generative-api"),
  uploadOwnedGenerativeClip: jest.fn(),
  uploadOwnedVoiceover: jest.fn(),
  createOwnedGenerativeJob: jest.fn(),
  getOwnedGenerativeJobStatus: jest.fn(),
  openGenerativeJobInEditor: jest.fn(),
  retryOwnedGenerativeJob: jest.fn(),
}));

import CreatePage from "@/app/create/page";
import {
  createOwnedGenerativeJob,
  openGenerativeJobInEditor,
  retryOwnedGenerativeJob,
  uploadOwnedGenerativeClip,
} from "@/lib/generative-api";

function makeStatus(overrides = {}) {
  return {
    job_id: "job-1",
    status: "processing",
    variants: [],
    error_detail: null,
    created_at: "2026-08-21T10:00:00Z",
    updated_at: "2026-08-21T10:00:01Z",
    ...overrides,
  };
}

describe("authenticated create flow", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    window.sessionStorage.clear();
    window.history.replaceState(null, "", "/create");
    mockUsePolledJobStatus.mockReturnValue({
      data: null,
      error: null,
      refetch: mockRefetch,
      applyData: jest.fn(),
    });
    retryOwnedGenerativeJob.mockResolvedValue({ job_id: "job-failed", status: "queued" });
  });

  it("uploads footage first and preserves authored order and direction in the owned job", async () => {
    uploadOwnedGenerativeClip
      .mockResolvedValueOnce({ gcs_path: "users/u/first.mp4", kind: "video" })
      .mockResolvedValueOnce({ gcs_path: "users/u/second.jpg", kind: "image" });
    createOwnedGenerativeJob.mockResolvedValue({ job_id: "job-1", status: "queued" });

    const view = render(<CreatePage />);
    const input = view.container.querySelector('input[aria-label="Upload footage"]');
    const files = [
      new File(["a"], "first.mp4", { type: "video/mp4" }),
      new File(["b"], "second.jpg", { type: "image/jpeg" }),
    ];

    await act(async () => {
      Object.defineProperty(input, "files", { value: files, configurable: true });
      fireEvent.change(input);
    });
    await screen.findByText("first.mp4");
    fireEvent.change(screen.getByRole("textbox", { name: /tell kria what to emphasize/i }), {
      target: { value: "Lead with the reaction and keep it warm." },
    });
    const generate = screen.getByRole("button", { name: /create video/i });
    fireEvent.click(generate);
    fireEvent.click(generate);

    await waitFor(() =>
      expect(createOwnedGenerativeJob).toHaveBeenCalledWith(
        ["users/u/first.mp4", "users/u/second.jpg"],
        null,
        { intent: "Lead with the reaction and keep it warm." },
      ),
    );
    expect(await screen.findByTestId("progress-theater")).toBeInTheDocument();
    expect(createOwnedGenerativeJob).toHaveBeenCalledTimes(1);
  });

  it("preserves selected footage and direction when job submission fails, then retries", async () => {
    uploadOwnedGenerativeClip.mockResolvedValue({
      gcs_path: "users/u/kept-after-submit-error.mp4",
      kind: "video",
    });
    createOwnedGenerativeJob
      .mockRejectedValueOnce(new Error("The video service is unavailable"))
      .mockResolvedValueOnce({ job_id: "job-recovered", status: "queued" });

    const view = render(<CreatePage />);
    const input = view.container.querySelector('input[aria-label="Upload footage"]');
    await act(async () => {
      Object.defineProperty(input, "files", {
        value: [new File(["a"], "kept.mp4", { type: "video/mp4" })],
        configurable: true,
      });
      fireEvent.change(input);
    });
    await screen.findByText("kept.mp4");
    const direction = screen.getByRole("textbox", { name: /tell kria what to emphasize/i });
    fireEvent.change(direction, { target: { value: "Keep this exact direction" } });

    fireEvent.click(screen.getByRole("button", { name: /create video/i }));

    expect(
      await screen.findByText("We couldn’t create this video. Check your connection and try again."),
    ).toBeInTheDocument();
    expect(screen.getByText("kept.mp4")).toBeInTheDocument();
    expect(direction).toHaveValue("Keep this exact direction");
    fireEvent.click(screen.getByRole("button", { name: /create video/i }));

    await screen.findByTestId("progress-theater");
    expect(createOwnedGenerativeJob).toHaveBeenCalledTimes(2);
    expect(createOwnedGenerativeJob).toHaveBeenLastCalledWith(
      ["users/u/kept-after-submit-error.mp4"],
      null,
      { intent: "Keep this exact direction" },
    );
  });

  it("promotes the first ready job once and replaces the route with EditorShell", async () => {
    window.history.replaceState(null, "", "/create?job=job-ready");
    mockUsePolledJobStatus.mockReturnValue({
      data: makeStatus({
        job_id: "job-ready",
        status: "variants_ready_partial",
        finished_at: "2026-08-21T10:02:00Z",
        variants: [
          {
            variant_id: "variant-low",
            rank: 0,
            render_status: "ready",
            output_url: "https://storage/video.mp4",
          },
        ],
      }),
      error: null,
      refetch: mockRefetch,
      applyData: jest.fn(),
    });
    openGenerativeJobInEditor.mockResolvedValue({
      plan_item_id: "item-1",
      variant_id: "variant-low",
    });

    render(<CreatePage />);

    await waitFor(() =>
      expect(openGenerativeJobInEditor).toHaveBeenCalledTimes(1),
    );
    expect(openGenerativeJobInEditor).toHaveBeenCalledWith("job-ready", undefined);
    await waitFor(() =>
      expect(mockReplace).toHaveBeenCalledWith(
        "/plan/items/item-1/edit?variant=variant-low",
      ),
    );
  });

  it("recovers from a promotion error and ignores duplicate retry clicks", async () => {
    window.history.replaceState(null, "", "/create?job=job-ready");
    mockUsePolledJobStatus.mockReturnValue({
      data: makeStatus({
        job_id: "job-ready",
        status: "variants_ready_partial",
        finished_at: "2026-08-21T10:02:00Z",
        variants: [
          {
            variant_id: "variant-low",
            rank: 0,
            render_status: "ready",
            output_url: "https://storage/video.mp4",
          },
        ],
      }),
      error: null,
      refetch: mockRefetch,
      applyData: jest.fn(),
    });
    openGenerativeJobInEditor
      .mockRejectedValueOnce(new Error("The editor could not open"))
      .mockResolvedValueOnce({ plan_item_id: "item-1", variant_id: "variant-low" });

    render(<CreatePage />);

    const retry = await screen.findByRole("button", { name: /open editor again/i });
    expect(openGenerativeJobInEditor).toHaveBeenCalledTimes(1);
    fireEvent.click(retry);
    fireEvent.click(retry);

    await waitFor(() =>
      expect(mockReplace).toHaveBeenCalledWith(
        "/plan/items/item-1/edit?variant=variant-low",
      ),
    );
    expect(openGenerativeJobInEditor).toHaveBeenCalledTimes(2);
  });

  it("opens the editor as soon as one variant is ready while a sibling is still rendering", async () => {
    window.history.replaceState(null, "", "/create?job=job-processing");
    mockUsePolledJobStatus.mockReturnValue({
      data: makeStatus({
        job_id: "job-processing",
        status: "processing",
        variants: [
          {
            variant_id: "early-ready",
            rank: 0,
            render_status: "ready",
            output_url: "https://storage/video.mp4",
          },
          {
            variant_id: "still-rendering",
            rank: 1,
            render_status: "rendering",
            output_url: null,
          },
        ],
      }),
      error: null,
      refetch: mockRefetch,
      applyData: jest.fn(),
    });
    openGenerativeJobInEditor.mockResolvedValue({
      plan_item_id: "item-processing",
      variant_id: "early-ready",
    });

    render(<CreatePage />);

    await waitFor(() =>
      expect(openGenerativeJobInEditor).toHaveBeenCalledWith("job-processing", undefined),
    );
    expect(openGenerativeJobInEditor).toHaveBeenCalledTimes(1);
    await waitFor(() =>
      expect(mockReplace).toHaveBeenCalledWith(
        "/plan/items/item-processing/edit?variant=early-ready",
      ),
    );
  });

  it("uses the lowest-rank failed variant taxonomy when the job has no failure reason", async () => {
    window.history.replaceState(null, "", "/create?job=job-invalid");
    mockUsePolledJobStatus.mockReturnValue({
      data: makeStatus({
        job_id: "job-invalid",
        status: "variants_failed",
        failure_reason: null,
        variants: [
          { variant_id: "later", rank: 2, render_status: "failed", error_class: "timeout" },
          { variant_id: "first", rank: 1, render_status: "failed", error_class: "clip_read_error" },
        ],
      }),
      error: null,
      refetch: mockRefetch,
      applyData: jest.fn(),
    });

    render(<CreatePage />);

    expect(await screen.findByText("Review your footage")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /try render again/i })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /review my setup/i })).toBeInTheDocument();
  });

  it("retries a failed render without clearing uploaded media or direction", async () => {
    uploadOwnedGenerativeClip.mockResolvedValue({
      gcs_path: "users/u/kept.mp4",
      kind: "video",
    });
    createOwnedGenerativeJob.mockResolvedValueOnce({ job_id: "job-failed", status: "queued" });
    mockUsePolledJobStatus.mockReturnValue({
      data: makeStatus({ job_id: "job-failed", status: "processing_failed" }),
      error: null,
      refetch: mockRefetch,
      applyData: jest.fn(),
    });

    const view = render(<CreatePage />);
    const input = view.container.querySelector('input[aria-label="Upload footage"]');
    await act(async () => {
      Object.defineProperty(input, "files", {
        value: [new File(["a"], "kept.mp4", { type: "video/mp4" })],
        configurable: true,
      });
      fireEvent.change(input);
    });
    fireEvent.change(screen.getByRole("textbox", { name: /tell kria what to emphasize/i }), {
      target: { value: "Keep this direction" },
    });
    fireEvent.click(screen.getByRole("button", { name: /create video/i }));
    expect(await screen.findByRole("button", { name: /retry render/i })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /retry render/i }));

    await waitFor(() => expect(retryOwnedGenerativeJob).toHaveBeenCalledWith("job-failed"));
    expect(createOwnedGenerativeJob).toHaveBeenCalledTimes(1);
  });

  it("hydrates uploaded paths and direction after a refresh so a failed job can retry", async () => {
    uploadOwnedGenerativeClip.mockResolvedValue({
      gcs_path: "users/u/persisted.mp4",
      kind: "video",
    });
    createOwnedGenerativeJob.mockResolvedValueOnce({ job_id: "job-persisted", status: "queued" });
    retryOwnedGenerativeJob.mockResolvedValueOnce({ job_id: "job-persisted", status: "queued" });
    mockUsePolledJobStatus.mockReturnValue({
      data: makeStatus({ job_id: "job-persisted", status: "processing_failed" }),
      error: null,
      refetch: mockRefetch,
      applyData: jest.fn(),
    });

    const firstView = render(<CreatePage />);
    const input = firstView.container.querySelector('input[aria-label="Upload footage"]');
    await act(async () => {
      Object.defineProperty(input, "files", {
        value: [new File(["a"], "persisted.mp4", { type: "video/mp4" })],
        configurable: true,
      });
      fireEvent.change(input);
    });
    fireEvent.change(screen.getByRole("textbox", { name: /tell kria what to emphasize/i }), {
      target: { value: "Keep this after refresh" },
    });
    fireEvent.click(screen.getByRole("button", { name: /create video/i }));
    await screen.findByRole("button", { name: /retry render/i });
    await waitFor(() =>
      expect(window.sessionStorage.getItem("kria:create-draft:v1:user-1")).toContain(
        "job-persisted",
      ),
    );

    firstView.unmount();
    const refreshedView = render(<CreatePage />);
    expect(await screen.findByRole("button", { name: /retry render/i })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /retry render/i }));

    await waitFor(() => expect(retryOwnedGenerativeJob).toHaveBeenCalledWith("job-persisted"));
    expect(createOwnedGenerativeJob).toHaveBeenCalledTimes(1);
    refreshedView.unmount();
  });

  it("keeps the create action sticky at desktop breakpoints used by 200% zoom", () => {
    render(<CreatePage />);

    const chrome = screen.getByRole("button", { name: /create video/i }).parentElement;
    expect(chrome?.className).toContain("sticky");
    expect(chrome?.className).not.toContain("sm:static");
  });
});
