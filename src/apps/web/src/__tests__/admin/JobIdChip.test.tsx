// @ts-nocheck
import { act, fireEvent, render, screen } from "@testing-library/react";
import "@testing-library/jest-dom";

import { JobIdChip } from "@/app/admin/_shared/JobIdChip";

const JOB_ID = "12345678-90ab-cdef-1234-567890abcdef";

describe("JobIdChip", () => {
  const writeText = jest.fn();

  beforeEach(() => {
    jest.useFakeTimers();
    writeText.mockResolvedValue(undefined);
    Object.defineProperty(window.navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });
  });

  afterEach(() => {
    jest.runOnlyPendingTimers();
    jest.useRealTimers();
    jest.clearAllMocks();
  });

  it("renders the 8-character prefix by default and full UUID in title", () => {
    render(<JobIdChip jobId={JOB_ID} />);

    expect(screen.getByText("12345678")).toBeInTheDocument();
    expect(screen.getByText("12345678")).toHaveAttribute("title", JOB_ID);
  });

  it("copies the full UUID and clears the copied state", async () => {
    render(<JobIdChip jobId={JOB_ID} />);

    fireEvent.click(screen.getByLabelText("Copy job ID"));

    expect(writeText).toHaveBeenCalledWith(JOB_ID);
    expect(await screen.findByText("Copied")).toBeInTheDocument();

    act(() => {
      jest.advanceTimersByTime(1500);
    });

    expect(screen.queryByText("Copied")).not.toBeInTheDocument();
  });

  it("has an accessible copy button label", () => {
    render(<JobIdChip jobId={JOB_ID} />);

    expect(screen.getByLabelText("Copy job ID")).toBeInTheDocument();
  });
});
