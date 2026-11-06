# Speaker background evidence (KRI-1114..1119)

Artifacts for the Talk-to-camera green-screen PR. All were produced on 2026-10-31. No video is committed: the MP4s the montages and pixel reports were cut from stay on the developer machine. Design and failure-mode rows: `docs/reviews/speaker-background-failure-modes.md`. Server and rollout notes: `docs/pipelines/speaker-background.md`.

Labels used below.

- **iPhone 13 Pro**: `iPhone14,2`, iOS 27.0.1, 5.7 GB RAM, thermal `nominal` at launch. Real `VisionPersonSegmenter`, the shipping exporter and the shipping config.
- **Mac**: the development Mac, which exports the same edits with the real Vision matte. The simulator cannot run the real Vision matte, so the Mac and the iPhone are the two pixel proofs.

## device/

- `montage-frames-device-on_photo_60.png`: frames from the iPhone 13 Pro export of a 60 s edit with a photo behind the speaker (a magenta and cyan checker, so a leak is visible).
- `montage-frames-device-on_video_60.png`: the same for a looping video background.
- `pixel-audio-report-frames-device.json`: pixel check on those iPhone exports at 5.5, 20.5, 40.5 and 59.0 s. Wall regions are at least 99.7% background-checker color (100% for the photo case). Face regions show 0% checker color, and their mean difference from the source frame stays at or under 6.9 of 255 (torso at or under 3.9). It also holds the tone check of the speaker audio source (440 Hz, 5 s).
- `export-timing-results.json`: iPhone 13 Pro 60 s export wall time, memory and thermal state. This file holds one control run (no matte, 19.6 s) and three matte runs (photo 62.5 s and 61.0 s, video 61.1 s), peak memory 136 to 186 MB, thermal `nominal`. Build: Debug configuration with `-O` and whole-module optimization. The wider range quoted in failure-mode rows S14 and S26 (photo 59.7 to 62.5 s, video 49.4 to 61.1 s) comes from further runs of the same build that are not in this file. A stock Debug build (`-Onone`) took 118 to 121 s for the photo case. A true Release build was not measured.
- `camera-matte-spike-results.json`: the throwaway `MatteSpike` harness on the iPhone 13 Pro, Release configuration. Part A is export cost by matte quality and mask rate. Part B is the live front camera with a person in front of it: with the `balanced` matte the data output delivers 30 fps with a 25 ms median matte cost, with the `accurate` matte only 10 fps (97 ms median, 198 frames dropped as late in the 10 s window). Both runs produced a playable file with audio, no frame gap over 50 ms and thermal `nominal`. Part B ran the user on camera for 10 s per mode.

## mac/

- `montage-frames-mac-on_photo.png`: frames from the Mac export of the same photo-background edit, for comparison with the device montage.
- `pixel-audio-report-frames-mac.json`: the same pixel check on the Mac export.
- `compile-report.json`: server-side compile of the recipe for each case (photo, video, 60 s variants, flag off, never asked, protocol 26). Shows the `speaker_background` lane on the on cases, and that flag-off and protocol-26 recipes are byte-identical to a request that never asked.
- `item2-426-evidence.txt`: a protocol 26 client that polls a pinned recipe gets 426 `client_update_required` with `minimum_client_protocol: 27`.

## matte-spike/

- `results.md`: the Phase 0 Mac spike (KRI-1114) that chose the matte settings. Its footage is low resolution, so its edge-quality verdict is weaker than its cost and rate findings. Its machine path was replaced with `<scratch>`.
- `edge_dl_hair.png`: curly hair against a wall, the hardest edge. Tiles run from the old production recipe to the accurate soft mask with a steep curve (variants A to J in `results.md`).
- `K_dl_hair.png`: the same hair crop from a later spike run (15:23 UTC, after `results.md` was written). `results.md` does not describe this tile.
