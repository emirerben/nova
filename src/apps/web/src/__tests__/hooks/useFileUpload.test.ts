import { act, renderHook } from "@testing-library/react";
import { useFileUpload } from "@/hooks/useFileUpload";

// Mock crypto.randomUUID
Object.defineProperty(globalThis, "crypto", {
  value: { randomUUID: () => "test-uuid-1234" },
});

describe("useFileUpload", () => {
  const mockGetPresigned = jest.fn().mockResolvedValue({
    upload_url: "https://storage.example.com/signed",
    gcs_path: "templates/abc/file.mp4",
  });

  beforeEach(() => {
    jest.clearAllMocks();
  });

  it("starts with empty state", () => {
    const { result } = renderHook(() =>
      useFileUpload({ getPresignedUrl: mockGetPresigned }),
    );

    expect(result.current.files).toEqual([]);
    expect(result.current.uploading).toBe(false);
    expect(result.current.successfulPaths).toEqual([]);
  });

  it("addFiles creates entries with initial state", () => {
    const { result } = renderHook(() =>
      useFileUpload({ getPresignedUrl: mockGetPresigned }),
    );

    const file = new File(["video"], "test.mp4", { type: "video/mp4" });

    act(() => {
      result.current.addFiles([file]);
    });

    expect(result.current.files).toHaveLength(1);
    expect(result.current.files[0].file).toBe(file);
    expect(result.current.files[0].progress).toBe(0);
    expect(result.current.files[0].error).toBeNull();
    expect(result.current.files[0].gcsPath).toBeNull();
  });

  it("removeFile removes a file by id", () => {
    const { result } = renderHook(() =>
      useFileUpload({ getPresignedUrl: mockGetPresigned }),
    );

    const file = new File(["video"], "test.mp4", { type: "video/mp4" });

    act(() => {
      result.current.addFiles([file]);
    });

    const fileId = result.current.files[0].id;

    act(() => {
      result.current.removeFile(fileId);
    });

    expect(result.current.files).toHaveLength(0);
  });

  it("clearFiles removes all files", () => {
    const { result } = renderHook(() =>
      useFileUpload({ getPresignedUrl: mockGetPresigned }),
    );

    act(() => {
      result.current.addFiles([
        new File(["a"], "a.mp4", { type: "video/mp4" }),
        new File(["b"], "b.mp4", { type: "video/mp4" }),
      ]);
    });

    expect(result.current.files).toHaveLength(2);

    act(() => {
      result.current.clearFiles();
    });

    expect(result.current.files).toHaveLength(0);
  });
});
