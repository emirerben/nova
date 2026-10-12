import KriaMediaEngine
import XCTest
@testable import Kria

/// KRI-606: the post-wide size and per-photo Fit. The table and the Fit placement are pinned to the same fixture the
/// server's Python reference is (`tests/fixtures/slide-framing-parity/v1.json`); the save contract must say exactly
/// what the creator did (set, clear, or nothing) so an older app can never wipe a value it cannot see.
@MainActor final class SlidePostSizeTests: XCTestCase {
    // MARK: Parity with the cloud

    private struct Fixture: Decodable {
        struct Crop: Decodable { let x, y, width, height: Double }
        struct Case: Decodable {
            let name: String
            let image: [Double]
            let crop: Crop?
            let canvas: [Double]
            let expected: [Double]
        }
        struct Sizes: Decodable {
            struct Profile: Decodable { let `default`: String; let allowed: [String] }
            let canvases: [String: [Double]]
            let profiles: [String: Profile]
        }
        let fitPlacements: [Case]
        let sizes: Sizes
        let fitBlurSigmaPx: Double
        enum CodingKeys: String, CodingKey { case sizes; case fitPlacements = "fit_placements", fitBlurSigmaPx = "fit_blur_sigma_px" }
    }

    private func fixture() throws -> Fixture {
        var url = URL(fileURLWithPath: #filePath)
        for _ in 0..<6 { url.deleteLastPathComponent() }
        url.appendPathComponent("tests/fixtures/slide-framing-parity/v1.json")
        return try JSONDecoder().decode(Fixture.self, from: Data(contentsOf: url))
    }

    func testFitPlacementMatchesTheSharedFixtureWithinAHundredthOfAPixel() throws {
        let file = try fixture()
        XCTAssertGreaterThanOrEqual(file.fitPlacements.count, 5)
        for item in file.fitPlacements {
            let crop = item.crop.map { NormalizedSourceRect(x: $0.x, y: $0.y, width: $0.width, height: $0.height) }
            let rect = SlidePostFraming.placement(
                imageSize: CGSize(width: item.image[0], height: item.image[1]), crop: crop,
                canvas: CGSize(width: item.canvas[0], height: item.canvas[1]), fit: true)
            XCTAssertEqual(rect.minX, item.expected[0], accuracy: 0.01, item.name)
            XCTAssertEqual(rect.minY, item.expected[1], accuracy: 0.01, item.name)
            XCTAssertEqual(rect.width, item.expected[2], accuracy: 0.01, item.name)
            XCTAssertEqual(rect.height, item.expected[3], accuracy: 0.01, item.name)
        }
    }

    func testSizeTableMatchesTheServerProfiles() throws {
        let sizes = try fixture().sizes
        XCTAssertEqual(Set(sizes.canvases.keys), Set(SlidePostSize.allCases.map(\.rawValue)))
        for size in SlidePostSize.allCases {
            let expected = try XCTUnwrap(sizes.canvases[size.rawValue])
            XCTAssertEqual(size.canvas, CGSize(width: expected[0], height: expected[1]), size.rawValue)
            XCTAssertEqual(Int(size.canvas.width) % 2, 0); XCTAssertEqual(Int(size.canvas.height) % 2, 0)
        }
        for (profile, spec) in sizes.profiles {
            XCTAssertEqual(SlidePostSize.defaultSize(profile: profile).rawValue, spec.default, profile)
            XCTAssertEqual(SlidePostSize.allowed(profile: profile).map(\.rawValue), spec.allowed, profile)
        }
    }

    func testBlurSigmaMatchesTheServer() throws {
        XCTAssertEqual(Double(SlidePostFit.blurSigmaPx), try fixture().fitBlurSigmaPx)
    }

    func testFitNeverTrimsWhatFillWould() throws {
        for item in try fixture().fitPlacements {
            let crop = item.crop ?? .init(x: 0, y: 0, width: 1, height: 1)
            let rect = SlidePostFraming.placement(
                imageSize: CGSize(width: item.image[0], height: item.image[1]),
                crop: item.crop.map { NormalizedSourceRect(x: $0.x, y: $0.y, width: $0.width, height: $0.height) },
                canvas: CGSize(width: item.canvas[0], height: item.canvas[1]), fit: true)
            XCTAssertGreaterThanOrEqual(rect.minX + crop.x * rect.width, -0.01, item.name)
            XCTAssertLessThanOrEqual(rect.minX + (crop.x + crop.width) * rect.width, item.canvas[0] + 0.01, item.name)
            XCTAssertGreaterThanOrEqual(rect.minY + crop.y * rect.height, -0.01, item.name)
            XCTAssertLessThanOrEqual(rect.minY + (crop.y + crop.height) * rect.height, item.canvas[1] + 0.01, item.name)
        }
    }

    // MARK: Resolution

    func testSizesResolveAgainstThePlatform() {
        XCTAssertEqual(SlidePostSize.resolved(nil, profile: "tiktok_photo"), .fullScreen)
        XCTAssertEqual(SlidePostSize.resolved(nil, profile: "instagram_carousel"), .portrait)
        XCTAssertEqual(SlidePostSize.resolved("1:1", profile: "instagram_carousel"), .square)
        XCTAssertEqual(SlidePostSize.resolved("9:16", profile: "instagram_carousel"), .portrait, "no full screen on Instagram")
        XCTAssertEqual(SlidePostSize.resolved("1.91:1", profile: "tiktok_photo"), .fullScreen, "no wide on TikTok")
        XCTAssertEqual(SlidePostSize.resolved("7:3", profile: "tiktok_photo"), .fullScreen)
        XCTAssertNil(SlidePostSize.storedAspect("9:16", profile: "tiktok_photo"), "the default is stored as nil")
        XCTAssertEqual(SlidePostSize.storedAspect("1:1", profile: "tiktok_photo"), "1:1")
        XCTAssertEqual(SlidePostSize.allCases.filter(\.worksOnBoth).map(\.rawValue), ["4:5", "3:4", "1:1"])
    }

    func testLandscapeSlideKeepsItsOwnCanvasInsideASizedPost() {
        let landscape = SlidePostSlide(id: "s", assetID: "a", kind: "image", edits: .init(framing: .init(orientation: .landscape)))
        XCTAssertEqual(SlidePostFraming.canvas(for: landscape, profile: "tiktok_photo", aspect: "1:1"), CGSize(width: 1920, height: 1080))
        let plain = SlidePostSlide(id: "s", assetID: "a", kind: "image")
        XCTAssertEqual(SlidePostFraming.canvas(for: plain, profile: "tiktok_photo", aspect: "1:1"), CGSize(width: 1080, height: 1080))
        XCTAssertEqual(SlidePostFraming.canvas(for: plain, profile: "instagram_carousel", aspect: "3:4"), CGSize(width: 1080, height: 1440))
    }

    // MARK: Save contract

    private func object(_ value: some Encodable) throws -> [String: Any] {
        try XCTUnwrap(JSONSerialization.jsonObject(with: JSONEncoder().encode(value)) as? [String: Any])
    }

    func testEncodingAlwaysWritesFitSoAClearIsNeverMistakenForAnOlderApp() throws {
        XCTAssertTrue(try object(SlidePostEdits())["fit"] is NSNull, "A missing key means keep what is stored")
        XCTAssertEqual(try object(SlidePostEdits(fit: .blur))["fit"] as? String, "blur")
    }

    func testSaveRequestAndDraftAlwaysSayWhichSizeTheyHold() throws {
        let plain = SlidePostDraft(version: 2, platformProfile: "tiktok_photo", slides: [.init(id: "s", assetID: "a", kind: "image")])
        XCTAssertTrue(try object(SlidePostSaveRequest(draft: plain, expectedVersion: 2))["aspect"] is NSNull)
        XCTAssertTrue(try object(plain)["aspect"] is NSNull, "the chat-edit draft says it too")

        var sized = plain; sized.aspect = "1:1"
        XCTAssertEqual(try object(SlidePostSaveRequest(draft: sized, expectedVersion: 2))["aspect"] as? String, "1:1")
        XCTAssertEqual(try object(sized)["aspect"] as? String, "1:1")
    }

    func testDraftRoundTripsAndOlderPayloadsStillDecode() throws {
        var sized = SlidePostDraft(version: 2, platformProfile: "instagram_carousel", slides: [.init(id: "s", assetID: "a", kind: "image", edits: .init(fit: .white))])
        sized.aspect = "3:4"; sized.caption = "hi"; sized.coverIndex = 0
        let back = try JSONDecoder().decode(SlidePostDraft.self, from: JSONEncoder().encode(sized))
        XCTAssertEqual(back, sized)
        let old = try JSONDecoder().decode(SlidePostDraft.self, from: Data(#"{"schema_version":1,"version":2,"platform_profile":"tiktok_photo","slides":[],"cover_index":0,"caption":"","user_edited":false}"#.utf8))
        XCTAssertNil(old.aspect)
        let nullFit = try JSONDecoder().decode(SlidePostEdits.self, from: Data(#"{"look_preset":"none","fit":null}"#.utf8))
        let missing = try JSONDecoder().decode(SlidePostEdits.self, from: Data(#"{"look_preset":"none"}"#.utf8))
        let unknown = try JSONDecoder().decode(SlidePostEdits.self, from: Data(#"{"look_preset":"none","fit":"sepia"}"#.utf8))
        XCTAssertNil(nullFit.fit); XCTAssertNil(missing.fit); XCTAssertNil(unknown.fit)
    }

    func testChangingTheSizeOrFitDirtiesTheEditor() {
        let a = SlidePostDraft(version: 1, platformProfile: "tiktok_photo", slides: [.init(id: "s", assetID: "a", kind: "image")])
        var b = a; b.aspect = "1:1"
        XCTAssertFalse(a.hasSameContent(as: b))
        var c = a; c.slides[0].edits = .init(fit: .blur)
        XCTAssertFalse(a.hasSameContent(as: c))
    }

    func testTheCapabilityDecodesAndDefaultsOff() throws {
        let on = try JSONDecoder().decode(CreationCapabilities.self, from: Data(#"{"formats":[],"slide_post_sizes":true}"#.utf8))
        let old = try JSONDecoder().decode(CreationCapabilities.self, from: Data(#"{"formats":[]}"#.utf8))
        XCTAssertTrue(on.slidePostSizesEnabled)
        XCTAssertFalse(old.slidePostSizesEnabled)
    }

    // MARK: Editing

    private func session(profile: String, kinds: [String] = ["image", "image", "image"]) -> SlidePostSession {
        let session = SlidePostSession()
        let slides = kinds.enumerated().map { SlidePostSlide(id: "s\($0.offset + 1)", assetID: "a\($0.offset + 1)", kind: $0.element) }
        session.draft = SlidePostDraft(version: 1, platformProfile: profile, slides: slides)
        return session
    }

    private let crop = NormalizedSourceRect(x: 0.1, y: 0.1, width: 0.5, height: 0.5)

    func testSettingTheSizeIsOneUndoableEditAndDropsPortraitCropsOnly() {
        let session = session(profile: "tiktok_photo")
        session.setFraming(slideID: "s1", .init(orientation: .portrait, sourceCrop: crop))
        session.setFraming(slideID: "s2", .init(orientation: .landscape, sourceCrop: crop))
        let reset = session.setAspect("1:1")
        XCTAssertEqual(reset, 1)
        XCTAssertEqual(session.draft?.aspect, "1:1")
        XCTAssertNil(session.draft?.slides[0].edits?.framing, "a rectangle for the old shape is mis-shaped for the new canvas")
        XCTAssertEqual(session.draft?.slides[1].edits?.framing?.sourceCrop, crop, "landscape slides keep their own shape and crop")
        session.undoEdit()
        XCTAssertNil(session.draft?.aspect)
        XCTAssertEqual(session.draft?.slides[0].edits?.framing?.sourceCrop, crop, "one undo restores size and crops together")
    }

    func testChoosingTheDefaultSizeStoresNil() {
        let session = session(profile: "tiktok_photo")
        XCTAssertEqual(session.setAspect("9:16"), 0)
        XCTAssertNil(session.draft?.aspect)
        XCTAssertFalse(session.canUndoEdit, "nothing changed, nothing to undo")
        session.setAspect("1:1"); session.setAspect(nil)
        XCTAssertNil(session.draft?.aspect)
    }

    func testSwitchingPlatformFallsBackToThatPlatformsDefaultSize() {
        let session = session(profile: "tiktok_photo")
        session.setAspect("1:1")
        session.setPlatform("instagram_carousel")
        XCTAssertEqual(session.draft?.aspect, "1:1", "square is allowed on both")
        XCTAssertEqual(session.draft?.size, .square)
        session.setAspect("1.91:1")
        session.setPlatform("tiktok_photo")
        XCTAssertNil(session.draft?.aspect, "1.91:1 is Instagram-only")
        XCTAssertEqual(session.draft?.size, .fullScreen)
    }

    func testAStagedAIDraftWithASizeThePlatformLacksIsCoerced() {
        let session = session(profile: "tiktok_photo")
        var staged = session.draft!
        staged.platformProfile = "instagram_carousel"; staged.aspect = "9:16"
        session.stageDraft(staged)
        XCTAssertNil(session.draft?.aspect)
    }

    func testFitIsPerPhotoAndFitAllSkipsVideos() {
        let session = session(profile: "instagram_carousel", kinds: ["image", "video", "image"])
        session.setFit(slideID: "s2", .blur)
        XCTAssertNil(session.draft?.slides[1].edits?.fit, "videos always fill the frame")
        session.setFitForAllPhotos(.white)
        XCTAssertEqual(session.draft?.slides.map { $0.edits?.fit }, [.white, nil, .white])
        XCTAssertEqual(session.fitSummary, .uniform(.white))
        session.setFit(slideID: "s1", .black)
        XCTAssertEqual(session.fitSummary, .mixed)
        session.undoEdit(); session.undoEdit()
        XCTAssertEqual(session.fitSummary, .uniform(nil))
        session.setFitForAllPhotos(nil)
        XCTAssertFalse(session.canUndoEdit, "already fill")
    }

    func testUnrelatedEditsKeepTheSizeAndFit() {
        let session = session(profile: "tiktok_photo")
        session.setAspect("4:5"); session.setFitForAllPhotos(.blur)
        session.setLook(slideID: "s1", preset: "golden_hour")
        _ = session.addText(slideID: "s1")
        XCTAssertEqual(session.draft?.aspect, "4:5")
        XCTAssertEqual(session.draft?.slides[0].edits?.fit, .blur)
    }
}
