import KriaMediaEngine
import XCTest
@testable import Kria

/// KRI-1656: every background is a bar on the timeline. These pin the failure modes written down before the code:
/// a window hidden under `own` (F1), a gap too short to hold an `own` row flashing the hidden fill (F2), more rows than
/// the wire holds (F3), a drag changing the bar's id (F5), a crop that drops `y` or divides by zero (F12, F13).
extension NativeEditorSession {
    /// Test setup only: a timed background appended after the last one, clipped to the edit (what adding one did before bars).
    @discardableResult
    func appendBackgroundWindowForTest(mediaID: String, kind: VisualMediaKind, length: Double = 4) -> Bool {
        let existing = speakerBackgroundWindows
        let start = existing.last?.endS ?? 0
        let end = min(start + length, backgroundBarDuration)
        guard end - start >= EditorSpeakerBackgroundWindow.minDuration else { return false }
        var n = existing.count
        while existing.contains(where: { $0.localID == EditorSpeakerBackground.localID(n) }) { n += 1 }
        let window = EditorSpeakerBackgroundWindow(localID: EditorSpeakerBackground.localID(n), mediaID: mediaID, kind: kind, sourceStartS: 0, startS: start, endS: end)
        return setSpeakerBackgroundWindows(existing + [window])
    }
}

@MainActor
final class BackgroundBarsTests: XCTestCase {
    // MARK: Helpers

    private func session(lane: JSONValue? = nil, sections: Bool = true) -> NativeEditorSession {
        // The fixture reads the lane a stub Save kept (shared defaults): start from none, whatever ran before.
        UserDefaults.standard.removeObject(forKey: NativeEditorUITestFixtures.savedSpeakerBackgroundKey)
        var draft = NativeEditorUITestFixtures.speakerBackground
        var caps = draft.serverSnapshot["editor_capabilities"]?.objectValue ?? [:]
        caps["speaker_background_windows"] = .object(["editable": .bool(true)])
        caps["background_focus"] = .object(["editable": .bool(true)])
        caps["speaker_background_color"] = .object(["editable": .bool(true)])
        if sections { caps["speaker_sections"] = .object(["editable": .bool(true)]) }
        draft.serverSnapshot["editor_capabilities"] = .object(caps)
        if let lane {
            var payload = draft.serverSnapshot["editor_payload"]?.objectValue ?? [:]
            var parts = payload["sections"]?.objectValue ?? [:]
            parts["speaker_background"] = lane
            payload["sections"] = .object(parts)
            draft.serverSnapshot["editor_payload"] = .object(payload)
        }
        return NativeEditorSession(draft: draft)
    }

    private func fillOnly(_ id: String = "fill", kind: String = "image") -> JSONValue {
        .object(["media_id": .string(id), "kind": .string(kind), "source_start_s": .number(0)])
    }

    /// The invariants every bar edit must leave: windows ordered and valid, no window under an `own` row, no fill showing
    /// in a span a bar could not own, and the rows within the wire's caps.
    private func assertLaneIsSound(_ background: EditorSpeakerBackground?, duration: Double, file: StaticString = #filePath, line: UInt = #line) {
        guard let background else { return }
        XCTAssertTrue(background.isWireValid(duration: duration), "the lane is on the wire's rules", file: file, line: line)
        for window in background.windows {
            for row in background.layoutWindows where row.layout == .own {
                XCTAssertFalse(window.startS < row.endS - 1e-6 && row.startS < window.endS - 1e-6,
                               "F1: window \(window.localID) is hidden under an own row", file: file, line: line)
            }
        }
        XCTAssertTrue(background.fillSpans(duration: duration).allSatisfy { $0.length < EditorSpeakerBackground.barMinSeconds },
                      "F2: the hidden fill shows somewhere", file: file, line: line)
        XCTAssertLessThanOrEqual(background.windows.count, SpeakerBackgroundLimits.maxWindows, "F3", file: file, line: line)
        XCTAssertLessThanOrEqual(background.layoutWindows.count, SpeakerBackgroundLimits.maxLayoutWindows, "F3", file: file, line: line)
    }

    // MARK: Adding

    func testTappingWithNoBackgroundMakesOneBarOverTheWholeVideo() {
        let editor = session()
        XCTAssertNil(editor.speakerBackground)
        XCTAssertTrue(editor.addSpeakerBackground(mediaID: "fixture-bg-photo", kind: .image))
        let d = editor.backgroundBarDuration
        XCTAssertEqual(editor.speakerBackgroundWindows.map { [$0.startS, $0.endS] }, [[0, d]])
        XCTAssertTrue(editor.backgroundFillRegions.isEmpty, "the hidden fill shows no second bar")
        XCTAssertTrue(editor.speakerLayoutWindows.isEmpty)
        assertLaneIsSound(editor.speakerBackground, duration: d)
    }

    func testTappingInsideABarSplitsItAtThePlayhead() {
        let editor = session()
        let d = editor.backgroundBarDuration
        XCTAssertTrue(editor.addSpeakerBackground(mediaID: "fixture-bg-photo", kind: .image))
        editor.currentTime = d / 2
        XCTAssertTrue(editor.addSpeakerBackground(mediaID: "fixture-bg-video", kind: .video))
        let windows = editor.speakerBackgroundWindows
        XCTAssertEqual(windows.map(\.mediaID), ["fixture-bg-photo", "fixture-bg-video"])
        XCTAssertEqual(windows[0].startS, 0)
        XCTAssertEqual(windows[0].endS, windows[1].startS, accuracy: 1e-9, "no gap and no overlap")
        XCTAssertEqual(windows[0].endS, d / 2, accuracy: 0.002)
        XCTAssertEqual(windows[1].endS, d)
        assertLaneIsSound(editor.speakerBackground, duration: d)
    }

    func testTappingCloserThanABarToTheStartReplacesTheWholeBar() {
        let editor = session()
        XCTAssertTrue(editor.addSpeakerBackground(mediaID: "fixture-bg-photo", kind: .image))
        editor.currentTime = 0.2
        XCTAssertTrue(editor.addSpeakerBackground(mediaID: "fixture-bg-video", kind: .video))
        XCTAssertEqual(editor.speakerBackgroundWindows.map(\.mediaID), ["fixture-bg-video"], "no sliver of the old bar is left")
        assertLaneIsSound(editor.speakerBackground, duration: editor.backgroundBarDuration)
    }

    func testTappingAtTheEndOfTheVideoStillLeavesBarsOfBarLength() {
        let editor = session()
        let d = editor.backgroundBarDuration
        XCTAssertTrue(editor.addSpeakerBackground(mediaID: "fixture-bg-photo", kind: .image))
        editor.currentTime = d
        XCTAssertTrue(editor.addSpeakerBackground(mediaID: "fixture-bg-video", kind: .video))
        XCTAssertEqual(editor.speakerBackgroundWindows.count, 2)
        XCTAssertTrue(editor.speakerBackgroundWindows.allSatisfy { $0.endS - $0.startS >= EditorSpeakerBackgroundWindow.minDuration })
        assertLaneIsSound(editor.speakerBackground, duration: d)
    }

    // MARK: Removing

    func testRemovingABarShowsHerOwnTakeInItsSpanAndOneUndoBringsItBack() {
        let editor = session()
        let d = editor.backgroundBarDuration
        XCTAssertTrue(editor.addSpeakerBackground(mediaID: "fixture-bg-photo", kind: .image))
        editor.currentTime = d / 2
        XCTAssertTrue(editor.addSpeakerBackground(mediaID: "fixture-bg-video", kind: .video))
        let first = editor.speakerBackgroundWindows[0]
        XCTAssertTrue(editor.removeSpeakerBackgroundBar(id: first.localID))
        XCTAssertEqual(editor.speakerBackgroundWindows.map(\.mediaID), ["fixture-bg-video"])
        let own = editor.speakerLayoutWindows.filter { $0.layout == .own }
        XCTAssertEqual(own.count, 1)
        XCTAssertEqual(own[0].startS, first.startS, accuracy: 1e-9)
        XCTAssertEqual(own[0].endS, first.endS, accuracy: 1e-9)
        assertLaneIsSound(editor.speakerBackground, duration: d)
        editor.undo()
        XCTAssertEqual(editor.speakerBackgroundWindows.map(\.mediaID), ["fixture-bg-photo", "fixture-bg-video"])
        XCTAssertTrue(editor.speakerLayoutWindows.isEmpty)
    }

    func testRemovingTheLastBarRemovesTheBackground() {
        let editor = session()
        XCTAssertTrue(editor.addSpeakerBackground(mediaID: "fixture-bg-photo", kind: .image))
        XCTAssertTrue(editor.removeSpeakerBackgroundBar(id: editor.speakerBackgroundWindows[0].localID))
        XCTAssertNil(editor.speakerBackground, "nothing left to show: the lane goes, as None does")
    }

    // MARK: A whole-video fill is one bar

    func testAFillAloneIsOneBarAndTrimmingItMakesWindowsInOneUndoStep() {
        let editor = session(lane: fillOnly())
        let d = editor.backgroundBarDuration
        XCTAssertEqual(editor.backgroundFillRegions.map(\.id), ["fill-0"])
        XCTAssertEqual(editor.backgroundFillRegions[0].span.start, 0)
        XCTAssertEqual(editor.backgroundFillRegions[0].span.end, d, accuracy: 1e-9)
        XCTAssertTrue(editor.canEditBackgroundBarTiming(id: "fill-0"))
        XCTAssertFalse(editor.hasUnsavedChanges)

        let token = NativeTimelineGestureID()
        XCTAssertTrue(editor.beginTimedEdgeTrim(kind: .speakerBackgroundWindow, id: "fill-0", edge: .trailing, gestureID: token))
        editor.updateTimedEdgeTrim(by: -(d / 2), gestureID: token)
        editor.endTimedEdgeTrim(gestureID: token)

        XCTAssertEqual(editor.speakerBackgroundWindows.map(\.localID), ["fill-0"], "the piece keeps its id as it becomes a window")
        XCTAssertEqual(editor.speakerBackgroundWindows[0].endS, d / 2, accuracy: 0.002)
        let own = editor.speakerLayoutWindows.filter { $0.layout == .own }
        XCTAssertEqual(own.map { [$0.startS.rounded(), $0.endS.rounded()] }, [[(d / 2).rounded(), d.rounded()]], "the freed half shows her own take")
        assertLaneIsSound(editor.speakerBackground, duration: d)

        editor.undo()
        XCTAssertTrue(editor.speakerBackgroundWindows.isEmpty, "one undo step back to the fill alone")
        XCTAssertTrue(editor.speakerLayoutWindows.isEmpty)
        XCTAssertFalse(editor.hasUnsavedChanges)
    }

    func testTouchingAFillBarWithoutMovingItChangesNothing() {
        let editor = session(lane: fillOnly())
        let token = NativeTimelineGestureID()
        XCTAssertTrue(editor.beginTimedEdgeTrim(kind: .speakerBackgroundWindow, id: "fill-0", edge: .trailing, gestureID: token))
        editor.updateTimedEdgeTrim(by: 0, gestureID: token)
        editor.endTimedEdgeTrim(gestureID: token)
        XCTAssertTrue(editor.speakerBackgroundWindows.isEmpty, "no window is made for a touch")
        XCTAssertFalse(editor.hasUnsavedChanges)
    }

    func testWithoutSectionsTheFillBarCannotBeTrimmed() {
        let editor = session(lane: fillOnly(), sections: false)
        XCTAssertEqual(editor.backgroundFillRegions.map(\.id), ["fill-0"], "it is still a bar on the lane")
        XCTAssertFalse(editor.canEditBackgroundBarTiming(id: "fill-0"), "F17: no gaps can show her own take, so no trim")
        XCTAssertFalse(editor.beginTimedEdgeTrim(kind: .speakerBackgroundWindow, id: "fill-0", edge: .trailing, gestureID: NativeTimelineGestureID()))
        XCTAssertFalse(editor.hasUnsavedChanges)
    }

    func testWithoutSectionsAddingFromThePlayheadStillSplitsOverTheFill() {
        let editor = session(lane: fillOnly(), sections: false)
        let d = editor.backgroundBarDuration
        editor.currentTime = d / 2
        XCTAssertTrue(editor.addSpeakerBackground(mediaID: "fixture-bg-video", kind: .video))
        XCTAssertEqual(editor.speakerBackgroundWindows.map(\.mediaID), ["fixture-bg-video"])
        XCTAssertEqual(editor.speakerBackgroundWindows[0].endS, d, accuracy: 1e-9)
        XCTAssertTrue(editor.speakerLayoutWindows.isEmpty, "no sections: no own rows are ever written")
    }

    // MARK: Dragging

    func testATrimNeverLeavesAGapTooShortForHerOwnTake() {
        for edge in [NativeTrimEdge.leading, .trailing] {
            let editor = session()
            let d = editor.backgroundBarDuration
            XCTAssertTrue(editor.addSpeakerBackground(mediaID: "fixture-bg-photo", kind: .image))
            editor.currentTime = d / 2
            XCTAssertTrue(editor.addSpeakerBackground(mediaID: "fixture-bg-video", kind: .video))
            // Pull the first bar's trailing edge (or the second's leading edge) a hair short of the other: a 0.3 s gap.
            let id = editor.speakerBackgroundWindows[edge == .trailing ? 0 : 1].localID
            let token = NativeTimelineGestureID()
            XCTAssertTrue(editor.beginTimedEdgeTrim(kind: .speakerBackgroundWindow, id: id, edge: edge, gestureID: token))
            editor.updateTimedEdgeTrim(by: edge == .trailing ? -0.3 : 0.3, gestureID: token)
            editor.endTimedEdgeTrim(gestureID: token)
            assertLaneIsSound(editor.speakerBackground, duration: d)
        }
    }

    func testRandomEditsKeepTheLaneSound() {
        var seed: UInt64 = 0x9E3779B97F4A7C15
        func next() -> Double {
            seed = seed &* 6364136223846793005 &+ 1442695040888963407
            return Double(seed >> 11) / Double(1 << 53)
        }
        let editor = session()
        let d = editor.backgroundBarDuration
        for _ in 0..<120 {
            let roll = next()
            if roll < 0.45 || editor.speakerBackground == nil {
                editor.currentTime = next() * d
                _ = editor.addSpeakerBackground(mediaID: next() < 0.5 ? "fixture-bg-photo" : "fixture-bg-video", kind: next() < 0.5 ? .image : .video)
            } else if roll < 0.65, let bar = editor.speakerBackgroundWindows.randomElement() {
                _ = editor.removeSpeakerBackgroundBar(id: bar.localID)
            } else if let bar = editor.speakerBackgroundWindows.randomElement() {
                let token = NativeTimelineGestureID()
                let edge: NativeTrimEdge = next() < 0.5 ? .leading : .trailing
                if editor.beginTimedEdgeTrim(kind: .speakerBackgroundWindow, id: bar.localID, edge: edge, gestureID: token) {
                    editor.updateTimedEdgeTrim(by: (next() - 0.5) * d, gestureID: token)
                    editor.endTimedEdgeTrim(gestureID: token)
                }
            }
            assertLaneIsSound(editor.speakerBackground, duration: d)
        }
    }

    // MARK: Colour

    func testAColourFromThePlayheadTakesTheRestOfTheBar() {
        let editor = session()
        let d = editor.backgroundBarDuration
        XCTAssertTrue(editor.addSpeakerBackground(mediaID: "fixture-bg-photo", kind: .image))
        editor.currentTime = d / 2
        XCTAssertTrue(editor.setSpeakerBackgroundColorFromPlayhead(hex: "#00ff00"))
        XCTAssertEqual(editor.speakerBackgroundColor, "#00FF00")
        XCTAssertEqual(editor.speakerBackgroundWindows.map(\.mediaID), ["fixture-bg-photo"])
        XCTAssertEqual(editor.speakerBackgroundWindows[0].endS, d / 2, accuracy: 0.002, "the photo keeps the part before the playhead")
        XCTAssertEqual(editor.backgroundFillRegions.count, 1, "the rest is one colour bar")
        XCTAssertEqual(editor.backgroundFillRegions[0].span.end, d, accuracy: 1e-9)
    }

    // MARK: Crop on the focus wire

    func testCropAndFocusRoundTripOnEveryShapeOfPicture() throws {
        let canvas = 9.0 / 16.0
        for imageAspect in [16.0 / 9.0, 9.0 / 16.0, 1.0, 4.0 / 3.0] {
            for zoom in [1.0, 1.25, 1.5, 2.0, 3.0] {
                for (x, y) in [(0.0, 0.0), (0.5, 0.5), (1.0, 1.0), (0.2, 0.8), (0.9, 0.1)] {
                    let focus = SpeakerBackgroundFocus(x: x, y: y, zoom: zoom)
                    let rect = try XCTUnwrap(BackgroundCropMapping.crop(from: focus, imageAspect: imageAspect, outputAspect: canvas),
                                             "zoom \(zoom) is a crop")
                    XCTAssertGreaterThanOrEqual(rect.x, -1e-9)
                    XCTAssertGreaterThanOrEqual(rect.y, -1e-9)
                    XCTAssertLessThanOrEqual(rect.x + rect.width, 1 + 1e-9)
                    XCTAssertLessThanOrEqual(rect.y + rect.height, 1 + 1e-9)
                    XCTAssertTrue(KriaCropGeometry.matchesOutput(rect, imageAspect: imageAspect, outputAspect: canvas, tolerance: 1e-6),
                                  "the crop has the canvas's shape")
                    let back = BackgroundCropMapping.focus(from: rect, current: nil, imageAspect: imageAspect, outputAspect: canvas)
                    // An axis with no spare room (F13) cannot say where along it the crop sits.
                    let spareX = 1 - rect.width > 1e-6, spareY = 1 - rect.height > 1e-6
                    let centred = (!spareX || abs(x - 0.5) <= 0.002) && (!spareY || abs(y - 0.5) <= 0.002) && abs(zoom - 1) <= 0.002
                    if centred { XCTAssertNil(back, "the centred 1x cover-fit is stored as no crop") ; continue }
                    let value = try XCTUnwrap(back)
                    XCTAssertEqual(value.zoom, zoom, accuracy: 1e-3)
                    if spareX { XCTAssertEqual(value.x, x, accuracy: 1e-3, "x, aspect \(imageAspect) zoom \(zoom)") }
                    if spareY { XCTAssertEqual(value.y, y, accuracy: 1e-3, "F12: y is kept, aspect \(imageAspect) zoom \(zoom)") }
                }
            }
        }
    }

    func testAZoomedOutFocusIsNotACropAndAnAxisWithNoRoomKeepsItsStoredValue() throws {
        let canvas = 9.0 / 16.0
        XCTAssertNil(BackgroundCropMapping.crop(from: SpeakerBackgroundFocus(x: 0.2, y: 0.5, zoom: 0.5), imageAspect: 16.0 / 9.0, outputAspect: canvas),
                     "below 1x the whole picture sits in the frame with bands: nothing to crop")
        // A 9:16 picture at 1x fills the frame exactly: no spare room either way, so the stored aim stays.
        let full = KriaCropGeometry.fillCrop(imageAspect: 9.0 / 16.0, outputAspect: canvas)
        let kept = BackgroundCropMapping.focus(from: full, current: SpeakerBackgroundFocus(x: 0.3, y: 0.7, zoom: 1), imageAspect: 9.0 / 16.0, outputAspect: canvas)
        XCTAssertEqual(kept?.x, 0.3)
        XCTAssertEqual(kept?.y, 0.7)
        XCTAssertFalse(kept?.x.isNaN ?? false)
    }

    func testTheZoomCeilingFollowsAStoredZoomPastIt() {
        let base = BackgroundCropMapping.minimumWidth(imageAspect: 1, outputAspect: 9.0 / 16.0, storedZoom: nil)
        let stored = BackgroundCropMapping.minimumWidth(imageAspect: 1, outputAspect: 9.0 / 16.0, storedZoom: 4)
        XCTAssertEqual(base, KriaCropGeometry.fillCrop(imageAspect: 1, outputAspect: 9.0 / 16.0).width / 3, accuracy: 1e-9)
        XCTAssertLessThan(stored, base, "a server-aimed 4x is shown as it is and never jumps when touched")
    }
}
