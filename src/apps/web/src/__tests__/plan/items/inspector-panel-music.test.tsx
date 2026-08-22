import "@testing-library/jest-dom";
import React from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import InspectorPanel from "@/app/plan/items/[id]/_editor/InspectorPanel";
import type { MusicTrackSummary } from "@/lib/music-api";

const noop = jest.fn();

const track: MusicTrackSummary = {
  id: "track-2",
  title: "New Song",
  artist: "Nova",
  thumbnail_url: null,
  section_duration_s: 12,
  required_clips_min: 1,
  required_clips_max: 8,
  template_kind: "beat_sync",
  user_slot_count: 4,
  user_slot_accepts: ["video"],
};

function renderMusicInspector(overrides = {}) {
  const onPickMusic = jest.fn();
  const onPatchBackgroundMusic = jest.fn();
  const onRemoveBackgroundMusic = jest.fn();
  render(
    <InspectorPanel
      selection={{ kind: "music", id: "bed" }}
      bar={null}
      clipTiming={null}
      sfx={null}
      overlay={null}
      tab="basic"
      sampleWord={null}
      appliedPresetId={null}
      contentRef={React.createRef<HTMLTextAreaElement>()}
      onEditText={noop}
      onPatch={noop}
      onPatchTextTiming={noop}
      onPatchClipTiming={noop}
      onPreviewClipTiming={noop}
      onRecordClipTiming={noop}
      onPatchSfx={noop}
      onDeleteSfx={noop}
      onPatchOverlay={noop}
      onPreviewOverlay={noop}
      onRecordOverlay={noop}
      onDeleteOverlay={noop}
      mixLevel={null}
      mixEditable={false}
      mixLabel="Current Song"
      musicTracks={[track]}
      musicLoading={false}
      currentMusicTrackId="track-1"
      musicEditable
      onPickMusic={onPickMusic}
      onPatchBackgroundMusic={onPatchBackgroundMusic}
      onRemoveBackgroundMusic={onRemoveBackgroundMusic}
      onPatchMix={noop}
      smartPlaceAvailable={false}
      onClose={noop}
      onPickPreset={noop}
      {...overrides}
    />,
  );
  return { onPickMusic, onPatchBackgroundMusic, onRemoveBackgroundMusic };
}

describe("InspectorPanel music bed", () => {
  it("shows song swapping even when bed-level mixing is fixed", () => {
    const { onPickMusic } = renderMusicInspector();

    expect(screen.queryByText("This sound bed is locked for this edit.")).not.toBeInTheDocument();
    expect(screen.getByText("Bed level is fixed for this edit.")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /New Song/i }));
    expect(onPickMusic).toHaveBeenCalledWith("track-2");
  });

  it("edits background music trim, mute, volume, and removal", async () => {
    const user = userEvent.setup();
    const { onPatchBackgroundMusic, onRemoveBackgroundMusic } = renderMusicInspector({
      currentMusicTrackId: "track-2",
      backgroundMusic: {
        track_id: "track-2",
        enabled: true,
        start_s: 2,
        end_s: 10,
        gain_db: -20,
        muted: false,
      },
      backgroundMusicTrackDurationS: 30,
    });

    fireEvent.click(screen.getByLabelText("Mute"));
    expect(onPatchBackgroundMusic).toHaveBeenCalledWith({ muted: true });

    // Volume is a shadcn Slider (Radix), not a native range input — it has no
    // `.value` to set via fireEvent.change; drive it by keyboard instead. It's
    // fully controlled by `gain_db`, which this mock doesn't feed back in, so
    // the displayed aria-valuenow doesn't persist across the interaction —
    // only the onValueChange payload is asserted.
    const volume = screen.getByRole("slider", { name: "Volume" });
    expect(volume).toHaveAttribute("aria-valuenow", "-20");
    volume.focus();
    await user.keyboard("{ArrowRight}");
    expect(onPatchBackgroundMusic).toHaveBeenCalledWith({ gain_db: -19.5 });

    fireEvent.change(screen.getByLabelText("Trim start"), { target: { value: "4" } });
    expect(onPatchBackgroundMusic).toHaveBeenCalledWith({ start_s: 4, end_s: 10 });

    fireEvent.change(screen.getByLabelText("Trim end"), { target: { value: "12" } });
    expect(onPatchBackgroundMusic).toHaveBeenCalledWith({ end_s: 12 });

    fireEvent.click(screen.getByRole("button", { name: "Remove" }));
    expect(onRemoveBackgroundMusic).toHaveBeenCalled();
  });
});
