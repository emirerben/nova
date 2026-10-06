import ImageIO
import KriaMediaEngine
import UniformTypeIdentifiers
import XCTest
@testable import Kria

/// KRI-463: image-only slide posts render on the phone from the original photo and never touch the server render.
@MainActor final class SlidePostOnDeviceRenderTests: XCTestCase {
    private let itemID = "22222222-2222-2222-2222-222222222222"
    private var defaults: UserDefaults!
    private var suite = ""
    private var directory: URL!

    override func setUp() {
        super.setUp()
        suite = "SlidePostOnDeviceRenderTests.\(UUID())"; defaults = UserDefaults(suiteName: suite)
        directory = FileManager.default.temporaryDirectory.appending(path: "ondevice-\(UUID())")
        try? FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
    }
    override func tearDown() {
        NativeEditorURLProtocol.handler = nil
        defaults.removePersistentDomain(forName: suite)
        try? FileManager.default.removeItem(at: directory)
        super.tearDown()
    }

    /// Left half red, right half blue. `orientation` 6 stores it rotated: shown upright, red is on TOP.
    private func photo(name: String, width: Int = 400, height: Int = 200, orientation: Int? = nil) throws -> URL {
        let format = UIGraphicsImageRendererFormat(); format.scale = 1; format.opaque = true
        let image = UIGraphicsImageRenderer(size: CGSize(width: width, height: height), format: format).image { _ in
            UIColor.red.setFill(); UIRectFill(CGRect(x: 0, y: 0, width: width / 2, height: height))
            UIColor.blue.setFill(); UIRectFill(CGRect(x: width / 2, y: 0, width: width - width / 2, height: height))
        }
        let url = directory.appending(path: name)
        let destination = try XCTUnwrap(CGImageDestinationCreateWithURL(url as CFURL, UTType.jpeg.identifier as CFString, 1, nil))
        var properties: [CFString: Any] = [kCGImageDestinationLossyCompressionQuality: 1.0]
        if let orientation { properties[kCGImagePropertyOrientation] = orientation }
        CGImageDestinationAddImage(destination, try XCTUnwrap(image.cgImage), properties as CFDictionary)
        XCTAssertTrue(CGImageDestinationFinalize(destination))
        return url
    }

    private func asset(_ id: String, source: URL?, display: URL? = nil, kind: String = "image") -> SlidePostAsset {
        SlidePostAsset(id: id, kind: kind, status: "ready", displayURL: display, sourceURL: source)
    }
    private func slide(_ id: String, texts: [SlidePostTextElement]? = nil, look: String = "none", kind: String = "image") -> SlidePostSlide {
        SlidePostSlide(id: id, assetID: "asset-\(id)", kind: kind, edits: SlidePostEdits(lookPreset: look, texts: texts))
    }
    private func caption(_ text: String = "Hello") -> SlidePostTextElement {
        var element = SlidePostTextElement(id: "t", text: text)
        element.position = "center"; element.sizePx = 160
        return element
    }

    private struct Pixel { let r: Int, g: Int, b: Int }
    private func decoded(_ data: Data) throws -> (size: CGSize, pixel: (Double, Double) -> Pixel, bitmap: [UInt8], width: Int, height: Int) {
        let cg = try XCTUnwrap(UIImage(data: data)?.cgImage)
        let width = cg.width, height = cg.height
        var bytes = [UInt8](repeating: 0, count: width * height * 4)
        let context = CGContext(data: &bytes, width: width, height: height, bitsPerComponent: 8, bytesPerRow: width * 4,
                                space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue)!
        context.draw(cg, in: CGRect(x: 0, y: 0, width: width, height: height))
        let snapshot = bytes
        return (CGSize(width: width, height: height), { fx, fy in
            let i = (Int(fy * Double(height - 1)) * width + Int(fx * Double(width - 1))) * 4
            return Pixel(r: Int(snapshot[i]), g: Int(snapshot[i + 1]), b: Int(snapshot[i + 2]))
        }, snapshot, width, height)
    }

    private func render(_ slide: SlidePostSlide, _ asset: SlidePostAsset, profile: String = "instagram_carousel") async throws -> Data {
        try await SlidePostOnDeviceRender.renderSlide(slide: slide, asset: asset, profile: profile)
    }

    // MARK: Rendering

    func testOutputSizeMatchesProfileAtTwoX() async throws {
        let url = try photo(name: "a.jpg")
        let insta = try decoded(try await render(slide("a"), asset("asset-a", source: url)))
        XCTAssertEqual(insta.size, CGSize(width: 2160, height: 2700))
        let tiktok = try decoded(try await render(slide("a"), asset("asset-a", source: url), profile: "tiktok_photo"))
        XCTAssertEqual(tiktok.size, CGSize(width: 2160, height: 3840))
    }

    func testExifOrientationIsAppliedBeforeCropping() async throws {
        let url = try photo(name: "rot.jpg", orientation: 6)   // 6 = rotate 90 CW: red half ends up on top
        let out = try decoded(try await render(slide("a"), asset("asset-a", source: url)))
        let top = out.pixel(0.5, 0.2), bottom = out.pixel(0.5, 0.8)
        XCTAssertGreaterThan(top.r, 200); XCTAssertLessThan(top.b, 60)
        XCTAssertGreaterThan(bottom.b, 200); XCTAssertLessThan(bottom.r, 60)
    }

    func testUnorientedLandscapePhotoIsCoverCroppedLeftRight() async throws {
        let url = try photo(name: "plain.jpg")
        let out = try decoded(try await render(slide("a"), asset("asset-a", source: url)))
        XCTAssertGreaterThan(out.pixel(0.2, 0.5).r, 200)
        XCTAssertGreaterThan(out.pixel(0.8, 0.5).b, 200)
    }

    func testTextIsDrawnOverThePhoto() async throws {
        let url = try photo(name: "t.jpg")
        let plain = try decoded(try await render(slide("a"), asset("asset-a", source: url)))
        let texted = try decoded(try await render(slide("a", texts: [caption()]), asset("asset-a", source: url)))
        XCTAssertEqual(texted.size, plain.size)
        var changed = 0
        for y in Int(Double(plain.height) * 0.4)..<Int(Double(plain.height) * 0.6) {
            for x in stride(from: 0, to: plain.width, by: 2) {
                let i = (y * plain.width + x) * 4
                if abs(Int(plain.bitmap[i]) - Int(texted.bitmap[i])) + abs(Int(plain.bitmap[i + 2]) - Int(texted.bitmap[i + 2])) > 120 { changed += 1 }
            }
        }
        XCTAssertGreaterThan(changed, 500, "white text pixels differ from the bare photo in the centre band")
        // And far from the text nothing changed.
        let i = (Int(Double(plain.height) * 0.05) * plain.width + plain.width / 4) * 4
        XCTAssertEqual(plain.bitmap[i], texted.bitmap[i])
    }

    /// Pixels in `rect` the mist mark lifts off pure red (its grey adds green; red footage has none).
    private func markedPixels(_ bitmap: [UInt8], width: Int, in rect: CGRect) -> Int {
        var count = 0
        for y in Int(rect.minY)..<Int(rect.maxY) {
            for x in Int(rect.minX)..<Int(rect.maxX) where bitmap[(y * width + x) * 4 + 1] > 50 { count += 1 }
        }
        return count
    }

    /// KRI-472: every slide carries the Kria mark bottom-left, where the server render and the video engine put
    /// it, scaled with the canvas (the 4:5 carousel gets the smaller, height-bound mark), and nowhere else.
    func testEverySlideCarriesTheWatermarkBottomLeft() async throws {
        let url = try photo(name: "w.jpg")   // left half red, so the mark's corner is pure red
        let tile = try XCTUnwrap(UIImage(contentsOfFile: try XCTUnwrap(KriaBranding.watermarkURL(.mist)).path)).size
        for profile in ["tiktok_photo", "instagram_carousel"] {
            let out = try decoded(try await render(slide("a"), asset("asset-a", source: url), profile: profile))
            let rect = KriaBranding.watermarkTileRect(canvas: out.size, tileSize: tile)
            XCTAssertGreaterThan(markedPixels(out.bitmap, width: out.width, in: rect), 500, profile)
            let above = rect.offsetBy(dx: 0, dy: -rect.minY + out.size.height * 0.1)
            XCTAssertEqual(markedPixels(out.bitmap, width: out.width, in: above), 0, "\(profile): the mark is drawn once, bottom-left")
        }
    }

    func testExpiredSignedURLSurfacesForTheCallerToRefresh() async throws {
        let target = asset("asset-a", source: URL(string: "https://example.test/x.jpg"))
        do {
            _ = try await SlidePostOnDeviceRender.renderSlide(slide: slide("a"), asset: target, profile: "tiktok_photo", fetch: { _ in throw SlidePostImageCache.LoadError.expired })
            XCTFail("expected expired")
        } catch { XCTAssertEqual(error as? SlidePostImageCache.LoadError, .expired) }
    }

    // MARK: supports()

    func testSupportsOnlyImageOnlyUnlookedPostsWithAPhotoURL() throws {
        let url = try photo(name: "s.jpg")
        let ok = asset("asset-a", source: url)
        func draft(_ slides: [SlidePostSlide]) -> SlidePostDraft { SlidePostDraft(version: 1, platformProfile: "tiktok_photo", slides: slides) }
        XCTAssertTrue(SlidePostOnDeviceRender.supports(draft([slide("a", texts: [caption()])]), assets: [ok]))
        XCTAssertTrue(SlidePostOnDeviceRender.supports(draft([slide("a")]), assets: [asset("asset-a", source: nil, display: url)]), "display URL is the fallback")
        XCTAssertFalse(SlidePostOnDeviceRender.supports(draft([slide("a", kind: "video")]), assets: [ok]))
        XCTAssertFalse(SlidePostOnDeviceRender.supports(draft([slide("a", look: "golden_hour")]), assets: [ok]))
        XCTAssertFalse(SlidePostOnDeviceRender.supports(draft([slide("a")]), assets: [asset("asset-a", source: nil)]))
        XCTAssertFalse(SlidePostOnDeviceRender.supports(draft([slide("a")]), assets: []))
        var unknownFont = caption(); unknownFont.fontFamily = "No-Such-Font"
        XCTAssertFalse(SlidePostOnDeviceRender.supports(draft([slide("a", texts: [unknownFont])]), assets: [ok]))
        XCTAssertFalse(SlidePostOnDeviceRender.supports(nil, assets: [ok]))
    }

    // MARK: Export flow

    private func session(slides: [SlidePostSlide], assets: [SlidePostAsset], profile: String = "tiktok_photo") async -> SlidePostSession {
        let draft = SlidePostDraft(version: 1, platformProfile: profile, slides: slides, caption: "Caption")   // not rendered: no server render exists
        let state = SlidePostState(itemID: itemID, title: "Post", draft: draft, assets: assets, renderStatus: "not_rendered")
        var requests = 0
        NativeEditorURLProtocol.handler = { _ in requests += 1; return (200, try JSONEncoder().encode(state)) }
        let result = SlidePostSession(defaults: defaults)
        await result.refresh(api: NativeEditorTestSupport.api(), itemID: itemID)
        return result
    }

    func testSaveToPhotosRendersEverySlideWithoutServerRenderAndSavesTwiceWritesTwice() async throws {
        let a = try photo(name: "1.jpg"), b = try photo(name: "2.jpg", orientation: 6)
        let session = await session(slides: [slide("a", texts: [caption()]), slide("b")], assets: [asset("asset-a", source: a), asset("asset-b", source: b)])
        var requested: [String] = []
        NativeEditorURLProtocol.handler = { request in requested.append("\(request.httpMethod ?? "GET") \(request.url?.path ?? "")"); return (500, Data()) }
        var batches: [[SlidePostExporter.PhotoResource]] = []
        var sizes: [CGSize] = []
        let exporter = SlidePostExporter(authorizePhotos: { .authorized }, writePhotos: { resources in
            batches.append(resources)
            for resource in resources { sizes.append(UIImage(contentsOfFile: resource.url.path)?.size ?? .zero) }
        })
        await exporter.export(.photos, session: session, api: NativeEditorTestSupport.api(), itemID: itemID)
        await exporter.export(.photos, session: session, api: NativeEditorTestSupport.api(), itemID: itemID)
        XCTAssertEqual(batches.map(\.count), [2, 2], "one Photos asset per slide, every time")
        XCTAssertEqual(sizes, Array(repeating: CGSize(width: 2160, height: 3840), count: 4))
        XCTAssertEqual(batches[0].map(\.url.lastPathComponent), ["01.jpg", "02.jpg"])
        XCTAssertTrue(batches[0][0].creationDate < batches[0][1].creationDate)
        XCTAssertEqual(exporter.status, .savedToPhotos(2))
        XCTAssertTrue(requested.isEmpty, "no save / generate / poll request: \(requested)")
        XCTAssertFalse(FileManager.default.fileExists(atPath: batches[0][0].url.path), "temp dir is cleaned up")
    }

    func testShareProducesJPEGsAndCaptionFromTheUnsavedDraft() async throws {
        let a = try photo(name: "1.jpg")
        let session = await session(slides: [slide("a")], assets: [asset("asset-a", source: a)])
        session.draft?.caption = "Edited, unsaved"
        XCTAssertTrue(session.hasUnsavedChanges)
        let exporter = SlidePostExporter(authorizePhotos: { .authorized }, writePhotos: { _ in })
        await exporter.export(.share, session: session, api: NativeEditorTestSupport.api(), itemID: itemID)
        XCTAssertTrue(exporter.isSharing)
        XCTAssertEqual(exporter.shareItems.map(\.lastPathComponent), ["01.jpg", "caption.txt"])
        XCTAssertEqual(try String(contentsOf: try XCTUnwrap(exporter.shareItems.last)), "Edited, unsaved")
        XCTAssertNotNil(UIImage(contentsOfFile: exporter.shareItems[0].path))
        exporter.discardShareDirectory()
    }

    func testExpiredURLRefreshesTheSessionOnceThenRetries() async throws {
        let a = try photo(name: "1.jpg")
        let session = await session(slides: [slide("a")], assets: [asset("asset-a", source: a)])
        var calls = 0, saved = 0
        let exporter = SlidePostExporter(authorizePhotos: { .authorized }, writePhotos: { _ in saved += 1 }, onDeviceRender: { slide, asset, profile in
            calls += 1
            if calls == 1 { throw SlidePostImageCache.LoadError.expired }
            return try await SlidePostOnDeviceRender.renderSlide(slide: slide, asset: asset, profile: profile)
        })
        await exporter.export(.photos, session: session, api: NativeEditorTestSupport.api(), itemID: itemID)
        XCTAssertEqual(calls, 2); XCTAssertEqual(saved, 1)
    }

    func testRenderFailureFailsTheExportWithoutWritingPhotos() async throws {
        let a = try photo(name: "1.jpg")
        let session = await session(slides: [slide("a")], assets: [asset("asset-a", source: a)])
        var saved = 0
        let exporter = SlidePostExporter(authorizePhotos: { .authorized }, writePhotos: { _ in saved += 1 }, onDeviceRender: { _, _, _ in throw SlidePostOnDeviceRender.RenderError.undecodable })
        await exporter.export(.photos, session: session, api: NativeEditorTestSupport.api(), itemID: itemID)
        XCTAssertEqual(saved, 0)
        if case .failed = exporter.status {} else { XCTFail("expected failed, got \(exporter.status)") }
    }
}
