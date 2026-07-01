/**
 * Regression test for the useAudioRecorder extraction.
 *
 * The generative VoiceRecorder bar is a SHIPPED component; extracting the mic /
 * MediaRecorder / waveform / upload-fallback logic into useAudioRecorder must not
 * change its behaviour. This test drives the real VoiceRecorder (which now consumes
 * the hook) and asserts:
 *   1. Recording: Record → live waveform + Stop → uploads the recorded Blob.
 *   2. Mic-blocked: getUserMedia rejection surfaces the mic-blocked notice and
 *      never leaves the recording state.
 *   3. Upload fallback: choosing an audio file uploads it and shows the name.
 */

import "@testing-library/jest-dom";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { VoiceRecorder } from "@/app/generative/VoiceRecorder";

// ── uploadVoiceover mock ──────────────────────────────────────────────────────
const uploadVoiceoverMock = jest.fn();
jest.mock("@/lib/generative-api", () => ({
  uploadVoiceover: (...args: unknown[]) => uploadVoiceoverMock(...args),
}));

// ── Web-audio / MediaRecorder mocks ───────────────────────────────────────────

let lastRecorder: FakeMediaRecorder | null = null;

class FakeMediaRecorder {
  ondataavailable: ((e: { data: Blob }) => void) | null = null;
  onstop: (() => void) | null = null;
  state = "inactive";
  constructor(public stream: MediaStream) {
    lastRecorder = this;
  }
  start() {
    this.state = "recording";
  }
  stop() {
    this.state = "inactive";
    // Emit one chunk, then fire onstop (mirrors the browser sequence).
    this.ondataavailable?.({ data: new Blob(["audio-bytes"], { type: "audio/webm" }) });
    this.onstop?.();
  }
}

class FakeAudioContext {
  createMediaStreamSource() {
    return { connect: jest.fn() };
  }
  createAnalyser() {
    return {
      fftSize: 0,
      frequencyBinCount: 8,
      getByteTimeDomainData: (arr: Uint8Array) => arr.fill(128),
      connect: jest.fn(),
    };
  }
  close() {
    return Promise.resolve();
  }
}

const fakeTrack = { stop: jest.fn() };
const fakeStream = { getTracks: () => [fakeTrack] } as unknown as MediaStream;

function installMediaMocks(getUserMedia: ReturnType<typeof jest.fn>) {
  Object.defineProperty(navigator, "mediaDevices", {
    configurable: true,
    value: { getUserMedia },
  });
  (window as unknown as { MediaRecorder: unknown }).MediaRecorder = FakeMediaRecorder;
  (window as unknown as { AudioContext: unknown }).AudioContext = FakeAudioContext;
  // Canvas 2D context (jsdom returns null otherwise).
  HTMLCanvasElement.prototype.getContext = jest.fn(() => ({
    clearRect: jest.fn(),
    fillRect: jest.fn(),
    beginPath: jest.fn(),
    moveTo: jest.fn(),
    lineTo: jest.fn(),
    stroke: jest.fn(),
  })) as unknown as HTMLCanvasElement["getContext"];
  // rAF: run once synchronously so drawWaveform doesn't spin.
  window.requestAnimationFrame = ((cb: FrameRequestCallback) => {
    cb(0);
    return 1;
  }) as typeof window.requestAnimationFrame;
  window.cancelAnimationFrame = jest.fn();
}

beforeEach(() => {
  uploadVoiceoverMock.mockReset();
  uploadVoiceoverMock.mockResolvedValue({ gcs_path: "music-uploads/take.webm", kind: "audio" });
  lastRecorder = null;
  global.URL.createObjectURL = jest.fn(() => "blob:take");
  global.URL.revokeObjectURL = jest.fn();
});

describe("useAudioRecorder (via VoiceRecorder regression)", () => {
  it("records a take and uploads the recorded Blob", async () => {
    const getUserMedia = jest.fn().mockResolvedValue(fakeStream);
    installMediaMocks(getUserMedia);
    const onVoiceover = jest.fn();

    render(<VoiceRecorder onVoiceover={onVoiceover} />);

    // Start recording.
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /record voiceover/i }));
    });
    expect(getUserMedia).toHaveBeenCalledWith({ audio: true });
    // Waveform canvas + Stop appear (recording state).
    expect(screen.getByRole("button", { name: /stop recording/i })).toBeInTheDocument();

    // Stop → onstop fires → upload runs with the recorded Blob.
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /stop recording/i }));
    });
    await waitFor(() => expect(uploadVoiceoverMock).toHaveBeenCalledTimes(1));
    const [blobArg, nameArg] = uploadVoiceoverMock.mock.calls[0];
    expect(blobArg).toBeInstanceOf(Blob);
    expect(nameArg).toBe("voiceover.webm");
    await waitFor(() =>
      expect(onVoiceover).toHaveBeenCalledWith("music-uploads/take.webm"),
    );
    expect(lastRecorder).not.toBeNull();
  });

  it("handles mic-blocked and offers the upload fallback", async () => {
    const getUserMedia = jest.fn().mockRejectedValue(new DOMException("denied", "NotAllowedError"));
    installMediaMocks(getUserMedia);
    const onVoiceover = jest.fn();

    render(<VoiceRecorder onVoiceover={onVoiceover} />);

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /record voiceover/i }));
    });

    // Mic-blocked notice shows; we never enter the recording state.
    expect(screen.getByText(/mic blocked/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /stop recording/i })).not.toBeInTheDocument();
    // Upload fallback is still offered.
    expect(screen.getByText(/upload audio/i)).toBeInTheDocument();
    expect(uploadVoiceoverMock).not.toHaveBeenCalled();
  });

  it("uploads a chosen audio file (upload fallback) and shows its name", async () => {
    const getUserMedia = jest.fn().mockResolvedValue(fakeStream);
    installMediaMocks(getUserMedia);
    const onVoiceover = jest.fn();

    const { container } = render(<VoiceRecorder onVoiceover={onVoiceover} />);

    const fileInput = container.querySelector('input[type="file"]') as HTMLInputElement;
    const file = new File(["bytes"], "my-vo.mp3", { type: "audio/mpeg" });

    await act(async () => {
      fireEvent.change(fileInput, { target: { files: [file] } });
    });

    await waitFor(() => expect(uploadVoiceoverMock).toHaveBeenCalledTimes(1));
    const [blobArg, nameArg] = uploadVoiceoverMock.mock.calls[0];
    expect(blobArg).toBe(file);
    expect(nameArg).toBe("my-vo.mp3");
    await waitFor(() =>
      expect(onVoiceover).toHaveBeenCalledWith("music-uploads/take.webm"),
    );
    // Review shows the file's display name.
    expect(await screen.findByText("my-vo.mp3")).toBeInTheDocument();
  });
});
