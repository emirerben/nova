/**
 * StableVideo — stable-src-until-identity-changes behaviour.
 *
 * Verifies:
 * 1. Same identity + new src (signature churn) → no src change in DOM.
 * 2. Changed identity → adopts new src (new render swaps the video).
 * 3. onError → falls forward to latest src (expired-signature recovery).
 * 4. null / undefined / malformed src → renders without throwing.
 * 5. Pathname-based stability when no identity prop is given.
 */

import "@testing-library/jest-dom";
import { act, fireEvent, render } from "@testing-library/react";
import { StableVideo } from "@/components/StableVideo";

describe("StableVideo", () => {
  it("holds the initial src when signature churns (same identity)", () => {
    const { rerender, container } = render(
      <StableVideo
        src="https://storage.example.com/obj.mp4?sig=A"
        identity="2024-01-01T10:00:00"
      />,
    );
    const video = container.querySelector("video")!;
    expect(video.getAttribute("src")).toContain("sig=A");

    // Same identity, new signature — must NOT change the src.
    rerender(
      <StableVideo
        src="https://storage.example.com/obj.mp4?sig=B"
        identity="2024-01-01T10:00:00"
      />,
    );
    expect(video.getAttribute("src")).toContain("sig=A");
  });

  it("adopts the new src when identity changes (new render = new bytes)", () => {
    const { rerender, container } = render(
      <StableVideo
        src="https://storage.example.com/obj.mp4?sig=A"
        identity="2024-01-01T10:00:00"
      />,
    );
    const video = container.querySelector("video")!;
    expect(video.getAttribute("src")).toContain("sig=A");

    // Identity changed → must adopt new src.
    rerender(
      <StableVideo
        src="https://storage.example.com/obj.mp4?sig=NEW"
        identity="2024-01-01T11:00:00"
      />,
    );
    expect(video.getAttribute("src")).toContain("sig=NEW");
  });

  it("falls forward to the latest src on onError (expired signature)", async () => {
    const { rerender, container } = render(
      <StableVideo
        src="https://storage.example.com/obj.mp4?sig=A"
        identity="2024-01-01T10:00:00"
      />,
    );
    const video = container.querySelector("video")!;
    expect(video.getAttribute("src")).toContain("sig=A");

    // Signature churns — still held at A because identity unchanged.
    rerender(
      <StableVideo
        src="https://storage.example.com/obj.mp4?sig=B"
        identity="2024-01-01T10:00:00"
      />,
    );
    expect(video.getAttribute("src")).toContain("sig=A");

    // onError fires (sig=A expired) → must fall forward to the latest src (B).
    await act(async () => {
      fireEvent.error(video);
    });
    expect(video.getAttribute("src")).toContain("sig=B");
  });

  it("chains a caller-supplied onError handler", async () => {
    const callerOnError = jest.fn();
    const { container } = render(
      <StableVideo
        src="https://storage.example.com/obj.mp4?sig=A"
        identity="id-1"
        onError={callerOnError}
      />,
    );
    const video = container.querySelector("video")!;
    await act(async () => {
      fireEvent.error(video);
    });
    expect(callerOnError).toHaveBeenCalledTimes(1);
  });

  it("does not throw on null src", () => {
    expect(() => {
      const { container } = render(<StableVideo src={null} identity="x" />);
      expect(container.querySelector("video")).toBeInTheDocument();
    }).not.toThrow();
  });

  it("does not throw on undefined src", () => {
    expect(() => {
      const { container } = render(<StableVideo identity="x" />);
      expect(container.querySelector("video")).toBeInTheDocument();
    }).not.toThrow();
  });

  it("does not throw on a malformed URL when no identity is supplied", () => {
    expect(() => {
      render(<StableVideo src="not-a-valid-url" />);
    }).not.toThrow();
  });

  it("adopts the first real src when starting from null", () => {
    const { rerender, container } = render(
      <StableVideo src={null} identity="2024-01-01T10:00:00" />,
    );
    const video = container.querySelector("video")!;
    // null src → no src attribute
    expect(video.getAttribute("src")).toBeNull();

    // First real src arrives
    rerender(
      <StableVideo
        src="https://storage.example.com/obj.mp4?sig=A"
        identity="2024-01-01T10:00:00"
      />,
    );
    expect(video.getAttribute("src")).toContain("sig=A");
  });

  describe("pathname-based stability (no identity prop)", () => {
    it("holds src when only the query string changes", () => {
      const { rerender, container } = render(
        <StableVideo src="https://storage.example.com/obj.mp4?sig=A" />,
      );
      const video = container.querySelector("video")!;
      expect(video.getAttribute("src")).toContain("sig=A");

      // Same pathname, different signature — held.
      rerender(<StableVideo src="https://storage.example.com/obj.mp4?sig=B" />);
      expect(video.getAttribute("src")).toContain("sig=A");
    });

    it("adopts new src when the pathname changes (different object)", () => {
      const { rerender, container } = render(
        <StableVideo src="https://storage.example.com/obj.mp4?sig=A" />,
      );
      const video = container.querySelector("video")!;

      rerender(<StableVideo src="https://storage.example.com/new-obj.mp4?sig=C" />);
      expect(video.getAttribute("src")).toContain("sig=C");
    });
  });
});
