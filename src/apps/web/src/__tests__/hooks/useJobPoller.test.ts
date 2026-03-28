import { act, renderHook, waitFor } from "@testing-library/react";
import { useJobPoller } from "@/hooks/useJobPoller";

describe("useJobPoller", () => {
  it("starts with null data and no error", () => {
    const { result } = renderHook(() =>
      useJobPoller<{ status: string }>(null, {
        fetchStatus: jest.fn(),
        isTerminal: () => false,
      }),
    );

    expect(result.current.data).toBeNull();
    expect(result.current.error).toBeNull();
    expect(result.current.polling).toBe(false);
  });

  it("starts polling when jobId is provided", async () => {
    const mockFetch = jest.fn().mockResolvedValue({ status: "queued" });

    const { result } = renderHook(() =>
      useJobPoller("job-123", {
        fetchStatus: mockFetch,
        isTerminal: (d) => d.status === "done",
        intervalMs: 60000, // long interval so only the initial poll fires
      }),
    );

    await waitFor(() => {
      expect(result.current.data).toEqual({ status: "queued" });
    });

    expect(mockFetch).toHaveBeenCalledWith("job-123");
    expect(result.current.polling).toBe(true);
  });

  it("stops polling on terminal state", async () => {
    const mockFetch = jest.fn().mockResolvedValue({ status: "done" });

    const { result } = renderHook(() =>
      useJobPoller("job-456", {
        fetchStatus: mockFetch,
        isTerminal: (d) => d.status === "done",
        intervalMs: 60000,
      }),
    );

    await waitFor(() => {
      expect(result.current.data?.status).toBe("done");
    });

    expect(result.current.polling).toBe(false);
  });

  it("sets error on fetch failure", async () => {
    const mockFetch = jest.fn().mockRejectedValue(new Error("Network error"));

    const { result } = renderHook(() =>
      useJobPoller("job-err", {
        fetchStatus: mockFetch,
        isTerminal: () => false,
        intervalMs: 60000,
      }),
    );

    await waitFor(() => {
      expect(result.current.error).toBe("Network error");
    });

    expect(result.current.polling).toBe(false);
  });

  it("restart begins polling with a new job ID", async () => {
    const mockFetch = jest.fn().mockResolvedValue({ status: "queued" });

    const { result } = renderHook(() =>
      useJobPoller<{ status: string }>(null, {
        fetchStatus: mockFetch,
        isTerminal: () => false,
        intervalMs: 60000,
      }),
    );

    expect(result.current.polling).toBe(false);

    act(() => {
      result.current.restart("new-job-789");
    });

    await waitFor(() => {
      expect(mockFetch).toHaveBeenCalledWith("new-job-789");
    });

    expect(result.current.polling).toBe(true);
  });
});
