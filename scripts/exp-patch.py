"""Throwaway (KRI-1604): put measurement prints or a deliberate fault into one arm of the comparison."""
import pathlib
import sys

SONG_TEST = pathlib.Path("src/apps/ios/Tests/KriaUITests/SongOrderUITests.swift")
PREVIEW = pathlib.Path("src/apps/ios/Kria/Features/SongTimelinePreview.swift")
SURFACE = pathlib.Path("src/apps/ios/Kria/Core/CameraRollSurface.swift")
FORMAT = pathlib.Path("src/apps/ios/Tests/KriaUITests/FormatCards.swift")


def swap(path, old, new):
    text = path.read_text()
    if text.count(old) != 1:
        raise SystemExit(f"{path}: expected exactly one {old!r}, found {text.count(old)}")
    path.write_text(text.replace(old, new))


arm = sys.argv[1]
if arm == "fix":
    swap(
        SONG_TEST,
        '        XCTAssertTrue((before - 0.15...after + 0.15).contains(songInClip)',
        '        print("SONG-FIX before=\\(before) chip=\\(songInClip) after=\\(after) video=\\(clocks?.video ?? -1) song=\\(clocks?.song ?? -1) low=\\(songInClip - before) high=\\(after - songInClip)")\n'
        '        XCTAssertTrue((before - 0.15...after + 0.15).contains(songInClip)',
    )
    swap(
        FORMAT,
        '        let ready = XCTNSPredicateExpectation(',
        '        print("FORMAT-READY atFirstLook=\\(card.exists && card.isEnabled)")\n        let ready = XCTNSPredicateExpectation(',
    )
elif arm == "main":
    swap(
        SONG_TEST,
        '        XCTAssertEqual(previewVideoTime(app), playhead(app) - 4, accuracy: 0.6, "the video stays in step with the song")',
        '        let t0 = Date(); let v = previewVideoTime(app); let t1 = Date(); let p = playhead(app); let t2 = Date()\n'
        '        print("SONG-MAIN video=\\(v) playhead=\\(p) off=\\(v - (p - 4)) videoRead=\\(t1.timeIntervalSince(t0)) playheadRead=\\(t2.timeIntervalSince(t1))")\n'
        '        XCTAssertEqual(v, p - 4, accuracy: 0.6, "the video stays in step with the song")',
    )
elif arm == "control-video":
    # The picture is reported 1 s ahead of where the song is: "in step" must fail.
    swap(PREVIEW, "let shown = model.videoTime ?? takeTime", "let shown = model.videoTime.map { $0 + 1 } ?? takeTime")
elif arm == "control-label":
    # Chip and picture both 1 s ahead of the transport once playing: only the playhead bracket can see it.
    swap(PREVIEW, "let shown = model.videoTime ?? takeTime", "let shown = (model.videoTime ?? takeTime) + (takeTime > 0.25 ? 1 : 0)")
    swap(
        PREVIEW,
        'String(localized: "Clip \\(clipNumber(id)) · \\(SongTimelineMath.timecode(takeTime)), video at',
        'String(localized: "Clip \\(clipNumber(id)) · \\(SongTimelineMath.timecode(takeTime + (takeTime > 0.25 ? 1 : 0))), video at',
    )
elif arm == "control-format":
    # The cards never stop being busy: the helper must fail at the card, not let the tap through.
    swap(
        pathlib.Path("src/apps/ios/Kria/Features/ChatWorkspaceView.swift"),
        "FormatStage(formats: availableFormats, isBusy: isActing || !capabilitiesAreAuthoritative, select: selectFormat)",
        "FormatStage(formats: availableFormats, isBusy: true, select: selectFormat)",
    )
elif arm == "control-undo":
    # Mutations no longer wait for the one before: the restore overtakes the held delete, so the delete wins.
    swap(SURFACE, "mutationChain = Task { await previous?.value; await work() }", "mutationChain = Task { await work() }")
elif arm == "control-use":
    # The chat only opens while the progress sheet is up: closing the sheet loses the work's result.
    swap(
        SURFACE,
        "            guard let self else { return false }\n            let opened = await openChat(thread)",
        "            guard let self else { return false }\n            guard shownUse?.id == story.id else { return false }\n            let opened = await openChat(thread)",
    )
else:
    raise SystemExit(f"unknown arm {arm}")
