export interface PreviewSourceAudioOption {
  mix: string;
  audio_url: string;
}

export interface VirtualPreviewAudio {
  active: boolean;
  kind: "music" | "source" | "rendered" | "native";
  muted: boolean;
  startS: number;
  url: string | null;
}

/**
 * Resolve the audio authority used after the editor swaps its rendered video
 * for the raw-source virtual timeline.
 *
 * Raw clip decks are correct only for native/interleaved audio that follows a
 * changed clip timeline. Music, narration, and explicitly selected source beds
 * live outside those raw files and must be carried across on their own media
 * element. For visual-only edits, the text-free rendered base is also a safe
 * fallback when the separately signed music URL is temporarily unavailable.
 */
export function resolveVirtualPreviewAudio({
  virtualPreviewRequested,
  clipDirty,
  musicDirty,
  backgroundMusicDirty,
  musicTrackActive,
  musicAudioUrl,
  musicStartS,
  sourceAudioMix,
  sourceAudioOptions,
  baseVideoUrl,
  narrationApplied,
  videoMuted,
  soundMuted,
}: {
  virtualPreviewRequested: boolean;
  clipDirty: boolean;
  musicDirty: boolean;
  backgroundMusicDirty: boolean;
  musicTrackActive: boolean;
  musicAudioUrl: string | null;
  musicStartS: number;
  sourceAudioMix: string | null;
  sourceAudioOptions: readonly PreviewSourceAudioOption[];
  baseVideoUrl: string | null;
  narrationApplied: boolean;
  videoMuted: boolean;
  soundMuted: boolean;
}): VirtualPreviewAudio {
  if (!virtualPreviewRequested) {
    return {
      active: false,
      kind: "native",
      muted: videoMuted,
      startS: 0,
      url: null,
    };
  }

  const selectedSourceAudio = sourceAudioOptions.find(
    (option) => option.mix === (sourceAudioMix ?? "interleaved"),
  );
  const explicitSourceAudio =
    selectedSourceAudio && selectedSourceAudio.mix !== "interleaved"
      ? selectedSourceAudio
      : null;
  if (explicitSourceAudio) {
    return {
      active: true,
      kind: "source",
      muted: videoMuted,
      startS: 0,
      url: explicitSourceAudio.audio_url || null,
    };
  }

  if (musicTrackActive) {
    if (musicAudioUrl) {
      return {
        active: true,
        kind: "music",
        muted: soundMuted,
        startS: musicStartS,
        url: musicAudioUrl,
      };
    }
    const renderedBedIsCurrent =
      !clipDirty && !musicDirty && !backgroundMusicDirty;
    if (renderedBedIsCurrent && baseVideoUrl) {
      return {
        active: true,
        kind: "rendered",
        muted: soundMuted,
        startS: 0,
        url: baseVideoUrl,
      };
    }
    // Keep the raw decks muted when the selected song cannot be reconstructed.
    // Leaking their original sound would falsely preview a different edit.
    return {
      active: true,
      kind: "music",
      muted: soundMuted,
      startS: musicStartS,
      url: null,
    };
  }

  if (narrationApplied) {
    return {
      active: true,
      kind: "rendered",
      muted: videoMuted,
      startS: 0,
      // A temporarily missing/expired base URL must stay silent until the
      // owner-scoped status refresh completes. Falling through to native deck
      // audio would preview a different soundtrack from the saved narration.
      url: baseVideoUrl || null,
    };
  }

  // The prepared interleaved bed is exact for visual-only changes. A changed
  // clip timeline must instead use the remapped raw-deck audio.
  if (!clipDirty && selectedSourceAudio) {
    return {
      active: true,
      kind: "source",
      muted: videoMuted,
      startS: 0,
      // Preserve authority even while a signed prepared-bed URL is being
      // refreshed; raw per-clip audio is not equivalent to the assembled bed.
      url: selectedSourceAudio.audio_url || null,
    };
  }

  return {
    active: false,
    kind: "native",
    muted: videoMuted,
    startS: 0,
    url: null,
  };
}
