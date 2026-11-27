# KRI-1651 acceptance

Use a mixed native montage with four independent source occurrences. Video A is 2 seconds, photo B is 2 seconds, video C is 2 seconds, and photo D is 2 seconds. The photo source entries live in the guided revision, outside the legacy uploaded-video path list.

For “make the images show only 0.2 seconds”, the new timeline must remain A (2 s), B (0.2 s), C (2 s), D (0.2 s). It must keep the original media identities. A result with only one shortened photo, a different photo, a shortened video, or a changed overlay is wrong. At 30 fps, each 0.2-second still is six frames. Reject a whole-video reinterpretation. If the creator names overlays instead, do not silently edit the main timeline.

The finished iPhone editor should expose the numbered reorder sheet beside the timeline, including photos. The order stays local until confirmation. Confirm once, preserve existing source indexes and cuts, move clip-local timed lanes with their clip, and keep song lyrics and spanning overlays on their existing clock. Undo once restores the opening order. Preserve split-screen sections as units. The same intended order and timing must survive preview, Save, reopen, and native export.

The photo-only picker is reachable in the Footage step of an eligible phone montage. It reuses the existing still-image upload contract; this does not convert intentionally placed overlays into cuts.
