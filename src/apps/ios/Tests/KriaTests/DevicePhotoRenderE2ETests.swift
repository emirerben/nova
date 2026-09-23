import Foundation
import XCTest
import AVFoundation
import CoreMedia
import CoreGraphics
import KriaMediaEngine
@testable import Kria

private final class RequestLog: @unchecked Sendable { var urls: [String] = [] }

/// KRI-121 end-to-end proof, opt-in: renders recipes compiled by the server's
/// `compile_phone_guided_plan` through the production device resolver and
/// exporter on the simulator. `scripts/ios/phone-photo-render-e2e.py` writes
/// `KRIA_E2E_DIR`: footage crossfading into a Visuals photo, the same photo as
/// a supporting card, a Visuals video and a transparent cutout; plus a project
/// made only of Visuals.
@MainActor final class DevicePhotoRenderE2ETests: XCTestCase {
    private typealias RGB = [Int]
    // Caption fill is white with a black outline (`_CAPTION_TEXT_COLOR` in
    // `phone_captions.py`); these only need to separate "some caption glyphs
    // rendered" from "none did" over a generously-padded region, not measure
    // exact coverage -- see `nearWhiteTextPixelCount`. Ported from
    // `DeviceMontageRenderE2ETests`.
    private let captionPixelPresenceThreshold = 40
    private let captionPixelAbsenceCeiling = 5
    override func tearDown() { NativeEditorURLProtocol.handler = nil; super.tearDown() }

    func testServerCompiledVisualsRenderOnTheIPhone() async throws {
        let input = try inputDirectory()
        let frames = try await render(status: "status.json", input: input, bindsFootage: true, output: "device-render",
                                      at: ["video": 1.0, "crossfade": 2.85, "photo": 4.0, "card": 5.7, "pool-video": 7.7, "cutout": 9.7])
        func sample(_ name: String, _ x: Int, _ y: Int) throws -> RGB { pixel(try XCTUnwrap(frames[name]), x: x, y: y) }
        // Footage: solid blue. Photo: cover-cropped, left half red, right half green.
        XCTAssertTrue(isColor(try sample("video", 270, 960), [0, 0, 255]))
        XCTAssertTrue(isColor(try sample("photo", 270, 960), [255, 0, 0]))
        XCTAssertTrue(isColor(try sample("photo", 810, 960), [0, 255, 0]))
        let blend = try sample("crossfade", 270, 960)
        XCTAssertTrue(blend[0] > 40 && blend[2] > 40, "crossfade mixes blue into red: \(blend)")
        // Card: the whole 4:3 photo (885x664) centered in a black 885x1382 card at
        // (96, 268), over a blurred cover of the same photo.
        XCTAssertTrue(isColor(try sample("card", 96 + 221, 960), [255, 0, 0]))
        XCTAssertTrue(isColor(try sample("card", 96 + 664, 960), [0, 255, 0]))
        XCTAssertTrue(isColor(try sample("card", 540, 268 + 120), [0, 0, 0]), "bars inside the card are black")
        XCTAssertTrue(isColor(try sample("card", 540, 1650 - 120), [0, 0, 0]))
        let blurLeft = try sample("card", 40, 120), blurRight = try sample("card", 1040, 120)
        XCTAssertTrue(blurLeft[0] > 180 && blurLeft[1] < 80, "blurred cover outside the card: \(blurLeft)")
        XCTAssertTrue(blurRight[1] > 180 && blurRight[0] < 80, "blurred cover outside the card: \(blurRight)")
        // Visuals video: solid yellow. Cutout: transparent center mattes to black
        // (never the white stored under it); opaque magenta edges are untouched.
        XCTAssertTrue(isColor(try sample("pool-video", 540, 960), [255, 255, 0]))
        XCTAssertTrue(isColor(try sample("cutout", 540, 960), [0, 0, 0]))
        XCTAssertTrue(isColor(try sample("cutout", 100, 100), [255, 0, 255]))
    }

    func testProjectMadeOnlyOfVisualsRendersOnTheIPhone() async throws {
        let input = try inputDirectory()
        let frames = try await render(status: "status-visuals-only.json", input: input, bindsFootage: false, output: "visuals-only",
                                      at: ["only-photo": 1.0, "only-pool-video": 3.0])
        XCTAssertTrue(isColor(pixel(try XCTUnwrap(frames["only-photo"]), x: 270, y: 960), [255, 0, 0]))
        XCTAssertTrue(isColor(pixel(try XCTUnwrap(frames["only-pool-video"]), x: 540, y: 960), [255, 255, 0]))
    }

    /// Opt-in editor-media proof: the server compiles the raw editor visual
    /// blocks into a separate overlay track. It contains a full-frame photo
    /// in both contain and cover modes, plus a higher-z video whose exact
    /// [2s, 4s] source trim is cyan. The fixture metadata carries all sample
    /// points so changing the fixture does not leave this test's assertions
    /// pointed at a stale recipe.
    func testServerCompiledEditorMediaRendersOnTheIPhone() async throws {
        let input = try inputDirectory()
        let meta = try XCTUnwrap(
            JSONSerialization.jsonObject(with: Data(contentsOf: input.appendingPathComponent("e2e.json"))) as? [String: Any]
        )
        let caseMeta = try XCTUnwrap(meta["editor_media"] as? [String: Any])
        let statusFile = try XCTUnwrap(caseMeta["status_file"] as? String)
        let samples = try XCTUnwrap(caseMeta["samples"] as? [[String: Any]])
        let times = try Dictionary(uniqueKeysWithValues: samples.map { sample in
            (try XCTUnwrap(sample["name"] as? String), try XCTUnwrap(sample["t"] as? Double))
        })

        let status = try JSONDecoder().decode(
            DeviceRenderStatusResponse.self,
            from: Data(contentsOf: input.appendingPathComponent(statusFile))
        )
        let overlay = try XCTUnwrap(status.request.recipe.tracks.first { $0.id == "editor-media" })
        let trimmedVideo = try XCTUnwrap(overlay.clips.first { $0.id == "editor-media-trimmed-video" })
        XCTAssertEqual(trimmedVideo.sourceStart, 2, accuracy: 0.001)
        XCTAssertEqual(trimmedVideo.sourceDuration, 2, accuracy: 0.001)
        XCTAssertEqual(trimmedVideo.visualPlacement?.contain, false)

        let frames = try await render(
            status: statusFile, input: input, bindsFootage: true, output: "editor-media", at: times,
            requiredEffectiveCapabilities: [.stillImages, .visualVideos, .visualBlocks],
            dropCapabilityChecks: [try XCTUnwrap(caseMeta["drop_capability"] as? String)]
        )
        for sample in samples {
            let name = try XCTUnwrap(sample["name"] as? String)
            let image = try XCTUnwrap(frames[name])
            let expected = try XCTUnwrap(sample["rgb"] as? RGB)
            XCTAssertTrue(
                isColor(pixel(image, x: try XCTUnwrap(sample["x"] as? Int), y: try XCTUnwrap(sample["y"] as? Int)), expected),
                "\(name) did not match \(expected)"
            )
        }
    }

    /// KRI-132: a voiceover-timed guided story -- 5 clips + 5 Visuals-pool
    /// photos tile the whole 48s narration duration
    /// (`compile_phone_guided_plan`'s new `narration:` parameter), a
    /// hard-replace narration audio track (`audio.originalVolume == 0`,
    /// unlike the montage/narrated compilers' mixed footage bed), an opening
    /// title, and one static caption per spoken word-group at y=0.82.
    /// Caption-region and narration-audio checks are ported from
    /// `DeviceMontageRenderE2ETests` (`caption_samples` / `expectsNarrationAudio`).
    func testNarratedStoryWithPhotosRendersOnTheIPhone() async throws {
        let input = try inputDirectory()
        let meta = try XCTUnwrap(
            JSONSerialization.jsonObject(with: Data(contentsOf: input.appendingPathComponent("e2e.json"))) as? [String: Any]
        )
        let caseMeta = try XCTUnwrap(meta["narrated_story"] as? [String: Any])
        let statusFile = try XCTUnwrap(caseMeta["status_file"] as? String)
        let footageClips = try XCTUnwrap(caseMeta["clips"] as? [[String: Any]])
        let captionSamples = try XCTUnwrap(caseMeta["caption_samples"] as? [[String: Any]])

        let frames = try await render(
            status: statusFile, input: input, bindsFootage: true, output: "narrated-story",
            at: ["clip0": 2.5, "clip2": 21.7, "photo0": 7.3, "photo3": 36.1],
            footageClips: footageClips,
            voiceoverAssetID: caseMeta["voiceover_asset_id"] as? String,
            voiceoverFile: caseMeta["voiceover_file"] as? String,
            captionSamples: captionSamples, expectsNarrationAudio: true,
            requiredEffectiveCapabilities: [.stillImages, .narrationAudio],
            dropCapabilityChecks: ["stillImages", "narrationAudio"]
        )
        func sample(_ name: String, _ x: Int, _ y: Int) throws -> RGB { pixel(try XCTUnwrap(frames[name]), x: x, y: y) }
        // Clips: solid red / lime. Photos: solid orange / teal.
        XCTAssertTrue(isColor(try sample("clip0", 540, 960), [255, 0, 0]))
        XCTAssertTrue(isColor(try sample("clip2", 540, 960), [0, 255, 0]))
        XCTAssertTrue(isColor(try sample("photo0", 540, 960), [255, 165, 0]))
        XCTAssertTrue(isColor(try sample("photo3", 540, 960), [0, 128, 128]))
    }

    /// "Clean up speech" on a guided narrated story: before planning, the
    /// server cut the recording's long pauses into a pinned WAV derivative
    /// (`app/services/guided_speech_cleanup.py`), so the recipe's single
    /// narration clip plays that cleaned file and captions follow the cleaned
    /// word times. Proves on the production exporter that the WAV decodes,
    /// the export matches the cleaned timeline rather than the raw recording,
    /// and no long pause survives in the rendered audio.
    func testCleanedNarratedStoryRendersOnTheIPhone() async throws {
        let input = try inputDirectory()
        let meta = try XCTUnwrap(
            JSONSerialization.jsonObject(with: Data(contentsOf: input.appendingPathComponent("e2e.json"))) as? [String: Any]
        )
        let caseMeta = try XCTUnwrap(meta["narrated_story_cleaned"] as? [String: Any])
        let samples = try XCTUnwrap(caseMeta["samples"] as? [[String: Any]])
        var times: [String: Double] = [:]
        for sample in samples { times[try XCTUnwrap(sample["name"] as? String)] = try XCTUnwrap(sample["t"] as? Double) }

        let frames = try await render(
            status: try XCTUnwrap(caseMeta["status_file"] as? String), input: input, bindsFootage: true,
            output: "narrated-story-cleaned", at: times,
            footageClips: try XCTUnwrap(caseMeta["clips"] as? [[String: Any]]),
            voiceoverAssetID: caseMeta["voiceover_asset_id"] as? String,
            voiceoverFile: caseMeta["voiceover_file"] as? String,
            captionSamples: try XCTUnwrap(caseMeta["caption_samples"] as? [[String: Any]]), expectsNarrationAudio: true,
            requiredEffectiveCapabilities: [.stillImages, .narrationAudio],
            dropCapabilityChecks: ["stillImages", "narrationAudio"]
        )
        for sample in samples {
            let name = try XCTUnwrap(sample["name"] as? String)
            let expected = try XCTUnwrap(sample["rgb"] as? [Int])
            let image = try XCTUnwrap(frames[name])
            XCTAssertTrue(
                isColor(pixel(image, x: try XCTUnwrap(sample["x"] as? Int), y: try XCTUnwrap(sample["y"] as? Int)), expected),
                "\(name) did not match \(expected)"
            )
        }

        let rawDuration = try XCTUnwrap(caseMeta["raw_voiceover_duration_s"] as? Double)
        let cleanedDuration = try XCTUnwrap(caseMeta["duration_s"] as? Double)
        XCTAssertLessThan(cleanedDuration, rawDuration - 5, "the recipe must follow the cleaned voiceover, not the raw recording")
        let movie = input.appendingPathComponent("frames/narrated-story-cleaned.mp4")
        let longest = try await longestSilentRun(of: AVURLAsset(url: movie))
        let allowed = try XCTUnwrap(caseMeta["max_silence_s"] as? Double)
        let rawLongest = try XCTUnwrap(caseMeta["raw_max_silence_s"] as? Double)
        XCTAssertLessThan(allowed, rawLongest, "fixture must contain pauses that cleanup removes")
        XCTAssertLessThanOrEqual(longest, allowed, "a removed pause is still audible in the export")
    }

    private func inputDirectory() throws -> URL {
        guard let path = ProcessInfo.processInfo.environment["KRIA_E2E_DIR"] else { throw XCTSkip("Set KRIA_E2E_DIR to run") }
        return URL(fileURLWithPath: path)
    }

    private func render(
        status name: String, input: URL, bindsFootage: Bool, output: String, at times: [String: Double],
        footageClips: [[String: Any]]? = nil,
        voiceoverAssetID: String? = nil, voiceoverFile: String? = nil,
        captionSamples: [[String: Any]] = [], expectsNarrationAudio: Bool = false,
        requiredEffectiveCapabilities: [MediaCapability] = [.stillImages, .visualVideos],
        dropCapabilityChecks: [String] = ["stillImages", "visualVideos"]
    ) async throws -> [String: CGImage] {
        let status = try JSONDecoder().decode(DeviceRenderStatusResponse.self, from: Data(contentsOf: input.appendingPathComponent(name)))
        let meta = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(contentsOf: input.appendingPathComponent("e2e.json"))) as? [String: Any])
        let recipe = status.request.recipe

        // The iPhone takes the local route only while every capability this
        // call cares about is verified.
        let verified = try XCTUnwrap(meta["verified_features"] as? [String])
        func route(_ features: [String]) -> ExportRoute {
            DeviceRenderSessions.decision(recipe, capabilities: PhoneRenderingCapabilities(enabled: true, recipeVersions: [2], verifiedFeatures: features)).route
        }
        XCTAssertTrue(recipe.effectiveCapabilities.isSuperset(of: requiredEffectiveCapabilities))
        let decision = DeviceRenderSessions.decision(recipe, capabilities: PhoneRenderingCapabilities(enabled: true, recipeVersions: [2], verifiedFeatures: verified))
        XCTAssertEqual(decision.route, .local, "\(decision.reason ?? "") missing=\(decision.missingCapabilities)")
        for capability in dropCapabilityChecks {
            XCTAssertEqual(route(verified.filter { $0 != capability }), .cloud, "dropping \(capability)")
        }

        // Footage is a device original bound at upload time; Visuals never are.
        let project = ProjectDirectory(root: FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString))
        defer { try? FileManager.default.removeItem(at: project.root) }
        try project.createIfNeeded()
        let store = SourceAssetStore(project: project)
        if bindsFootage {
            if let footageClips {
                // Multi-clip case (KRI-132 narrated_story): one bound original
                // per clip, mirroring DeviceMontageRenderE2ETests's own loop.
                for clip in footageClips {
                    let mediaID = try XCTUnwrap(clip["media_id"] as? String)
                    let file = try XCTUnwrap(clip["file"] as? String)
                    let original = project.originals.appendingPathComponent(file)
                    try FileManager.default.copyItem(at: input.appendingPathComponent(file), to: original)
                    try store.bind(mediaID: mediaID, original: MediaAsset(id: mediaID, relativePath: "originals/\(file)",
                                                                             fingerprint: try SHA256Fingerprinter().fingerprint(file: original)))
                }
            } else {
                let original = project.originals.appendingPathComponent("video.mp4")
                try FileManager.default.copyItem(at: input.appendingPathComponent("video.mp4"), to: original)
                let videoID = try XCTUnwrap(meta["video_media_id"] as? String)
                try store.bind(mediaID: videoID, original: MediaAsset(id: videoID, relativePath: "originals/video.mp4",
                                                                         fingerprint: try SHA256Fingerprinter().fingerprint(file: original)))
            }
        }

        // The API grants each pinned visual; storage serves its bytes. A
        // recorded voiceover (KRI-132) resolves through the SAME per-asset
        // grant endpoint under its own asset id -- see
        // DeviceMontageRenderE2ETests's own narration case.
        let files = try XCTUnwrap(meta["visual_files"] as? [String: String])
        let visualIDs = (recipe.assetManifest?.assets ?? []).compactMap { asset -> String? in if case .visual = asset.source { asset.id } else { nil } }
        var bytes = try Dictionary(uniqueKeysWithValues: visualIDs.map { ($0, try Data(contentsOf: input.appendingPathComponent(try XCTUnwrap(files[$0])))) })
        if let voiceoverAssetID, let voiceoverFile {
            bytes[voiceoverAssetID] = try Data(contentsOf: input.appendingPathComponent(voiceoverFile))
        }
        let log = RequestLog()
        NativeEditorURLProtocol.handler = { request in
            log.urls.append(request.url?.absoluteString ?? "")
            if request.url?.host == "storage.e2e.test" { return (200, bytes[request.url?.lastPathComponent ?? ""] ?? Data()) }
            let body = try JSONSerialization.jsonObject(with: NativeEditorTestSupport.bodyData(request)) as? [String: Any]
            let assetID = body?["asset_id"] as? String ?? ""
            return (200, Data(#"{"asset_id":"\#(assetID)","download_url":"https://storage.e2e.test/\#(assetID)","expires_at":"2099-01-01T00:00:00Z"}"#.utf8))
        }
        func resolver() -> AuthorizedDeviceSourceResolver {
            AuthorizedDeviceSourceResolver(api: NativeEditorTestSupport.api(), request: status.request, originals: store,
                library: RenderLibraryCache(root: project.root.appending(path: "library", directoryHint: .isDirectory)),
                downloadSession: NativeEditorTestSupport.session())
        }
        let urls = try await resolver().resolve(for: recipe)
        var expectedGrantedIDs = Set(visualIDs)
        if let voiceoverAssetID { expectedGrantedIDs.insert(voiceoverAssetID) }
        XCTAssertEqual(Set(log.urls.filter { $0.contains("storage.e2e.test") }), Set(expectedGrantedIDs.map { "https://storage.e2e.test/\($0)" }))
        // A second resolve renders from the verified cache: no grants, no downloads,
        // and videos still get a playable extension.
        log.urls = []
        let cached = try await resolver().resolve(for: recipe)
        XCTAssertEqual(cached, urls)
        XCTAssertEqual(log.urls, [])
        for asset in recipe.assetManifest?.assets ?? [] {
            guard case .visual(_, _, let kind) = asset.source else { continue }
            let url = try XCTUnwrap(urls[asset.id])
            if kind == .video { XCTAssertTrue(url.path.contains("visual-videos") && url.pathExtension == "mp4", url.path) }
        }

        let frames = input.appendingPathComponent("frames", isDirectory: true)
        try FileManager.default.createDirectory(at: frames, withIntermediateDirectories: true)
        let movie = frames.appendingPathComponent("\(output).mp4")
        try? FileManager.default.removeItem(at: movie)
        // Unbranded: asserts the renderer reproduces the recipe exactly.
        let checkpoint = try await AVFoundationLocalExporter(stateStore: FileExportStateStore(directory: project.root.appendingPathComponent("state")), branding: .none)
            .export(recipe: recipe, assetURLs: urls, outputURL: movie)
        XCTAssertEqual(checkpoint.status, .completed)

        let asset = AVURLAsset(url: movie)
        let duration = try await asset.load(.duration).seconds
        XCTAssertEqual(duration, TimelineMath.totalDuration(of: recipe), accuracy: 0.1)
        if bindsFootage {
            let audio = try await asset.loadTracks(withMediaType: .audio)
            XCTAssertFalse(audio.isEmpty, "source audio survives next to stills")
        }
        if expectsNarrationAudio {
            let peak = try await peakAmplitude(of: asset)
            XCTAssertGreaterThan(peak, 0.01, "the recorded voiceover must be audible")
        }
        let generator = AVAssetImageGenerator(asset: asset)
        generator.requestedTimeToleranceBefore = .zero; generator.requestedTimeToleranceAfter = .zero
        var images: [String: CGImage] = [:]
        for (name, time) in times {
            let image = try await generator.image(at: CMTime(seconds: time, preferredTimescale: 600)).image
            XCTAssertEqual([image.width, image.height], [recipe.canvas.width, recipe.canvas.height])
            let destination = try XCTUnwrap(CGImageDestinationCreateWithURL(frames.appendingPathComponent("\(name).png") as CFURL, "public.png" as CFString, 1, nil))
            CGImageDestinationAddImage(destination, image, nil)
            XCTAssertTrue(CGImageDestinationFinalize(destination))
            images[name] = image
        }
        for sample in captionSamples {
            let sampleName = try XCTUnwrap(sample["name"] as? String)
            let t = try XCTUnwrap(sample["t"] as? Double)
            let region = try XCTUnwrap(sample["region"] as? [Int])
            let expectText = try XCTUnwrap(sample["expect_text"] as? Bool)
            let image = try await generator.image(at: CMTime(seconds: t, preferredTimescale: 600)).image
            let count = try nearWhiteTextPixelCount(image, region: region)
            if expectText {
                XCTAssertGreaterThan(count, captionPixelPresenceThreshold, "\(sampleName) at \(t)s: expected caption pixels in \(region), found \(count)")
            } else {
                XCTAssertLessThanOrEqual(count, captionPixelAbsenceCeiling, "\(sampleName) at \(t)s: expected no caption pixels in \(region), found \(count)")
            }
        }
        return images
    }

    /// H.264 rounds saturated colors by a few levels.
    private func isColor(_ pixel: RGB, _ expected: RGB) -> Bool { zip(pixel, expected).allSatisfy { abs($0 - $1) <= 40 } }

    /// `x`, `y` from the top-left, as the cloud renderer measures.
    private func pixel(_ image: CGImage, x: Int, y: Int) -> RGB {
        var rgba = [UInt8](repeating: 0, count: 4)
        let context = CGContext(data: &rgba, width: 1, height: 1, bitsPerComponent: 8, bytesPerRow: 4,
                                space: CGColorSpace(name: CGColorSpace.sRGB)!, bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)!
        context.draw(image, in: CGRect(x: -x, y: -(image.height - 1 - y), width: image.width, height: image.height))
        return rgba.prefix(3).map(Int.init)
    }

    /// Peak absolute sample amplitude (normalized 0...1) across the asset's
    /// first audio track, read directly via `AVAssetReader` -- no playback,
    /// no dependency on device volume/mute state. Ported verbatim from
    /// `DeviceMontageRenderE2ETests`.
    private func peakAmplitude(of asset: AVAsset) async throws -> Float {
        guard let track = try await asset.loadTracks(withMediaType: .audio).first else { return 0 }
        let reader = try AVAssetReader(asset: asset)
        let outputSettings: [String: Any] = [
            AVFormatIDKey: kAudioFormatLinearPCM,
            AVLinearPCMIsFloatKey: true,
            AVLinearPCMBitDepthKey: 32,
            AVLinearPCMIsNonInterleaved: false,
        ]
        let output = AVAssetReaderTrackOutput(track: track, outputSettings: outputSettings)
        reader.add(output)
        _ = reader.startReading()
        var peak: Float = 0
        while let buffer = output.copyNextSampleBuffer() {
            guard let blockBuffer = CMSampleBufferGetDataBuffer(buffer) else { continue }
            let length = CMBlockBufferGetDataLength(blockBuffer)
            var data = [UInt8](repeating: 0, count: length)
            _ = CMBlockBufferCopyDataBytes(blockBuffer, atOffset: 0, dataLength: length, destination: &data)
            data.withUnsafeBytes { raw in
                for value in raw.bindMemory(to: Float32.self) { peak = max(peak, abs(value)) }
            }
        }
        return peak
    }

    /// Longest stretch (seconds) of the first audio track whose 10 ms windows
    /// all peak below `threshold`, read via `AVAssetReader` like `peakAmplitude`.
    private func longestSilentRun(of asset: AVAsset, threshold: Float = 0.01) async throws -> Double {
        guard let track = try await asset.loadTracks(withMediaType: .audio).first else { return .infinity }
        let reader = try AVAssetReader(asset: asset)
        let sampleRate = 48_000.0
        let output = AVAssetReaderTrackOutput(track: track, outputSettings: [
            AVFormatIDKey: kAudioFormatLinearPCM,
            AVLinearPCMIsFloatKey: true,
            AVLinearPCMBitDepthKey: 32,
            AVLinearPCMIsNonInterleaved: false,
            AVNumberOfChannelsKey: 1,
            AVSampleRateKey: sampleRate,
        ])
        reader.add(output)
        _ = reader.startReading()
        let window = Int(sampleRate / 100)
        var windowPeak: Float = 0, windowFill = 0, silentWindows = 0, longestWindows = 0
        while let buffer = output.copyNextSampleBuffer() {
            guard let blockBuffer = CMSampleBufferGetDataBuffer(buffer) else { continue }
            let length = CMBlockBufferGetDataLength(blockBuffer)
            var data = [UInt8](repeating: 0, count: length)
            _ = CMBlockBufferCopyDataBytes(blockBuffer, atOffset: 0, dataLength: length, destination: &data)
            data.withUnsafeBytes { raw in
                for value in raw.bindMemory(to: Float32.self) {
                    windowPeak = max(windowPeak, abs(value))
                    windowFill += 1
                    guard windowFill == window else { continue }
                    silentWindows = windowPeak < threshold ? silentWindows + 1 : 0
                    longestWindows = max(longestWindows, silentWindows)
                    windowPeak = 0; windowFill = 0
                }
            }
        }
        return Double(longestWindows) / 100
    }

    /// Counts pixels within `region` (`[x0, y0, x1, y1]`, top-left origin, as
    /// Python's `_caption_region` reports them) that are bright and
    /// low-saturation -- a font-independent stand-in for "a caption glyph is
    /// there" (no OCR in this harness). Ported verbatim from
    /// `DeviceMontageRenderE2ETests`.
    private func nearWhiteTextPixelCount(_ image: CGImage, region: [Int]) throws -> Int {
        guard region.count == 4 else { return 0 }
        let x0 = max(0, region[0]), y0 = max(0, region[1])
        let x1 = min(image.width, region[2]), y1 = min(image.height, region[3])
        let width = x1 - x0, height = y1 - y0
        guard width > 0, height > 0 else { return 0 }
        var rgba = [UInt8](repeating: 0, count: width * height * 4)
        let context = try XCTUnwrap(CGContext(
            data: &rgba, width: width, height: height, bitsPerComponent: 8, bytesPerRow: width * 4,
            space: CGColorSpace(name: CGColorSpace.sRGB)!, bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
        ))
        context.draw(image, in: CGRect(x: -x0, y: -(image.height - height - y0), width: image.width, height: image.height))
        var count = 0
        for i in stride(from: 0, to: rgba.count, by: 4) where rgba[i] > 200 && rgba[i + 1] > 200 && rgba[i + 2] > 200 {
            count += 1
        }
        return count
    }
}
