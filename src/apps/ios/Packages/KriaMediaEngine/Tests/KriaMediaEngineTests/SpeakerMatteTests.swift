#if canImport(AVFoundation)
import Foundation
import XCTest
import CoreImage
@testable import KriaMediaEngine

/// The speaker matte's answers, driven through the `NativePersonSegmenting` seam with synthetic
/// confidence maps. Each test names its row in `docs/reviews/speaker-background-failure-modes.md`.
final class SpeakerMatteTests: XCTestCase {
    private let canvas = CGRect(x: 0, y: 0, width: 120, height: 200)
    private let frame = 1.0 / 30
    private var speaker: CIImage { CIImage(color: .white).cropped(to: canvas) }
    private let leftHalf = speakerConfidence(120, 200, columns: 0..<60)
    private let rightHalf = speakerConfidence(120, 200, columns: 60..<120)

    /// The cut-out speaker's alpha (0...255) at canvas points, y counted from the top.
    private func alpha(_ answer: SpeakerMatte.Answer, at points: [(Int, Int)]) throws -> [Int] {
        guard case .cutout(let mask) = answer else {
            XCTFail("expected a cut-out, got \(answer)")
            return []
        }
        let cut = try XCTUnwrap(SpeakerMatte.cutOut(speaker, mask: mask, canvas: canvas, config: .shipping))
        let width = Int(canvas.width), height = Int(canvas.height)
        var pixels = [UInt8](repeating: 0, count: width * height * 4)
        CIContext(options: [.workingColorSpace: CGColorSpace(name: CGColorSpace.linearSRGB)!])
            .render(cut, toBitmap: &pixels, rowBytes: width * 4, bounds: canvas, format: .RGBA8, colorSpace: CGColorSpace(name: CGColorSpace.sRGB)!)
        return points.map { x, y in Int(pixels[(y * width + x) * 4 + 3]) }
    }

    private func isOriginal(_ answer: SpeakerMatte.Answer) -> Bool { if case .original = answer { true } else { false } }
    private func isBackgroundOnly(_ answer: SpeakerMatte.Answer) -> Bool { if case .backgroundOnly = answer { true } else { false } }

    /// The cut-out alpha along a row across a confidence ramp from 0 (left) to 1 (right).
    private func rampRow(_ config: SpeakerMatteConfig) throws -> [Int] {
        let ramp = SubjectConfidenceMap(width: 120, height: 4, values: (0..<480).map { Float($0 % 120) / 119 })
        let answer = SpeakerMatte(segmenter: ScriptedSegmenter(map: ramp), config: config).answer(for: speaker, at: 1, canvas: canvas, clipKey: "clip-0")
        guard case .cutout(let mask) = answer else {
            XCTFail("expected a cut-out, got \(answer)")
            return []
        }
        let cut = try XCTUnwrap(SpeakerMatte.cutOut(speaker, mask: mask, canvas: canvas, config: config))
        var pixels = [UInt8](repeating: 0, count: 120 * 200 * 4)
        CIContext(options: [.workingColorSpace: CGColorSpace(name: CGColorSpace.linearSRGB)!])
            .render(cut, toBitmap: &pixels, rowBytes: 120 * 4, bounds: canvas, format: .RGBA8, colorSpace: CGColorSpace(name: CGColorSpace.sRGB)!)
        return (0..<120).map { Int(pixels[(100 * 120 + $0) * 4 + 3]) }
    }

    /// S1: confidence keeps a soft edge. A ramp from 0 to 1 across the frame comes out as alpha 0 below
    /// the curve's low edge (0.35), 255 above its high edge (0.65) and a smooth, rising run of intermediate
    /// values between: no hard cut.
    func testSoftEdgeKeepsIntermediateAlpha() throws {
        let row = try rampRow(.shipping)
        XCTAssertEqual(row.count, 120)
        for x in 0..<120 {
            let confidence = Double(x) / 119
            if confidence <= 0.33 { XCTAssertLessThanOrEqual(row[x], 2, "x \(x) is below the edge") }
            if confidence >= 0.67 { XCTAssertGreaterThanOrEqual(row[x], 253, "x \(x) is inside the speaker") }
        }
        XCTAssertEqual(Double(row[60]), 128, accuracy: 20, "confidence 0.5 is half alpha")
        XCTAssertEqual(row, row.sorted(), "alpha rises with confidence")
        XCTAssertGreaterThanOrEqual(Set(row.filter { $0 > 0 && $0 < 255 }).count, 20, "a soft edge, not a hard cut")
    }

    /// S1: the steep curve is applied and is what narrows the edge. Without it (edges at 0 and 1) the
    /// translucent band spans the whole ramp, the wide pale halo the spike showed; with the shipping curve it
    /// spans about the 0.35...0.65 confidence band.
    func testEdgeCurveNarrowsTheSoftEdge() throws {
        var flat = SpeakerMatteConfig.shipping
        flat.edgeLow = 0
        flat.edgeHigh = 1
        let curved = try rampRow(.shipping).filter { $0 > 2 && $0 < 253 }.count
        let wide = try rampRow(flat).filter { $0 > 2 && $0 < 253 }.count
        XCTAssertEqual(Double(curved), 120 * 0.3, accuracy: 8, "the shipping curve's soft band")
        XCTAssertGreaterThanOrEqual(wide, curved * 5 / 2, "without the curve the soft band is far wider")
    }

    /// S2: every output frame is segmented and answers its own mask, so a moving speaker never steps.
    /// A 15 Hz hold would answer odd frames with the even frames' silhouette.
    func testEveryFrameIsSegmented() throws {
        let segmenter = ScriptedSegmenter { [leftHalf, rightHalf] call in call % 2 == 0 ? leftHalf : rightHalf }
        let matte = SpeakerMatte(segmenter: segmenter)
        for index in 0..<30 {
            let values = try alpha(matte.answer(for: speaker, at: 1 + Double(index) * frame, canvas: canvas, clipKey: "clip-0"),
                                   at: [(20, 100), (100, 100)])
            XCTAssertEqual(values, index % 2 == 0 ? [255, 0] : [0, 255], "frame \(index)")
        }
        XCTAssertEqual(segmenter.calls, 30)
    }

    /// S2 fallback: with a lower mask rate (the device may force 15 Hz) each tick is segmented once and
    /// the frames inside it hold that tick's mask; never a cross-fade between ticks.
    func testLowerMaskRateHoldsBetweenTicks() throws {
        var config = SpeakerMatteConfig.shipping
        config.maskRate = 15
        let segmenter = ScriptedSegmenter { [leftHalf, rightHalf] call in call % 2 == 0 ? leftHalf : rightHalf }
        let matte = SpeakerMatte(segmenter: segmenter, config: config)
        for index in 0..<30 {
            let values = try alpha(matte.answer(for: speaker, at: 1 + Double(index) * frame, canvas: canvas, clipKey: "clip-0"),
                                   at: [(20, 100), (100, 100)])
            XCTAssertEqual(values, (index / 2) % 2 == 0 ? [255, 0] : [0, 255], "frame \(index) holds its tick's mask")
        }
        XCTAssertEqual(segmenter.calls, 15)
    }

    /// S3: a throwing frame holds the last good mask for the grace window, then the frame renders
    /// without the background. Never a missing speaker, never an exception.
    func testErrorHoldsLastGoodMask() throws {
        let matte = SpeakerMatte(segmenter: ScriptedSegmenter { [leftHalf] call in
            if call == 0 { return leftHalf }
            throw MediaEngineError.exportFailed
        })
        XCTAssertEqual(try alpha(matte.answer(for: speaker, at: 1, canvas: canvas, clipKey: "clip-0"), at: [(20, 100), (100, 100)]), [255, 0])
        for index in 1...14 {
            let held = matte.answer(for: speaker, at: 1 + Double(index) * frame, canvas: canvas, clipKey: "clip-0")
            XCTAssertEqual(try alpha(held, at: [(20, 100), (100, 100)]), [255, 0], "frame \(index) holds the last good mask")
        }
        for index in 16...20 {
            XCTAssertTrue(isOriginal(matte.answer(for: speaker, at: 1 + Double(index) * frame, canvas: canvas, clipKey: "clip-0")),
                          "frame \(index) is past the \(SpeakerMatteConfig.shipping.holdGrace) s grace: original footage")
        }
    }

    /// S3: a matte that never produced a mask renders the original footage: errors, empty answers, no segmenter.
    func testNeverProducedMaskRendersOriginal() {
        let failing = SpeakerMatte(segmenter: ScriptedSegmenter { _ in throw MediaEngineError.exportFailed })
        for index in 0..<5 {
            XCTAssertTrue(isOriginal(failing.answer(for: speaker, at: Double(index) * frame, canvas: canvas, clipKey: "clip-0")))
        }
        XCTAssertTrue(isOriginal(SpeakerMatte(segmenter: ScriptedSegmenter(map: nil)).answer(for: speaker, at: 0, canvas: canvas, clipKey: "clip-0")))
        XCTAssertTrue(isOriginal(SpeakerMatte(segmenter: nil).answer(for: speaker, at: 0, canvas: canvas, clipKey: "clip-0")))
    }

    /// S4: nobody in frame is a legitimate answer: the background alone, at once, with no grace hold.
    func testEmptyMaskShowsBackgroundOnly() throws {
        let empty = speakerConfidence(120, 200, columns: 0..<0)
        let matte = SpeakerMatte(segmenter: ScriptedSegmenter { [leftHalf] call in call == 0 ? leftHalf : empty })
        XCTAssertEqual(try alpha(matte.answer(for: speaker, at: 1, canvas: canvas, clipKey: "clip-0"), at: [(20, 100)]), [255])
        XCTAssertTrue(isBackgroundOnly(matte.answer(for: speaker, at: 1 + frame, canvas: canvas, clipKey: "clip-0")))
    }

    /// S5: a mask over the coverage maximum is unreliable. It never replaces the frame with the background:
    /// with a good mask before it the matte holds that one, without one the frame stays original.
    func testFullFrameMaskIsUnreliable() throws {
        let full = speakerConfidence(120, 200, columns: 0..<120)
        XCTAssertTrue(isOriginal(SpeakerMatte(segmenter: ScriptedSegmenter(map: full)).answer(for: speaker, at: 1, canvas: canvas, clipKey: "clip-0")))
        let matte = SpeakerMatte(segmenter: ScriptedSegmenter { [leftHalf] call in call == 0 ? leftHalf : full })
        _ = matte.answer(for: speaker, at: 1, canvas: canvas, clipKey: "clip-0")
        XCTAssertEqual(try alpha(matte.answer(for: speaker, at: 1 + frame, canvas: canvas, clipKey: "clip-0"), at: [(20, 100), (100, 100)]), [255, 0])
    }

    /// S6: specks far under 0.2 % of the frame are dropped from the mask; the speaker is kept.
    func testTinyFragmentsAreDropped() throws {
        var map = speakerConfidence(120, 200, columns: 10..<60, rows: 50..<200)
        for y in 20..<22 { for x in 100..<102 { map = map.setting(x, y, 1) } }
        let values = try alpha(SpeakerMatte(segmenter: ScriptedSegmenter(map: map)).answer(for: speaker, at: 1, canvas: canvas, clipKey: "clip-0"),
                               at: [(100, 20), (101, 21), (35, 120)])
        XCTAssertEqual(values, [0, 0, 255])
    }

    /// S7: a cut to another speaker clip never inherits the previous clip's silhouette.
    func testClipKeyChangeResetsHistory() {
        let matte = SpeakerMatte(segmenter: ScriptedSegmenter { [leftHalf] call in
            if call == 0 { return leftHalf }
            throw MediaEngineError.exportFailed
        })
        _ = matte.answer(for: speaker, at: 1, canvas: canvas, clipKey: "clip-0")
        XCTAssertTrue(isOriginal(matte.answer(for: speaker, at: 1 + frame, canvas: canvas, clipKey: "clip-1")))
    }

    /// S7: a seek (a jump over 0.25 s) drops the held mask even inside the grace window; ordinary
    /// playback steps keep it.
    func testSeekResetsHistory() throws {
        let matte = SpeakerMatte(segmenter: ScriptedSegmenter { [leftHalf] call in
            if call == 0 { return leftHalf }
            throw MediaEngineError.exportFailed
        })
        _ = matte.answer(for: speaker, at: 1, canvas: canvas, clipKey: "clip-0")
        XCTAssertEqual(try alpha(matte.answer(for: speaker, at: 1.1, canvas: canvas, clipKey: "clip-0"), at: [(20, 100)]), [255])
        XCTAssertTrue(isOriginal(matte.answer(for: speaker, at: 1.45, canvas: canvas, clipKey: "clip-0")))
    }

    /// The real Vision path on a real talking-head frame (`Fixtures/speaker-matte-talking.jpg`, a frame of
    /// `src/apps/web/public/plan/type-posters/talking.mp4`), at both qualities the device may ship: the face
    /// is kept, the wall goes, the edge is soft, and the mask is Vision's own buffer, not a downsampled copy.
    /// Skipped where Vision has no person model.
    func testRealVisionCutsOutARealSpeaker() throws {
        let url = try XCTUnwrap(Bundle.module.url(forResource: "speaker-matte-talking", withExtension: "jpg", subdirectory: "Fixtures"))
        let canvas = CGRect(x: 0, y: 0, width: 1080, height: 1920)
        let photo = try XCTUnwrap(CIImage(contentsOf: url))
        let scale = max(canvas.width / photo.extent.width, canvas.height / photo.extent.height)
        let frame = photo.transformed(by: CGAffineTransform(scaleX: scale, y: scale)).cropped(to: canvas)
        for (name, config) in speakerMatteConfigsUnderTest {
            let segmenter = VisionPersonSegmenter()
            let raw: CVPixelBuffer?
            do { raw = try segmenter.personMask(for: frame, canvas: canvas, config: config) } catch { throw XCTSkip("Vision person segmentation unavailable: \(error)") }
            let mask = try XCTUnwrap(raw, name)
            XCTAssertGreaterThanOrEqual(CVPixelBufferGetWidth(mask) * CVPixelBufferGetHeight(mask), 150_000, "\(name): Vision's buffer, never the text matte's 192x256 copy")

            guard case .cutout(let canvasMask) = SpeakerMatte(segmenter: segmenter, config: config).answer(for: frame, at: 0, canvas: canvas, clipKey: "clip-0") else {
                return XCTFail("\(name): a real speaker must be cut out")
            }
            let cut = try XCTUnwrap(SpeakerMatte.cutOut(CIImage(color: .white).cropped(to: canvas), mask: canvasMask, canvas: canvas, config: config))
            let width = Int(canvas.width), height = Int(canvas.height)
            var pixels = [UInt8](repeating: 0, count: width * height * 4)
            CIContext().render(cut, toBitmap: &pixels, rowBytes: width * 4, bounds: canvas, format: .RGBA8, colorSpace: CGColorSpace(name: CGColorSpace.sRGB)!)
            func alpha(fx: Double, fy: Double) -> Int { Int(pixels[(Int(fy * Double(height)) * width + Int(fx * Double(width))) * 4 + 3]) }
            XCTAssertGreaterThanOrEqual(alpha(fx: 0.5, fy: 0.55), 250, "\(name): the face is kept")
            XCTAssertGreaterThanOrEqual(alpha(fx: 0.5, fy: 0.9), 250, "\(name): the shirt is kept")
            XCTAssertLessThanOrEqual(alpha(fx: 0.05, fy: 0.05), 5, "\(name): the ceiling goes")
            XCTAssertLessThanOrEqual(alpha(fx: 0.92, fy: 0.3), 5, "\(name): the wall goes")
            let soft = stride(from: 3, to: pixels.count, by: 4).filter { (16...239).contains(pixels[$0]) }.count
            XCTAssertGreaterThan(soft, 500, "\(name): the silhouette has a soft edge")
        }
    }
}

private extension SubjectConfidenceMap {
    func setting(_ x: Int, _ y: Int, _ value: Float) -> SubjectConfidenceMap {
        var copy = values
        copy[y * width + x] = value
        return SubjectConfidenceMap(width: width, height: height, values: copy)
    }
}
#endif
