import Foundation
import XCTest
import AVFoundation
import CoreMedia
import CoreGraphics
import KriaMediaEngine
@testable import Kria

private final class RequestLog: @unchecked Sendable { var urls: [String] = [] }

/// KRI-132 end-to-end proof, opt-in: renders recipes compiled by the server's
/// SECOND phone compiler -- `app.pipeline.phone_montage_plan
/// .compile_phone_montage_plan`, for the montage/day_vlog/single_hero
/// generative-edit archetypes -- through the production device resolver and
/// exporter on the simulator. Sibling of `DevicePhotoRenderE2ETests`
/// (KRI-121), which only ever exercises the guided compiler
/// (`compile_phone_guided_plan`) and never carries text layers or a music
/// track, so this file is the first device E2E coverage for the montage
/// compiler, for a real agent-text intro (fonts + `positionedText`/
/// `animatedText`), and for a licensed music bed (`.library` asset
/// resolution) on this harness.
///
/// `scripts/ios/phone-montage-render-e2e.py` writes `KRIA_E2E_DIR` with
/// four cases, each isolating one compiler branch:
///   - `cuts_text`  -- plain cuts, preserved original audio, an agent-text
///                     intro (basicComposition/positionedText/animatedText).
///   - `music`      -- a licensed music bed replaces the original audio, no
///                     text/transition (basicComposition/audioMix/musicBed).
///   - `crossfade`  -- a crossfade transition, no text/music
///                     (basicComposition/crossfade).
///   - `narration`  -- a recorded voiceover mixed under the clips' own audio
///                     (KRI-132; basicComposition/audioMix/narrationAudio).
///
/// `day_vlog`/`single_hero` are deliberately NOT separate cases: read
/// `app/pipeline/phone_montage_plan.py` in full -- `compile_phone_montage_plan`
/// never references `resolved_archetype` or `edit_format` anywhere. Archetype
/// only steers what the upstream matcher/decision phase selects as
/// `assembly_steps` BEFORE this compiler ever sees them; the compiler itself
/// treats every montage-family archetype identically, so a format-specific
/// phone case would just be a relabeled `cuts_text`/`crossfade` case, not a
/// new branch. See the script's module docstring for the full writeup.
///
/// Three more cases exercise the OTHER two KRI-132 phone compilers, neither
/// of which goes through `compile_phone_montage_plan`:
///   - `subtitled_sentence`/`subtitled_word` -- `app.pipeline
///     .phone_subtitled_plan.compile_phone_subtitled_plan` ("Talking to
///     camera"): one portrait clip, its own audio, sentence (`pop-in`) or
///     word (`karaoke-line`) captions.
///   - `narrated` -- `app.pipeline.phone_narrated_plan
///     .compile_phone_narrated_plan`: two clips tiled onto narration step
///     windows; the second clip is shorter than its step, exercising
///     `TimelineClip.rate < 1` (slow-down, never freeze-hold).
/// All three additionally carry a `caption_samples` list in `e2e.json`
/// (region derived from the compiled recipe's own text-layer geometry, see
/// the script's `_caption_region`), asserted in `assertCase` below via
/// `nearWhiteTextPixelCount`.
@MainActor final class DeviceMontageRenderE2ETests: XCTestCase {
    private typealias RGB = [Int]
    // Caption fill is white with a black outline (`_CAPTION_TEXT_COLOR` in
    // `phone_captions.py`); these only need to separate "some caption glyphs
    // rendered" from "none did" over a generously-padded region, not measure
    // exact coverage -- see `nearWhiteTextPixelCount`.
    private let captionPixelPresenceThreshold = 40
    private let captionPixelAbsenceCeiling = 5
    override func tearDown() { NativeEditorURLProtocol.handler = nil; super.tearDown() }

    func testPlainCutsWithOriginalAudioAndTextIntroRendersOnTheIPhone() async throws {
        try await assertCase("cuts_text")
    }

    func testLicensedMusicBedReplacesOriginalAudioOnTheIPhone() async throws {
        try await assertCase("music")
    }

    func testCrossfadeTransitionRendersOnTheIPhone() async throws {
        try await assertCase("crossfade")
    }

    /// KRI-132: a recorded voiceover mixed under the clips' own audio
    /// (mix=0.4), resolved through the same per-asset grant as the music
    /// case's `.library` asset -- now a `.voiceover` asset -- and played from
    /// the verified cache under a playable extension `PlayableAudioFile`
    /// gives it (the source tone is longer than the video timeline, so this
    /// also proves the compiler's narration-duration clamp end to end).
    func testVoiceoverNarrationRendersOnTheIPhone() async throws {
        try await assertCase("narration")
    }

    /// KRI-132: "Talking to camera" (subtitled) phone compiler --
    /// `app.pipeline.phone_subtitled_plan.compile_phone_subtitled_plan` --
    /// sentence-style (`pop-in`) captions over the source clip's own audio.
    func testSubtitledSentenceCaptionsRenderOnTheIPhone() async throws {
        try await assertCase("subtitled_sentence")
    }

    /// Same compiler, `caption_style="word"` -- per-word timings compile to
    /// the karaoke-line highlight sweep instead of plain pop-in blocks.
    func testSubtitledWordCaptionsRenderOnTheIPhone() async throws {
        try await assertCase("subtitled_word")
    }

    /// KRI-132: the narrated-walkthrough phone compiler --
    /// `app.pipeline.phone_narrated_plan.compile_phone_narrated_plan` --
    /// two clips tiled onto narration step windows; the second clip is
    /// shorter than its step so its `TimelineClip.rate` is exercised < 1
    /// (slow-down, never freeze-hold), captions on top, footage bed audible
    /// under the voice.
    func testNarratedWalkthroughRendersOnTheIPhone() async throws {
        try await assertCase("narrated")
    }

    // MARK: -

    private func assertCase(_ caseID: String) async throws {
        let input = try inputDirectory()
        let meta = try XCTUnwrap(
            JSONSerialization.jsonObject(with: Data(contentsOf: input.appendingPathComponent("e2e.json"))) as? [String: Any]
        )
        let verified = try XCTUnwrap(meta["verified_features"] as? [String])
        let cases = try XCTUnwrap(meta["cases"] as? [String: Any])
        let caseMeta = try XCTUnwrap(cases[caseID] as? [String: Any])

        let statusFile = try XCTUnwrap(caseMeta["status_file"] as? String)
        let status = try JSONDecoder().decode(
            DeviceRenderStatusResponse.self, from: Data(contentsOf: input.appendingPathComponent(statusFile))
        )
        let recipe = status.request.recipe

        // Route decision: local while every capability this case declares is
        // verified; dropping the case's own signature capability must fall
        // back to cloud, exactly mirroring `DevicePhotoRenderE2ETests`'s
        // stillImages/visualVideos pattern but for the montage compiler's own
        // capability set (positionedText / musicBed / crossfade).
        func route(_ features: [String]) -> ExportRoute {
            DeviceRenderSessions.decision(
                recipe, capabilities: PhoneRenderingCapabilities(enabled: true, recipeVersions: [2], verifiedFeatures: features)
            ).route
        }
        let requiredCapabilities = try XCTUnwrap(caseMeta["required_capabilities"] as? [String])
        XCTAssertEqual(Set(recipe.requiredCapabilities.map(\.rawValue)), Set(requiredCapabilities), caseID)
        XCTAssertEqual(route(verified), .local, caseID)
        let dropCapability = try XCTUnwrap(caseMeta["drop_capability"] as? String)
        XCTAssertEqual(route(verified.filter { $0 != dropCapability }), .cloud, "\(caseID): dropping \(dropCapability)")

        // Every montage clip is a device original bound at upload time --
        // unlike the guided compiler, this compiler never emits a `.visual`
        // (Visuals-pool) asset.
        let project = ProjectDirectory(root: FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString))
        defer { try? FileManager.default.removeItem(at: project.root) }
        try project.createIfNeeded()
        let store = SourceAssetStore(project: project)
        let clips = try XCTUnwrap(caseMeta["clips"] as? [[String: Any]])
        for clip in clips {
            let mediaID = try XCTUnwrap(clip["media_id"] as? String)
            let file = try XCTUnwrap(clip["file"] as? String)
            let destination = project.originals.appendingPathComponent(file)
            try FileManager.default.copyItem(at: input.appendingPathComponent(file), to: destination)
            try store.bind(
                mediaID: mediaID,
                original: MediaAsset(
                    id: mediaID, relativePath: "originals/\(file)",
                    fingerprint: try SHA256Fingerprinter().fingerprint(file: destination)
                )
            )
        }

        // The only non-original assets a montage recipe can carry are a
        // licensed music bed (`.library`, catalog "music") and, for the text
        // case, a bundled font (`.library`, catalog "font" -- installed from
        // the app bundle, no network grant; see `AuthorizedDeviceSourceResolver
        // .resolve`'s font branch). `AuthorizedDeviceSourceResolver` treats
        // `.library` exactly like `.visual`: one per-asset grant, one
        // download, one verified-cache install (KRI-121's seam, reused
        // as-is) -- so a licensed track needs no new resolver support, only
        // its bytes behind the same mocked grant endpoint Visuals already use.
        var bytes: [String: Data] = [:]
        if let musicAssetID = caseMeta["music_asset_id"] as? String, let musicFile = caseMeta["music_file"] as? String {
            bytes[musicAssetID] = try Data(contentsOf: input.appendingPathComponent(musicFile))
        }
        // KRI-132: a `.voiceover` asset resolves through the exact same
        // per-asset grant endpoint as `.library`/`.visual` -- same mock, new asset id.
        if let voiceoverAssetID = caseMeta["voiceover_asset_id"] as? String, let voiceoverFile = caseMeta["voiceover_file"] as? String {
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
        let library = RenderLibraryCache(root: project.root.appending(path: "library", directoryHint: .isDirectory))
        let resolver = AuthorizedDeviceSourceResolver(
            api: NativeEditorTestSupport.api(), request: status.request, originals: store,
            library: library, downloadSession: NativeEditorTestSupport.session()
        )
        let urls = try await resolver.resolve(for: recipe)
        if let musicAssetID = caseMeta["music_asset_id"] as? String {
            XCTAssertTrue(
                log.urls.contains { $0 == "https://storage.e2e.test/\(musicAssetID)" },
                "the music bed must download through the same per-asset grant Visuals use"
            )
        }
        if let voiceoverAssetID = caseMeta["voiceover_asset_id"] as? String {
            XCTAssertTrue(
                log.urls.contains { $0 == "https://storage.e2e.test/\(voiceoverAssetID)" },
                "the voiceover must download through the same per-asset grant Visuals/library use"
            )
        }

        let frames = input.appendingPathComponent("frames", isDirectory: true)
        try FileManager.default.createDirectory(at: frames, withIntermediateDirectories: true)
        let movie = frames.appendingPathComponent("\(caseID).mp4")
        try? FileManager.default.removeItem(at: movie)
        // Unbranded: this asserts the renderer reproduces the recipe, down to
        // exact duration and sampled pixels. Brand furniture is verified
        // separately by KriaMediaEngine's BrandingTests.
        let checkpoint = try await AVFoundationLocalExporter(
            stateStore: FileExportStateStore(directory: project.root.appendingPathComponent("state")),
            branding: .none
        ).export(recipe: recipe, assetURLs: urls, outputURL: movie)
        XCTAssertEqual(checkpoint.status, .completed, caseID)

        let asset = AVURLAsset(url: movie)
        let duration = try await asset.load(.duration).seconds
        let expectedDuration = try XCTUnwrap(caseMeta["duration_s"] as? Double)
        XCTAssertEqual(duration, expectedDuration, accuracy: 0.1, caseID)

        let audioTracks = try await asset.loadTracks(withMediaType: .audio)
        XCTAssertFalse(audioTracks.isEmpty, "\(caseID): every montage export always writes an AAC track")
        if caseMeta["expects_music_audio"] as? Bool == true {
            let peak = try await peakAmplitude(of: asset)
            XCTAssertGreaterThan(peak, 0.01, "\(caseID): the licensed music bed must be audible")
        }
        if caseMeta["expects_narration_audio"] as? Bool == true {
            let peak = try await peakAmplitude(of: asset)
            XCTAssertGreaterThan(peak, 0.01, "\(caseID): the recorded voiceover must be audible")
        }

        let generator = AVAssetImageGenerator(asset: asset)
        generator.requestedTimeToleranceBefore = .zero; generator.requestedTimeToleranceAfter = .zero
        let samples = try XCTUnwrap(caseMeta["samples"] as? [[String: Any]])
        for sample in samples {
            let name = try XCTUnwrap(sample["name"] as? String)
            let t = try XCTUnwrap(sample["t"] as? Double)
            let x = try XCTUnwrap(sample["x"] as? Int)
            let y = try XCTUnwrap(sample["y"] as? Int)
            let expected = try XCTUnwrap(sample["rgb"] as? [Int])
            let image = try await generator.image(at: CMTime(seconds: t, preferredTimescale: 600)).image
            XCTAssertEqual([image.width, image.height], [recipe.canvas.width, recipe.canvas.height], "\(caseID)/\(name)")
            let destination = try XCTUnwrap(
                CGImageDestinationCreateWithURL(frames.appendingPathComponent("\(caseID)-\(name).png") as CFURL, "public.png" as CFString, 1, nil)
            )
            CGImageDestinationAddImage(destination, image, nil)
            XCTAssertTrue(CGImageDestinationFinalize(destination))
            let observed = pixel(image, x: x, y: y)
            XCTAssertTrue(isColor(observed, expected), "\(caseID)/\(name) at \(t)s: \(observed) != \(expected)")
        }
        if let blend = caseMeta["blend_sample"] as? [String: Any] {
            let t = try XCTUnwrap(blend["t"] as? Double)
            let x = try XCTUnwrap(blend["x"] as? Int)
            let y = try XCTUnwrap(blend["y"] as? Int)
            let image = try await generator.image(at: CMTime(seconds: t, preferredTimescale: 600)).image
            let value = pixel(image, x: x, y: y)
            // The two crossfade clips are solid blue [0,0,255] and yellow
            // [255,255,0]; halfway through the overlap both channel families
            // must show through the blend, matching
            // `DevicePhotoRenderE2ETests`'s own crossfade-blend assertion style.
            XCTAssertTrue(value[2] > 40 && (value[0] > 40 || value[1] > 40), "\(caseID) crossfade blend: \(value)")
        }
        if let captionSamples = caseMeta["caption_samples"] as? [[String: Any]] {
            for sample in captionSamples {
                let name = try XCTUnwrap(sample["name"] as? String)
                let t = try XCTUnwrap(sample["t"] as? Double)
                let region = try XCTUnwrap(sample["region"] as? [Int])
                let expectText = try XCTUnwrap(sample["expect_text"] as? Bool)
                let image = try await generator.image(at: CMTime(seconds: t, preferredTimescale: 600)).image
                let count = try nearWhiteTextPixelCount(image, region: region)
                if expectText {
                    XCTAssertGreaterThan(
                        count, captionPixelPresenceThreshold,
                        "\(caseID)/\(name) at \(t)s: expected caption pixels in \(region), found \(count)"
                    )
                } else {
                    XCTAssertLessThanOrEqual(
                        count, captionPixelAbsenceCeiling,
                        "\(caseID)/\(name) at \(t)s: expected no caption pixels in \(region), found \(count)"
                    )
                }
            }
        }
    }

    private func inputDirectory() throws -> URL {
        guard let path = ProcessInfo.processInfo.environment["KRIA_E2E_DIR"] else { throw XCTSkip("Set KRIA_E2E_DIR to run") }
        return URL(fileURLWithPath: path)
    }

    /// Peak absolute sample amplitude (normalized 0...1) across the asset's
    /// first audio track, read directly via `AVAssetReader` -- no playback,
    /// no dependency on device volume/mute state.
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

    /// H.264/AAC round saturated colors by a few levels.
    private func isColor(_ pixel: RGB, _ expected: RGB) -> Bool { zip(pixel, expected).allSatisfy { abs($0 - $1) <= 40 } }

    /// `x`, `y` from the top-left, as the cloud renderer measures.
    private func pixel(_ image: CGImage, x: Int, y: Int) -> RGB {
        var rgba = [UInt8](repeating: 0, count: 4)
        let context = CGContext(data: &rgba, width: 1, height: 1, bitsPerComponent: 8, bytesPerRow: 4,
                                space: CGColorSpace(name: CGColorSpace.sRGB)!, bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)!
        context.draw(image, in: CGRect(x: -x, y: -(image.height - 1 - y), width: image.width, height: image.height))
        return rgba.prefix(3).map(Int.init)
    }

    /// Counts pixels within `region` (`[x0, y0, x1, y1]`, top-left origin, as
    /// Python's `_caption_region` reports them) that are bright and
    /// low-saturation -- a font-independent stand-in for "a caption glyph is
    /// there" (no OCR in this harness, mirroring `overlay_verify.py`'s own
    /// opaque-pixel bbox check). Generalizes `pixel(_:x:y:)`'s single-pixel
    /// draw offset to a whole sub-rectangle instead of one point.
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
