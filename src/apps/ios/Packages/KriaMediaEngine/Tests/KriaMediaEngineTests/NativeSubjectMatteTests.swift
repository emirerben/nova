import Foundation
import XCTest
@testable import KriaMediaEngine
#if canImport(AVFoundation)
@preconcurrency import AVFoundation
import CoreImage

// Text behind people on the phone. Each test names the failure mode it pins from
// `docs/reviews/text-behind-subject-phone-failure-modes.md`.

private func confidence(_ width: Int, _ height: Int, columns: Range<Int>? = nil, rows: Range<Int>? = nil,
                        value: Float = 0.9, background: Float = 0.05) -> SubjectConfidenceMap {
    var values = [Float](repeating: background, count: width * height)
    if let columns {
        for y in (rows ?? 0..<height) { for x in columns { values[y * width + x] = value } }
    }
    return SubjectConfidenceMap(width: width, height: height, values: values)
}

final class SubjectMaskTreatmentTests: XCTestCase {
    func testRectangleAboveTheCutIsKeptAndBackgroundDropped() {
        let mask = SubjectMaskTreatment.treat(confidence(40, 30, columns: 0..<20), history: [])
        XCTAssertEqual(mask[15 * 40 + 5], 255)
        XCTAssertEqual(mask[15 * 40 + 30], 0)
        XCTAssertEqual(SubjectMaskTreatment.coverage(mask), 0.5, accuracy: 0.001)
    }

    func testConfidenceIsHardCutAtPointFour() {
        XCTAssertEqual(SubjectMaskTreatment.treat(confidence(20, 20, columns: 0..<20, value: 0.39), history: []).max(), 0)
        XCTAssertEqual(SubjectMaskTreatment.treat(confidence(20, 20, columns: 0..<20, value: 0.4), history: []).min(), 255)
    }

    /// F2: one frame of a person-shaped false positive between two empty frames never reaches the text.
    func testSingleFrameBlipIsRemovedByTheMedian() {
        let empty = confidence(40, 30)
        let blip = confidence(40, 30, columns: 0..<20)
        XCTAssertEqual(SubjectMaskTreatment.treat(blip, history: [empty, empty]).max(), 0)
        // The mirror: a subject that has been there two frames survives one dropped detection.
        XCTAssertEqual(SubjectMaskTreatment.coverage(SubjectMaskTreatment.treat(empty, history: [blip, blip])), 0.5, accuracy: 0.001)
    }

    /// F4: speckle and distant passers-by (< 0.2 % of the frame) are not occluders; a real subject is.
    func testTinyFragmentsAreDropped() {
        var values = [Float](repeating: 0, count: 100 * 100)
        values[50 * 100 + 50] = 1                                   // one pixel
        for y in 10..<30 { for x in 10..<30 { values[y * 100 + x] = 1 } }  // 4 % block
        let mask = SubjectMaskTreatment.treat(SubjectConfidenceMap(width: 100, height: 100, values: values), history: [])
        XCTAssertEqual(mask[50 * 100 + 50], 0)
        XCTAssertEqual(mask[20 * 100 + 20], 255)
        XCTAssertEqual(SubjectMaskTreatment.coverage(mask), 0.04, accuracy: 0.0001)
    }

    func testIoUAndDownsampling() {
        let a = SubjectMaskTreatment.treat(confidence(40, 30, columns: 0..<20), history: [])
        let b = SubjectMaskTreatment.treat(confidence(40, 30, columns: 10..<30), history: [])
        XCTAssertEqual(SubjectMaskTreatment.iou(a, b), 1.0 / 3.0, accuracy: 0.001)
        XCTAssertEqual(SubjectMaskTreatment.iou([0, 0], [0, 0]), 1)
        let small = confidence(400, 300, columns: 0..<200).downsampled(maxPixels: 10_000)
        XCTAssertLessThanOrEqual(small.width * small.height, 10_000)
        XCTAssertEqual(SubjectMaskTreatment.coverage(SubjectMaskTreatment.treat(small, history: [])), 0.5, accuracy: 0.05)
    }
}

final class SubjectMaskStabilityTests: XCTestCase {
    /// F2: a silhouette that blinks on and off every tick is worse than plain text.
    func testStrobingPresenceLatchesUnstable() {
        var stability = SubjectMaskStability()
        for tick in 0..<15 { stability.observe(present: tick % 2 == 0, iouWithPrevious: nil, at: Double(tick) / 15) }
        XCTAssertTrue(stability.unstable)
    }

    /// A subject that walks out once (one flip) is a scene change, not instability.
    func testASingleDepartureIsStable() {
        var stability = SubjectMaskStability()
        for tick in 0..<30 { stability.observe(present: tick < 15, iouWithPrevious: tick < 15 && tick > 0 ? 0.9 : nil, at: Double(tick) / 15) }
        XCTAssertFalse(stability.unstable)
    }

    /// F2 (shape): a mask that stays present but jumps shape every frame latches too.
    func testLargeShapeJumpsLatchUnstable() {
        var steady = SubjectMaskStability(), jumpy = SubjectMaskStability()
        for tick in 0..<10 {
            steady.observe(present: true, iouWithPrevious: tick == 0 ? nil : 0.85, at: Double(tick) / 15)
            jumpy.observe(present: true, iouWithPrevious: tick == 0 ? nil : 0.1, at: Double(tick) / 15)
        }
        XCTAssertFalse(steady.unstable)
        XCTAssertTrue(jumpy.unstable)
        jumpy.reset()
        XCTAssertFalse(jumpy.unstable)
    }
}

/// A segmenter that answers the same map every tick (or throws), standing in for Vision.
private final class FixedSegmenter: NativePersonSegmenting {
    var map: SubjectConfidenceMap?
    var error: Error?
    var calls = 0
    init(map: SubjectConfidenceMap?, error: Error? = nil) { self.map = map; self.error = error }
    func confidence(for frame: CIImage, canvas: CGRect) throws -> SubjectConfidenceMap? {
        calls += 1
        if let error { throw error }
        return map
    }
}

final class SubjectOcclusionTests: XCTestCase {
    private let canvas = CGRect(x: 0, y: 0, width: 200, height: 300)

    private func alpha(_ image: CIImage, at points: [(Int, Int)]) -> [UInt8] {
        let context = CIContext(options: [.workingColorSpace: CGColorSpace(name: CGColorSpace.linearSRGB)!,
                                          .outputColorSpace: CGColorSpace(name: CGColorSpace.sRGB)!])
        var pixels = [UInt8](repeating: 0, count: Int(canvas.width * canvas.height) * 4)
        context.render(image, toBitmap: &pixels, rowBytes: Int(canvas.width) * 4, bounds: canvas, format: .RGBA8, colorSpace: CGColorSpace(name: CGColorSpace.sRGB)!)
        // Bitmap rows are top-down; `points` are given in Core Image (bottom-left) coordinates.
        return points.map { x, y in pixels[((Int(canvas.height) - 1 - y) * Int(canvas.width) + x) * 4 + 3] }
    }

    /// F1: a known rectangle mask zeroes the text alpha inside the rectangle and leaves it alone outside.
    func testRectangleMaskZeroesTextAlphaInsideAndKeepsItOutside() throws {
        let text = CIImage(color: .white).cropped(to: CGRect(x: 20, y: 100, width: 160, height: 60))
        var mask = [UInt8](repeating: 0, count: 20 * 30)
        for y in 0..<30 { for x in 0..<10 { mask[y * 20 + x] = 255 } }  // person on the left half of the canvas
        let maskImage = try XCTUnwrap(SubjectOcclusion.maskImage(mask, width: 20, height: 30, canvas: canvas))
        let occluded = SubjectOcclusion.apply(to: text, mask: maskImage)
        let values = alpha(occluded, at: [(40, 130), (60, 110), (160, 130), (140, 150)])
        XCTAssertEqual(values[0], 0); XCTAssertEqual(values[1], 0)
        XCTAssertEqual(values[2], 255); XCTAssertEqual(values[3], 255)
        // The plain text is untouched where the person is not.
        XCTAssertEqual(alpha(text, at: [(40, 130)]), [255])
    }

    /// F11: a half-confidence mask keeps about half the alpha. If the mask were colour-managed into the
    /// linear working space, 0.5 would read as ~0.21 and the feather/edges would bite far too deep.
    func testHalfMaskGivesHalfAlphaNotALinearisedFifth() throws {
        let text = CIImage(color: .white).cropped(to: canvas)
        let maskImage = try XCTUnwrap(SubjectOcclusion.maskImage([UInt8](repeating: 128, count: 20 * 30), width: 20, height: 30, canvas: canvas))
        let value = Int(alpha(SubjectOcclusion.apply(to: text, mask: maskImage), at: [(100, 150)])[0])
        XCTAssertTrue((105...150).contains(value), "half mask gave alpha \(value)")
    }

    /// F3, F5, F6, F13: every "unsane" answer paints plain text; the mask is rate-limited and held between ticks.
    func testMatteFallsBackToPlainTextForUnsaneMasksAndHoldsBetweenTicks() {
        let scene = CIImage(color: .red).cropped(to: canvas)
        let person = FixedSegmenter(map: confidence(20, 30, columns: 0..<10))
        let matte = NativeSubjectMatte(segmenter: person)
        XCTAssertNotNil(matte.mask(for: scene, at: 1.0, canvas: canvas, cutKey: "a"))
        XCTAssertNotNil(matte.mask(for: scene, at: 1.02, canvas: canvas, cutKey: "a"))  // same 15 Hz tick: held
        XCTAssertEqual(person.calls, 1)
        XCTAssertEqual(matte.statistics.heldFrames, 1)
        _ = matte.mask(for: scene, at: 1.1, canvas: canvas, cutKey: "a")
        XCTAssertEqual(person.calls, 2)
        // A hard cut resets the temporal state (history, flips, held mask) and segments again.
        _ = matte.mask(for: scene, at: 1.1, canvas: canvas, cutKey: "b")
        XCTAssertEqual(matte.statistics.resets, 2)  // first use + the cut
        // A seek far away also resets (F6).
        _ = matte.mask(for: scene, at: 5.0, canvas: canvas, cutKey: "b")
        XCTAssertEqual(matte.statistics.resets, 3)

        XCTAssertNil(NativeSubjectMatte(segmenter: FixedSegmenter(map: confidence(20, 30, columns: 0..<20, value: 1)))
            .mask(for: scene, at: 0, canvas: canvas, cutKey: "a"), "a mask that swallows the frame paints plain text (F3)")
        XCTAssertNil(NativeSubjectMatte(segmenter: FixedSegmenter(map: confidence(20, 30)))
            .mask(for: scene, at: 0, canvas: canvas, cutKey: "a"), "nobody in frame (F5)")
        XCTAssertNil(NativeSubjectMatte(segmenter: FixedSegmenter(map: nil, error: MediaEngineError.exportFailed))
            .mask(for: scene, at: 0, canvas: canvas, cutKey: "a"), "a segmenter error never fails the frame (F13)")
        XCTAssertNil(NativeSubjectMatte(segmenter: nil).mask(for: scene, at: 0, canvas: canvas, cutKey: "a"))
    }

    /// F2 end to end: strobing detections latch the cut to plain text, and the latch clears at the next cut.
    func testStrobingDetectionsLatchTheCutToPlainText() {
        let scene = CIImage(color: .red).cropped(to: canvas)
        let segmenter = FixedSegmenter(map: nil)
        let matte = NativeSubjectMatte(segmenter: segmenter)
        let present = confidence(20, 30, columns: 0..<10), absent = confidence(20, 30)
        // Two ticks on, two off: a strobe the 3-frame median cannot smooth away (a 1-in-3 blip it does, see
        // `testSingleFrameBlipIsRemovedByTheMedian`), so presence flips every other tick for a second.
        for tick in 0..<30 {
            segmenter.map = tick % 4 < 2 ? present : absent
            _ = matte.mask(for: scene, at: Double(tick) / 15, canvas: canvas, cutKey: "a")
        }
        segmenter.map = present
        for tick in 30..<36 { _ = matte.mask(for: scene, at: Double(tick) / 15, canvas: canvas, cutKey: "a") }
        XCTAssertNil(matte.mask(for: scene, at: 36.0 / 15, canvas: canvas, cutKey: "a"), "latched for the rest of the cut")
        for tick in 40..<43 { _ = matte.mask(for: scene, at: Double(tick) / 15, canvas: canvas, cutKey: "b") }
        XCTAssertNotNil(matte.mask(for: scene, at: 43.0 / 15, canvas: canvas, cutKey: "b"), "the next cut starts clean")
    }
}

/// The REAL build path: `AVPlayerPreviewComposer` -> `RecipeVideoCompositor`, sampled through
/// `AVAssetImageGenerator` (preview) and `AVFoundationLocalExporter` (export).
final class NativeSubjectMatteCompositionTests: XCTestCase {
    private let width = 270, height = 480

    private struct Scene {
        let directory: URL
        let urls: [String: URL]
        let recipe: EditRecipe
    }

    @MainActor private func scene(behindSubject: Bool?) throws -> Scene {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let root = try XCTUnwrap(#filePath.range(of: "/src/apps/ios/"))
        let font = URL(fileURLWithPath: String(#filePath[..<root.lowerBound])).appendingPathComponent("src/apps/api/assets/fonts/Inter-Bold.ttf")
        let photo = directory.appendingPathComponent("red.png")
        try CIContext().writePNGRepresentation(of: CIImage(color: .red).cropped(to: CGRect(x: 0, y: 0, width: width, height: height)),
            to: photo, format: .RGBA8, colorSpace: CGColorSpace(name: CGColorSpace.sRGB)!)
        let urls = ["photo": photo, "font-Inter-Bold.ttf": font]
        let assets = try urls.map { MediaAsset(id: $0.key, relativePath: $0.key, fingerprint: try SHA256Fingerprinter().fingerprint(file: $0.value)) }
        let references = try assets.map { asset in
            RenderAssetReference(id: asset.id, fingerprint: try RenderFingerprint(asset.fingerprint!),
                source: asset.id == "photo" ? .original(mediaID: "photo") : .library(catalog: .font, catalogID: "Inter-Bold.ttf", generation: asset.fingerprint!.hex))
        }
        var layer = try AuthoredTextLayout.compile(id: "title", text: "WWWWWWWW", start: 0, end: 3,
            style: .init(fontAssetID: "font-Inter-Bold.ttf", size: 44, widthFraction: 0.98,
                         color: .init(red: 1, green: 1, blue: 1, alpha: 1), strokeWidth: 0, shadows: []),
            fontURL: font, canvas: Canvas(width: width, height: height))
        layer.behindSubject = behindSubject
        let recipe = EditRecipe(schemaVersion: 2, rendererVersion: "kria-ios-2", canvas: Canvas(width: width, height: height), assets: assets,
            tracks: [TimelineTrack(id: "v", kind: .video, clips: [TimelineClip(id: "c", sourceAssetID: "photo", sourceDuration: 3)])],
            assetManifest: RenderAssetManifest(assets: references), textLayers: [layer])
        return Scene(directory: directory, urls: urls, recipe: recipe)
    }

    /// Text ink (anything not the red footage) on each side of the canvas, leaving a 15 px band either side
    /// of the middle alone: the mask is feathered by a few canvas pixels around its edge at x = 135.
    private func textPixelsPerHalf(_ image: CGImage) -> (left: Int, right: Int) {
        var pixels = [UInt8](repeating: 0, count: width * height * 4)
        CIContext().render(CIImage(cgImage: image), toBitmap: &pixels, rowBytes: width * 4, bounds: CGRect(x: 0, y: 0, width: width, height: height),
                           format: .RGBA8, colorSpace: CGColorSpace(name: CGColorSpace.sRGB)!)
        var left = 0, right = 0
        for y in 0..<height {
            for x in 0..<width {
                let offset = (y * width + x) * 4
                // Red footage has ~0 green; any text ink (white) raises green well above that.
                guard pixels[offset + 1] > 80 else { continue }
                if x < width / 2 - 15 { left += 1 } else if x > width / 2 + 15 { right += 1 }
            }
        }
        return (left, right)
    }

    @MainActor private func sample(_ scene: Scene, at time: Double) async throws -> CGImage {
        let preview = try await AVPlayerPreviewComposer().makePreview(recipe: scene.recipe, assetURLs: scene.urls)
        let generator = AVAssetImageGenerator(asset: preview.playerItem.asset)
        generator.videoComposition = preview.playerItem.videoComposition
        generator.requestedTimeToleranceBefore = .zero; generator.requestedTimeToleranceAfter = .zero
        return try await generator.image(at: CMTime(seconds: time, preferredTimescale: 600)).image
    }

    /// F1 + F8: with a person over the left half of the canvas, the title's left half disappears behind them
    /// in the preview composition AND in the exported file, while the right half still shows.
    @MainActor func testPersonOnTheLeftHidesTheLeftHalfOfTheTitleInPreviewAndExport() async throws {
        let previous = NativeSubjectMatte.segmenterFactory
        defer { NativeSubjectMatte.segmenterFactory = previous }
        // A person over exactly the left half of the canvas (x < 135 of 270).
        NativeSubjectMatte.segmenterFactory = { FixedSegmenter(map: confidence(30, 48, columns: 0..<15)) }

        let plain = try scene(behindSubject: nil)
        defer { try? FileManager.default.removeItem(at: plain.directory) }
        let occluded = try scene(behindSubject: true)
        defer { try? FileManager.default.removeItem(at: occluded.directory) }
        XCTAssertTrue(occluded.recipe.effectiveCapabilities.contains(.behindSubject))
        XCTAssertFalse(plain.recipe.effectiveCapabilities.contains(.behindSubject))

        let plainCounts = textPixelsPerHalf(try await sample(plain, at: 1.0))
        XCTAssertGreaterThan(plainCounts.left, 200, "the plain title spans the left half")
        XCTAssertGreaterThan(plainCounts.right, 200, "the plain title spans the right half")

        let previewCounts = textPixelsPerHalf(try await sample(occluded, at: 1.0))
        XCTAssertLessThan(previewCounts.left, plainCounts.left / 20, "the title is hidden behind the person on the left")
        XCTAssertGreaterThan(previewCounts.right, plainCounts.right * 8 / 10, "the right half still shows the title")

        let output = occluded.directory.appendingPathComponent("behind.mp4")
        _ = try await AVFoundationLocalExporter(stateStore: FileExportStateStore(directory: occluded.directory.appendingPathComponent("state")), branding: .none)
            .export(recipe: occluded.recipe, assetURLs: occluded.urls, outputURL: output)
        let exported = AVAssetImageGenerator(asset: AVURLAsset(url: output))
        exported.requestedTimeToleranceBefore = .zero; exported.requestedTimeToleranceAfter = .zero
        let exportCounts = textPixelsPerHalf(try await exported.image(at: CMTime(seconds: 1.0, preferredTimescale: 600)).image)
        XCTAssertLessThan(exportCounts.left, plainCounts.left / 20, "export hides the same half")
        XCTAssertGreaterThan(exportCounts.right, plainCounts.right * 7 / 10, "export keeps the other half")
    }

    /// F3 + F13: a mask that swallows the frame, or a segmenter that fails, paints the plain title.
    @MainActor func testUnsaneMaskPaintsThePlainTitle() async throws {
        let previous = NativeSubjectMatte.segmenterFactory
        defer { NativeSubjectMatte.segmenterFactory = previous }

        let plain = try scene(behindSubject: nil)
        defer { try? FileManager.default.removeItem(at: plain.directory) }
        let plainCounts = textPixelsPerHalf(try await sample(plain, at: 1.0))

        for segmenter in [FixedSegmenter(map: confidence(27, 48, columns: 0..<27, value: 1)),
                          FixedSegmenter(map: nil, error: MediaEngineError.exportFailed)] {
            NativeSubjectMatte.segmenterFactory = { segmenter }
            let occluded = try scene(behindSubject: true)
            defer { try? FileManager.default.removeItem(at: occluded.directory) }
            let counts = textPixelsPerHalf(try await sample(occluded, at: 1.0))
            XCTAssertEqual(counts.left, plainCounts.left)
            XCTAssertEqual(counts.right, plainCounts.right)
        }
    }

    func testBehindSubjectRoundTripsTheWireAndRefusesAGiantTitle() throws {
        let run = PositionedTextRun(text: "Hi", fontAssetID: "font", fontSize: 40, x: 0, baselineY: 40, letterSpacing: 0, shaped: false,
            fill: TextInk(red: 1, green: 1, blue: 1, alpha: 1), stroke: TextInk(red: 0, green: 0, blue: 0, alpha: 1), strokeWidth: 0)
        let layer = PortableTextLayer(id: "t", start: 0, end: 1, anchorX: 0, anchorY: 0, rotationDegrees: 0, runs: [run], behindSubject: true)
        let data = try RecipeJSON.encoder().encode(layer)
        let json = try XCTUnwrap(String(data: data, encoding: .utf8))
        XCTAssertTrue(json.contains("\"behind_subject\":true"))
        XCTAssertEqual(try RecipeJSON.decoder().decode(PortableTextLayer.self, from: data).behindSubject, true)
        let plain = PortableTextLayer(id: "t", start: 0, end: 1, anchorX: 0, anchorY: 0, rotationDegrees: 0, runs: [run])
        XCTAssertFalse(try XCTUnwrap(String(data: RecipeJSON.encoder().encode(plain), encoding: .utf8)).contains("behind_subject"))
        let giant = PortableTextLayer(id: "t", start: 0, end: 1, anchorX: 0, anchorY: 0, rotationDegrees: 0, runs: [run],
            giantTitle: GiantTitleTransition(originX: 0, originY: 0), behindSubject: true)
        XCTAssertThrowsError(try giant.validate(duration: 2, manifest: nil))
    }
}
#endif
