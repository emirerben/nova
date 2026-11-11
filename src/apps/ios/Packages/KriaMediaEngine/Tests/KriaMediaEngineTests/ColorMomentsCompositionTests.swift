import Foundation
import XCTest
@testable import KriaMediaEngine
#if canImport(AVFoundation)
@preconcurrency import AVFoundation
import CoreImage

/// KRI-1350 colour moments through the real compositor and exporter, read back frame by frame.
/// Failure modes: `docs/reviews/colour-moments-failure-modes.md`. Answer key (Çiğdem's reel, measured):
/// `tests/fixtures/colour-moments/cigdem-reel-answer-key.json`.
///
/// Set `KRIA_COLOR_MOMENTS_EVIDENCE_DIR` to keep the exported MP4 and its recipe JSON, then measure it
/// independently: `scripts/verify-colour-moments.py check <mp4> --expect <(... expect-from-recipe <recipe>)`.
final class ColorMomentsCompositionTests: XCTestCase {
    private let size = (180, 320)
    private let fps = 30.0
    /// A red wall like the reference reel: colourful, mid-bright, so mono, flashes and dips all read clearly.
    private let wall = CGColor(red: 0.62, green: 0.16, blue: 0.14, alpha: 1)

    private func makeDirectory() throws -> URL {
        let url = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        return url
    }

    /// A short burst (fast attack, 0.25 s decay) standing in for a library whoosh/hit, silent elsewhere.
    private func writeHit(_ directory: URL) throws -> URL {
        let url = directory.appendingPathComponent("hit.m4a")
        let format = try XCTUnwrap(AVAudioFormat(standardFormatWithSampleRate: 48_000, channels: 1))
        let frames = AVAudioFrameCount(0.5 * 48_000)
        let buffer = try XCTUnwrap(AVAudioPCMBuffer(pcmFormat: format, frameCapacity: frames)); buffer.frameLength = frames
        for index in 0..<Int(frames) {
            let t = Double(index) / 48_000
            let envelope = t < 0.25 ? exp(-t * 12) : 0
            buffer.floatChannelData![0][index] = Float(0.7 * envelope * sin(t * 2 * .pi * 900))
        }
        let file = try AVAudioFile(forWriting: url, settings: [AVFormatIDKey: kAudioFormatMPEG4AAC, AVSampleRateKey: 48_000,
            AVNumberOfChannelsKey: 1, AVEncoderBitRateKey: 128_000], commonFormat: .pcmFormatFloat32, interleaved: false)
        try file.write(from: buffer)
        return url
    }

    private static let moment = ColorMoment(id: "cm1", start: 1.0, end: 2.5, tone: .mono,
        enter: .init(style: .lightLeak), exit: .init(style: .dipBlack))

    private func recipe(moments: [ColorMoment] = [moment], endFade: EndFade? = EndFade(duration: 1.0), sound: Bool = true) -> EditRecipe {
        var tracks = [TimelineTrack(id: "video", kind: .video, clips: [
            TimelineClip(id: "wall", sourceAssetID: "wall", sourceDuration: 4, volume: 0)
        ])]
        // The sound locked to the exit edge: an `sfx` audio clip starting exactly at the edge, as the server compiles it.
        if sound { tracks.append(TimelineTrack(id: "sfx", kind: .audio, clips: [
            TimelineClip(id: "sfx-cm1-exit", sourceAssetID: "hit", sourceDuration: 0.5, timelineStart: 2.5)
        ])) }
        return EditRecipe(canvas: Canvas(width: size.0, height: size.1), frameRate: fps,
            assets: [MediaAsset(id: "wall", relativePath: "wall")] + (sound ? [MediaAsset(id: "hit", relativePath: "hit")] : []),
            tracks: tracks, colorMoments: moments, endFade: endFade)
    }

    // MARK: timing (pure)

    func testEdgeWeightMatchesTheMeasuredLightLeakShape() {
        // Çiğdem's light leak, normalised peak lift: ±1 frame ≈ 0.53/0.57, ±2 ≈ 0.39/0.27, ±3 ≈ 0.07/0.19.
        let d = 1.0 / 3, t = 1.0
        XCTAssertEqual(ColorMomentTiming.weight(time: t, edgeAt: t, duration: d), 1, accuracy: 1e-9)
        for (offset, expected) in [(1.0, 0.55), (2.0, 0.23), (3.0, 0.11)] {
            for sign in [-1.0, 1.0] {
                let w = ColorMomentTiming.weight(time: t + sign * offset / fps, edgeAt: t, duration: d)
                XCTAssertEqual(w, expected, accuracy: 0.03, "\(sign * offset) frames")
            }
        }
        XCTAssertEqual(ColorMomentTiming.weight(time: t - d / 2, edgeAt: t, duration: d), 0)
        XCTAssertEqual(ColorMomentTiming.weight(time: t + d / 2, edgeAt: t, duration: d), 0)
    }

    func testToneIsOnFromTheStartFrameToTheFrameBeforeTheEnd() {
        let moments = [Self.moment]
        func tone(_ frame: Int) -> ColorMoment.Tone? {
            ColorMomentTiming.sample(moments, endFade: nil, total: 4, frameRate: fps, at: Double(frame) / fps).tone
        }
        XCTAssertNil(tone(29)); XCTAssertEqual(tone(30), .mono); XCTAssertEqual(tone(74), .mono); XCTAssertNil(tone(75))
        // A server literal one ulp off the frame grid still lands on its frame.
        let offGrid = ColorMoment(id: "x", start: 1.0000000000000002, end: 2.4999999999999996, tone: .mono)
        XCTAssertEqual(ColorMomentTiming.sample([offGrid], endFade: nil, total: 4, frameRate: fps, at: 1.0).tone, .mono)
        XCTAssertNil(ColorMomentTiming.sample([offGrid], endFade: nil, total: 4, frameRate: fps, at: 2.5).tone)
    }

    func testEndFadeReachesBlackOnTheLastFrameAndNeverTouchesTheOutro() {
        let fade = EndFade(duration: 1.0)
        func amount(_ t: Double) -> Double { ColorMomentTiming.sample([], endFade: fade, total: 4, frameRate: fps, at: t).fadeToBlack }
        XCTAssertEqual(amount(2.99), 0)
        XCTAssertEqual(amount(3.0), 0)
        XCTAssertEqual(amount(119 / fps), 1, accuracy: 1e-9)
        XCTAssertEqual(amount(4.0), 0, "the outro after the edit is untouched")
        XCTAssertEqual(amount(3.5), 0.5 / (1 - 1 / fps), accuracy: 1e-9, "linear in between")
    }

    // MARK: wire

    func testRecipeWithoutMomentsEncodesNoNewKeys() throws {
        let plain = recipe(moments: [], endFade: nil, sound: false)
        let json = String(decoding: try RecipeJSON.encode(plain), as: UTF8.self)
        XCTAssertFalse(json.contains("color_moments")); XCTAssertFalse(json.contains("end_fade"))
        XCTAssertFalse(plain.effectiveCapabilities.contains(.colorMoments))
        XCTAssertTrue(recipe().effectiveCapabilities.contains(.colorMoments))
        XCTAssertTrue(recipe(moments: [], endFade: EndFade(duration: 1)).effectiveCapabilities.contains(.colorMoments))
    }

    func testParityFixtureDecodesAndValidatesLikeTheServer() throws {
        let root = URL(fileURLWithPath: #filePath)
        var url = root
        for _ in 0..<8 { url.deleteLastPathComponent() }
        let fixture = try JSONSerialization.jsonObject(with: Data(contentsOf: url.appendingPathComponent("tests/fixtures/colour-moments/parity-v1.json"))) as! [String: Any]
        for (index, case_) in (fixture["valid"] as! [[String: Any]]).enumerated() {
            var base = try JSONSerialization.jsonObject(with: try RecipeJSON.encode(recipe(moments: [], endFade: nil, sound: false))) as! [String: Any]
            for (key, value) in case_ where key != "name" { base[key] = value }
            let decoded = try RecipeJSON.decode(try JSONSerialization.data(withJSONObject: base))
            XCTAssertNoThrow(try decoded.validate(), "valid case \(index) \(case_["name"] ?? "")")
            XCTAssertTrue(decoded.effectiveCapabilities.contains(.colorMoments))
            // Round trip keeps the wire byte-for-byte equal to what was sent.
            let again = try JSONSerialization.jsonObject(with: try RecipeJSON.encode(decoded)) as! [String: Any]
            XCTAssertEqual(again["color_moments"] as? NSArray, base["color_moments"] as? NSArray, "round trip \(index)")
        }
        for (index, case_) in (fixture["invalid"] as! [[String: Any]]).enumerated() {
            var base = try JSONSerialization.jsonObject(with: try RecipeJSON.encode(recipe(moments: [], endFade: nil, sound: false))) as! [String: Any]
            for (key, value) in case_ where key != "name" { base[key] = value }
            let data = try JSONSerialization.data(withJSONObject: base)
            let rejected = (try? RecipeJSON.decode(data)).map { (try? $0.validate()) == nil } ?? true
            XCTAssertTrue(rejected, "invalid case \(index) \(case_["name"] ?? "") must be refused")
        }
    }

    // MARK: pixels

    private struct Measured { let luma: [Double]; let saturation: [Double]; let audioDB: [Double] }

    /// Per-frame mean luma (BT.601 on encoded sRGB, 0-255) and mean saturation (max-min) of the exported file,
    /// plus per-frame audio RMS in dBFS. Mirrors `scripts/verify-colour-moments.py`.
    @MainActor private func measure(_ url: URL) async throws -> Measured {
        let asset = AVURLAsset(url: url)
        let reader = try AVAssetReader(asset: asset)
        let videoTracks = try await asset.loadTracks(withMediaType: .video)
        let video = try XCTUnwrap(videoTracks.first)
        let videoOut = AVAssetReaderTrackOutput(track: video, outputSettings: [kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA])
        reader.add(videoOut)
        var audioOut: AVAssetReaderTrackOutput?
        if let audio = try await asset.loadTracks(withMediaType: .audio).first {
            let output = AVAssetReaderTrackOutput(track: audio, outputSettings: [AVFormatIDKey: kAudioFormatLinearPCM, AVLinearPCMBitDepthKey: 32,
                AVLinearPCMIsFloatKey: true, AVLinearPCMIsNonInterleaved: false, AVSampleRateKey: 48_000, AVNumberOfChannelsKey: 1])
            reader.add(output); audioOut = output
        }
        XCTAssertTrue(reader.startReading())
        var luma: [Double] = [], saturation: [Double] = []
        while let sample = videoOut.copyNextSampleBuffer(), let buffer = CMSampleBufferGetImageBuffer(sample) {
            CVPixelBufferLockBaseAddress(buffer, .readOnly)
            let (w, h, stride) = (CVPixelBufferGetWidth(buffer), CVPixelBufferGetHeight(buffer), CVPixelBufferGetBytesPerRow(buffer))
            let base = CVPixelBufferGetBaseAddress(buffer)!.assumingMemoryBound(to: UInt8.self)
            var y = 0.0, s = 0.0
            for row in Swift.stride(from: 0, to: h, by: 4) { for col in Swift.stride(from: 0, to: w, by: 4) {
                let p = base.advanced(by: row * stride + col * 4)
                let (b, g, r) = (Double(p[0]), Double(p[1]), Double(p[2]))
                y += 0.299 * r + 0.587 * g + 0.114 * b
                s += max(r, g, b) - min(r, g, b)
            } }
            let n = Double(((h + 3) / 4) * ((w + 3) / 4))
            luma.append(y / n); saturation.append(s / n)
            CVPixelBufferUnlockBaseAddress(buffer, .readOnly)
        }
        var samples: [Float] = []
        while let out = audioOut, let sample = out.copyNextSampleBuffer(), let block = CMSampleBufferGetDataBuffer(sample) {
            let length = CMBlockBufferGetDataLength(block)
            var chunk = [Float](repeating: 0, count: length / 4)
            _ = chunk.withUnsafeMutableBytes { CMBlockBufferCopyDataBytes(block, atOffset: 0, dataLength: length, destination: $0.baseAddress!) }
            samples += chunk
        }
        let hop = 48_000 / Int(fps)
        let audioDB = (0..<luma.count).map { frame -> Double in
            let slice = samples.dropFirst(frame * hop).prefix(hop)
            guard !slice.isEmpty else { return -120 }
            let power = slice.reduce(0.0) { $0 + Double($1 * $1) } / Double(slice.count)
            return 10 * log10(power + 1e-12)
        }
        return Measured(luma: luma, saturation: saturation, audioDB: audioDB)
    }

    @MainActor func testExportedFramesShowTheMomentOnItsFramesWithTheSoundOnTheDip() async throws {
        let directory = try makeDirectory(); defer { try? FileManager.default.removeItem(at: directory) }
        let wallURL = try await SolidVideoFixture.make(directory: directory, name: "wall", color: wall, seconds: 4.2, size: size)
        let urls = ["wall": wallURL, "hit": try writeHit(directory)]
        let edit = recipe()
        try edit.validate()
        let output = directory.appendingPathComponent("colour-moments.mp4")
        let checkpoint = try await AVFoundationLocalExporter(stateStore: FileExportStateStore(directory: directory.appendingPathComponent("state")), branding: .none)
            .export(recipe: edit, assetURLs: urls, outputURL: output)
        XCTAssertEqual(checkpoint.status, .completed)
        if let keep = ProcessInfo.processInfo.environment["KRIA_COLOR_MOMENTS_EVIDENCE_DIR"] {
            let dir = URL(fileURLWithPath: keep)
            try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
            try? FileManager.default.removeItem(at: dir.appendingPathComponent("colour-moments.mp4"))
            try FileManager.default.copyItem(at: output, to: dir.appendingPathComponent("colour-moments.mp4"))
            try RecipeJSON.encode(edit).write(to: dir.appendingPathComponent("colour-moments.recipe.json"))
        }
        let m = try await measure(output)
        XCTAssertEqual(m.luma.count, 120)
        let base = m.luma[10], baseSat = m.saturation[10]
        XCTAssertGreaterThan(baseSat, 60, "the wall is colourful before the moment")

        // Light leak into the moment: brightest frame exactly on the edge (frame 30), within the 1/3 s window only.
        let leakFrames = 24...36
        let leakPeak = leakFrames.max { m.luma[$0] < m.luma[$1] }!
        XCTAssertEqual(leakPeak, 30, "flash peak lands on the edge frame")
        XCTAssertGreaterThan(m.luma[30], 200, "near-white core")
        XCTAssertGreaterThan(m.saturation[27], baseSat, "warm burst on the shoulder, like the reference")
        XCTAssertEqual(m.luma[24], base, accuracy: 3, "untouched before the edge window")

        // Black-and-white for frames 30...74; the edges' own 1/3 s windows (±5 frames) still carry their flash.
        for frame in 36...69 { XCTAssertLessThan(m.saturation[frame], 6, "mono at frame \(frame)") }
        // Colour returns on frame 75; the dip bottoms out there.
        let dipFrames = 69...81
        let dipLow = dipFrames.min { m.luma[$0] < m.luma[$1] }!
        XCTAssertEqual(dipLow, 75, "dip bottoms out on the edge frame")
        XCTAssertLessThan(m.luma[75], 20)
        XCTAssertGreaterThan(m.saturation[81], 40, "colour is back after the dip")

        // The sound starts on the dip: loudest jump within one frame of frame 75.
        let onset = (6..<m.audioDB.count).first { m.audioDB[$0] > -40 && m.audioDB[$0] - m.audioDB[($0 - 6)..<$0].reduce(0, +) / 6 > 12 }
        XCTAssertNotNil(onset); XCTAssertEqual(Double(onset ?? 0), 75, accuracy: 1, "sound onset vs dip frame")

        // End fade: untouched at frame 89, linear to black by the last frame.
        XCTAssertEqual(m.luma[89], base, accuracy: 3)
        XCTAssertLessThan(m.luma[119], 20)
        XCTAssertGreaterThan(m.luma[100], m.luma[110]); XCTAssertGreaterThan(m.luma[110], m.luma[119])
    }

    @MainActor func testWithoutMomentsEveryFrameIsByteIdenticalToTheOldPath() async throws {
        let directory = try makeDirectory(); defer { try? FileManager.default.removeItem(at: directory) }
        let wallURL = try await SolidVideoFixture.make(directory: directory, name: "wall", color: wall, seconds: 2.2, size: size)
        let plain = EditRecipe(canvas: Canvas(width: size.0, height: size.1), frameRate: fps, assets: [MediaAsset(id: "wall", relativePath: "wall")],
            tracks: [TimelineTrack(id: "video", kind: .video, clips: [TimelineClip(id: "wall", sourceAssetID: "wall", sourceDuration: 2)])])
        let preview = try await AVPlayerPreviewComposer().makePreview(recipe: plain, assetURLs: ["wall": wallURL])
        let instructions = try XCTUnwrap(preview.playerItem.videoComposition?.instructions as? [RecipeVideoInstruction])
        XCTAssertTrue(instructions.allSatisfy { $0.colorMoments == nil }, "no post-process pass without moments")
        // A moment that ends before a frame leaves that frame byte-identical.
        let late = ColorMoment(id: "late", start: 1.5, end: 2.0, tone: .mono)
        var withMoment = plain; withMoment.colorMoments = [late]
        let a = try await previewFrame(plain, urls: ["wall": wallURL], at: 0.5)
        let b = try await previewFrame(withMoment, urls: ["wall": wallURL], at: 0.5)
        func pixels(_ f: SpeakerTestFrame) -> [[Int]] { (0..<f.height).flatMap { y in (0..<f.width).map { f.rgb($0, y) } } }
        XCTAssertEqual(pixels(a), pixels(b))
        let c = try await previewFrame(withMoment, urls: ["wall": wallURL], at: 1.7)
        XCTAssertNotEqual(pixels(a), pixels(c))
    }

    /// The tone is applied to the whole composed picture: a speaker over a background turns grey together (FM 8),
    /// while a caption keeps its authored colour (FM 9).
    @MainActor func testToneCoversEveryLayerButNotTheCaption() async throws {
        let directory = try makeDirectory(); defer { try? FileManager.default.removeItem(at: directory) }
        let wallURL = try await SolidVideoFixture.make(directory: directory, name: "wall", color: wall, seconds: 2.2, size: size)
        let card = try solidSpeakerTestImage(directory: directory, name: "card", color: CGColor(red: 0.1, green: 0.5, blue: 0.9, alpha: 1), size: (60, 60))
        var edit = EditRecipe(canvas: Canvas(width: size.0, height: size.1), frameRate: fps,
            assets: [MediaAsset(id: "wall", relativePath: "wall"), MediaAsset(id: "card", relativePath: "card")],
            tracks: [TimelineTrack(id: "video", kind: .video, clips: [TimelineClip(id: "wall", sourceAssetID: "wall", sourceDuration: 2)]),
                     TimelineTrack(id: "cards", kind: .overlay, clips: [TimelineClip(id: "card", sourceAssetID: "card", sourceDuration: 2,
                        transform: MediaTransform(scale: 0.3))])])
        edit.colorMoments = [ColorMoment(id: "m", start: 0, end: 2, tone: .mono)]
        let frame = try await previewFrame(edit, urls: ["wall": wallURL, "card": card], at: 1.0)
        for (fx, fy) in [(0.05, 0.05), (0.5, 0.5)] {
            let p = frame.rgb(fx: fx, fy: fy)
            XCTAssertLessThanOrEqual((p.max() ?? 0) - (p.min() ?? 0), 4, "grey at (\(fx), \(fy)): \(p)")
        }
        // A caption over the moment stays yellow.
        let caption = TextTreatment(text: "Hi", fontName: "Helvetica-Bold", fontSize: 90, colorRGBA: [1, 0.85, 0, 1], anchor: .center, animation: .none)
        let captionVideo = TimelineClip(id: "wall", sourceAssetID: "wall", sourceDuration: 2, text: caption)
        var withText = edit; withText.tracks[0].clips = [captionVideo]
        let texted = try await previewFrame(withText, urls: ["wall": wallURL, "card": card], at: 1.0)
        var yellow = 0
        for y in 0..<texted.height { for x in 0..<texted.width {
            let p = texted.rgb(x, y); if p[0] > 200 && p[1] > 160 && p[2] < 80 { yellow += 1 }
        } }
        XCTAssertGreaterThan(yellow, 50, "caption keeps its authored colour inside a mono moment")
    }
}
#endif
