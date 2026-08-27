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
