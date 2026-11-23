import CoreVideo
import IOSurface
import XCTest
@testable import Kria

/// Failure modes of handing Vision a smaller frame, written before the scaler:
/// 1. the aspect changes, so the mask no longer lines up with the camera frame it is stretched over;
/// 2. a frame that is already small gets blown up, or the scaler fails on it;
/// 3. the rows flip or the picture mirrors, so the mask is upside down against the camera;
/// 4. a frame size change mid-session keeps drawing into buffers of the old size;
/// 5. buffers pile up: an output stays alive after it was given back, a frame finds no buffer ready although the
///    last one was given back, or nothing limits how many exist when the caller keeps them;
/// 6. the scaler cannot render and the segmentation fails instead of falling back to the full frame.
final class SegmentationInputScalerTests: XCTestCase {
    private func frame(width: Int, height: Int, topLeftRed: Bool = true) -> CVPixelBuffer {
        var buffer: CVPixelBuffer?
        CVPixelBufferCreate(nil, width, height, kCVPixelFormatType_32BGRA, [kCVPixelBufferIOSurfacePropertiesKey: [:]] as CFDictionary, &buffer)
        let pixels = buffer!
        CVPixelBufferLockBaseAddress(pixels, [])
        defer { CVPixelBufferUnlockBaseAddress(pixels, []) }
        let base = CVPixelBufferGetBaseAddress(pixels)!.assumingMemoryBound(to: UInt8.self)
        let stride = CVPixelBufferGetBytesPerRow(pixels)
        for y in 0..<height {
            for x in 0..<width {
                let red = topLeftRed && x < width / 2 && y < height / 2
                let i = y * stride + x * 4
                base[i] = 0; base[i + 1] = 0; base[i + 2] = red ? 255 : 0; base[i + 3] = 255
            }
        }
        return pixels
    }

    /// The memory behind a buffer. The pool hands a reused surface out inside a new `CVPixelBuffer` object, so an
    /// object's address follows the allocator and changes with timing. Surface ids are reused too once a surface is
    /// freed, so counting them shows buffers that stay alive, not whether one was reused or made again (KRI-1599).
    private func surface(_ buffer: CVPixelBuffer) -> IOSurfaceID {
        CVPixelBufferGetIOSurface(buffer).map { IOSurfaceGetID($0.takeUnretainedValue()) } ?? 0
    }

    private func red(_ buffer: CVPixelBuffer, x: Int, y: Int) -> Int {
        CVPixelBufferLockBaseAddress(buffer, .readOnly)
        defer { CVPixelBufferUnlockBaseAddress(buffer, .readOnly) }
        let base = CVPixelBufferGetBaseAddress(buffer)!.assumingMemoryBound(to: UInt8.self)
        return Int(base[y * CVPixelBufferGetBytesPerRow(buffer) + x * 4 + 2])
    }

    func testAPortraitCameraFrameKeepsItsAspectAndTheMasksShortSide() throws {
        let size = try XCTUnwrap(SegmentationInputScaler.targetSize(for: CGSize(width: 1080, height: 1920)))
        XCTAssertEqual(size.width, CGFloat(LiveSpeakerMatteConfig.segmentationShortSide))
        XCTAssertEqual(size.width / size.height, 1080.0 / 1920.0, accuracy: 0.005)
        XCTAssertEqual(size.height.truncatingRemainder(dividingBy: 2), 0)
        let landscape = try XCTUnwrap(SegmentationInputScaler.targetSize(for: CGSize(width: 1920, height: 1080)))
        XCTAssertEqual(landscape.height, CGFloat(LiveSpeakerMatteConfig.segmentationShortSide))
    }

    func testAFrameThatIsAlreadySmallIsLeftAlone() {
        XCTAssertNil(SegmentationInputScaler.targetSize(for: CGSize(width: 36, height: 64)))
        XCTAssertNil(SegmentationInputScaler.targetSize(for: CGSize(width: 384, height: 512)))
        let small = frame(width: 36, height: 64)
        XCTAssertTrue(SegmentationInputScaler().scaled(small) === small, "the same buffer comes back, nothing is rendered")
    }

    func testTheScaledFrameIsTheSamePictureTheSameWayUp() {
        let scaler = SegmentationInputScaler()
        let scaled = scaler.scaled(frame(width: 540, height: 960))
        XCTAssertEqual(CVPixelBufferGetWidth(scaled), 384)
        XCTAssertEqual(CVPixelBufferGetHeight(scaled), 682)
        XCTAssertGreaterThan(red(scaled, x: 20, y: 20), 200, "the marker is still top-left (row 0 is the top)")
        XCTAssertLessThan(red(scaled, x: 360, y: 20), 50, "and not mirrored to the top-right")
        XCTAssertLessThan(red(scaled, x: 20, y: 660), 50, "and not flipped to the bottom-left")
    }

    func testAChangeOfFrameSizeRendersAtTheNewSizeAndBuffersAreRecycled() {
        let scaler = SegmentationInputScaler()
        XCTAssertEqual(CVPixelBufferGetHeight(scaler.scaled(frame(width: 540, height: 960))), 682)
        XCTAssertEqual(CVPixelBufferGetWidth(scaler.scaled(frame(width: 1080, height: 810))), 512)
        // Each output is given back before the next frame, as on the frame queue.
        var alive = Set<IOSurfaceID>()
        let input = frame(width: 540, height: 960)
        for index in 0..<30 {
            let output = scaler.scaled(input)
            XCTAssertFalse(output === input, "frame \(index) is scaled: the buffer given back is ready for it")
            alive.insert(surface(output))
        }
        XCTAssertFalse(alive.contains(0), "every output is backed by a surface")
        XCTAssertLessThanOrEqual(alive.count, 8, "outputs that were given back do not stay alive (30 renders)")
    }

    func testOutputsTheCallerKeepsStopAtAHandfulAndAReleasedOneIsUsedAgain() {
        let scaler = SegmentationInputScaler()
        let input = frame(width: 540, height: 960)
        var kept: [CVPixelBuffer] = []
        for _ in 0..<30 { kept.append(scaler.scaled(input)) }
        let pooled = Set(kept.filter { $0 !== input }.map(surface))
        XCTAssertGreaterThan(pooled.count, 0, "the first frames are scaled")
        XCTAssertLessThanOrEqual(pooled.count, 8, "kept outputs cannot grow without limit (30 renders, none given back)")
        XCTAssertEqual(kept.filter { $0 === input }.count, 30 - pooled.count, "every kept output has its own memory, and past the limit the camera frame itself comes back")
        kept.removeAll()
        XCTAssertFalse(scaler.scaled(input) === input, "once outputs are given back, frames are scaled again")
    }
}
