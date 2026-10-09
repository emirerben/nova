import Foundation
import XCTest
import AVFoundation
import CoreMedia
import CoreGraphics
import KriaMediaEngine
@testable import Kria

private final class RequestLog: @unchecked Sendable { var urls: [String] = [] }

/// KRI-132 end-to-end proof, opt-in: renders recipes compiled by the server's
/// SECOND phone compiler -- `app.pipeline.phone_voiceover_montage_plan
/// .compile_phone_voiceover_montage_plan`, for the montage/day_vlog/single_hero
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
/// `app/pipeline/phone_voiceover_montage_plan.py` in full -- `compile_phone_voiceover_montage_plan`
/// never references `resolved_archetype` or `edit_format` anywhere. Archetype
/// only steers what the upstream matcher/decision phase selects as
/// `assembly_steps` BEFORE this compiler ever sees them; the compiler itself
/// treats every montage-family archetype identically, so a format-specific
/// phone case would just be a relabeled `cuts_text`/`crossfade` case, not a
/// new branch. See the script's module docstring for the full writeup.
///
/// Four more cases exercise the OTHER two KRI-132 phone compilers, neither
/// of which goes through `compile_phone_voiceover_montage_plan`:
///   - `subtitled_sentence`/`subtitled_word` -- `app.pipeline
///     .phone_subtitled_plan.compile_phone_subtitled_plan` ("Talking to
///     camera"): one portrait clip, its own audio, sentence (`pop-in`) or
///     word (`karaoke-line`) captions.
///   - `talking_head` -- the same Subtitled compiler with KRI-136
///     `cutaways=...`: the speaker remains audible while a muted, full-frame
///     b-roll clip replaces only the intended video window.
///   - `narrated` -- `app.pipeline.phone_narrated_plan
///     .compile_phone_narrated_plan`: two clips tiled onto narration step
///     windows; the second clip is shorter than its step, exercising
///     `TimelineClip.rate < 1` (slow-down, never freeze-hold).
/// The captioned cases additionally carry a `caption_samples` list in `e2e.json`
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

    /// KRI-283: a landscape (1080x1920 pixels + 90 degree rotation) talking-to-camera clip compiled with
    /// `landscape_fit="fit"` letterboxes through the device exporter: black bars above/below the video
    /// band, red-left / blue-right preserved, and the caption drawn in the lower bar.
    func testSubtitledLandscapeLetterboxRendersOnTheIPhone() async throws {
        try await assertCase("subtitled_landscape_fit")
    }

    /// KRI-297: three FULL-SCREEN Visuals (red still, landscape blue still, green video) over a
    /// grey portrait speaker: each covers the whole frame (centre + four corners) in its window,
    /// the speaker is back between windows, speaker audio continues, captions stay on top.
    func testSubtitledFullscreenVisualsRenderOnTheIPhone() async throws {
        try await assertCase("subtitled_fullscreen_visuals")
    }

    /// KRI-257 / Plan 025 A3: multi-clip self-narrated Talking keeps the
    /// speaker's audio spine continuous, mutes the visual cutaway, draws that
    /// cutaway only in its scheduled window, and continues captions across it.
    func testTalkingHeadCutawayRendersOnTheIPhone() async throws {
        try await assertCase("talking_head")
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

    /// KRI-524: this case is generated by the server compiler from the saved
    /// word-by-word fading creation capture, then its portable text operation
    /// is replayed for “twice as fast”.  The generator writes both persisted
    /// draft boundaries beside the device recipe; this is not a hand-written
    /// Swift expectation masquerading as a creation result.
    func testCapturedCreationWordsRetimedThenExportOnTheIPhone() async throws {
        try await assertCase("creation_words_retimed")
    }

    func testCompoundWordTrimSavedThenExportOnTheIPhone() async throws {
        try await assertCase("compound_words_trim_save", expectedCreationProvenance: "authored_editor_fixture")
    }

    // MARK: -

    private func assertCase(_ caseID: String, expectedCreationProvenance: String = "authored_model_transport_fixture") async throws {
        let input = try inputDirectory()
        let meta = try XCTUnwrap(
            JSONSerialization.jsonObject(with: Data(contentsOf: input.appendingPathComponent("e2e.json"))) as? [String: Any]
        )
        let verified = try XCTUnwrap(meta["verified_features"] as? [String])
        let cases = try XCTUnwrap(meta["cases"] as? [String: Any])
        // KRI-220: the voiceover-less cases (cuts_text/music/crossfade) are retired from the
        // fixture -- `compile_phone_voiceover_montage_plan` now writes voiceover montages only.
        guard let caseMeta = cases[caseID] as? [String: Any] else {
            throw XCTSkip("\(caseID) is not in e2e.json (retired by KRI-220)")
        }

        let statusFile = try XCTUnwrap(caseMeta["status_file"] as? String)
        let status = try JSONDecoder().decode(
            DeviceRenderStatusResponse.self, from: Data(contentsOf: input.appendingPathComponent(statusFile))
        )
        let recipe = status.request.recipe

        if let expectedWords = caseMeta["expected_words"] as? [String] {
            // The compiler appends the two generated Location labels after
            // the creation sequence.  Preserve their independent coverage
            // while asserting the ordered word run at the front of the
            // actual recipe.
            let renderedWords = recipe.textLayers.prefix(expectedWords.count).map { $0.runs.map(\.text).joined() }
            XCTAssertEqual(renderedWords, expectedWords, "\(caseID): the exported recipe must retain each created word")
            XCTAssertEqual(
                try XCTUnwrap(recipe.textLayers.prefix(expectedWords.count).map(\.end).max()),
                try XCTUnwrap(caseMeta["expected_word_end_s"] as? Double),
                accuracy: 1.0 / 30.0,
                "\(caseID): the saved follow-up must halve the word sequence window"
            )
            let provenance = try XCTUnwrap(caseMeta["model_transport_provenance"] as? [String: String])
            XCTAssertEqual(provenance["creation"], expectedCreationProvenance)
        }

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
        // KRI-297: Visuals-pool assets (`.visual`) ride the same per-asset grant, keyed by asset id.
        if let visualFiles = caseMeta["visual_files"] as? [String: String] {
            for (assetID, file) in visualFiles { bytes[assetID] = try Data(contentsOf: input.appendingPathComponent(file)) }
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
        if let audioSamples = caseMeta["audio_samples"] as? [[String: Any]] {
            for sample in audioSamples {
                let name = try XCTUnwrap(sample["name"] as? String)
                let t = try XCTUnwrap(sample["t"] as? Double)
                let speakerHz = try XCTUnwrap(sample["speaker_hz"] as? Double)
                let mutedHz = try XCTUnwrap(sample["muted_hz"] as? Double)
                let speaker = try await toneAmplitude(of: asset, at: t, frequency: speakerHz)
                let cutaway = try await toneAmplitude(of: asset, at: t, frequency: mutedHz)
                XCTAssertGreaterThan(
                    speaker, 0.005,
                    "\(caseID)/\(name): speaker audio must remain audible at \(t)s"
                )
                XCTAssertLessThan(
                    cutaway, speaker * 0.2,
                    "\(caseID)/\(name): the cutaway's own audio must stay muted at \(t)s"
                )
            }
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
        if let element = caseMeta["title_element"] as? [String: Any] {
            try await assertTitlePreviewMatchesExport(
                caseID: caseID, recipe: recipe, urls: urls, export: asset, element: element,
                region: try XCTUnwrap(caseMeta["title_region"] as? [Int]),
                times: try XCTUnwrap(caseMeta["title_preview_samples"] as? [Double]),
                frames: frames, state: project.root.appendingPathComponent("preview-state")
            )
        }
    }

    /// KRI-455: the editor preview compiles a phone Voiceover title from the
    /// variant's title element (`narrated_title_text_elements`; read-only, or
    /// editable since KRI-465), never from the pinned recipe. Swap that compile into the export's own recipe,
    /// render both through the same exporter, and compare the title band: the
    /// lit text must cover the same box (the native layout keeps fractional
    /// line steps the server truncates, so allow a couple of pixels) and the
    /// same amount of the frame.
    private func assertTitlePreviewMatchesExport(
        caseID: String, recipe: KriaMediaEngine.EditRecipe, urls: [String: URL], export: AVURLAsset, element: [String: Any],
        region: [Int], times: [Double], frames: URL, state: URL
    ) async throws {
        let snapshot = try JSONDecoder().decode(
            [String: JSONValue].self, from: JSONSerialization.data(withJSONObject: ["text_elements": [element]])
        )
        let document = EditorDocument(snapshot: snapshot)
        let title = try XCTUnwrap(document.textElements.first)
        // The status route sends the title read-only (kill-switch shape) or, since
        // KRI-465, editable; the fixture may come from either server version.
        XCTAssertEqual(title.id, "narrated-title", "\(caseID): the voiceover title element")
        let compiler = try NativeEditorRenderCompiler(fontDirectory: XCTUnwrap(Bundle.main.url(forResource: "fonts", withExtension: nil)))
        // Only the text compile matters here; the placeholder footage is never read.
        let placeholder = ResolvedEditorSource(clipIndex: 0, mediaID: "placeholder", asset: MediaAsset(id: "placeholder",
            relativePath: "placeholder.mov", fingerprint: AssetFingerprint(hex: String(repeating: "a", count: 64), byteCount: 1)),
            url: URL(fileURLWithPath: "/placeholder.mov"))
        let span = title.endS + 1
        let clip = EditorClip(id: UUID(), assetID: UUID(), sourceClipIndex: 0, start: 0, end: span,
            trimIn: 0, trimOut: span, sourceDuration: span, slotID: "slot")
        // An editable title needs the timeline item the session builds for it; a
        // read-only one draws without one.
        let items = title.isReadOnly ? [] : [NativeEditorTimelineItem(
            selection: EditorSelection(kind: .text, id: title.id), start: title.startS, end: title.endS)]
        let previewTitle = try XCTUnwrap(
            compiler.compile(document: document, clips: [clip], items: items, sources: [0: placeholder]).recipe.textLayers.first
        )
        var preview = recipe
        let exportedTitle = try XCTUnwrap(preview.textLayers.firstIndex { $0.id.hasPrefix("title-") })
        preview.textLayers[exportedTitle] = previewTitle
        let movie = frames.appendingPathComponent("\(caseID)-title-preview.mp4")
        try? FileManager.default.removeItem(at: movie)
        let checkpoint = try await AVFoundationLocalExporter(stateStore: FileExportStateStore(directory: state), branding: .none)
            .export(recipe: preview, assetURLs: urls, outputURL: movie)
        XCTAssertEqual(checkpoint.status, .completed, "\(caseID) title preview")

        let exported = AVAssetImageGenerator(asset: export)
        exported.requestedTimeToleranceBefore = .zero; exported.requestedTimeToleranceAfter = .zero
        let previewed = AVAssetImageGenerator(asset: AVURLAsset(url: movie))
        previewed.requestedTimeToleranceBefore = .zero; previewed.requestedTimeToleranceAfter = .zero
        for t in times {
            let time = CMTime(seconds: t, preferredTimescale: 600)
            let a = try await exported.image(at: time).image
            let b = try await previewed.image(at: time).image
            for (image, kind) in [(a, "export"), (b, "preview")] {
                let destination = try XCTUnwrap(CGImageDestinationCreateWithURL(
                    frames.appendingPathComponent("\(caseID)-title-\(kind)-\(t).png") as CFURL, "public.png" as CFString, 1, nil))
                CGImageDestinationAddImage(destination, image, nil)
                XCTAssertTrue(CGImageDestinationFinalize(destination))
            }
            let lit = try litBox(a, region: region), litPreview = try litBox(b, region: region)
            print("[title-preview] \(caseID) t=\(t) export=\(lit) preview=\(litPreview)")
            XCTAssertGreaterThan(lit.count, captionPixelPresenceThreshold, "\(caseID) t=\(t): the exported title is on screen")
            XCTAssertEqual(Double(litPreview.count), Double(lit.count), accuracy: Double(lit.count) * 0.05,
                           "\(caseID) t=\(t): the preview lights as much of the frame as the export")
            for (side, edge) in ["left", "top", "right", "bottom"].enumerated() {
                XCTAssertEqual(litPreview.box[side], lit.box[side], accuracy: 3, "\(caseID) t=\(t): title \(edge) edge")
            }
        }
    }

    /// Bright, low-saturation pixels in `region` (as `nearWhiteTextPixelCount`
    /// counts them) and their bounding box `[left, top, right, bottom]` in
    /// frame pixels, top-left origin.
    private func litBox(_ image: CGImage, region: [Int]) throws -> (count: Int, box: [Double]) {
        let x0 = max(0, region[0]), y0 = max(0, region[1])
        let x1 = min(image.width, region[2]), y1 = min(image.height, region[3])
        let width = x1 - x0, height = y1 - y0
        var rgba = [UInt8](repeating: 0, count: width * height * 4)
        let context = try XCTUnwrap(CGContext(
            data: &rgba, width: width, height: height, bitsPerComponent: 8, bytesPerRow: width * 4,
            space: CGColorSpace(name: CGColorSpace.sRGB)!, bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
        ))
        context.draw(image, in: CGRect(x: -x0, y: -(image.height - height - y0), width: image.width, height: image.height))
        var count = 0, left = width, top = height, right = -1, bottom = -1
        for row in 0..<height {
            for column in 0..<width {
                let i = (row * width + column) * 4
                guard rgba[i] > 200, rgba[i + 1] > 200, rgba[i + 2] > 200 else { continue }
                count += 1
                left = min(left, column); right = max(right, column); top = min(top, row); bottom = max(bottom, row)
            }
        }
        return (count, [Double(x0 + left), Double(y0 + top), Double(x0 + right), Double(y0 + bottom)])
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

    /// Correlates a short mono PCM window with one sine/cosine pair. Fixture
    /// clips deliberately use distinct tones, so this proves the speaker's
    /// audio survives throughout a cutaway and that the cutaway source never
    /// enters the audio mix without relying on simulator playback volume.
    private func toneAmplitude(of asset: AVAsset, at time: Double, frequency: Double) async throws -> Double {
        let audioTracks = try await asset.loadTracks(withMediaType: .audio)
        let track = try XCTUnwrap(audioTracks.first)
        let reader = try AVAssetReader(asset: asset)
        let sampleRate = 48_000.0
        let window = 0.3
        reader.timeRange = CMTimeRange(
            start: CMTime(seconds: max(0, time - window / 2), preferredTimescale: 48_000),
            duration: CMTime(seconds: window, preferredTimescale: 48_000)
        )
        let output = AVAssetReaderTrackOutput(track: track, outputSettings: [
            AVFormatIDKey: kAudioFormatLinearPCM,
            AVSampleRateKey: sampleRate,
            AVNumberOfChannelsKey: 1,
            AVLinearPCMIsFloatKey: true,
            AVLinearPCMBitDepthKey: 32,
            AVLinearPCMIsNonInterleaved: false,
        ])
        reader.add(output)
        XCTAssertTrue(reader.startReading())
        var pcm: [Float] = []
        while let buffer = output.copyNextSampleBuffer() {
            guard let blockBuffer = CMSampleBufferGetDataBuffer(buffer) else { continue }
            let length = CMBlockBufferGetDataLength(blockBuffer)
            var data = [UInt8](repeating: 0, count: length)
            _ = CMBlockBufferCopyDataBytes(blockBuffer, atOffset: 0, dataLength: length, destination: &data)
            data.withUnsafeBytes { raw in
                pcm += raw.bindMemory(to: Float32.self)
            }
        }
        let edge = min(Int(sampleRate * 0.02), pcm.count / 4)
        let samples = Array(pcm.dropFirst(edge).dropLast(edge))
        guard samples.count > 100 else { return 0 }
        var sine = 0.0, cosine = 0.0
        for (index, value) in samples.enumerated() {
            let phase = 2 * Double.pi * frequency * Double(index) / sampleRate
            sine += Double(value) * sin(phase)
            cosine += Double(value) * cos(phase)
        }
        return 2 * hypot(sine, cosine) / Double(samples.count)
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
