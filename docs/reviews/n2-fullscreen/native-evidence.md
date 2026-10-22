# N2 full-screen native replay

The offline authored replay passed on iPhone 17 Pro Simulator (iOS 26.5, UUID `9869B5F5-B545-420A-8254-270EC45565CE`): `DeviceMontageRenderE2ETests/testN2FullscreenCabinReplay` reported 1 passed, 0 failed. The exported movie duration was 19.633979 seconds.

`scripts/ios/phone-n2-fullscreen-e2e.py` reads the local N2 source kit and compiles the real narrated recipe. It uses an estimated, authored caption schedule: 0.2–2.8, 3.0–5.5, 5.7–10.0, 10.3–14.0, and 14.2–19.3 seconds. The bathtub occupies the full frame from 6.2–7.7 seconds; the river photo occupies it from 10.3–11.8 seconds, followed by the river video from its opening frame; the laptop ends the replay from 14.0 seconds through the narration end. The title is `3 things we loved`.

Inspection artifacts from the passing export are local only: `/private/tmp/n2-fullscreen-e2e/frames/n2_fullscreen.mp4` and `n2_fullscreen-{loft,bathtub_photo,river_photo,river_opening,laptop_final}.png` in the same directory. Frame inspection confirmed the bathtub, river photo, river opening, final laptop, and exact title.

After changing the sequence beat to the production `asset-n2-river-photo` identifier, the rebuilt canonical recipe JSON had the same immutable SHA-256 digest as the exported recipe: `2bb312039a0624eb00e03951fefd85446156b583f47716029e5177c59e398ba1`. The existing export evidence therefore remains applicable.

This is renderer and data-flow evidence for an offline authored replay. It does not evaluate model understanding of the footage.
