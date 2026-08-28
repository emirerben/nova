import { fireEvent, render } from "@testing-library/react";
import "@testing-library/jest-dom";
import { StablePoster } from "@/components/StablePoster";

describe("StablePoster", () => {
  it("holds the initial signed URL while the poster signature refreshes", () => {
    const { container, rerender } = render(
      <StablePoster
        src="https://storage.example.com/poster.jpg?sig=A"
        identity="jobs/job-1/output.mp4"
        alt="poster"
      />,
    );
    const image = container.querySelector("img")!;
    expect(image).toHaveAttribute("src", expect.stringContaining("sig=A"));

    rerender(
      <StablePoster
        src="https://storage.example.com/poster.jpg?sig=B"
        identity="jobs/job-1/output.mp4"
        alt="poster"
      />,
    );
    expect(image).toHaveAttribute("src", expect.stringContaining("sig=A"));
  });

  it("does not report a recoverable stale signature unless the refreshed URL also fails", () => {
    const onError = jest.fn();
    const { container, rerender } = render(
      <StablePoster
        src="https://storage.example.com/poster.jpg?sig=A"
        identity="jobs/job-1/output.mp4"
        alt="poster"
        fallback={<span>poster unavailable</span>}
        onError={onError}
      />,
    );

    rerender(
      <StablePoster
        src="https://storage.example.com/poster.jpg?sig=B"
        identity="jobs/job-1/output.mp4"
        alt="poster"
        fallback={<span>poster unavailable</span>}
        onError={onError}
      />,
    );
    fireEvent.error(container.querySelector("img")!);

    expect(onError).not.toHaveBeenCalled();
    expect(container.querySelector("img")).toHaveAttribute(
      "src",
      expect.stringContaining("sig=B"),
    );

    fireEvent.error(container.querySelector("img")!);
    expect(onError).toHaveBeenCalledTimes(1);
  });

  it("shows the fallback after the current poster fails", () => {
    const { container, getByText } = render(
      <StablePoster
        src="https://storage.example.com/poster.jpg?sig=A"
        alt="poster"
        fallback={<span>poster unavailable</span>}
      />,
    );
    fireEvent.error(container.querySelector("img")!);
    expect(getByText("poster unavailable")).toBeInTheDocument();
  });

  it("retries a refreshed signed URL after an image error", () => {
    const { container, rerender } = render(
      <StablePoster
        src="https://storage.example.com/poster.jpg?sig=A"
        identity="jobs/job-1/output.mp4"
        alt="poster"
        fallback={<span>poster unavailable</span>}
      />,
    );

    fireEvent.error(container.querySelector("img")!);
    expect(container.querySelector("img")).toBeNull();

    rerender(
      <StablePoster
        src="https://storage.example.com/poster.jpg?sig=B"
        identity="jobs/job-1/output.mp4"
        alt="poster"
        fallback={<span>poster unavailable</span>}
      />,
    );

    expect(container.querySelector("img")).toHaveAttribute("src", expect.stringContaining("sig=B"));
  });

  it("does not retry every signature refresh after the retry also fails", () => {
    const { container, rerender, getByText } = render(
      <StablePoster
        src="https://storage.example.com/poster.jpg?sig=A"
        identity="jobs/job-1/output.mp4"
        alt="poster"
        fallback={<span>poster unavailable</span>}
      />,
    );

    fireEvent.error(container.querySelector("img")!);
    rerender(
      <StablePoster
        src="https://storage.example.com/poster.jpg?sig=B"
        identity="jobs/job-1/output.mp4"
        alt="poster"
        fallback={<span>poster unavailable</span>}
      />,
    );
    fireEvent.error(container.querySelector("img")!);

    rerender(
      <StablePoster
        src="https://storage.example.com/poster.jpg?sig=C"
        identity="jobs/job-1/output.mp4"
        alt="poster"
        fallback={<span>poster unavailable</span>}
      />,
    );

    expect(container.querySelector("img")).toBeNull();
    expect(getByText("poster unavailable")).toBeInTheDocument();
  });

  it("adopts each caller-authorized signed URL after a reported failure", () => {
    const onError = jest.fn();
    const { container, rerender } = render(
      <StablePoster
        src="https://storage.example.com/poster.jpg?sig=A"
        identity="jobs/job-1/output.mp4"
        retryKey="sig-A"
        alt="poster"
        fallback={<span>poster unavailable</span>}
        onError={onError}
      />,
    );

    fireEvent.error(container.querySelector("img")!);
    expect(onError).toHaveBeenCalledTimes(1);

    rerender(
      <StablePoster
        src="https://storage.example.com/poster.jpg?sig=B"
        identity="jobs/job-1/output.mp4"
        retryKey="sig-B"
        alt="poster"
        fallback={<span>poster unavailable</span>}
        onError={onError}
      />,
    );
    expect(container.querySelector("img")).toHaveAttribute(
      "src",
      expect.stringContaining("sig=B"),
    );
    fireEvent.error(container.querySelector("img")!);
    expect(onError).toHaveBeenCalledTimes(2);

    rerender(
      <StablePoster
        src="https://storage.example.com/poster.jpg?sig=C"
        identity="jobs/job-1/output.mp4"
        retryKey="sig-C"
        alt="poster"
        fallback={<span>poster unavailable</span>}
        onError={onError}
      />,
    );
    expect(container.querySelector("img")).toHaveAttribute(
      "src",
      expect.stringContaining("sig=C"),
    );
  });

  it("recovers when a failed source disappears before a caller-authorized URL arrives", () => {
    const { container, rerender, getByText } = render(
      <StablePoster
        src="https://storage.example.com/poster.jpg?sig=A"
        identity="jobs/job-1/output.mp4"
        retryKey="sig-A"
        alt="poster"
        fallback={<span>poster unavailable</span>}
      />,
    );

    fireEvent.error(container.querySelector("img")!);
    rerender(
      <StablePoster
        src={null}
        identity="jobs/job-1/output.mp4"
        alt="poster"
        fallback={<span>poster unavailable</span>}
      />,
    );
    expect(container.querySelector("img")).toBeNull();
    expect(getByText("poster unavailable")).toBeInTheDocument();

    rerender(
      <StablePoster
        src="https://storage.example.com/poster.jpg?sig=B"
        identity="jobs/job-1/output.mp4"
        retryKey="sig-B"
        alt="poster"
        fallback={<span>poster unavailable</span>}
      />,
    );

    expect(container.querySelector("img")).toHaveAttribute(
      "src",
      expect.stringContaining("sig=B"),
    );
  });

  it("clears a held poster when the identity changes without a poster", () => {
    const { container, rerender, getByText } = render(
      <StablePoster
        src="https://storage.example.com/old.jpg"
        identity="jobs/job-1/old.mp4"
        alt="poster"
        fallback={<span>poster unavailable</span>}
      />,
    );

    rerender(
      <StablePoster
        src={null}
        identity="jobs/job-1/new.mp4"
        alt="poster"
        fallback={<span>poster unavailable</span>}
      />,
    );

    expect(container.querySelector("img")).toBeNull();
    expect(getByText("poster unavailable")).toBeInTheDocument();
  });

  it("does not revive an old poster when the current source is temporarily absent", () => {
    const { container, rerender, getByText } = render(
      <StablePoster
        src="https://storage.example.com/old.jpg"
        identity="jobs/job-1/output.mp4"
        alt="poster"
        fallback={<span>poster unavailable</span>}
      />,
    );

    rerender(
      <StablePoster
        src={null}
        identity="jobs/job-1/output.mp4"
        alt="poster"
        fallback={<span>poster unavailable</span>}
      />,
    );

    expect(container.querySelector("img")).toBeNull();
    expect(getByText("poster unavailable")).toBeInTheDocument();
  });
});
