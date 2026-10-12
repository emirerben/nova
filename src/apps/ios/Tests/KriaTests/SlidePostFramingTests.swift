import KriaMediaEngine
import XCTest
@testable import Kria

/// KRI-44: the phone and the cloud must put the same pixels on a slide, and the save request must say exactly
/// what the creator did to a crop (set, clear, or nothing).
@MainActor final class SlidePostFramingTests: XCTestCase {
    // MARK: Cover-fit parity with the cloud

    private struct ParityFile: Decodable {
        struct Case: Decodable {
            struct Crop: Decodable { let x, y, width, height: Double }
            let name: String
            let image: [Double]
            let crop: Crop?
            let canvas: [Double]
            let expected: [Double]
        }
        let placements: [Case]
    }

    /// The repo-root fixture the Python reference (`cover_placement`) is pinned to as well.
    private func parityFile() throws -> ParityFile {
        var url = URL(fileURLWithPath: #filePath)
        for _ in 0..<6 { url.deleteLastPathComponent() }
        url.appendPathComponent("tests/fixtures/slide-framing-parity/v1.json")
        return try JSONDecoder().decode(ParityFile.self, from: Data(contentsOf: url))
    }

    func testPlacementMatchesTheSharedParityFixtureWithinAHundredthOfAPixel() throws {
        let file = try parityFile()
        XCTAssertGreaterThanOrEqual(file.placements.count, 6)
        for item in file.placements {
            let crop = item.crop.map { NormalizedSourceRect(x: $0.x, y: $0.y, width: $0.width, height: $0.height) }
            let rect = SlidePostFraming.placement(
                imageSize: CGSize(width: item.image[0], height: item.image[1]), crop: crop,
                canvas: CGSize(width: item.canvas[0], height: item.canvas[1]), fit: false)
            XCTAssertEqual(rect.minX, item.expected[0], accuracy: 0.01, item.name)
            XCTAssertEqual(rect.minY, item.expected[1], accuracy: 0.01, item.name)
            XCTAssertEqual(rect.width, item.expected[2], accuracy: 0.01, item.name)
            XCTAssertEqual(rect.height, item.expected[3], accuracy: 0.01, item.name)
        }
    }

    func testLandscapeIsOnlyHonouredOnTikTokPhotoPosts() {
        let slide = SlidePostSlide(id: "s", assetID: "a", kind: "image", edits: .init(framing: .init(orientation: .landscape)))
        XCTAssertEqual(SlidePostFraming.canvas(for: slide, profile: "tiktok_photo", aspect: nil), CGSize(width: 1920, height: 1080))
        XCTAssertEqual(SlidePostFraming.canvas(for: slide, profile: "instagram_carousel", aspect: nil), CGSize(width: 1080, height: 1350))
        let plain = SlidePostSlide(id: "s", assetID: "a", kind: "image")
        XCTAssertEqual(SlidePostFraming.canvas(for: plain, profile: "tiktok_photo", aspect: nil), CGSize(width: 1080, height: 1920))
    }

    // MARK: Three-state save semantics

    private func editsJSON(_ edits: SlidePostEdits) throws -> [String: Any] {
        try XCTUnwrap(JSONSerialization.jsonObject(with: JSONEncoder().encode(edits)) as? [String: Any])
    }

    func testMissingAndNullFramingBothDecodeAsNoFraming() throws {
        let missing = try JSONDecoder().decode(SlidePostEdits.self, from: Data(#"{"look_preset":"none"}"#.utf8))
        let null = try JSONDecoder().decode(SlidePostEdits.self, from: Data(#"{"look_preset":"none","framing":null}"#.utf8))
        XCTAssertNil(missing.framing)
        XCTAssertNil(null.framing)
    }

    func testEncodingAlwaysWritesTheFramingKeySoAClearIsNeverMistakenForAnOlderApp() throws {
        let unframed = try editsJSON(SlidePostEdits())
        XCTAssertTrue(unframed.keys.contains("framing"), "A missing key means keep what is stored")
        XCTAssertTrue(unframed["framing"] is NSNull)

        let framed = try editsJSON(.init(framing: .init(orientation: .landscape, sourceCrop: .init(x: 0.1, y: 0, width: 0.8, height: 1))))
        let framing = try XCTUnwrap(framed["framing"] as? [String: Any])
        XCTAssertEqual(framing["orientation"] as? String, "landscape")
        XCTAssertEqual((framing["source_crop"] as? [String: Any])?["width"] as? Double, 0.8)
    }

    func testFramedSlidesSurviveTheSharedFixtureIntoTheSaveRequest() throws {
        var url = URL(fileURLWithPath: #filePath)
        url.deleteLastPathComponent(); url.deleteLastPathComponent()
        url.appendPathComponent("Fixtures/SlidePostBindingRoundTrip.json")
        let draft = try JSONDecoder().decode(SlidePostDraft.self, from: Data(contentsOf: url))
        XCTAssertNil(draft.slides[0].edits?.framing, "An older fixture without the key still decodes")
        XCTAssertNil(draft.slides[1].edits?.framing)
        XCTAssertEqual(draft.slides[2].edits?.framing?.orientation, .landscape)
        XCTAssertEqual(draft.slides[2].edits?.framing?.sourceCrop, .init(x: 0.1, y: 0, width: 0.8, height: 1))

        let body = try XCTUnwrap(JSONSerialization.jsonObject(with: JSONEncoder().encode(SlidePostSaveRequest(draft: draft, expectedVersion: 2))) as? [String: Any])
        let slides = try XCTUnwrap(body["slides"] as? [[String: Any]])
        for index in 0..<2 {
            let edits = try XCTUnwrap(slides[index]["edits"] as? [String: Any])
            XCTAssertTrue(edits["framing"] is NSNull, "slide \(index) must say `framing: null`, not omit it")
        }
        let framed = try XCTUnwrap((slides[2]["edits"] as? [String: Any])?["framing"] as? [String: Any])
        XCTAssertEqual(framed["orientation"] as? String, "landscape")
    }

    // MARK: Editing

    private func session(profile: String) -> SlidePostSession {
        let session = SlidePostSession()
        let slides = (1...2).map { SlidePostSlide(id: "s\($0)", assetID: "a\($0)", kind: "image") }
        session.draft = SlidePostDraft(version: 1, platformProfile: profile, slides: slides)
        return session
    }

    func testSettingAndClearingAFrameIsOneUndoableStagedEdit() {
        let session = session(profile: "tiktok_photo")
        let crop = NormalizedSourceRect(x: 0.2, y: 0.1, width: 0.5, height: 0.8)
        session.setFraming(slideID: "s1", .init(orientation: .portrait, sourceCrop: crop))
        XCTAssertEqual(session.draft?.slides[0].edits?.framing?.sourceCrop, crop)
        XCTAssertTrue(session.canUndoEdit)
        session.setFraming(slideID: "s1", .init())
        XCTAssertNil(session.draft?.slides[0].edits?.framing, "The default shape is stored as no framing")
        session.undoEdit()
        XCTAssertEqual(session.draft?.slides[0].edits?.framing?.sourceCrop, crop)
    }

    func testInstagramNeverKeepsALandscapeSlide() {
        let instagram = session(profile: "instagram_carousel")
        instagram.setFraming(slideID: "s1", .init(orientation: .landscape))
        XCTAssertNil(instagram.draft?.slides[0].edits?.framing, "Landscape is refused on Instagram")

        // A staged edit that moves a TikTok post to Instagram turns its landscape slides back to portrait.
        let tiktok = session(profile: "tiktok_photo")
        tiktok.setFraming(slideID: "s2", .init(orientation: .landscape))
        XCTAssertEqual(tiktok.draft?.slides[1].edits?.framing?.orientation, .landscape)
        var moved = tiktok.draft!
        moved.platformProfile = "instagram_carousel"
        tiktok.stageDraft(moved)
        XCTAssertNil(tiktok.draft?.slides[1].edits?.framing)
    }

    func testTheCapabilityDecodesAndDefaultsOff() throws {
        let on = try JSONDecoder().decode(CreationCapabilities.self, from: Data(#"{"formats":[],"slide_post_framing":true}"#.utf8))
        let old = try JSONDecoder().decode(CreationCapabilities.self, from: Data(#"{"formats":[]}"#.utf8))
        XCTAssertTrue(on.slidePostFramingEnabled)
        XCTAssertFalse(old.slidePostFramingEnabled)
    }
}
