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
        var restarts = 0
        func restart() { restarts += 1 }
        func frame() -> CIImage? { CIImage(color: .blue).cropped(to: CGRect(origin: .zero, size: LiveSpeakerMatteConfig.previewSize)) }
        func setActive(_ active: Bool) { self.active.append(active) }
        func stop() {}
    }

    /// The matte on the display path, as before the async lever. The async path has its own tests.
    private var synchronous: LiveBackgroundTuning { var tuning = LiveBackgroundTuning(); tuning.asyncSegmentation = false; return tuning }

    func testAFailingMatteDrawsTheRawFrameAndKeepsGoing() {
        let preview = LiveBackgroundPreview(segmenter: FailingSegmenter(), tuning: synchronous)
        preview.setBackground(Still())
        for _ in 0..<5 { preview.consume(pixelBuffer(), mirrored: true) }
        XCTAssertEqual(preview.statistics, LiveBackgroundStatistics(framesConsumed: 5, framesMatted: 0, framesRaw: 5))
    }

    func testAWorkingMatteIsCountedAndNoBackgroundYetIsRaw() {
        let preview = LiveBackgroundPreview(segmenter: FullSegmenter(), tuning: synchronous)
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

    private func writeVideo(seconds: Double, to url: URL, size: CGSize = CGSize(width: 64, height: 64)) async throws {
        let writer = try AVAssetWriter(outputURL: url, fileType: .mov)
        let input = AVAssetWriterInput(mediaType: .video, outputSettings: [AVVideoCodecKey: AVVideoCodecType.h264, AVVideoWidthKey: Int(size.width), AVVideoHeightKey: Int(size.height)])
        let adaptor = AVAssetWriterInputPixelBufferAdaptor(assetWriterInput: input, sourcePixelBufferAttributes: nil)
        writer.add(input); writer.startWriting(); writer.startSession(atSourceTime: .zero)
        let frames = Int(seconds * 30)
        for i in 0..<frames {
            while !input.isReadyForMoreMediaData { try await Task.sleep(for: .milliseconds(5)) }
            let buffer = pixelBuffer(width: Int(size.width), height: Int(size.height), (UInt8(i * 255 / max(1, frames)), 0, 255))
            adaptor.append(buffer, withPresentationTime: CMTime(value: CMTimeValue(i), timescale: 30))
        }
        input.markAsFinished()
        await writer.finishWriting()
        XCTAssertEqual(writer.status, .completed)
    }

    private var previousVideoLoop: LiveBackgroundTuning { var tuning = LiveBackgroundTuning(); tuning.loopedVideoBackground = false; return tuning }

    func testAVideoBackgroundDeliversFramesAndLoops() async throws {
        try await assertVideoLoops(LiveBackgroundTuning())
        try await assertVideoLoops(previousVideoLoop)
    }

    private func assertVideoLoops(_ tuning: LiveBackgroundTuning) async throws {
        let url = FileManager.default.temporaryDirectory.appendingPathComponent("\(UUID().uuidString).mov")
        defer { try? FileManager.default.removeItem(at: url) }
        try await writeVideo(seconds: 0.5, to: url)
        let video = try await LoopingVideoBackground(url: url, tuning: tuning)
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

    /// Failure modes of the looper path, written before it: an output attached only when an item becomes current misses that
    /// pass's first frames (measured on the simulator: a 135 ms hole at every loop, against 45 ms with an output waiting on
    /// every item); frames are decoded larger than they are drawn; a landscape or turned track is sized from the wrong side.
    func testEveryLoopedItemHasAnOutputWaitingAndFramesAreDecodedAtTheDrawnSize() async throws {
        let url = FileManager.default.temporaryDirectory.appendingPathComponent("\(UUID().uuidString).mov")
        defer { try? FileManager.default.removeItem(at: url) }
        try await writeVideo(seconds: 0.5, to: url, size: CGSize(width: 1080, height: 1920))
        var fullSizeDecode = LiveBackgroundTuning(); fullSizeDecode.drawnSizeVideoDecode = false
        var neither = previousVideoLoop; neither.drawnSizeVideoDecode = false
        for (tuning, expected) in [(LiveBackgroundTuning(), CGSize(width: 540, height: 960)), (fullSizeDecode, CGSize(width: 1080, height: 1920)),
                                   (previousVideoLoop, CGSize(width: 540, height: 960)), (neither, CGSize(width: 1080, height: 1920))] {
            let video = try await LoopingVideoBackground(url: url, tuning: tuning)
            defer { video.stop() }
            for _ in 0..<60 where video.decodedSize == nil { _ = video.frame(); try await Task.sleep(for: .milliseconds(50)) }
            XCTAssertEqual(video.decodedSize, expected, "\(tuning)")
            if tuning.loopedVideoBackground { XCTAssertGreaterThan(video.loopedItemCount, 0)
                XCTAssertEqual(video.outputCount, video.loopedItemCount, "an output waits on every copy the looper rotates through") }
        }
    }

    /// Failure modes of reading the looper's frames, written before the fix: the looper preloads the next copy, and that
    /// copy's output reports its frame 0 as new while the current pass is still playing, so (1) the first frame is drawn
    /// early, about 3 frames before the end of the pass, (2) the real wrap then has no fresh frame for ~75 ms, (3) the pass
    /// resumes after the early frame, (4) it may happen at only one wrap per rotation through the copies, so a single
    /// wrap does not show it.
    ///
    /// This test used to sample a live player every 30 ms for 11 real seconds. A busy runner that held the sampler back for
    /// a second skipped the end of a pass, and the skip read as a wrap from the middle of it (KRI-1379). It now drives the
    /// same picking code through every copy on a scripted clock, twice a frame, with each copy's output behaving as
    /// measured above, so every frame of every pass is checked and nothing depends on how fast the runner is. The live
    /// looper (outputs on every copy, wrapping) stays covered by the two tests above.
    func testTheLoopedBackgroundWrapsOnlyAtTheEndOfAPass() throws {
        let frames = 60
        let looper = ScriptedLooper(copies: 3, framesPerPass: frames, preloadFrames: 3,
                                    buffers: (0..<frames).map { pixelBuffer(width: 4, height: 4, (UInt8($0 * 4), 0, 255)) })
        var picker = LoopedFramePicker(orientation: .up)
        let context = LiveBackgroundCompositor.makeContext()
        func frameIndex(_ image: CIImage) -> Int {
            var rgba = [UInt8](repeating: 0, count: 4)
            context.render(image, toBitmap: &rgba, rowBytes: 4, bounds: CGRect(x: image.extent.midX, y: image.extent.midY, width: 1, height: 1),
                           format: .RGBA8, colorSpace: CGColorSpace(name: CGColorSpace.sRGB))
            return Int((Double(rgba[0]) / 4).rounded())
        }
        var drawn: [Int] = []
        for tick in 0..<(2 * looper.copies * looper.ticksPerPass) {   // two rotations through every copy
            looper.now = tick
            let image = try XCTUnwrap(picker.frame(onScreen: looper.item(onScreenAt: tick), outputs: looper.outputs, hostTime: ScriptedLooper.seconds(tick)))
            drawn.append(frameIndex(image))
        }
        // Every pass draws its frames in order, each when it is due: none early, none held past its time, no pass resumed.
        let due = drawn.indices.map { ($0 % looper.ticksPerPass) / 2 }
        let wrong = drawn.indices.first { drawn[$0] != due[$0] }
        XCTAssertNil(wrong, "at tick \(wrong ?? 0) (pass \((wrong ?? 0) / looper.ticksPerPass)) frame \(wrong.map { drawn[$0] } ?? 0) was drawn where frame \(wrong.map { due[$0] } ?? 0) was due: \(drawn)")
        XCTAssertEqual(zip(drawn, drawn.dropFirst()).filter { $1 < $0 }.count, 2 * looper.copies - 1, "one wrap per pass, each from the last frame to the first")
    }

    /// `AVPlayerLooper`'s copies on a scripted clock, each output behaving as `AVPlayerItemVideoOutput` was measured to on
    /// the simulator (KRI-1183): the copy on screen has each frame of its pass from the moment it is due; the next copy, from
    /// `preloadFrames` frames before the end of the current pass, already reports its frame 0 as new (its time is held at
    /// zero until it starts); any other copy has nothing new. Time counts in ticks of half a frame at 30 fps.
    private final class ScriptedLooper {
        static let ticksPerSecond: CMTimeScale = 60
        static func seconds(_ tick: Int) -> CFTimeInterval { CFTimeInterval(tick) / CFTimeInterval(ticksPerSecond) }
        static func tick(_ seconds: CFTimeInterval) -> Int { Int((seconds * CFTimeInterval(ticksPerSecond)).rounded()) }

        let copies: Int, framesPerPass: Int, preloadFrames: Int
        var ticksPerPass: Int { framesPerPass * 2 }
        let buffers: [CVPixelBuffer]
        private let items: [NSObject]
        private(set) var outputs: [(item: AnyObject, output: any LoopedVideoOutput)] = []
        /// The host clock, set by the test before each read.
        var now = 0

        init(copies: Int, framesPerPass: Int, preloadFrames: Int, buffers: [CVPixelBuffer]) {
            self.copies = copies; self.framesPerPass = framesPerPass; self.preloadFrames = preloadFrames; self.buffers = buffers
            items = (0..<copies).map { _ in NSObject() }
            outputs = items.indices.map { (items[$0], Copy(looper: self, index: $0)) }
        }

        /// The pass on screen at `tick`, and the item playing it (what `AVQueuePlayer.currentItem` answers).
        func pass(at tick: Int) -> Int { tick / ticksPerPass }
        func item(onScreenAt tick: Int) -> AnyObject { items[pass(at: tick) % copies] }

        /// The pass copy `index` is playing or preloading at `tick`, if any.
        func pass(ofCopy index: Int, at tick: Int) -> Int? {
            let current = pass(at: tick)
            if current % copies == index { return current }
            let next = current + 1
            if next % copies == index, tick >= next * ticksPerPass - preloadFrames * 2 { return next }
            return nil
        }

        final class Copy: LoopedVideoOutput {
            unowned let looper: ScriptedLooper
            let index: Int
            private var handedOut: (pass: Int, frame: Int)?
            init(looper: ScriptedLooper, index: Int) { self.looper = looper; self.index = index }

            func itemTime(forHostTime hostTimeInSeconds: CFTimeInterval) -> CMTime {
                let tick = ScriptedLooper.tick(hostTimeInSeconds)
                guard let pass = looper.pass(ofCopy: index, at: tick), pass == looper.pass(at: tick) else { return .zero }
                return CMTime(value: CMTimeValue(tick - pass * looper.ticksPerPass), timescale: ScriptedLooper.ticksPerSecond)
            }

            func hasNewPixelBuffer(forItemTime itemTime: CMTime) -> Bool { newFrame(at: itemTime) != nil }

            func copyPixelBuffer(forItemTime itemTime: CMTime, itemTimeForDisplay outItemTimeForDisplay: UnsafeMutablePointer<CMTime>?) -> CVPixelBuffer? {
                guard let new = newFrame(at: itemTime) else { return nil }
                handedOut = new
                return looper.buffers[new.frame]
            }

            /// The frame at `itemTime`, if this copy has it decoded now and has not handed it out in this pass yet.
            private func newFrame(at itemTime: CMTime) -> (pass: Int, frame: Int)? {
                guard let pass = looper.pass(ofCopy: index, at: looper.now) else { return nil }
                let frame = Int(CMTimeConvertScale(itemTime, timescale: ScriptedLooper.ticksPerSecond, method: .roundTowardZero).value) / 2
                let decoded = pass == looper.pass(at: looper.now) ? (looper.now - pass * looper.ticksPerPass) / 2 : 0
                guard frame <= decoded, frame < looper.framesPerPass else { return nil }
                if let handedOut, handedOut.pass == pass, handedOut.frame >= frame { return nil }
                return (pass, frame)
            }
        }
    }

    func testDecodeSizeFollowsTheSideTheCoverFitUses() {
        XCTAssertEqual(LoopingVideoBackground.decodeSize(natural: CGSize(width: 1080, height: 1920), orientation: .up), CGSize(width: 540, height: 960))
        XCTAssertEqual(LoopingVideoBackground.decodeSize(natural: CGSize(width: 3840, height: 2160), orientation: .right), CGSize(width: 960, height: 540),
                       "stored landscape, shown portrait: scaled by the shown size, kept in the stored shape")
        XCTAssertEqual(LoopingVideoBackground.decodeSize(natural: CGSize(width: 3840, height: 2160), orientation: .up), CGSize(width: 1708, height: 960),
                       "a landscape video is cover-fit by its height")
        XCTAssertNil(LoopingVideoBackground.decodeSize(natural: CGSize(width: 480, height: 854), orientation: .up), "never upscaled")
        XCTAssertNil(LoopingVideoBackground.decodeSize(natural: .zero, orientation: .up))
    }

    func testRestartSeeksAVideoBackToItsFirstFrameAndItKeepsLooping() async throws {
        let url = FileManager.default.temporaryDirectory.appendingPathComponent("\(UUID().uuidString).mov")
        defer { try? FileManager.default.removeItem(at: url) }
        try await writeVideo(seconds: 3, to: url)
        let video = try await LoopingVideoBackground(url: url)
        defer { video.stop() }
        for _ in 0..<80 where video.playbackSeconds < 0.8 { try await Task.sleep(for: .milliseconds(50)) }
        XCTAssertGreaterThanOrEqual(video.playbackSeconds, 0.8, "it was well into the clip when Record started")
        video.restart()
        var restartedAt = Double.infinity
        for _ in 0..<40 { restartedAt = min(restartedAt, video.playbackSeconds); try await Task.sleep(for: .milliseconds(25)) }
        XCTAssertLessThan(restartedAt, 0.3, "restart lands on the first frame")
        let after = video.playbackSeconds
        try await Task.sleep(for: .milliseconds(400))
        XCTAssertGreaterThan(video.playbackSeconds, after, "and it plays on afterwards")
    }

    func testRestartOnAPhotoBackgroundChangesNothing() throws {
        let url = FileManager.default.temporaryDirectory.appendingPathComponent("\(UUID().uuidString).jpg")
        defer { try? FileManager.default.removeItem(at: url) }
        try writeImage(width: 40, height: 80, orientation: nil, redLeftHalf: true, to: url)
        let photo = try StillBackground(url: url)
        let before = photo.frame()
        photo.restart()
        XCTAssertEqual(photo.frame()?.extent, before?.extent)
    }

    func testThePreviewForwardsARestartOnlyToTheCurrentBackground() {
        let preview = LiveBackgroundPreview(segmenter: FullSegmenter())
        let first = Still(), second = Still()
        preview.restartBackground()
        preview.setBackground(first)
        preview.restartBackground()
        preview.setBackground(second)
        preview.restartBackground()
        XCTAssertEqual(first.restarts, 1, "a replaced background is never restarted again")
        XCTAssertEqual(second.restarts, 1)
    }

    func testAVideoBackgroundWithNoVideoTrackIsRefused() async throws {
        let url = FileManager.default.temporaryDirectory.appendingPathComponent("\(UUID().uuidString).txt")
        try Data("x".utf8).write(to: url)
        defer { try? FileManager.default.removeItem(at: url) }
        do { _ = try await LoopingVideoBackground(url: url); XCTFail("expected a failure") } catch { }
    }

    // MARK: Presentation (device finding 2026-10-31: the whole composite was upside down on screen)
    //
    // compose() is pinned in Core Image space elsewhere. These tests send the composite through the SAME render step the
    // screen uses and read the buffer back, where row 0 is the top row the layer shows. (The first version drew into a
    // CAMetalLayer texture; the simulator's readback passed with a vertical flip while the iPhone showed it upside down.
    // Pixel buffers have the same row order on both, so these tests now mean the same thing on the device.)

    private let previewWidth = Int(LiveSpeakerMatteConfig.previewSize.width)
    private let previewHeight = Int(LiveSpeakerMatteConfig.previewSize.height)

    /// 36x64 camera frame: red marker block in the top-left corner (buffer row 0 is the top), black elsewhere.
    private func markerFrame() -> CVPixelBuffer {
        let buffer = pixelBuffer(width: 36, height: 64, (0, 0, 0))
        CVPixelBufferLockBaseAddress(buffer, [])
        let base = CVPixelBufferGetBaseAddress(buffer)!.assumingMemoryBound(to: UInt8.self)
        let stride = CVPixelBufferGetBytesPerRow(buffer)
        for y in 0..<16 { for x in 0..<12 { let i = y * stride + x * 4; base[i] = 0; base[i + 1] = 0; base[i + 2] = 255 } }
        CVPixelBufferUnlockBaseAddress(buffer, [])
        return buffer
    }

    /// Renders through the presenter's own `render` and returns (r, g, b) at a pixel; row 0 is the top row the layer shows.
    private func texturePixels(of image: CIImage, with preview: LiveBackgroundPreview) throws -> (Int, Int) -> (r: Int, g: Int, b: Int) {
        let buffer = try XCTUnwrap(preview.render(image))
        CVPixelBufferLockBaseAddress(buffer, .readOnly)
        let stride = CVPixelBufferGetBytesPerRow(buffer)
        let count = stride * CVPixelBufferGetHeight(buffer)
        let bytes = Array(UnsafeBufferPointer(start: CVPixelBufferGetBaseAddress(buffer)!.assumingMemoryBound(to: UInt8.self), count: count))
        CVPixelBufferUnlockBaseAddress(buffer, .readOnly)
        return { x, row in let i = row * stride + x * 4; return (Int(bytes[i + 2]), Int(bytes[i + 1]), Int(bytes[i])) }
    }

    func testACameraMarkerInTheTopLeftLandsInTextureRowZeroColumnZero() throws {
        let preview = LiveBackgroundPreview(segmenter: FullSegmenter())
        let image = preview.composite(frame: markerFrame(), matte: nil, background: nil, mirrored: false)
        let pixel = try texturePixels(of: image, with: preview)
        XCTAssertGreaterThan(pixel(40, 40).r, 200, "top-left marker is at the top of the texture")
        XCTAssertLessThan(pixel(40, previewHeight - 40).r, 50, "and not at the bottom")
        XCTAssertLessThan(pixel(previewWidth - 40, 40).r, 50, "and not at the top-right")
    }

    func testAMirroredCameraMarkerMovesToTheTopRightAndStaysAtTheTop() throws {
        let preview = LiveBackgroundPreview(segmenter: FullSegmenter())
        let image = preview.composite(frame: markerFrame(), matte: nil, background: nil, mirrored: true)
        let pixel = try texturePixels(of: image, with: preview)
        XCTAssertGreaterThan(pixel(previewWidth - 40, 40).r, 200)
        XCTAssertLessThan(pixel(40, 40).r, 50)
        XCTAssertLessThan(pixel(previewWidth - 40, previewHeight - 40).r, 50)
    }

    func testAPhotoBackgroundMarkerInTheTopLeftIsUprightOnScreen() throws {
        let url = FileManager.default.temporaryDirectory.appendingPathComponent("\(UUID().uuidString).png")
        defer { try? FileManager.default.removeItem(at: url) }
        let context = CGContext(data: nil, width: 90, height: 160, bitsPerComponent: 8, bytesPerRow: 0, space: CGColorSpaceCreateDeviceRGB(),
                                bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)!
        context.setFillColor(CGColor(red: 0, green: 0, blue: 0, alpha: 1)); context.fill(CGRect(x: 0, y: 0, width: 90, height: 160))
        // CGContext origin is bottom-left: y 120..160 is the TOP of the picture.
        context.setFillColor(CGColor(red: 0, green: 1, blue: 0, alpha: 1)); context.fill(CGRect(x: 0, y: 120, width: 30, height: 40))
        try XCTUnwrap(UIImage(cgImage: context.makeImage()!).pngData()).write(to: url)
        let background = try XCTUnwrap(StillBackground(url: url).frame())
        let preview = LiveBackgroundPreview(segmenter: FullSegmenter())
        // No camera in front: a mask of zeros shows only the background.
        let empty = FullSegmenterBuffer.zeros()
        let image = preview.composite(frame: markerFrame(), matte: empty, background: background, mirrored: false)
        let pixel = try texturePixels(of: image, with: preview)
        XCTAssertGreaterThan(pixel(40, 40).g, 200, "photo marker top-left is at the top of the texture")
        XCTAssertLessThan(pixel(40, previewHeight - 40).g, 50)
    }

    func testAVideoBackgroundMarkerInTheTopLeftIsUprightOnScreen() async throws {
        let url = FileManager.default.temporaryDirectory.appendingPathComponent("\(UUID().uuidString).mov")
        defer { try? FileManager.default.removeItem(at: url) }
        try await writeMarkerVideo(to: url)
        let video = try await LoopingVideoBackground(url: url)
        defer { video.stop() }
        var frame: CIImage?
        for _ in 0..<60 where frame == nil { frame = video.frame(); try await Task.sleep(for: .milliseconds(50)) }
        let preview = LiveBackgroundPreview(segmenter: FullSegmenter())
        let image = preview.composite(frame: markerFrame(), matte: FullSegmenterBuffer.zeros(), background: try XCTUnwrap(frame), mirrored: false)
        let pixel = try texturePixels(of: image, with: preview)
        XCTAssertGreaterThan(pixel(40, 40).g, 150, "video marker top-left is at the top of the texture")
        XCTAssertLessThan(pixel(40, previewHeight - 40).g, 60)
    }

    private func writeMarkerVideo(to url: URL) async throws {
        let writer = try AVAssetWriter(outputURL: url, fileType: .mov)
        let input = AVAssetWriterInput(mediaType: .video, outputSettings: [AVVideoCodecKey: AVVideoCodecType.h264, AVVideoWidthKey: 90, AVVideoHeightKey: 160])
        let adaptor = AVAssetWriterInputPixelBufferAdaptor(assetWriterInput: input, sourcePixelBufferAttributes: nil)
        writer.add(input); writer.startWriting(); writer.startSession(atSourceTime: .zero)
        let frame = pixelBuffer(width: 90, height: 160, (0, 0, 0))
        CVPixelBufferLockBaseAddress(frame, [])
        let base = CVPixelBufferGetBaseAddress(frame)!.assumingMemoryBound(to: UInt8.self)
        let stride = CVPixelBufferGetBytesPerRow(frame)
        for y in 0..<40 { for x in 0..<30 { let i = y * stride + x * 4; base[i] = 0; base[i + 1] = 255; base[i + 2] = 0 } } // BGRA green, buffer row 0 = top
        CVPixelBufferUnlockBaseAddress(frame, [])
        for i in 0..<15 {
            while !input.isReadyForMoreMediaData { try await Task.sleep(for: .milliseconds(5)) }
            adaptor.append(frame, withPresentationTime: CMTime(value: CMTimeValue(i), timescale: 30))
        }
        input.markAsFinished()
        await writer.finishWriting()
        XCTAssertEqual(writer.status, .completed)
    }
}

enum FullSegmenterBuffer {
    /// An all-zero 8-bit mask: nobody in frame.
    static func zeros() -> CVPixelBuffer {
        var buffer: CVPixelBuffer?
        CVPixelBufferCreate(nil, 6, 8, kCVPixelFormatType_OneComponent8, [kCVPixelBufferIOSurfacePropertiesKey: [:]] as CFDictionary, &buffer)
        let pixels = buffer!
        CVPixelBufferLockBaseAddress(pixels, [])
        memset(CVPixelBufferGetBaseAddress(pixels), 0, CVPixelBufferGetBytesPerRow(pixels) * 8)
        CVPixelBufferUnlockBaseAddress(pixels, [])
        return pixels
    }
}
