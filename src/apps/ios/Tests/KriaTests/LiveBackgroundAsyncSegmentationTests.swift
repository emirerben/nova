import CoreImage
import CoreVideo
import XCTest
@testable import Kria

/// Failure modes of showing the camera frame at once and the mask when it is ready, written before the code:
/// 1. the display waits for Vision, or frames queue up behind a slow mask and are segmented long after they were taken;
/// 2. a mask from an earlier frame is shown without anyone knowing how much earlier;
/// 3. after a pause or a rebuilt session the mask or picture from before it is shown on the first frames after it;
/// 4. a mask computed for pixels of another size or mirroring is stretched over this frame;
/// 5. a stalled segmenter leaves an ever older outline on screen;
/// 6. a failing segmenter blanks the screen instead of showing the raw camera, and never recovers;
/// 7. no background chosen still spends Vision on every frame.
/// Not testable off a phone: camera frames held too long starving the capture pool. Core Image already keeps the last few
/// inputs it rendered (measured here: a pool of 4 buffers runs dry on frame 4 or 5 on the simulator with either path), so
/// the device run counts `talk.live.dropped.OutOfBuffers`.
final class LiveBackgroundAsyncSegmentationTests: XCTestCase {
    /// Answers with a small mask, once `open()` has been called; counts how many frames it was asked about.
    private final class GatedSegmenter: PersonSegmenting, @unchecked Sendable {
        private let lock = NSLock()
        private var gate = DispatchSemaphore(value: 0)
        private var isOpen = false
        private(set) var calls = 0
        var fails = false
        struct Failure: Error {}

        func close() { lock.lock(); isOpen = false; gate = DispatchSemaphore(value: 0); lock.unlock() }
        func open() { lock.lock(); isOpen = true; let gate = gate; lock.unlock(); gate.signal() }

        func mask(for frame: CVPixelBuffer) throws -> CVPixelBuffer {
            lock.lock(); calls += 1; let wait = !isOpen; let gate = gate; let failing = fails; lock.unlock()
            if wait { gate.wait() }
            if failing { throw Failure() }
            var buffer: CVPixelBuffer?
            CVPixelBufferCreate(nil, 6, 8, kCVPixelFormatType_OneComponent8, [kCVPixelBufferIOSurfacePropertiesKey: [:]] as CFDictionary, &buffer)
            return buffer!
        }
    }

    private final class Backdrop: LiveBackgroundProviding, @unchecked Sendable {
        func frame() -> CIImage? { CIImage(color: .blue).cropped(to: CGRect(origin: .zero, size: LiveSpeakerMatteConfig.previewSize)) }
        func setActive(_ active: Bool) {}
        func restart() {}
        func stop() {}
    }

    private func camera(width: Int = 36, height: Int = 64) -> CVPixelBuffer {
        var buffer: CVPixelBuffer?
        CVPixelBufferCreate(nil, width, height, kCVPixelFormatType_32BGRA, [kCVPixelBufferIOSurfacePropertiesKey: [:]] as CFDictionary, &buffer)
        return buffer!
    }

    private func preview(_ segmenter: GatedSegmenter, withBackground: Bool = true) -> LiveBackgroundPreview {
        let preview = LiveBackgroundPreview(segmenter: segmenter, tuning: LiveBackgroundTuning())
        if withBackground { preview.setBackground(Backdrop()) }
        return preview
    }

    func testTheFrameIsDrawnWhileTheMaskIsBeingComputedAndNoStaleFrameIsQueued() {
        let segmenter = GatedSegmenter(), preview = preview(segmenter)
        segmenter.open()
        preview.consume(camera(), mirrored: true)
        preview.waitForSegmentation()
        XCTAssertEqual(preview.statistics.framesAwaitingMask, 1, "nothing to draw under the very first frame yet")

        segmenter.close()
        preview.consume(camera(), mirrored: true)
        XCTAssertEqual(preview.statistics.lastMaskOffsetFrames, 1, "drawn at once, with the mask of the frame before it")
        preview.consume(camera(), mirrored: true)
        preview.consume(camera(), mirrored: true)
        XCTAssertEqual(preview.statistics.lastMaskOffsetFrames, 3, "and the offset grows while the segmenter is busy")
        XCTAssertEqual(segmenter.calls, 2, "frames 3 and 4 were not queued behind frame 2")
        XCTAssertEqual(preview.statistics.framesMatted, 3)

        segmenter.open()
        preview.waitForSegmentation()
        preview.consume(camera(), mirrored: true)
        XCTAssertEqual(preview.statistics.lastMaskOffsetFrames, 3, "frame 5 wears the mask of frame 2, not of 3 or 4")
        XCTAssertEqual(segmenter.calls, 3)
    }

    func testAMaskFromBeforeAPauseIsNeverShownAfterIt() {
        let segmenter = GatedSegmenter(), preview = preview(segmenter)
        segmenter.open()
        preview.consume(camera(), mirrored: true); preview.waitForSegmentation()
        preview.sessionDidStop()
        preview.consume(camera(), mirrored: true); preview.waitForSegmentation()
        XCTAssertEqual(preview.statistics.framesMatted, 0)
        XCTAssertEqual(preview.statistics.framesAwaitingMask, 2, "the first frame after the pause waits for a mask of its own session")

    }

    func testAMaskThatFinishesAfterTheSessionStoppedIsThrownAway() {
        let segmenter = GatedSegmenter(), preview = preview(segmenter)
        preview.consume(camera(), mirrored: true)
        preview.sessionDidStop()
        segmenter.open(); preview.waitForSegmentation()
        preview.consume(camera(), mirrored: true)
        XCTAssertEqual(preview.statistics.framesMatted, 0)
        XCTAssertEqual(preview.statistics.framesAwaitingMask, 2)
        preview.waitForSegmentation()
    }

    func testAMaskOfOtherPixelsIsNotUsed() {
        let segmenter = GatedSegmenter(), preview = preview(segmenter)
        segmenter.open()
        preview.consume(camera(), mirrored: true); preview.waitForSegmentation()
        preview.consume(camera(), mirrored: true)
        XCTAssertEqual(preview.statistics.framesMatted, 1)
        preview.waitForSegmentation()
        segmenter.close()
        preview.consume(camera(), mirrored: false)
        XCTAssertEqual(preview.statistics.framesMatted, 1, "a mirrored mask does not fit an unmirrored frame")
        segmenter.open(); preview.waitForSegmentation()
        preview.consume(camera(width: 72, height: 128), mirrored: false)
        XCTAssertEqual(preview.statistics.framesMatted, 1, "nor does a mask of a 36x64 frame fit a 72x128 one")
    }

    func testAMaskOlderThanHalfASecondIsNotTrusted() {
        let segmenter = GatedSegmenter(), preview = preview(segmenter)
        segmenter.open()
        preview.consume(camera(), mirrored: true, capturedAt: 10.0); preview.waitForSegmentation()
        segmenter.close()
        preview.consume(camera(), mirrored: true, capturedAt: 10.4)
        XCTAssertEqual(preview.statistics.framesMatted, 1)
        preview.consume(camera(), mirrored: true, capturedAt: 10.6)
        XCTAssertEqual(preview.statistics.framesMatted, 1)
        XCTAssertEqual(preview.statistics.framesRaw, 1, "a stalled segmenter shows the camera, not an old outline")
        segmenter.open(); preview.waitForSegmentation()
    }

    func testAFailingSegmenterShowsTheRawCameraAndRecovers() {
        let segmenter = GatedSegmenter(), preview = preview(segmenter)
        segmenter.open(); segmenter.fails = true
        preview.consume(camera(), mirrored: true); preview.waitForSegmentation()
        preview.consume(camera(), mirrored: true); preview.waitForSegmentation()
        preview.consume(camera(), mirrored: true); preview.waitForSegmentation()
        XCTAssertEqual(preview.statistics.framesRaw, 2)
        segmenter.fails = false
        preview.consume(camera(), mirrored: true); preview.waitForSegmentation()
        preview.consume(camera(), mirrored: true)
        XCTAssertEqual(preview.statistics.framesMatted, 1, "matting resumes with the next answer")
    }

    func testWithoutABackgroundVisionIsNotAsked() {
        let segmenter = GatedSegmenter(), preview = preview(segmenter, withBackground: false)
        for _ in 0..<5 { preview.consume(camera(), mirrored: true) }
        preview.waitForSegmentation()
        XCTAssertEqual(segmenter.calls, 0)
        XCTAssertEqual(preview.statistics.framesRaw, 5)
    }
}
