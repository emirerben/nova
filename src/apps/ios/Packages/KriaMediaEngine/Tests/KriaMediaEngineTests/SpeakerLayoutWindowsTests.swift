#if canImport(AVFoundation)
import XCTest
@preconcurrency import AVFoundation
import CoreImage
@testable import KriaMediaEngine

/// KRI-1558: the speaker's layout per section. One take, three sections drawn `own`, `small` and `picture`,
/// then a layout-free stretch that falls back to the layer's frame: every frame, in the preview and in the
/// export, is the layout its section names, and the change lands on the section's first frame.
final class SpeakerLayoutWindowsTests: XCTestCase {
    private let corner = SpeakerFrame(widthFraction: 0.42, xFraction: 0.76, yFraction: 0.80)

    private enum Drawn: String { case own, full, small, picture }

    /// What the frame shows: the room around the person (own take), the person full size over the background,
    /// the person small in the bottom-right corner, or the background alone.
    private func drawn(_ frame: SpeakerTestFrame) -> String {
        let middle = frame.rgb(fx: 0.5, fy: 0.6), edge = frame.rgb(fx: 0.1, fy: 0.5)
        let corner = frame.rgb(fx: 0.76, fy: 0.85), beside = frame.rgb(fx: 0.60, fy: 0.85)
        if isColor(middle, SpeakerColor.person) && isColor(edge, SpeakerColor.room, tolerance: 90) { return Drawn.own.rawValue }
        if isColor(middle, SpeakerColor.person) && isColor(edge, SpeakerColor.background) { return Drawn.full.rawValue }
        if isColor(middle, SpeakerColor.background) && isColor(corner, SpeakerColor.person)
            && isColor(beside, SpeakerColor.background) { return Drawn.small.rawValue }
        if isColor(middle, SpeakerColor.background) && isColor(corner, SpeakerColor.background)
            && isColor(edge, SpeakerColor.background) { return Drawn.picture.rawValue }
        return "neither: middle \(middle) edge \(edge) corner \(corner) beside \(beside)"
    }

    @MainActor func testEachSectionIsDrawnWithItsLayoutInPreviewAndExport() async throws {
        useSpeakerSegmenter(self, GreenPersonSegmenter())
        let directory = try speakerTestDirectory(self)
        let speaker = try await writeSpeakerClip(directory: directory, size: (90, 160), seconds: 4)
        let background = try solidSpeakerTestImage(directory: directory)
        var scene = try speakerRecipe(canvas: Canvas(width: 90, height: 160), speaker: speaker, duration: 4,
                                      background: SpeakerTestBackground(url: background))
        // Sections: 0-1 own, 1-2 small, 2-3 picture; 3-4 has no layout window, so the layer's frame (none: full) draws.
        let layouts = [SpeakerLayoutWindow(start: 0, end: 1, layout: .own),
                       SpeakerLayoutWindow(start: 1, end: 2, layout: .small, frame: corner),
                       SpeakerLayoutWindow(start: 2, end: 3, layout: .picture)]
        let layer = try XCTUnwrap(scene.recipe.speakerBackground)
        scene.recipe.speakerBackground = SpeakerBackgroundLayer(assetID: layer.assetID, mediaKind: layer.mediaKind,
            windows: layer.windows, speakerFrame: layer.speakerFrame, fullScreenWindows: layer.fullScreenWindows,
            layoutWindows: layouts)
        try scene.recipe.validate()
        XCTAssertTrue(scene.recipe.effectiveCapabilities.contains(.speakerSections))
        let output = try await exportSpeakerRecipe(scene.recipe, urls: scene.urls, directory: directory)
        let expected: [(frame: Int, drawn: Drawn)] = [(1, .own), (29, .own), (30, .small), (59, .small), (60, .picture),
                                                      (89, .picture), (90, .full), (110, .full)]
        for (index, want) in expected {
            let time = Double(index) / 30 + 0.001
            let exported = drawn(try await fileFrame(output, at: time))
            let previewed = drawn(try await previewFrame(scene.recipe, urls: scene.urls, at: time))
            XCTAssertEqual(exported, want.rawValue, "export frame \(index)")
            XCTAssertEqual(previewed, want.rawValue, "preview frame \(index)")
        }
    }

    /// A full-screen window (Qendresa item 16) wins over a layout window; outside it the layout window draws.
    @MainActor func testAFullScreenWindowWinsOverALayoutWindow() async throws {
        useSpeakerSegmenter(self, GreenPersonSegmenter())
        let directory = try speakerTestDirectory(self)
        let speaker = try await writeSpeakerClip(directory: directory, size: (90, 160), seconds: 2)
        let background = try solidSpeakerTestImage(directory: directory)
        var scene = try speakerRecipe(canvas: Canvas(width: 90, height: 160), speaker: speaker, duration: 2,
                                      background: SpeakerTestBackground(url: background))
        let layer = try XCTUnwrap(scene.recipe.speakerBackground)
        scene.recipe.speakerBackground = SpeakerBackgroundLayer(assetID: layer.assetID, mediaKind: layer.mediaKind,
            fullScreenWindows: [SpeakerFullScreenWindow(start: 0, end: 1)],
            layoutWindows: [SpeakerLayoutWindow(start: 0, end: 2, layout: .picture)])
        try scene.recipe.validate()
        let output = try await exportSpeakerRecipe(scene.recipe, urls: scene.urls, directory: directory)
        let opening = drawn(try await fileFrame(output, at: 0.5)), after = drawn(try await fileFrame(output, at: 1.5))
        XCTAssertEqual(opening, Drawn.own.rawValue)
        XCTAssertEqual(after, Drawn.picture.rawValue)
    }

    func testTheWireRejectsWhatThePhoneCannotDraw() throws {
        func decode(_ json: String) throws -> SpeakerBackgroundLayer {
            let decoder = JSONDecoder(); decoder.keyDecodingStrategy = .convertFromSnakeCase
            return try decoder.decode(SpeakerBackgroundLayer.self, from: Data(json.utf8))
        }
        let base = #"{"asset_id":"a","media_kind":"image","layout_windows":%@}"#
        let frame = #"{"width_fraction":0.42,"x_fraction":0.76,"y_fraction":0.8}"#
        XCTAssertNoThrow(try decode(String(format: base, #"[{"start":0,"end":1,"layout":"small","frame":\#(frame)}]"#)))
        // `small` needs a frame and only `small` carries one; spans are >= 0.5 s, sorted, disjoint; never empty.
        XCTAssertThrowsError(try decode(String(format: base, #"[{"start":0,"end":1,"layout":"small"}]"#)))
        XCTAssertThrowsError(try decode(String(format: base, #"[{"start":0,"end":1,"layout":"own","frame":\#(frame)}]"#)))
        XCTAssertThrowsError(try decode(String(format: base, #"[{"start":0,"end":0.4,"layout":"own"}]"#)))
        XCTAssertThrowsError(try decode(String(format: base, #"[{"start":0,"end":2,"layout":"own"},{"start":1,"end":3,"layout":"full"}]"#)))
        XCTAssertThrowsError(try decode(String(format: base, "[]")))
        XCTAssertThrowsError(try decode(String(format: base, #"[{"start":0,"end":1,"layout":"hidden"}]"#)))
    }
}
#endif
