// @ts-nocheck
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom";

const mockReplace = jest.fn();
const mockRouter = { replace: mockReplace };
jest.mock("next/navigation", () => ({
  useRouter: () => mockRouter,
}));

jest.mock("next-auth/react", () => ({
  useSession: () => ({ status: "authenticated", data: { user: { id: "user-1" } } }),
}));

jest.mock("@/lib/plan-api", () => ({
  attachClips: jest.fn(),
  createOrResumeManualDraft: jest.fn(),
  getPlanItemFresh: jest.fn(),
  initializeManualDraft: jest.fn(),
  requestUploadUrls: jest.fn(),
  uploadContentTypeForFile: jest.fn((file: File) => file.type),
  uploadToGcs: jest.fn(),
}));

import ManualCreatePage from "@/app/create/manual/page";
import {
  attachClips,
  createOrResumeManualDraft,
  getPlanItemFresh,
  initializeManualDraft,
  requestUploadUrls,
  uploadToGcs,
} from "@/lib/plan-api";

const emptyDraft = {
  plan_item_id: "item-1",
  job_id: "job-1",
  variant_id: null,
  status: "draft",
};

describe("hidden manual create flow", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    process.env.NEXT_PUBLIC_MANUAL_EDITOR_ENABLED = "true";
    createOrResumeManualDraft.mockResolvedValue(emptyDraft);
    getPlanItemFresh.mockResolvedValue({ clip_gcs_paths: [] });
    requestUploadUrls.mockResolvedValue([]);
    uploadToGcs.mockResolvedValue(undefined);
    attachClips.mockResolvedValue({});
    initializeManualDraft.mockResolvedValue({
      ...emptyDraft,
      variant_id: "original_text",
    });
  });

  afterAll(() => {
    delete process.env.NEXT_PUBLIC_MANUAL_EDITOR_ENABLED;
  });

  it("fails closed unless the manual-editor flag is exactly true", async () => {
    process.env.NEXT_PUBLIC_MANUAL_EDITOR_ENABLED = "1";

    render(<ManualCreatePage />);

    await waitFor(() => expect(mockReplace).toHaveBeenCalledWith("/plan"));
    expect(createOrResumeManualDraft).not.toHaveBeenCalled();
    expect(screen.queryByRole("heading", { name: /build from your footage/i })).not.toBeInTheDocument();
  });

  it("resumes an initialized draft directly in the canonical editor", async () => {
    createOrResumeManualDraft.mockResolvedValue({
      ...emptyDraft,
      variant_id: "original_text",
    });

    render(<ManualCreatePage />);

    await waitFor(() =>
      expect(mockReplace).toHaveBeenCalledWith(
        "/plan/items/item-1/edit?variant=original_text",
      ),
    );
    expect(getPlanItemFresh).not.toHaveBeenCalled();
  });

  it("hydrates attached media after refresh and preserves its stored order", async () => {
    getPlanItemFresh.mockResolvedValue({
      clip_gcs_paths: [
        "users/u/plan/item-1/one.mp4",
        "users/u/plan/item-1/two.mp4",
      ],
    });

    render(<ManualCreatePage />);

    expect(await screen.findByText("one.mp4")).toBeInTheDocument();
    expect(screen.getByText("two.mp4")).toBeInTheDocument();
    const continueButton = screen.getByRole("button", { name: /export video/i });
    expect(continueButton).toHaveClass("min-h-[48px]");
    expect(continueButton.parentElement?.className).toContain("sticky");
    expect(continueButton.parentElement?.className).not.toContain("sm:static");
    fireEvent.click(continueButton);

    await waitFor(() =>
      expect(attachClips).toHaveBeenCalledWith("item-1", [
        "users/u/plan/item-1/one.mp4",
        "users/u/plan/item-1/two.mp4",
      ]),
    );
    expect(initializeManualDraft).toHaveBeenCalledWith("item-1", [
      {
        gcs_path: "users/u/plan/item-1/one.mp4",
        duration_s: 5,
        kind: "video",
      },
      {
        gcs_path: "users/u/plan/item-1/two.mp4",
        duration_s: 5,
        kind: "video",
      },
    ]);
    await waitFor(() =>
      expect(mockReplace).toHaveBeenCalledWith(
        "/plan/items/item-1/edit?variant=original_text",
      ),
    );
  });

  it("uploads videos through signed URLs and attaches authored order", async () => {
    requestUploadUrls.mockResolvedValue([
      { upload_url: "https://upload/first", gcs_path: "users/u/plan/item-1/first.mp4" },
      { upload_url: "https://upload/second", gcs_path: "users/u/plan/item-1/second.mp4" },
    ]);
    const view = render(<ManualCreatePage />);
    await screen.findByRole("heading", { name: /arrange your footage/i });
    const input = view.container.querySelector('input[aria-label="Choose videos to add"]');
    const files = [
      new File(["video"], "first.mp4", { type: "video/mp4" }),
      new File(["video"], "second.mp4", { type: "video/mp4" }),
    ];

    await act(async () => {
      Object.defineProperty(input, "files", { value: files, configurable: true });
      fireEvent.change(input);
    });

    expect(await screen.findByText("first.mp4")).toBeInTheDocument();
    expect(screen.getByText("second.mp4")).toBeInTheDocument();
    expect(requestUploadUrls).toHaveBeenCalledWith("item-1", [
      { filename: "first.mp4", content_type: "video/mp4", file_size_bytes: 5 },
      { filename: "second.mp4", content_type: "video/mp4", file_size_bytes: 5 },
    ]);
    expect(uploadToGcs).toHaveBeenNthCalledWith(1, "https://upload/first", files[0]);
    expect(uploadToGcs).toHaveBeenNthCalledWith(2, "https://upload/second", files[1]);
    expect(attachClips).toHaveBeenCalledWith("item-1", [
      "users/u/plan/item-1/first.mp4",
      "users/u/plan/item-1/second.mp4",
    ]);
    expect(screen.getByRole("button", { name: "Remove first.mp4" })).toHaveClass(
      "min-h-[44px]",
      "min-w-[44px]",
    );
  });

  it("rejects photos before requesting upload URLs", async () => {
    const view = render(<ManualCreatePage />);
    await screen.findByRole("heading", { name: /arrange your footage/i });
    const input = view.container.querySelector('input[aria-label="Choose videos to add"]');
    const photo = new File(["photo"], "still.jpg", { type: "image/jpeg" });

    await act(async () => {
      Object.defineProperty(input, "files", { value: [photo], configurable: true });
      fireEvent.change(input);
    });

    expect(screen.getByRole("alert")).toHaveTextContent(/photo timelines.*available/i);
    expect(requestUploadUrls).not.toHaveBeenCalled();
    expect(uploadToGcs).not.toHaveBeenCalled();
  });

  it("retries an interrupted order save without uploading successful media again", async () => {
    requestUploadUrls.mockResolvedValue([
      { upload_url: "https://upload/video", gcs_path: "users/u/plan/item-1/video.mp4" },
    ]);
    attachClips
      .mockRejectedValueOnce(new Error("Connection interrupted"))
      .mockResolvedValue({});
    const view = render(<ManualCreatePage />);
    await screen.findByRole("heading", { name: /arrange your footage/i });
    const input = view.container.querySelector('input[aria-label="Choose videos to add"]');
    const file = new File(["video"], "video.mp4", { type: "video/mp4" });

    await act(async () => {
      Object.defineProperty(input, "files", { value: [file], configurable: true });
      fireEvent.change(input);
    });

    expect(await screen.findByText("video.mp4")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent(/files uploaded.*save their order/i);
    fireEvent.click(screen.getByRole("button", { name: /export video/i }));

    await waitFor(() => expect(initializeManualDraft).toHaveBeenCalledTimes(1));
    expect(uploadToGcs).toHaveBeenCalledTimes(1);
    expect(attachClips).toHaveBeenCalledTimes(2);
  });
});
