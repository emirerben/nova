import ImageIO
import UIKit
import UniformTypeIdentifiers
import XCTest
@testable import Kria

/// KRI-460: pictures pasted onto slides. The wire and the geometry are pinned by
/// `tests/fixtures/slide-sticker-parity/v1.json`, the same file the server's validator and renderer are held to.
@MainActor final class SlidePostStickerTests: XCTestCase {
    private let itemID = "44444444-4444-4444-4444-444444444444"
    private var defaults: UserDefaults!
    private var suite = ""
    private var directory: URL!

    override func setUp() {
        super.setUp()
        suite = "SlidePostStickerTests.\(UUID())"; defaults = UserDefaults(suiteName: suite)
        directory = FileManager.default.temporaryDirectory.appending(path: "slidesticker-\(UUID())")
        try? FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        SlidePostSession.stickerPollInterval = .milliseconds(5)
        SlidePostSession.stickerPollAttempts = 40
    }
    override func tearDown() {
        NativeEditorURLProtocol.handler = nil
        SlidePostSession.stickerPollInterval = .seconds(1)
        SlidePostSession.stickerPollAttempts = 120
        defaults.removePersistentDomain(forName: suite)
        try? FileManager.default.removeItem(at: directory)
        super.tearDown()
    }

    // MARK: Fixture

    private struct Fixture: Decodable {
        struct Case: Decodable {
            struct Size: Decodable { let w: Double; let h: Double }
            struct Expected: Decodable { let center_x: Double; let center_y: Double; let width_px: Double; let height_px: Double; let rotation_deg: Double }
            let name: String
            let canvas: Size
            let image: Size
            struct Wire: Decodable { let x_frac: Double; let y_frac: Double; let width_frac: Double; let rotation_deg: Double? }
            let sticker: Wire
            let expected: Expected
        }
        struct Validation: Decodable { let name: String; let valid: Bool; let reason: String?; let stickers: [JSONValue] }
        let cases: [Case]
        let validation: [Validation]
    }
    private func fixture() throws -> Fixture {
        var url = URL(fileURLWithPath: #filePath)
        for _ in 0..<6 { url.deleteLastPathComponent() }
        // Cross-platform fixtures live in the repo-root `tests/fixtures/` (like slide-drawing-parity); the server agent first
        // wrote this one under the API package, so look in both.
        let candidates = ["tests/fixtures/slide-sticker-parity/v1.json", "src/apps/api/tests/fixtures/slide-sticker-parity/v1.json"]
        let found = candidates.map { url.appending(path: $0) }.first { FileManager.default.fileExists(atPath: $0.path) }
        return try JSONDecoder().decode(Fixture.self, from: Data(contentsOf: try XCTUnwrap(found, "slide-sticker-parity/v1.json not found")))
    }

    func testGeometryMatchesTheServersFixtureForEveryCase() throws {
        let file = try fixture()
        XCTAssertGreaterThanOrEqual(file.cases.count, 10)
        for item in file.cases {
            let sticker = SlidePostSticker(id: "s", assetID: "a", xFrac: item.sticker.x_frac, yFrac: item.sticker.y_frac,
                                           widthFrac: item.sticker.width_frac, rotationDeg: item.sticker.rotation_deg ?? 0)
            let placed = SlidePostStickerGeometry.placement(for: sticker, imageSize: CGSize(width: item.image.w, height: item.image.h),
                                                            canvas: CGSize(width: item.canvas.w, height: item.canvas.h))
            XCTAssertEqual(placed.center.x, item.expected.center_x, accuracy: 1e-6, "\(item.name) centre x")
            XCTAssertEqual(placed.center.y, item.expected.center_y, accuracy: 1e-6, "\(item.name) centre y")
            XCTAssertEqual(placed.size.width, item.expected.width_px, accuracy: 1e-6, "\(item.name) width")
            XCTAssertEqual(placed.size.height, item.expected.height_px, accuracy: 1e-6, "\(item.name) height")
            XCTAssertEqual(placed.rotationDegrees, item.expected.rotation_deg, accuracy: 1e-6, "\(item.name) rotation")
        }
    }

    func testValidationMatchesTheServersFixtureForEveryCase() throws {
        let file = try fixture()
        XCTAssertGreaterThanOrEqual(file.validation.count, 14)
        for item in file.validation {
            // The wire is decoded strictly, so a value the model cannot read (a missing field) is also "not valid".
            let data = try JSONEncoder().encode(JSONValue.array(item.stickers))
            let stickers = try? JSONDecoder().decode([SlidePostSticker].self, from: data)
            var draft = SlidePostDraft(version: 1, platformProfile: "tiktok_photo", slides: [.init(id: "s", assetID: "a", kind: "image")])
            draft.slides[0].edits = SlidePostEdits(stickers: stickers)
            let verdict = stickers != nil && draft.validationMessage == nil
            XCTAssertEqual(verdict, item.valid, "\(item.name): \(draft.validationMessage ?? "accepted")")
        }
    }

    // MARK: Wire

    func testStickersAreOmittedWhenAbsentAndRoundTripWhenPresent() throws {
        let plain = SlidePostEdits()
        let plainJSON = try XCTUnwrap(JSONSerialization.jsonObject(with: JSONEncoder().encode(plain)) as? [String: Any])
        XCTAssertNil(plainJSON["stickers"], "a draft that never had a sticker stays byte-identical")

        let one = SlidePostSticker(id: "s1", assetID: "asset-1", xFrac: 0.25, yFrac: 0.75, widthFrac: 0.5, rotationDeg: -30)
        let edits = SlidePostEdits(stickers: [one])
        let json = try XCTUnwrap(JSONSerialization.jsonObject(with: JSONEncoder().encode(edits)) as? [String: Any])
        let row = try XCTUnwrap((json["stickers"] as? [[String: Any]])?.first)
        XCTAssertEqual(Set(row.keys), ["id", "asset_id", "x_frac", "y_frac", "width_frac", "rotation_deg"])
        XCTAssertEqual(row["asset_id"] as? String, "asset-1")
        XCTAssertEqual(try JSONDecoder().decode(SlidePostEdits.self, from: JSONEncoder().encode(edits)), edits)

        // An older server (or app) that round-trips the draft keeps the key; an upright sticker without rotation reads as 0.
        let raw = Data(#"{"look_preset":"none","stickers":[{"id":"s1","asset_id":"a","x_frac":0.5,"y_frac":0.5,"width_frac":0.4}]}"#.utf8)
        let decoded = try JSONDecoder().decode(SlidePostEdits.self, from: raw)
        XCTAssertEqual(decoded.stickers?.first?.rotationDeg, 0)
        let again = try XCTUnwrap(JSONSerialization.jsonObject(with: JSONEncoder().encode(decoded)) as? [String: Any])
        XCTAssertEqual((again["stickers"] as? [Any])?.count, 1)
    }

    func testRemovingTheLastStickerIsSentAsAnEmptyListButIsNotAnUnsavedChange() throws {
        var cleared = SlidePostEdits(); cleared.stickers = []
        let json = try XCTUnwrap(JSONSerialization.jsonObject(with: JSONEncoder().encode(cleared)) as? [String: Any])
        XCTAssertEqual((json["stickers"] as? [Any])?.count, 0, "an empty list is how a save clears the stored stickers")
        XCTAssertEqual(cleared, SlidePostEdits(), "[] and nil are the same content")
    }

    // MARK: Session

    private func session(slides: Int = 3, profile: String = "tiktok_photo") -> SlidePostSession {
        let session = SlidePostSession(defaults: defaults)
        session.draft = SlidePostDraft(version: 1, platformProfile: profile,
                                       slides: (1...slides).map { SlidePostSlide(id: "s\($0)", assetID: "a\($0)", kind: "image") })
        return session
    }

    func testAddingAStickerSelectsItAndIsOneUndoStep() {
        let session = session()
        let id = session.addSticker(slideID: "s1", assetID: "pic")
        XCTAssertNotNil(id)
        XCTAssertEqual(session.selectedStickerID, id)
        let sticker = session.draft?.slides[0].edits?.stickers?.first
        XCTAssertEqual([sticker?.xFrac, sticker?.yFrac, sticker?.widthFrac, sticker?.rotationDeg], [0.5, 0.5, 0.4, 0])
        XCTAssertTrue(session.hasUnsavedChanges)
        session.undoEdit()
        XCTAssertNil(session.draft?.slides[0].edits?.stickers)
        XCTAssertNil(session.selectedStickerID, "undoing the add drops the selection with it")
        session.redoEdit()
        XCTAssertEqual(session.draft?.slides[0].edits?.stickers?.count, 1)
    }

    func testAGestureIsOneUndoStepAndEverythingStaysInsideTheContract() throws {
        let session = session()
        let id = try XCTUnwrap(session.addSticker(slideID: "s1", assetID: "pic"))
        for step in 1...10 {
            session.updateSticker(slideID: "s1", stickerID: id, coalescing: "drag") { $0.xFrac = 0.5 + Double(step) * 0.04; $0.yFrac = 0.5 }
        }
        XCTAssertEqual(try XCTUnwrap(session.draft?.slides[0].edits?.stickers?.first).xFrac, 0.9, accuracy: 1e-9)
        session.undoEdit()
        XCTAssertEqual(try XCTUnwrap(session.draft?.slides[0].edits?.stickers?.first).xFrac, 0.5, accuracy: 1e-9, "the whole drag is one undo step")
        session.updateSticker(slideID: "s1", stickerID: id) { $0.xFrac = 4; $0.yFrac = -2; $0.widthFrac = 9; $0.rotationDeg = 400 }
        let clamped = try XCTUnwrap(session.draft?.slides[0].edits?.stickers?.first)
        XCTAssertEqual([clamped.xFrac, clamped.yFrac, clamped.widthFrac, clamped.rotationDeg], [1, 0, 1, 40])
        session.updateSticker(slideID: "s1", stickerID: id) { $0.widthFrac = 0.001 }
        XCTAssertEqual(session.draft?.slides[0].edits?.stickers?.first?.widthFrac, SlidePostSticker.widthFracRange.lowerBound)
        XCTAssertNil(session.draft?.validationMessage)
    }

    func testAtMostFourStickersAndTextAndStickerSelectionExcludeEachOther() {
        let session = session()
        for index in 1...4 { XCTAssertNotNil(session.addSticker(slideID: "s1", assetID: "pic-\(index)")) }
        XCTAssertNil(session.addSticker(slideID: "s1", assetID: "pic-5"))
        XCTAssertEqual(session.draft?.slides[0].edits?.stickers?.count, 4)
        XCTAssertNotNil(session.selectedStickerID)
        session.selectedTextID = "t"
        XCTAssertNil(session.selectedStickerID, "selecting a text deselects the picture")
        session.selectedStickerID = session.draft?.slides[0].edits?.stickers?.first?.id
        XCTAssertNil(session.selectedTextID, "and the other way round")
        session.selectedID = "s2"
        XCTAssertNil(session.selectedStickerID, "another slide has no selected picture")
    }

    func testRemovingStickersIsUndoableAndTheLastOneLeavesAnEmptyList() throws {
        let session = session()
        let id = try XCTUnwrap(session.addSticker(slideID: "s1", assetID: "pic"))
        session.removeSticker(slideID: "s1", stickerID: id)
        XCTAssertEqual(session.draft?.slides[0].edits?.effectiveStickers.count, 0)
        XCTAssertNil(session.selectedStickerID)
        session.undoEdit()
        XCTAssertEqual(session.draft?.slides[0].edits?.effectiveStickers.count, 1)
    }

    // MARK: A sticker's picture never becomes a slide

    private func asset(_ id: String, name: String? = nil, status: String = "ready") -> SlidePostAsset {
        SlidePostAsset(id: id, kind: "image", status: status, sourceFilename: name, sourceURL: URL(string: "https://storage.test/\(id)"), mediaStatus: "available")
    }
    private func refresh(_ session: SlidePostSession, assets: [SlidePostAsset], draft: SlidePostDraft?) async {
        let state = SlidePostState(itemID: itemID, title: "Post", draft: draft, assets: assets)
        NativeEditorURLProtocol.handler = { _ in (200, try JSONEncoder().encode(state)) }
        await session.refresh(api: NativeEditorTestSupport.api(), itemID: itemID)
    }

    func testAnAssetUsedAsAStickerIsNeverAutoAppendedAsASlide() async throws {
        var draft = SlidePostDraft(version: 1, platformProfile: "tiktok_photo", slides: [.init(id: "s1", assetID: "a1", kind: "image")])
        let session = SlidePostSession(defaults: defaults)
        await refresh(session, assets: [asset("a1")], draft: draft)
        session.appendNewlyReadyAssets()      // the session has now looked at the pool
        // The pool gains the picture; the sticker that uses it is in the unsaved draft.
        draft.slides[0].edits = SlidePostEdits(stickers: [SlidePostSticker(id: "k", assetID: "pic")])
        await refresh(session, assets: [asset("a1"), asset("pic", name: "pasted-1.png")], draft: draft)
        XCTAssertEqual(session.appendNewlyReadyAssets(), 0)
        XCTAssertEqual(session.draft?.slides.map(\.assetID), ["a1"])
        XCTAssertEqual(session.readyAssets.map(\.id), ["a1"], "the picture is not a candidate slide")
        XCTAssertTrue(session.slideAssets.allSatisfy { $0.id != "pic" })
    }

    func testASlideAssetIsStillAppendedAndSeededNormally() async throws {
        let session = SlidePostSession(defaults: defaults)
        await refresh(session, assets: [asset("a1"), asset("a2")], draft: nil)
        XCTAssertTrue(session.seedDraftIfNeeded())
        XCTAssertEqual(session.draft?.slides.map(\.assetID), ["a1", "a2"])
        await refresh(session, assets: [asset("a1"), asset("a2"), asset("a3")], draft: nil)
        XCTAssertEqual(session.appendNewlyReadyAssets(), 1, "ordinary imports still join the post")
    }

    /// The paste end to end with a stand-in uploader: the picture lands in the pool as `processing`, then `ready`, and a
    /// refresh lands between those two moments (the workspace polls on its own). It becomes a sticker, never a slide,
    /// and a freshly created session over the same stored state (a relaunch) still does not offer it as one.
    func testPasteUploadsThePictureAndPlacesItWithoutAddingASlide() async throws {
        let base = SlidePostDraft(version: 1, platformProfile: "tiktok_photo", slides: [.init(id: "s1", assetID: "a1", kind: "image"), .init(id: "s2", assetID: "a2", kind: "image")])
        // The pool is served from the URLProtocol's thread, so its logic lives in a nonisolated class, not in main-actor closures.
        final class Pool: @unchecked Sendable {
            var assets: [SlidePostAsset] = []
            var polls = 0
            func serve(itemID: String, draft: SlidePostDraft) throws -> (Int, Data) {
                polls += 1
                if polls >= 3, let index = assets.firstIndex(where: { $0.id == "pic" }) {
                    assets[index] = SlidePostAsset(id: "pic", kind: "image", status: "ready", sourceFilename: assets[index].sourceFilename,
                                                   sourceURL: URL(string: "https://storage.test/pic"), mediaStatus: "available")
                }
                return (200, try JSONEncoder().encode(SlidePostState(itemID: itemID, title: "Post", draft: draft, assets: assets)))
            }
        }
        let pool = Pool()
        pool.assets = [asset("a1"), asset("a2")]
        let session = SlidePostSession(defaults: defaults)
        await refresh(session, assets: pool.assets, draft: base)
        session.appendNewlyReadyAssets()
        let uploader = FakeUploader { name in pool.assets.append(self.asset("pic", name: name, status: "processing")) }
        let itemID = self.itemID
        NativeEditorURLProtocol.handler = { _ in try pool.serve(itemID: itemID, draft: base) }
        let file = directory.appending(path: "pasted-ab12cd34.png")
        try pngData().write(to: file)
        let context = SlidePostStickerPaste(isEnabled: true, api: NativeEditorTestSupport.api(), uploads: uploader, projectID: UUID(), itemID: itemID)
        await session.pasteImage(fileURL: file, slideID: "s2", context: context)

        XCTAssertNil(session.pasteNotice)
        XCTAssertEqual(uploader.filenames, ["pasted-ab12cd34.png"])
        XCTAssertEqual(session.draft?.slides.map(\.assetID), ["a1", "a2"], "no new slide")
        XCTAssertEqual(session.draft?.slides[1].edits?.stickers?.map(\.assetID), ["pic"])
        XCTAssertEqual(session.selectedStickerID, session.draft?.slides[1].edits?.stickers?.first?.id)
        // While it was still processing, and now that it is ready, it is not a slide candidate and has no pending tile.
        XCTAssertEqual(session.appendNewlyReadyAssets(), 0)
        XCTAssertFalse(SlidePostPendingMedia.tiles(assets: session.slideAssets, draftAssetIDs: [], records: [], progress: [:], inFlight: [:], failures: [],
                                                  projectID: UUID(), stickerUploadNames: session.stickerUploadNames).contains { $0.id == "asset-pic" })

        // Relaunch: a new session restores the stored names, so even a draft that lost the sticker never offers the picture.
        let reopened = SlidePostSession(defaults: defaults)
        await refresh(reopened, assets: pool.assets, draft: base)
        XCTAssertEqual(reopened.stickerUploadNames, ["pasted-ab12cd34.png"])
        XCTAssertEqual(reopened.appendNewlyReadyAssets(), 0)
        XCTAssertEqual(reopened.readyAssets.map(\.id), ["a1", "a2"])
    }

    func testPasteIsRefusedWithAReasonBeforeAnythingIsUploaded() async throws {
        let session = session()
        let uploader = FakeUploader { _ in }
        let file = directory.appending(path: "pasted-refused.png")
        try pngData().write(to: file)
        let off = SlidePostStickerPaste(isEnabled: false, api: NativeEditorTestSupport.api(), uploads: uploader, projectID: UUID(), itemID: itemID)
        await session.pasteImage(fileURL: file, slideID: "s1", context: off)
        XCTAssertNotNil(session.pasteNotice)
        XCTAssertTrue(uploader.filenames.isEmpty, "capability off: nothing uploaded")
        XCTAssertFalse(FileManager.default.fileExists(atPath: file.path), "the temp copy is not left behind")

        for index in 1...4 { session.addSticker(slideID: "s1", assetID: "p\(index)") }
        let again = directory.appending(path: "pasted-full.png")
        try pngData().write(to: again)
        let on = SlidePostStickerPaste(isEnabled: true, api: NativeEditorTestSupport.api(), uploads: uploader, projectID: UUID(), itemID: itemID)
        session.pasteNotice = nil
        await session.pasteImage(fileURL: again, slideID: "s1", context: on)
        XCTAssertTrue(session.pasteNotice?.contains("up to 4 pictures") == true, session.pasteNotice ?? "nil")
        XCTAssertTrue(uploader.filenames.isEmpty, "a full slide uploads nothing")
    }

    @MainActor final class FakeUploader: SlidePostStickerUploading {
        var filenames: [String] = []
        let onUpload: (String) -> Void
        init(onUpload: @escaping (String) -> Void) { self.onUpload = onUpload }
        func uploadSticker(fileURL: URL, projectID: UUID, itemID: String) async -> Bool {
            filenames.append(fileURL.lastPathComponent); onUpload(fileURL.lastPathComponent); return true
        }
    }

    // MARK: Rendering

    /// 400x200: the LEFT half opaque magenta, the right half fully transparent.
    private func pngData() throws -> Data {
        let format = UIGraphicsImageRendererFormat(); format.scale = 1; format.opaque = false
        let image = UIGraphicsImageRenderer(size: CGSize(width: 400, height: 200), format: format).image { _ in
            UIColor(red: 1, green: 0, blue: 1, alpha: 1).setFill(); UIRectFill(CGRect(x: 0, y: 0, width: 200, height: 200))
        }
        return try XCTUnwrap(image.pngData())
    }
    private func grayJPEG() throws -> URL {
        let format = UIGraphicsImageRendererFormat(); format.scale = 1; format.opaque = true
        let image = UIGraphicsImageRenderer(size: CGSize(width: 600, height: 600), format: format).image { _ in
            UIColor(white: 0.5, alpha: 1).setFill(); UIRectFill(CGRect(x: 0, y: 0, width: 600, height: 600))
        }
        let url = directory.appending(path: "gray.jpg")
        try XCTUnwrap(image.jpegData(compressionQuality: 1)).write(to: url)
        return url
    }
    private func pixel(_ data: Data, _ x: Int, _ y: Int) throws -> (r: Int, g: Int, b: Int) {
        let cg = try XCTUnwrap(UIImage(data: data)?.cgImage)
        var buffer = [UInt8](repeating: 0, count: cg.width * cg.height * 4)
        let context = try XCTUnwrap(CGContext(data: &buffer, width: cg.width, height: cg.height, bitsPerComponent: 8, bytesPerRow: cg.width * 4,
                                              space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue))
        context.draw(cg, in: CGRect(x: 0, y: 0, width: cg.width, height: cg.height))
        let i = (y * cg.width + x) * 4
        return (Int(buffer[i]), Int(buffer[i + 1]), Int(buffer[i + 2]))
    }
    private func isMagenta(_ p: (r: Int, g: Int, b: Int)) -> Bool { p.r > 200 && p.g < 60 && p.b > 200 }
    private func isGray(_ p: (r: Int, g: Int, b: Int)) -> Bool { abs(p.r - 128) < 14 && abs(p.g - 128) < 14 && abs(p.b - 128) < 14 }

    /// The sticker is drawn where the geometry says, transparency lets the photo show through, a clockwise turn is clockwise, and
    /// it sits under the watermark's corner but over the photo.
    func testPhotoRenderDrawsTheStickerWithItsTransparencyAndRotation() async throws {
        let source = try grayJPEG()
        let photo = SlidePostAsset(id: "a1", kind: "image", status: "ready", sourceURL: source)
        let picture = try XCTUnwrap(SlidePostStickerStore.decode(try pngData()))
        let upright = SlidePostSticker(id: "k", assetID: "pic", xFrac: 0.5, yFrac: 0.5, widthFrac: 0.5, rotationDeg: 0)
        var slide = SlidePostSlide(id: "s", assetID: "a1", kind: "image", edits: SlidePostEdits(stickers: [upright]))

        let draft = SlidePostDraft(version: 1, platformProfile: "tiktok_photo", slides: [slide])
        let picAsset = SlidePostAsset(id: "pic", kind: "image", status: "ready", sourceURL: URL(string: "https://storage.test/pic"))
        XCTAssertTrue(SlidePostOnDeviceRender.supports(draft, assets: [photo, picAsset]))
        XCTAssertFalse(SlidePostOnDeviceRender.supports(draft, assets: [photo]), "a sticker whose picture is unknown goes to the server renderer")

        var data = try await SlidePostOnDeviceRender.renderSlide(slide: slide, asset: photo, profile: "tiktok_photo", aspect: nil,
                                                                 stickers: [.init(sticker: upright, image: picture)], scale: 1)
        // 1080x1920 canvas; the sticker is 540 wide, 270 tall, centred: x 270...810, y 825...1095; its left half (x < 540) is opaque.
        XCTAssertTrue(isMagenta(try pixel(data, 400, 960)), "opaque half shows the picture")
        XCTAssertTrue(isGray(try pixel(data, 680, 960)), "transparent half shows the photo through it")
        XCTAssertTrue(isGray(try pixel(data, 200, 960)), "outside the sticker is the photo")
        XCTAssertTrue(isGray(try pixel(data, 400, 700)), "above the sticker is the photo")

        // Turned 90 degrees clockwise about its centre: the opaque left half becomes the TOP half.
        let turned = SlidePostSticker(id: "k", assetID: "pic", xFrac: 0.5, yFrac: 0.5, widthFrac: 0.5, rotationDeg: 90)
        slide.edits = SlidePostEdits(stickers: [turned])
        data = try await SlidePostOnDeviceRender.renderSlide(slide: slide, asset: photo, profile: "tiktok_photo", aspect: nil,
                                                             stickers: [.init(sticker: turned, image: picture)], scale: 1)
        XCTAssertTrue(isMagenta(try pixel(data, 540, 850)), "above the centre: the picture, clockwise")
        XCTAssertTrue(isGray(try pixel(data, 540, 1070)), "below the centre: transparent, so the photo")
        XCTAssertTrue(isGray(try pixel(data, 350, 960)), "the old horizontal footprint is empty now")
    }

    func testTheStoreKeepsAlphaAcrossAnotherLaunch() async throws {
        let png = try pngData()
        let url = directory.appending(path: "original.png")
        try png.write(to: url)
        let cache = directory.appending(path: "cache")
        let first = SlidePostStickerStore(directory: cache, fetch: { try Data(contentsOf: $0) })
        _ = try await first.image(assetID: "pic", url: url)
        try FileManager.default.removeItem(at: url)   // the signed URL is gone; only the cached original can serve it
        let second = SlidePostStickerStore(directory: cache, fetch: { _ in throw SlidePostImageCache.LoadError.expired })
        let image = try await second.image(assetID: "pic", url: url)
        let alpha = try XCTUnwrap(image.cgImage).alphaInfo
        XCTAssertFalse([.none, .noneSkipLast, .noneSkipFirst].contains(alpha), "alpha survived the disk cache")
    }
}
