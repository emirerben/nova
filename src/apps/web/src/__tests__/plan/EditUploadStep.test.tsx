import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { EditUploadStep } from "@/app/plan/_components/onboarding/EditUploadStep";
import { uploadGenerativeClip } from "@/lib/generative-api";

jest.mock("@/lib/generative-api", () => ({
  uploadGenerativeClip: jest.fn(),
}));

const mockUpload = uploadGenerativeClip as jest.MockedFunction<typeof uploadGenerativeClip>;

describe("EditUploadStep", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    Object.defineProperty(URL, "createObjectURL", {
      configurable: true,
      value: jest.fn(() => "blob:clip"),
    });
  });

  afterEach(() => {
    delete (URL as { createObjectURL?: typeof URL.createObjectURL }).createObjectURL;
  });

  it("keeps a failed clip and retries the same file", async () => {
    mockUpload
      .mockRejectedValueOnce(new Error("backend detail"))
      .mockResolvedValueOnce({ gcs_path: "users/u/clip.mp4", kind: "video" });

    render(<EditUploadStep onSubmit={jest.fn()} />);
    const file = new File(["video"], "walkthrough.mp4", { type: "video/mp4" });

    await act(async () => {
      fireEvent.change(screen.getByLabelText("Add video clips"), {
        target: { files: [file] },
      });
    });

    expect(
      await screen.findByText("walkthrough.mp4 couldn't upload. Use an MP4 or MOV video."),
    ).not.toBeNull();

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Retry upload" }));
    });

    await waitFor(() => expect(mockUpload).toHaveBeenCalledTimes(2));
    expect(mockUpload).toHaveBeenLastCalledWith(file);
  });
});
