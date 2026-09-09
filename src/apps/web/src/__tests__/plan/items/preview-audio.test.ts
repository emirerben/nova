import { resolveVirtualPreviewAudio } from "@/app/plan/items/[id]/_editor/preview-audio";

describe("resolveVirtualPreviewAudio", () => {
  const base = {
    virtualPreviewRequested: true,
    clipDirty: false,
    musicDirty: false,
    backgroundMusicDirty: false,
    musicTrackActive: false,
    musicAudioUrl: null,
    musicStartS: 0,
    sourceAudioMix: null,
    sourceAudioOptions: [],
    baseVideoUrl: "https://cdn.example.test/base.mp4",
    narrationApplied: false,
    videoMuted: false,
    soundMuted: false,
  } as const;

  it("keeps selected uploaded/source audio authoritative after a visual edit", () => {
    expect(
      resolveVirtualPreviewAudio({
        ...base,
        sourceAudioMix: "source_a",
        sourceAudioOptions: [
          {
            mix: "source_a",
            audio_url: "https://cdn.example.test/source-a.m4a",
          },
        ],
      }),
    ).toEqual({
      active: true,
      kind: "source",
      muted: false,
      startS: 0,
      url: "https://cdn.example.test/source-a.m4a",
    });
  });

  it("lets an explicit match-audio override take precedence over baked music", () => {
    expect(
      resolveVirtualPreviewAudio({
        ...base,
        musicTrackActive: true,
        musicAudioUrl: "https://cdn.example.test/music.m4a",
        sourceAudioMix: "source_a",
        sourceAudioOptions: [
          {
            mix: "source_a",
            audio_url: "https://cdn.example.test/source-a.m4a",
          },
        ],
      }),
    ).toEqual({
      active: true,
      kind: "source",
      muted: false,
      startS: 0,
      url: "https://cdn.example.test/source-a.m4a",
    });
  });

  it("uses the exact rendered narration bed instead of raw clip audio", () => {
    expect(
      resolveVirtualPreviewAudio({ ...base, narrationApplied: true }),
    ).toEqual({
      active: true,
      kind: "rendered",
      muted: false,
      startS: 0,
      url: "https://cdn.example.test/base.mp4",
    });
  });

  it("falls back to the rendered music bed for visual-only edits when track audio is unavailable", () => {
    expect(
      resolveVirtualPreviewAudio({
        ...base,
        musicTrackActive: true,
        musicAudioUrl: null,
        musicStartS: 42,
      }),
    ).toEqual({
      active: true,
      kind: "rendered",
      muted: false,
      startS: 0,
      url: "https://cdn.example.test/base.mp4",
    });
  });

  it.each([
    ["clip timeline", { clipDirty: true }],
    ["music window", { musicDirty: true }],
    ["background music", { backgroundMusicDirty: true }],
  ])("does not reuse stale rendered music after a %s edit", (_label, dirty) => {
    expect(
      resolveVirtualPreviewAudio({
        ...base,
        ...dirty,
        musicTrackActive: true,
        musicAudioUrl: null,
      }),
    ).toEqual({
      active: true,
      kind: "music",
      muted: false,
      startS: 0,
      url: null,
    });
  });

  it("uses the selected music URL and preserves its offset and mute state", () => {
    expect(
      resolveVirtualPreviewAudio({
        ...base,
        musicTrackActive: true,
        musicAudioUrl: "https://cdn.example.test/music.m4a",
        musicStartS: 12.5,
        soundMuted: true,
      }),
    ).toEqual({
      active: true,
      kind: "music",
      muted: true,
      startS: 12.5,
      url: "https://cdn.example.test/music.m4a",
    });
  });

  it("keeps narration authoritative while its rendered URL refreshes", () => {
    expect(
      resolveVirtualPreviewAudio({
        ...base,
        baseVideoUrl: null,
        narrationApplied: true,
      }),
    ).toEqual({
      active: true,
      kind: "rendered",
      muted: false,
      startS: 0,
      url: null,
    });
  });

  it("keeps the prepared interleaved bed authoritative for visual-only edits", () => {
    expect(
      resolveVirtualPreviewAudio({
        ...base,
        sourceAudioMix: "interleaved",
        sourceAudioOptions: [
          {
            mix: "interleaved",
            audio_url: "https://cdn.example.test/interleaved.m4a",
          },
        ],
      }),
    ).toEqual({
      active: true,
      kind: "source",
      muted: false,
      startS: 0,
      url: "https://cdn.example.test/interleaved.m4a",
    });
  });

  it("keeps decks silent while a prepared visual-only bed URL refreshes", () => {
    expect(
      resolveVirtualPreviewAudio({
        ...base,
        sourceAudioMix: "interleaved",
        sourceAudioOptions: [{ mix: "interleaved", audio_url: "" }],
      }),
    ).toEqual({
      active: true,
      kind: "source",
      muted: false,
      startS: 0,
      url: null,
    });
  });

  it("uses remapped native deck audio when the clip timeline changes", () => {
    expect(
      resolveVirtualPreviewAudio({
        ...base,
        clipDirty: true,
        sourceAudioMix: "interleaved",
        sourceAudioOptions: [
          {
            mix: "interleaved",
            audio_url: "https://cdn.example.test/interleaved.m4a",
          },
        ],
      }),
    ).toEqual({
      active: false,
      kind: "native",
      muted: false,
      startS: 0,
      url: null,
    });
  });

  it("does not activate a standalone bed outside virtual preview", () => {
    expect(
      resolveVirtualPreviewAudio({
        ...base,
        virtualPreviewRequested: false,
        videoMuted: true,
      }),
    ).toEqual({
      active: false,
      kind: "native",
      muted: true,
      startS: 0,
      url: null,
    });
  });
});
