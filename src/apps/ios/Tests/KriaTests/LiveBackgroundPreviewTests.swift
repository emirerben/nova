@preconcurrency import AVFoundation
import CoreImage
import ImageIO
import UniformTypeIdentifiers
import XCTest
@testable import Kria

/// KRI-1119 failure modes written before the code:
/// 1. the default camera graph (no background) must not gain an output, and the movie output must survive both
///    attaching and detaching the live one (S23);
/// 2. a segmentation failure draws the raw frame and counts it, never a crash or a blank frame (S25);
/// 3. frames reach the consumer only while one is set, and the consumer hears when the session stops (S25);
/// 4. a photo with an EXIF rotation is shown upright and cover-fit once at preview size (S10, S26);
/// 5. a video background loops, and the loop never holds audio.
final class LiveBackgroundPreviewTests: XCTestCase {
    // MARK: Graph (S23)

    func testTheDataOutputIsAddedOnlyWhenWantedAndTheMovieOutputIsUntouched() {
        let session = AVCaptureSession()
        let movie = AVCaptureMovieFileOutput()
        guard session.canAddOutput(movie) else { return XCTFail("the bare session cannot take a movie output here") }
        session.addOutput(movie)
        let relay = LiveFrameRelay(), queue = DispatchQueue(label: "test.frames")
        var live: AVCaptureVideoDataOutput?

        XCTAssertFalse(LiveFrameOutputGraph.reconcile(session: session, existing: &live, wanted: false, relay: relay, queue: queue))
        XCTAssertEqual(session.outputs.count, 1, "no background: the graph is exactly what it always was")
        XCTAssertNil(live)

        XCTAssertTrue(LiveFrameOutputGraph.reconcile(session: session, existing: &live, wanted: true, relay: relay, queue: queue))
        XCTAssertEqual(session.outputs.count, 2)
        XCTAssertTrue(session.outputs.contains(movie))
        let output = try? XCTUnwrap(live)
        XCTAssertEqual(output?.videoSettings[kCVPixelBufferPixelFormatTypeKey as String] as? OSType, kCVPixelFormatType_32BGRA)
        XCTAssertEqual(output?.alwaysDiscardsLateVideoFrames, true, "a slow matte must drop frames, never queue them behind the recording")

        XCTAssertTrue(LiveFrameOutputGraph.reconcile(session: session, existing: &live, wanted: true, relay: relay, queue: queue))
        XCTAssertEqual(session.outputs.count, 2, "asking twice does not add a second output")

        XCTAssertFalse(LiveFrameOutputGraph.reconcile(session: session, existing: &live, wanted: false, relay: relay, queue: queue))
        XCTAssertEqual(session.outputs, [movie], "clearing the background removes only the live output")
        XCTAssertNil(live)
    }

    @MainActor func testANewCameraStartsWithoutLiveFrames() {
        let camera = TeleprompterCamera()
        XCTAssertFalse(camera.liveFramesAttached)
    }

    // MARK: Relay

    private final class Consumer: LiveFrameConsumer, @unchecked Sendable {
        private let lock = NSLock()
        private var frames = 0, stops = 0
        var frameCount: Int { lock.lock(); defer { lock.unlock() }; return frames }
        var stopCount: Int { lock.lock(); defer { lock.unlock() }; return stops }
        private var flags: [Bool] = []
        var mirroredFlags: [Bool] { lock.lock(); defer { lock.unlock() }; return flags }
        func consume(_ frame: CVPixelBuffer, mirrored: Bool) { lock.lock(); frames += 1; flags.append(mirrored); lock.unlock() }
        func sessionDidStop() { lock.lock(); stops += 1; lock.unlock() }
    }

    private func pixelBuffer(width: Int = 36, height: Int = 64, _ rgb: (UInt8, UInt8, UInt8) = (255, 0, 0)) -> CVPixelBuffer {
        var buffer: CVPixelBuffer?
        CVPixelBufferCreate(nil, width, height, kCVPixelFormatType_32BGRA, [kCVPixelBufferIOSurfacePropertiesKey: [:]] as CFDictionary, &buffer)
        let pixels = buffer!
        CVPixelBufferLockBaseAddress(pixels, [])
        let base = CVPixelBufferGetBaseAddress(pixels)!.assumingMemoryBound(to: UInt8.self)
        let stride = CVPixelBufferGetBytesPerRow(pixels)
        for y in 0..<height { for x in 0..<width { let i = y * stride + x * 4; base[i] = rgb.2; base[i + 1] = rgb.1; base[i + 2] = rgb.0; base[i + 3] = 255 } }
        CVPixelBufferUnlockBaseAddress(pixels, [])
        return pixels
    }

    private func sampleBuffer(_ pixels: CVPixelBuffer) -> CMSampleBuffer {
        var format: CMVideoFormatDescription?
        CMVideoFormatDescriptionCreateForImageBuffer(allocator: nil, imageBuffer: pixels, formatDescriptionOut: &format)
        var timing = CMSampleTimingInfo(duration: .invalid, presentationTimeStamp: .zero, decodeTimeStamp: .invalid)
        var sample: CMSampleBuffer?
        CMSampleBufferCreateReadyWithImageBuffer(allocator: nil, imageBuffer: pixels, formatDescription: format!, sampleTiming: &timing, sampleBufferOut: &sample)
        return sample!
    }

    func testTheRelayDeliversFramesOnlyWhileAConsumerIsSetAndForwardsStop() {
        let relay = LiveFrameRelay(), consumer = Consumer()
        let output = AVCaptureVideoDataOutput(), connection = AVCaptureConnection(inputPorts: [], output: output)
        let sample = sampleBuffer(pixelBuffer())
        relay.captureOutput(output, didOutput: sample, from: connection)
        XCTAssertEqual(consumer.frameCount, 0)
        relay.setConsumer(consumer)
        relay.captureOutput(output, didOutput: sample, from: connection)
        relay.captureOutput(output, didOutput: sample, from: connection)
        XCTAssertEqual(consumer.frameCount, 2)
        XCTAssertEqual(consumer.mirroredFlags, [true, true], "an unmirrored connection means the preview applies the selfie flip")
        relay.sessionDidStop()
        XCTAssertEqual(consumer.stopCount, 1)
        relay.setConsumer(nil)
        relay.captureOutput(output, didOutput: sample, from: connection)
        XCTAssertEqual(consumer.frameCount, 2)
    }

    // MARK: Preview (S25)

    private struct FailingSegmenter: PersonSegmenting {
        struct Failure: Error {}
        func mask(for frame: CVPixelBuffer) throws -> CVPixelBuffer { throw Failure() }
    }

    private struct FullSegmenter: PersonSegmenting {
        func mask(for frame: CVPixelBuffer) throws -> CVPixelBuffer {
            var buffer: CVPixelBuffer?
            CVPixelBufferCreate(nil, 6, 8, kCVPixelFormatType_OneComponent8, [kCVPixelBufferIOSurfacePropertiesKey: [:]] as CFDictionary, &buffer)
            return buffer!
        }
    }

    private final class Still: LiveBackgroundProviding, @unchecked Sendable {
        var active: [Bool] = []
        func frame() -> CIImage? { CIImage(color: .blue).cropped(to: CGRect(origin: .zero, size: LiveSpeakerMatteConfig.previewSize)) }
        func setActive(_ active: Bool) { self.active.append(active) }
        func stop() {}
    }

    func testAFailingMatteDrawsTheRawFrameAndKeepsGoing() {
        let preview = LiveBackgroundPreview(segmenter: FailingSegmenter())
        preview.setBackground(Still())
        for _ in 0..<5 { preview.consume(pixelBuffer(), mirrored: true) }
        XCTAssertEqual(preview.statistics, LiveBackgroundStatistics(framesConsumed: 5, framesMatted: 0, framesRaw: 5))
    }

    func testAWorkingMatteIsCountedAndNoBackgroundYetIsRaw() {
        let preview = LiveBackgroundPreview(segmenter: FullSegmenter())
        preview.consume(pixelBuffer(), mirrored: true)
        XCTAssertEqual(preview.statistics.framesRaw, 1, "no background loaded yet: the camera alone")
        preview.setBackground(Still())
        preview.consume(pixelBuffer(), mirrored: true)
        XCTAssertEqual(preview.statistics.framesMatted, 1)
    }

    func testTheBackgroundPausesWithTheSessionAndResumesOnTheNextFrame() {
        let preview = LiveBackgroundPreview(segmenter: FullSegmenter())
        let background = Still()
        preview.setBackground(background)
        preview.sessionDidStop()
        XCTAssertEqual(background.active, [false])
        preview.consume(pixelBuffer(), mirrored: true)
        XCTAssertEqual(background.active, [false, true])
        preview.consume(pixelBuffer(), mirrored: true)
        XCTAssertEqual(background.active, [false, true], "only the first frame resumes it")
    }

    // MARK: Still background (S10, S26)

    private func writeImage(width: Int, height: Int, orientation: Int?, redLeftHalf: Bool, to url: URL) throws {
        let space = CGColorSpaceCreateDeviceRGB()
        let context = CGContext(data: nil, width: width, height: height, bitsPerComponent: 8, bytesPerRow: 0, space: space,
                                bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)!
        context.setFillColor(CGColor(red: 0, green: 0, blue: 1, alpha: 1)); context.fill(CGRect(x: 0, y: 0, width: width, height: height))
        context.setFillColor(CGColor(red: 1, green: 0, blue: 0, alpha: 1))
        context.fill(CGRect(x: 0, y: 0, width: width / 2, height: height))
        let destination = CGImageDestinationCreateWithURL(url as CFURL, UTType.jpeg.identifier as CFString, 1, nil)!
        var properties: [CFString: Any] = [kCGImageDestinationLossyCompressionQuality: 1.0]
        if let orientation { properties[kCGImagePropertyOrientation] = orientation }
        CGImageDestinationAddImage(destination, context.makeImage()!, properties as CFDictionary)
        XCTAssertTrue(CGImageDestinationFinalize(destination))
    }

    private func pixel(_ image: CIImage, x: Int, y: Int) -> (r: Int, g: Int, b: Int) {
        var rgba = [UInt8](repeating: 0, count: 4)
        LiveBackgroundCompositor.makeContext().render(image, toBitmap: &rgba, rowBytes: 4, bounds: CGRect(x: x, y: y, width: 1, height: 1),
                                                      format: .RGBA8, colorSpace: CGColorSpace(name: CGColorSpace.sRGB))
        return (Int(rgba[0]), Int(rgba[1]), Int(rgba[2]))
    }

    func testALandscapePhotoIsCoverFitOnceToPreviewSize() throws {
        let url = FileManager.default.temporaryDirectory.appendingPathComponent("\(UUID().uuidString).jpg")
        defer { try? FileManager.default.removeItem(at: url) }
        try writeImage(width: 400, height: 100, orientation: nil, redLeftHalf: true, to: url)
        let frame = try XCTUnwrap(StillBackground(url: url).frame())
        XCTAssertEqual(frame.extent, CGRect(origin: .zero, size: LiveSpeakerMatteConfig.previewSize))
        // Cover-fit centres the crop: the red/blue seam of the photo sits in the middle column.
        XCTAssertGreaterThan(pixel(frame, x: 100, y: 480).r, 200)
        XCTAssertGreaterThan(pixel(frame, x: 440, y: 480).b, 200)
    }

    func testARotatedPhotoUsesItsOrientation() throws {
        let url = FileManager.default.temporaryDirectory.appendingPathComponent("\(UUID().uuidString).jpg")
        defer { try? FileManager.default.removeItem(at: url) }
        // Stored 400x100 with the red half on the left. Orientation 6 displays it rotated clockwise: portrait, red on TOP.
        try writeImage(width: 400, height: 100, orientation: 6, redLeftHalf: true, to: url)
        let frame = try XCTUnwrap(StillBackground(url: url).frame())
        XCTAssertGreaterThan(pixel(frame, x: 270, y: 900).r, 200, "top of the displayed picture (Core Image y is up)")
        XCTAssertGreaterThan(pixel(frame, x: 270, y: 60).b, 200)
    }

    func testAnUnreadableBackgroundThrows() {
        let url = FileManager.default.temporaryDirectory.appendingPathComponent("\(UUID().uuidString).jpg")
        try? Data("not an image".utf8).write(to: url)
        defer { try? FileManager.default.removeItem(at: url) }
        XCTAssertThrowsError(try StillBackground(url: url))
    }

    // MARK: Video background

    func testTrackTransformsMapToTheRightOrientation() {
        XCTAssertEqual(LoopingVideoBackground.orientation(of: CGAffineTransform(a: 0, b: 1, c: -1, d: 0, tx: 0, ty: 0)), .right)
        XCTAssertEqual(LoopingVideoBackground.orientation(of: CGAffineTransform(a: 0, b: -1, c: 1, d: 0, tx: 0, ty: 0)), .left)
        XCTAssertEqual(LoopingVideoBackground.orientation(of: CGAffineTransform(a: -1, b: 0, c: 0, d: -1, tx: 0, ty: 0)), .down)
        XCTAssertEqual(LoopingVideoBackground.orientation(of: .identity), .up)
    }

    private func writeVideo(seconds: Double, to url: URL) async throws {
        let writer = try AVAssetWriter(outputURL: url, fileType: .mov)
        let input = AVAssetWriterInput(mediaType: .video, outputSettings: [AVVideoCodecKey: AVVideoCodecType.h264, AVVideoWidthKey: 64, AVVideoHeightKey: 64])
        let adaptor = AVAssetWriterInputPixelBufferAdaptor(assetWriterInput: input, sourcePixelBufferAttributes: nil)
        writer.add(input); writer.startWriting(); writer.startSession(atSourceTime: .zero)
        let frames = Int(seconds * 30)
        for i in 0..<frames {
            while !input.isReadyForMoreMediaData { try await Task.sleep(for: .milliseconds(5)) }
            let buffer = pixelBuffer(width: 64, height: 64, (UInt8(i * 255 / max(1, frames)), 0, 255))
            adaptor.append(buffer, withPresentationTime: CMTime(value: CMTimeValue(i), timescale: 30))
        }
        input.markAsFinished()
        await writer.finishWriting()
        XCTAssertEqual(writer.status, .completed)
    }

    func testAVideoBackgroundDeliversFramesAndLoops() async throws {
        let url = FileManager.default.temporaryDirectory.appendingPathComponent("\(UUID().uuidString).mov")
        defer { try? FileManager.default.removeItem(at: url) }
        try await writeVideo(seconds: 0.5, to: url)
        let video = try await LoopingVideoBackground(url: url)
        defer { video.stop() }

        var firstFrame: CIImage?
        for _ in 0..<60 where firstFrame == nil { firstFrame = video.frame(); try await Task.sleep(for: .milliseconds(50)) }
        XCTAssertEqual(firstFrame?.extent, CGRect(origin: .zero, size: LiveSpeakerMatteConfig.previewSize))

        var samples: [Double] = []
        for _ in 0..<30 { samples.append(video.playbackSeconds); try await Task.sleep(for: .milliseconds(80)) }
        let wrapped = zip(samples, samples.dropFirst()).contains { $1 < $0 - 0.2 }
        XCTAssertTrue(wrapped, "a 0.5 s video must wrap back to the start within 2.4 s: \(samples)")
        XCTAssertNotNil(video.frame(), "still delivering after the loop")
    }

    func testAVideoBackgroundWithNoVideoTrackIsRefused() async throws {
        let url = FileManager.default.temporaryDirectory.appendingPathComponent("\(UUID().uuidString).txt")
        try Data("x".utf8).write(to: url)
        defer { try? FileManager.default.removeItem(at: url) }
        do { _ = try await LoopingVideoBackground(url: url); XCTFail("expected a failure") } catch { }
    }
}
