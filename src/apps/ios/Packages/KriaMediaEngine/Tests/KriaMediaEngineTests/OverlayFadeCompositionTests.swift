import XCTest
@testable import KriaMediaEngine
#if canImport(AVFoundation)
import AVFoundation
import CoreImage

/// KRI-182: an overlay clip's `overlayFadeIn`/`overlayFadeOut` fades its
/// opacity on the shared placement curve while its `transform` positioning
/// (the editor's cover-fit pip math) stays exactly as it was.
final class OverlayFadeCompositionTests: XCTestCase {
    private let width = 108, height = 192

    @MainActor func testFadedPipRampsOpacityWithoutMovingOrResizingTheCard() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let context = CIContext()
        let space = CGColorSpace(name: CGColorSpace.sRGB)!
        func write(_ name: String, _ color: CIColor, _ size: CGSize) throws -> URL {
            let url = directory.appendingPathComponent(name + ".png")
            try context.writePNGRepresentation(of: CIImage(color: color).cropped(to: CGRect(origin: .zero, size: size)),
                to: url, format: .RGBA8, colorSpace: space)
            return url
        }
        // A non-square card: the source shape where transform and placement sizing diverge.
        let urls = ["base": try write("base", CIColor(red: 0.1, green: 0.1, blue: 0.1), CGSize(width: 16, height: 16)),
                    "card": try write("card", CIColor(red: 0, green: 1, blue: 0), CGSize(width: 60, height: 20))]
        let assets = try urls.keys.sorted().map { MediaAsset(id: $0, relativePath: $0,
            fingerprint: try SHA256Fingerprinter().fingerprint(file: urls[$0]!)) }
        let manifest = try RenderAssetManifest(assets: assets.map { asset in
            RenderAssetReference(id: asset.id, fingerprint: try RenderFingerprint(asset.fingerprint!), source: .original(mediaID: asset.id))
        })
        // NativeEditorRenderCompiler's pip transform: cover-fit, then 0.4 of the canvas width.
        let cover = 60 * max(Double(width) / 60, Double(height) / 20)
        let transform = MediaTransform(scale: (Double(width) * 0.4).rounded(.toNearestOrEven) / cover,
            positionX: (0.6 * Double(width)).rounded(.toNearestOrEven) - Double(width) / 2,
            positionY: (0.7 * Double(height)).rounded(.toNearestOrEven) - Double(height) / 2)
        func render(fade: Bool, at times: [Double]) async throws -> [[UInt8]] {
            let card = TimelineClip(id: "card", sourceAssetID: "card", sourceDuration: 2, timelineStart: 1, transform: transform,
                volume: 0, overlayAboveText: true, overlayPreserveAlpha: true,
                overlayFadeIn: fade ? true : nil, overlayFadeOut: fade ? true : nil)
            let recipe = EditRecipe(schemaVersion: 2, rendererVersion: "kria-ios-2", canvas: Canvas(width: width, height: height),
                assets: assets, tracks: [
                    TimelineTrack(id: "v", kind: .video, clips: [TimelineClip(id: "base", sourceAssetID: "base", sourceDuration: 4)]),
                    TimelineTrack(id: "overlays", kind: .overlay, clips: [card]),
                ], assetManifest: manifest)
            try recipe.validate()
            let preview = try await AVPlayerPreviewComposer().makePreview(recipe: recipe, assetURLs: urls)
            let generator = AVAssetImageGenerator(asset: preview.playerItem.asset)
            generator.videoComposition = preview.playerItem.videoComposition
            generator.requestedTimeToleranceBefore = .zero
            generator.requestedTimeToleranceAfter = .zero
            var frames: [[UInt8]] = []
            for time in times {
                let frame = try await generator.image(at: CMTime(seconds: time, preferredTimescale: 600)).image
                var pixels = [UInt8](repeating: 0, count: width * height * 4)
                context.render(CIImage(cgImage: frame, options: [.colorSpace: NSNull()]), toBitmap: &pixels, rowBytes: width * 4,
                               bounds: CGRect(x: 0, y: 0, width: width, height: height), format: .RGBA8, colorSpace: nil)
                frames.append(pixels)
            }
            return frames
        }
        // Frame-aligned samples: two frames in, fully in, mid-window, two frames before the end.
        let times = [1 + 2.0 / 30, 1 + 4.0 / 30, 2, 3 - 2.0 / 30]
        let faded = try await render(fade: true, at: times)
        let plain = try await render(fade: false, at: [2])[0]

        // Fully faded in, the card is byte-identical to the unfaded one:
        // the fade changed opacity only, never size or position.
        XCTAssertEqual(faded[2], plain, "an opaque faded card must match the plain card pixel for pixel")

        // The card's center: 0.6 * 108 = 65 across, 0.7 * 192 = 134 down (bitmap rows are top-down).
        func green(_ pixels: [UInt8], x: Int, y: Int) -> Double { Double(pixels[(y * width + x) * 4 + 1]) }
        let (cx, cy) = (65, 134)
        let base = green(plain, x: 5, y: 5), opaque = green(plain, x: cx, y: cy)
        XCTAssertGreaterThan(opaque - base, 150, "the plain card must be visible over the base")
        for (index, time) in times.enumerated() {
            let expected = VisualMediaPlacement.fadeEnvelope(at: time, windowStart: 1, windowEnd: 3, fadeIn: true, fadeOut: true)
            let measured = (green(faded[index], x: cx, y: cy) - base) / (opaque - base)
            XCTAssertEqual(measured, expected, accuracy: 0.03, "card opacity at t=\(time)")
            // Pixels well away from the card are never touched by its fade.
            XCTAssertEqual(green(faded[index], x: 5, y: 5), base)
            XCTAssertEqual(green(faded[index], x: 100, y: 20), green(plain, x: 100, y: 20))
        }
        XCTAssertLessThan(green(faded[0], x: cx, y: cy), green(faded[1], x: cx, y: cy), "fade-in must rise")
        XCTAssertLessThan(green(faded[3], x: cx, y: cy), opaque, "fade-out must fall before the window ends")
    }

    func testOverlayFadeAlphaFollowsThePlacementCurve() {
        let clip = TimelineClip(id: "card", sourceAssetID: "card", sourceDuration: 2, timelineStart: 1,
                                overlayFadeIn: true, overlayFadeOut: true)
        let placement = VisualMediaPlacement(order: 0, windowStart: 1, windowEnd: 3, fadeIn: true, fadeOut: true)
        for time in stride(from: 0.9, through: 3.1, by: 0.025) {
            XCTAssertEqual(clip.overlayFadeAlpha(at: time), placement.alpha(at: time), accuracy: 1e-12, "t=\(time)")
        }
        XCTAssertEqual(clip.overlayFadeAlpha(at: 1), 0)
        XCTAssertEqual(clip.overlayFadeAlpha(at: 1.075), 0.5, accuracy: 1e-9)
        XCTAssertEqual(clip.overlayFadeAlpha(at: 2), 1)
        XCTAssertEqual(clip.overlayFadeAlpha(at: 3), 0)
        var inOnly = clip; inOnly.overlayFadeOut = nil
        XCTAssertEqual(inOnly.overlayFadeAlpha(at: 2.99), 1)
        let plain = TimelineClip(id: "card", sourceAssetID: "card", sourceDuration: 2, timelineStart: 1)
        XCTAssertEqual(plain.overlayFadeAlpha(at: 1), 1)
        // Like the placement path, a window of 0.3 s or less never fades.
        let short = TimelineClip(id: "card", sourceAssetID: "card", sourceDuration: 0.25, timelineStart: 1,
                                 overlayFadeIn: true, overlayFadeOut: true)
        XCTAssertEqual(short.overlayFadeAlpha(at: 1), 1)
    }

    func testOverlayFadeIsOverlayOnlyAndNeverDoublesAPlacementFade() throws {
        let fingerprint = AssetFingerprint(hex: String(repeating: "a", count: 64), byteCount: 10)
        let asset = MediaAsset(id: "card", relativePath: "card", fingerprint: fingerprint)
        let manifest = try RenderAssetManifest(assets: [RenderAssetReference(id: "card",
            fingerprint: try RenderFingerprint(fingerprint), source: .original(mediaID: "card"))])
        func recipe(onOverlayTrack: Bool, placement: VisualMediaPlacement? = nil, transition: Transition? = nil,
                    fadeIn: Bool? = true, fadeOut: Bool? = nil) -> EditRecipe {
            let base = TimelineClip(id: "base", sourceAssetID: "card", sourceDuration: 2)
            let faded = TimelineClip(id: "faded", sourceAssetID: "card", sourceDuration: 1,
                timelineStart: onOverlayTrack ? 0.5 : 2, transition: transition, volume: 0, visualPlacement: placement,
                overlayFadeIn: fadeIn, overlayFadeOut: fadeOut)
            let tracks = onOverlayTrack
                ? [TimelineTrack(id: "v", kind: .video, clips: [base]), TimelineTrack(id: "o", kind: .overlay, clips: [faded])]
                : [TimelineTrack(id: "v", kind: .video, clips: [base, faded])]
            return EditRecipe(schemaVersion: 2, rendererVersion: "kria-ios-2", canvas: Canvas(width: 16, height: 16),
                              assets: [asset], tracks: tracks, assetManifest: manifest)
        }
        XCTAssertNoThrow(try recipe(onOverlayTrack: true).validate())
        XCTAssertTrue(recipe(onOverlayTrack: true).effectiveCapabilities.contains(.editorMedia))
        XCTAssertThrowsError(try recipe(onOverlayTrack: false).validate(), "an overlay fade on the video track")
        XCTAssertThrowsError(try recipe(onOverlayTrack: true, placement: VisualMediaPlacement(order: 0,
            windowStart: 0.5, windowEnd: 1.5, fadeIn: true)).validate(), "a placed clip fades through its placement only")
        // The exit edge alone trips both guards too.
        XCTAssertThrowsError(try recipe(onOverlayTrack: false, fadeIn: nil, fadeOut: true).validate(), "an exit fade on the video track")
        XCTAssertThrowsError(try recipe(onOverlayTrack: true, placement: VisualMediaPlacement(order: 0,
            windowStart: 0.5, windowEnd: 1.5, fadeOut: true), fadeIn: nil, fadeOut: true).validate(), "an exit fade on a placed clip")
        // A transition ramp is a second fade source; alone it stays valid.
        XCTAssertThrowsError(try recipe(onOverlayTrack: true, transition: Transition(duration: 0.5)).validate(),
            "an overlay fade compounded with a transition ramp")
        XCTAssertThrowsError(try recipe(onOverlayTrack: true, transition: Transition(kind: .wipeLeft, duration: 0.5), fadeIn: nil, fadeOut: true).validate(),
            "an overlay fade routed through a clip-transition wipe")
        XCTAssertNoThrow(try recipe(onOverlayTrack: true, transition: Transition(duration: 0.5), fadeIn: nil).validate())
    }

    /// The renderer fades a card across the bounds its layer actually
    /// occupies (a video card's track can end up to a source frame off
    /// `clip.duration`), so the fade-out lands on 0 where the layer stops
    /// drawing instead of cutting off part-way.
    func testOverlayFadeWindowFollowsTheLayerBoundsNotTheIdealizedDuration() throws {
        let clip = TimelineClip(id: "card", sourceAssetID: "card", sourceDuration: 2, timelineStart: 1,
                                overlayFadeIn: true, overlayFadeOut: true)
        let layerEnd = 2.967 // one 30 fps frame short of the idealized 3.0
        let window = try XCTUnwrap(OverlayFadeWindow(clip: clip, start: 1, end: layerEnd))
        XCTAssertEqual(window.alpha(at: layerEnd), 0)
        XCTAssertEqual(window.alpha(at: layerEnd - 0.075), 0.5, accuracy: 1e-9)
        XCTAssertEqual(window.alpha(at: 1.075), 0.5, accuracy: 1e-9)
        XCTAssertGreaterThan(clip.overlayFadeAlpha(at: layerEnd), 0.2, "the idealized window would still be visible here")
        XCTAssertNil(OverlayFadeWindow(clip: TimelineClip(id: "plain", sourceAssetID: "card", sourceDuration: 2, timelineStart: 1),
                                       start: 1, end: layerEnd))
        var placed = clip
        placed.visualPlacement = VisualMediaPlacement(order: 0, windowStart: 1, windowEnd: 3, fadeIn: true, fadeOut: true)
        XCTAssertNil(OverlayFadeWindow(clip: placed, start: 1, end: 3), "a placed clip fades through its placement")
        XCTAssertEqual(placed.overlayFadeAlpha(at: 1), 1, "and so does the clip-level helper")
    }

    func testUnfadedClipEncodesWithoutFadeKeys() throws {
        // Recipes without a fade must encode exactly as before (stable digests).
        let data = try JSONEncoder().encode(TimelineClip(id: "card", sourceAssetID: "card", sourceDuration: 1))
        let json = try XCTUnwrap(String(data: data, encoding: .utf8))
        XCTAssertFalse(json.contains("overlayFade"))
        let faded = TimelineClip(id: "card", sourceAssetID: "card", sourceDuration: 1, overlayFadeIn: true)
        XCTAssertEqual(try JSONDecoder().decode(TimelineClip.self, from: JSONEncoder().encode(faded)), faded)
    }

    /// The actual wire format (`RecipeJSON`) snake_cases keys, distinct from
    /// the plain camelCase `JSONEncoder` above. Unlike `sourceAssetID`
    /// (explicitly overridden in `CodingKeys` because Swift's automatic
    /// converter mangles the acronym), "overlayFadeIn"/"overlayFadeOut" have
    /// no acronym, so nothing pins that `convertToSnakeCase`/
    /// `convertFromSnakeCase` actually agree on them -- a server/client
    /// mismatch here would silently drop the fade over the wire.
    func testFadeKeysRoundTripThroughTheSnakeCaseWireFormat() throws {
        let encoder = JSONEncoder(); encoder.keyEncodingStrategy = .convertToSnakeCase
        let decoder = JSONDecoder(); decoder.keyDecodingStrategy = .convertFromSnakeCase
        let faded = TimelineClip(id: "card", sourceAssetID: "card", sourceDuration: 1, overlayFadeIn: true, overlayFadeOut: true)
        let data = try encoder.encode(faded)
        let json = try XCTUnwrap(String(data: data, encoding: .utf8))
        XCTAssertTrue(json.contains("\"overlay_fade_in\":true"), json)
        XCTAssertTrue(json.contains("\"overlay_fade_out\":true"), json)
        XCTAssertEqual(try decoder.decode(TimelineClip.self, from: data), faded)
    }

    /// KRI-182: the editor applies an entrance/exit-token edit through
    /// `LivePreviewComposition.updateText`'s fast, text-only path -- the same
    /// path `testStyledOverlayKeepsLayerOrderAgainstLegacyPeer` (NativeCompositionTests)
    /// pins for a `visualPlacement` edit. `withoutGains` must treat
    /// `overlayFadeIn`/`overlayFadeOut` like the other overlay-only fields it
    /// already ignores for the old-vs-new track comparison, and the rebuilt
    /// layer must pick up a fresh `OverlayFadeWindow` -- otherwise toggling a
    /// card's fade in the editor would either throw (forcing a full, visible
    /// reload) or silently not animate until the next full reload.
    @MainActor func testLiveUpdateTogglingOverlayFadeAppliesWithoutRebuildingTheComposition() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let context = CIContext()
        let space = CGColorSpace(name: CGColorSpace.sRGB)!
        let side = 32
        func write(_ name: String, _ color: CIColor) throws -> URL {
            let url = directory.appendingPathComponent(name + ".png")
            try context.writePNGRepresentation(of: CIImage(color: color).cropped(to: CGRect(x: 0, y: 0, width: side, height: side)),
                to: url, format: .RGBA8, colorSpace: space)
            return url
        }
        // A card the exact size of the canvas: its cover-fit transform is the
        // identity, so it fills the frame edge to edge and any pixel is
        // representative -- no cover-fit math to get right in the test itself.
        let urls = ["base": try write("base", .black), "card": try write("card", CIColor(red: 0, green: 1, blue: 0))]
        let assets = try urls.keys.sorted().map { MediaAsset(id: $0, relativePath: $0, fingerprint: try SHA256Fingerprinter().fingerprint(file: urls[$0]!)) }
        let manifest = try RenderAssetManifest(assets: assets.map { asset in
            RenderAssetReference(id: asset.id, fingerprint: try RenderFingerprint(asset.fingerprint!), source: .original(mediaID: asset.id))
        })
        var recipe = EditRecipe(schemaVersion: 2, rendererVersion: "kria-ios-2", canvas: Canvas(width: side, height: side),
            assets: assets, tracks: [
                TimelineTrack(id: "v", kind: .video, clips: [TimelineClip(id: "base", sourceAssetID: "base", sourceDuration: 4)]),
                TimelineTrack(id: "overlays", kind: .overlay, clips: [
                    TimelineClip(id: "card", sourceAssetID: "card", sourceDuration: 2, timelineStart: 1, volume: 0, overlayPreserveAlpha: true),
                ]),
            ], assetManifest: manifest)
        try recipe.validate()
        func green(_ live: LivePreviewComposition, at time: Double) async throws -> Double {
            let generator = AVAssetImageGenerator(asset: live.preview.playerItem.asset)
            generator.videoComposition = live.preview.playerItem.videoComposition
            generator.requestedTimeToleranceBefore = .zero; generator.requestedTimeToleranceAfter = .zero
            let frame = try await generator.image(at: CMTime(seconds: time, preferredTimescale: 600)).image
            var pixels = [UInt8](repeating: 0, count: side * side * 4)
            context.render(CIImage(cgImage: frame, options: [.colorSpace: NSNull()]), toBitmap: &pixels, rowBytes: side * 4,
                           bounds: CGRect(x: 0, y: 0, width: side, height: side), format: .RGBA8, colorSpace: nil)
            let index = ((side / 2) * side + side / 2) * 4
            return Double(pixels[index + 1])
        }
        let live = try await LivePreviewComposition(recipe: recipe, assetURLs: urls)
        let asset = live.preview.playerItem.asset
        // Before any fade is authored the card is fully opaque the instant its window opens.
        let beforeFade = try await green(live, at: 1.05)
        XCTAssertGreaterThan(beforeFade, 200)

        recipe.tracks[1].clips[0].overlayFadeIn = true
        recipe.tracks[1].clips[0].overlayFadeOut = true
        try live.updateText(recipe: recipe)
        XCTAssertTrue(asset === live.preview.playerItem.asset, "a fade-only edit must take the fast path, not rebuild the composition")

        // 0.05s into the 0.15s ramp: alpha ~= 1/3. Compared against this same
        // live preview's own opaque frame (mid-window, past the ramp) so the
        // check is self-contained and immune to compositor blending noise.
        let ramping = try await green(live, at: 1.05)
        let opaque = try await green(live, at: 2)
        XCTAssertGreaterThan(opaque, 200)
        XCTAssertEqual(ramping / opaque, 1.0 / 3, accuracy: 0.1, "toggling the fade on must ramp the live preview, not just the recipe returned to callers")
        XCTAssertLessThan(ramping, beforeFade - 20, "the live preview must visibly dim once the fade is toggled on")

        // Toggling back off must also apply on the fast path.
        recipe.tracks[1].clips[0].overlayFadeIn = nil
        recipe.tracks[1].clips[0].overlayFadeOut = nil
        try live.updateText(recipe: recipe)
        XCTAssertTrue(asset === live.preview.playerItem.asset)
        let restored = try await green(live, at: 1.05)
        XCTAssertGreaterThan(restored, 200)
    }

    /// A card can independently author `entrance_token: "fade"` and
    /// `exit_token: "dissolve-out"` on the same clip (the two guards in
    /// `NativeEditorRenderCompiler` are per-edge and independent; only the
    /// exit edge is mutually exclusive between "fade" and "dissolve-out").
    /// `overlayFadeIn` scales a uniform `alpha`; `overlayDissolveSeed`
    /// reshapes the image's own alpha channel via a noise matte. They must
    /// compose without either silently overriding the other.
    @MainActor func testFadeInComposesWithDissolveOutOnTheSameClip() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let context = CIContext()
        let space = CGColorSpace(name: CGColorSpace.sRGB)!
        // The dissolve's own warp displacement is sized in absolute units
        // calibrated for realistic frame sizes (see phone_media_dissolve_composition.json,
        // 640x480) -- a tiny canvas gets fully displaced off-frame almost
        // immediately once the dissolve starts, which would make this test
        // measure canvas-size artifacts rather than the fade/dissolve compose.
        let side = 480
        func write(_ name: String, _ color: CIColor) throws -> URL {
            let url = directory.appendingPathComponent(name + ".png")
            try context.writePNGRepresentation(of: CIImage(color: color).cropped(to: CGRect(x: 0, y: 0, width: side, height: side)),
                to: url, format: .RGBA8, colorSpace: space)
            return url
        }
        let urls = ["base": try write("base", .black), "card": try write("card", CIColor(red: 0, green: 1, blue: 0))]
        let assets = try urls.keys.sorted().map { MediaAsset(id: $0, relativePath: $0, fingerprint: try SHA256Fingerprinter().fingerprint(file: urls[$0]!)) }
        let manifest = try RenderAssetManifest(assets: assets.map { asset in
            RenderAssetReference(id: asset.id, fingerprint: try RenderFingerprint(asset.fingerprint!), source: .original(mediaID: asset.id))
        })
        // A 4 s window: fade-in's own 0.15 s ramp and dissolve-out's own
        // (<=1 s) activation window near the clip's end don't overlap, so
        // each effect's math can be checked where it alone is active.
        let recipe = EditRecipe(schemaVersion: 2, rendererVersion: "kria-ios-2", canvas: Canvas(width: side, height: side),
            assets: assets, tracks: [
                TimelineTrack(id: "v", kind: .video, clips: [TimelineClip(id: "base", sourceAssetID: "base", sourceDuration: 6)]),
                TimelineTrack(id: "overlays", kind: .overlay, clips: [
                    TimelineClip(id: "card", sourceAssetID: "card", sourceDuration: 4, timelineStart: 1, volume: 0,
                        overlayPreserveAlpha: true, overlayDissolveSeed: 211, overlayFadeIn: true),
                ]),
            ], assetManifest: manifest)
        try recipe.validate()
        // Everything -- recipe is already built above, but the preview,
        // generator, and every sample -- stays inside one function and only
        // plain Int sums cross back out: passing an AVAsset/AVVideoComposition/
        // AVAssetImageGenerator across a nested-function boundary trips Swift 6
        // "sending risks a data race" even though every call stays on the main actor.
        func greenInk(at times: [Double]) async throws -> [Int] {
            let preview = try await AVPlayerPreviewComposer().makePreview(recipe: recipe, assetURLs: urls)
            let generator = AVAssetImageGenerator(asset: preview.playerItem.asset)
            generator.videoComposition = preview.playerItem.videoComposition
            generator.requestedTimeToleranceBefore = .zero; generator.requestedTimeToleranceAfter = .zero
            var sums: [Int] = []
            for time in times {
                let frame = try await generator.image(at: CMTime(seconds: time, preferredTimescale: 600)).image
                var pixels = [UInt8](repeating: 0, count: side * side * 4)
                context.render(CIImage(cgImage: frame, options: [.colorSpace: NSNull()]), toBitmap: &pixels, rowBytes: side * 4,
                               bounds: CGRect(x: 0, y: 0, width: side, height: side), format: .RGBA8, colorSpace: nil)
                sums.append(stride(from: 1, to: pixels.count, by: 4).reduce(0) { $0 + Int(pixels[$1]) })
            }
            return sums
        }
        // t=1.075: mid fade-in ramp (alpha ~= 0.5); dissolve's own window
        // (the clip's final 1 s, i.e. local time >= 3) hasn't started.
        // t=2: fade fully in, dissolve still inactive -- the true "opaque, undissolved" baseline.
        // t=4.3: fade long finished (fadeOut was never authored, so alpha stays 1),
        // but local time 3.3 s is inside the dissolve's final-1s activation window,
        // measured (see git history of this test) to sit at ~50% of baseline ink --
        // comfortably clear of both "untouched" and "fully wiped" to avoid flaking
        // on the dissolve's own noise-seeded, non-linear decay curve.
        let sums = try await greenInk(at: [1.075, 2, 4.3])
        let ramping = sums[0], opaque = sums[1], dissolving = sums[2]
        XCTAssertEqual(Double(ramping) / Double(opaque), 0.5, accuracy: 0.1, "the fade-in ramp must still apply with a dissolve-out also authored on the same clip")
        XCTAssertGreaterThan(Double(dissolving), Double(opaque) * 0.2, "a partially-dissolved card must not be blanked by the unrelated (already-resolved) fade-in state")
        XCTAssertLessThan(Double(dissolving), Double(opaque) * 0.8, "dissolve-out must still visibly reduce coverage once it activates, independent of the fade-in")
    }

    /// `overlayPopIn` scales the positioned image around its own center;
    /// `overlayFadeIn`/`overlayFadeOut` scale a uniform alpha applied to that
    /// same positioned image afterward. Neither should perturb the other.
    @MainActor func testFadeAppliesAlphaWithoutChangingPopInGeometry() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let context = CIContext()
        let space = CGColorSpace(name: CGColorSpace.sRGB)!
        let side = 32
        func write(_ name: String, _ color: CIColor) throws -> URL {
            let url = directory.appendingPathComponent(name + ".png")
            try context.writePNGRepresentation(of: CIImage(color: color).cropped(to: CGRect(x: 0, y: 0, width: side, height: side)),
                to: url, format: .RGBA8, colorSpace: space)
            return url
        }
        let urls = ["base": try write("base", .black), "card": try write("card", CIColor(red: 0, green: 1, blue: 0))]
        let assets = try urls.keys.sorted().map { MediaAsset(id: $0, relativePath: $0, fingerprint: try SHA256Fingerprinter().fingerprint(file: urls[$0]!)) }
        let manifest = try RenderAssetManifest(assets: assets.map { asset in
            RenderAssetReference(id: asset.id, fingerprint: try RenderFingerprint(asset.fingerprint!), source: .original(mediaID: asset.id))
        })
        // Recipe, preview, generator, and every sample stay inside one
        // function per variant, returning only plain [UInt8] pixel buffers:
        // passing an AVAsset/AVVideoComposition/AVAssetImageGenerator across a
        // nested-function boundary trips Swift 6 "sending risks a data race"
        // even though every call in this test stays on the main actor.
        func frames(fade: Bool, at times: [Double]) async throws -> [[UInt8]] {
            let card = TimelineClip(id: "card", sourceAssetID: "card", sourceDuration: 2, timelineStart: 1, volume: 0,
                overlayPopIn: true, overlayPreserveAlpha: true, overlayFadeIn: fade ? true : nil, overlayFadeOut: fade ? true : nil)
            let recipe = EditRecipe(schemaVersion: 2, rendererVersion: "kria-ios-2", canvas: Canvas(width: side, height: side),
                assets: assets, tracks: [
                    TimelineTrack(id: "v", kind: .video, clips: [TimelineClip(id: "base", sourceAssetID: "base", sourceDuration: 4)]),
                    TimelineTrack(id: "overlays", kind: .overlay, clips: [card]),
                ], assetManifest: manifest)
            try recipe.validate()
            let preview = try await AVPlayerPreviewComposer().makePreview(recipe: recipe, assetURLs: urls)
            let generator = AVAssetImageGenerator(asset: preview.playerItem.asset)
            generator.videoComposition = preview.playerItem.videoComposition
            generator.requestedTimeToleranceBefore = .zero; generator.requestedTimeToleranceAfter = .zero
            var results: [[UInt8]] = []
            for time in times {
                let image = try await generator.image(at: CMTime(seconds: time, preferredTimescale: 600)).image
                var pixels = [UInt8](repeating: 0, count: side * side * 4)
                context.render(CIImage(cgImage: image, options: [.colorSpace: NSNull()]), toBitmap: &pixels, rowBytes: side * 4,
                               bounds: CGRect(x: 0, y: 0, width: side, height: side), format: .RGBA8, colorSpace: nil)
                results.append(pixels)
            }
            return results
        }
        // t=1.075: fade's own 0.5 waypoint. t=1.165: between the fade's
        // 0.15 s ramp and pop-in's own 0.18 s ramp -- fade has already
        // resolved to alpha==1 while pop-in is still animating.
        let times = [1.075, 1.165]
        let alone = try await frames(fade: false, at: times)
        let faded = try await frames(fade: true, at: times)

        // With fade fully resolved (t=1.165) the two renders must be
        // pixel-for-pixel identical -- proof the fade never perturbs pop-in's
        // own transform.
        XCTAssertEqual(alone[1], faded[1], "pop-in geometry must be unaffected by an authored fade once the fade itself has finished ramping in")

        // Earlier, mid-ramp: fade must still measurably dim the card relative
        // to pop-in alone at the same instant.
        func centerGreen(_ pixels: [UInt8]) -> Double {
            let index = ((side / 2) * side + side / 2) * 4
            return Double(pixels[index + 1])
        }
        XCTAssertGreaterThan(centerGreen(alone[0]), 200, "pop-in alone must already be fully opaque (no fade authored)")
        XCTAssertEqual(centerGreen(faded[0]) / centerGreen(alone[0]), 0.5, accuracy: 0.08, "fade alpha must still apply on top of a pop-in card")
    }

    /// `Composition.swift` wires `layers[...].overlayFade = OverlayFadeWindow(clip:start:end:)`
    /// at two call sites: the still-image branch (covered above by every
    /// other test in this file, all of which use a PNG card) and the
    /// decoded-video-track branch, used by a video media-overlay card. This
    /// drives a short synthesized H.264 clip through the fade to prove the
    /// second call site is wired exactly like the first.
    @MainActor func testFadeAppliesToADecodedVideoOverlayCardNotJustAStillImage() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let context = CIContext()
        let space = CGColorSpace(name: CGColorSpace.sRGB)!
        let side = 32
        let baseURL = directory.appendingPathComponent("base.png")
        try context.writePNGRepresentation(of: CIImage(color: .black).cropped(to: CGRect(x: 0, y: 0, width: side, height: side)),
            to: baseURL, format: .RGBA8, colorSpace: space)
        let cardURL = try await Self.writeSolidVideo(directory: directory, name: "card",
            color: CGColor(red: 0, green: 1, blue: 0, alpha: 1), side: side, frames: 90)
        let urls = ["base": baseURL, "card": cardURL]
        let assets = [
            MediaAsset(id: "base", relativePath: "base", fingerprint: try SHA256Fingerprinter().fingerprint(file: baseURL)),
            MediaAsset(id: "card", relativePath: "card", fingerprint: try SHA256Fingerprinter().fingerprint(file: cardURL)),
        ]
        let manifest = try RenderAssetManifest(assets: assets.map { asset in
            RenderAssetReference(id: asset.id, fingerprint: try RenderFingerprint(asset.fingerprint!), source: .original(mediaID: asset.id))
        })
        let recipe = EditRecipe(schemaVersion: 2, rendererVersion: "kria-ios-2", canvas: Canvas(width: side, height: side),
            assets: assets, tracks: [
                TimelineTrack(id: "v", kind: .video, clips: [TimelineClip(id: "base", sourceAssetID: "base", sourceDuration: 4)]),
                TimelineTrack(id: "overlays", kind: .overlay, clips: [
                    TimelineClip(id: "card", sourceAssetID: "card", sourceDuration: 2, timelineStart: 1, volume: 0,
                        overlayPreserveAlpha: true, overlayFadeIn: true, overlayFadeOut: true),
                ]),
            ], assetManifest: manifest)
        try recipe.validate()
        // Preview, generator, and every sample stay inside one function,
        // returning only plain Doubles: passing an AVAsset/AVVideoComposition/
        // AVAssetImageGenerator across a nested-function boundary trips Swift 6
        // "sending risks a data race" even though every call stays on the main actor.
        func green(at times: [Double]) async throws -> [Double] {
            let preview = try await AVPlayerPreviewComposer().makePreview(recipe: recipe, assetURLs: urls)
            let generator = AVAssetImageGenerator(asset: preview.playerItem.asset)
            generator.videoComposition = preview.playerItem.videoComposition
            generator.requestedTimeToleranceBefore = .zero; generator.requestedTimeToleranceAfter = .zero
            var values: [Double] = []
            for time in times {
                let image = try await generator.image(at: CMTime(seconds: time, preferredTimescale: 600)).image
                var pixels = [UInt8](repeating: 0, count: side * side * 4)
                context.render(CIImage(cgImage: image, options: [.colorSpace: NSNull()]), toBitmap: &pixels, rowBytes: side * 4,
                               bounds: CGRect(x: 0, y: 0, width: side, height: side), format: .RGBA8, colorSpace: nil)
                let index = ((side / 2) * side + side / 2) * 4
                values.append(Double(pixels[index + 1]))
            }
            return values
        }
        let values = try await green(at: [1.075, 2])
        let ramping = values[0], opaque = values[1]
        XCTAssertGreaterThan(opaque, 200, "the decoded video card must be visible once its fade has ramped in")
        XCTAssertEqual(ramping / opaque, 0.5, accuracy: 0.1, "a decoded-video overlay card's fade must ramp exactly like a still-image card's")
    }

    /// Minimal solid-color H.264 fixture so a test can drive a real decoded
    /// video track (not a PNG) through the compositor.
    @MainActor private static func writeSolidVideo(directory: URL, name: String, color: CGColor, side: Int, frames: Int) async throws -> URL {
        let url = directory.appendingPathComponent(name + ".mp4")
        let writer = try AVAssetWriter(outputURL: url, fileType: .mp4)
        let input = AVAssetWriterInput(mediaType: .video, outputSettings: [AVVideoCodecKey: AVVideoCodecType.h264, AVVideoWidthKey: side, AVVideoHeightKey: side])
        let adaptor = AVAssetWriterInputPixelBufferAdaptor(assetWriterInput: input,
            sourcePixelBufferAttributes: [kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32ARGB,
                kCVPixelBufferWidthKey as String: side, kCVPixelBufferHeightKey as String: side])
        writer.add(input)
        XCTAssertTrue(writer.startWriting())
        writer.startSession(atSourceTime: .zero)
        for index in 0..<frames {
            while !input.isReadyForMoreMediaData { try await Task.sleep(for: .milliseconds(2)) }
            var buffer: CVPixelBuffer?
            CVPixelBufferPoolCreatePixelBuffer(nil, try XCTUnwrap(adaptor.pixelBufferPool), &buffer)
            let pixel = try XCTUnwrap(buffer)
            CVPixelBufferLockBaseAddress(pixel, [])
            let bitmapContext = try XCTUnwrap(CGContext(data: CVPixelBufferGetBaseAddress(pixel), width: side, height: side,
                bitsPerComponent: 8, bytesPerRow: CVPixelBufferGetBytesPerRow(pixel),
                space: CGColorSpace(name: CGColorSpace.sRGB)!, bitmapInfo: CGImageAlphaInfo.noneSkipFirst.rawValue))
            bitmapContext.setFillColor(color)
            bitmapContext.fill(CGRect(x: 0, y: 0, width: side, height: side))
            CVPixelBufferUnlockBaseAddress(pixel, [])
            XCTAssertTrue(adaptor.append(pixel, withPresentationTime: CMTime(value: Int64(index), timescale: 30)))
        }
        input.markAsFinished()
        await writer.finishWriting()
        XCTAssertEqual(writer.status, .completed)
        return url
    }
}
#endif
