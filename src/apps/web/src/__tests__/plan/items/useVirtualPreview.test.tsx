import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, jest } from "@jest/globals";

import type { DraftSlot } from "@/app/generative/timeline-math";
import { useVirtualPreview } from "@/app/plan/items/[id]/_editor/useVirtualPreview";

const SLOT: DraftSlot = {
  key: "slot-1",
  slotId: "slot-1",
  clipIndex: 0,
  inS: 1.4,
  durationBeats: null,
  durationS: 2,
  removed: false,
  momentDescription: null,
};

function Harness({
  onPlayingChange,
  soundMuted = false,
}: {
  onPlayingChange: (playing: boolean) => void;
  soundMuted?: boolean;
}) {
  const preview = useVirtualPreview({
    enabled: true,
    slots: [SLOT],
    clips: [{ clip_index: 0, signed_url: "https://cdn.example.test/clip.mp4" }],
    grid: [],
    currentTime: 0,
    muted: false,
    musicAudioUrl: "https://cdn.example.test/music.m4a",
    musicStartS: 55.71,
    soundMuted,
    onTimeUpdate: jest.fn(),
    onDuration: jest.fn(),
    onPlayingChange,
    onSourceError: jest.fn(),
  });
  const { ref: videoARef, ...videoAProps } = preview.videoAProps;
  const { ref: videoBRef, ...videoBProps } = preview.videoBProps;
  const { ref: audioRef, ...audioProps } = preview.musicAudioProps!;

  return (
    <>
      <video data-testid="deck-a" ref={videoARef} {...videoAProps} />
      <video data-testid="deck-b" ref={videoBRef} {...videoBProps} />
      <audio data-testid="music" ref={audioRef} {...audioProps} />
      <button type="button" onClick={preview.play}>
        play
      </button>
    </>
  );
}

describe("useVirtualPreview music transport", () => {
  let playSpy: ReturnType<typeof jest.spyOn>;
  let pauseSpy: ReturnType<typeof jest.spyOn>;

  beforeEach(() => {
    jest.spyOn(window.HTMLMediaElement.prototype, "load").mockImplementation(() => {});
    pauseSpy = jest
      .spyOn(window.HTMLMediaElement.prototype, "pause")
      .mockImplementation(() => {});
  });

  afterEach(() => {
    jest.restoreAllMocks();
  });

  it("pauses the whole virtual transport when music playback is rejected", async () => {
    const onPlayingChange = jest.fn();
    playSpy = jest
      .spyOn(window.HTMLMediaElement.prototype, "play")
      .mockImplementation(function playMock(this: HTMLMediaElement) {
        if (this.tagName === "AUDIO") {
          return Promise.reject(new DOMException("blocked", "NotAllowedError"));
        }
        return Promise.resolve();
      });

    render(<Harness onPlayingChange={onPlayingChange} />);
    fireEvent.click(screen.getByRole("button", { name: "play" }));

    await waitFor(() => expect(onPlayingChange).toHaveBeenLastCalledWith(false));
    expect(pauseSpy).toHaveBeenCalled();

    const videoPlayCalls = playSpy.mock.instances.filter(
      (el: unknown) => (el as HTMLMediaElement).tagName === "VIDEO",
    );
    const audioPlayCalls = playSpy.mock.instances.filter(
      (el: unknown) => (el as HTMLMediaElement).tagName === "AUDIO",
    );
    const videoPauseCalls = pauseSpy.mock.instances.filter(
      (el: unknown) => (el as HTMLMediaElement).tagName === "VIDEO",
    );
    const audioPauseCalls = pauseSpy.mock.instances.filter(
      (el: unknown) => (el as HTMLMediaElement).tagName === "AUDIO",
    );
    expect(videoPlayCalls).toHaveLength(1);
    expect(audioPlayCalls).toHaveLength(1);
    expect(videoPauseCalls.length).toBeGreaterThan(0);
    expect(audioPauseCalls.length).toBeGreaterThan(0);
  });

  it("maps the sound-lane mute to the virtual music element", () => {
    const { rerender } = render(<Harness onPlayingChange={jest.fn()} soundMuted />);
    expect(screen.getByTestId("music")).toHaveProperty("muted", true);

    rerender(<Harness onPlayingChange={jest.fn()} soundMuted={false} />);
    expect(screen.getByTestId("music")).toHaveProperty("muted", false);
  });
});
