import CoreImage
import ImageIO
import KriaMediaEngine
import UniformTypeIdentifiers
import XCTest
@testable import Kria

/// KRI-1138: the phone and the cloud must turn a slide's photo the same way, and the save request must say exactly
/// what the creator did to a tilt (set, clear, or nothing). Failure modes, written before the code:
///  - wrong direction (counter-clockwise) or about the wrong point
///  - cloud and phone geometry drifting (the shared fixture)
///  - the backdrop missing, bleeding over the media, or a blur that renders black
///  - a decoded tilt this build cannot read silently becoming "no tilt" on the next save
///  - a drag of the dial stacking one undo step per change
@MainActor final class SlidePostTiltTests: XCTestCase {
    // MARK: Shared fixture with the cloud

    private struct Fixture: Decodable {
        struct Crop: Decodable { let x, y, width, height: Double }
        struct Size: Decodable { let w, h: Double }
        struct Expected: Decodable {
            let media: Size
            let rotatedBounds: Size
            let corners: [[Double]]
            let coversCanvas: Bool
            enum CodingKeys: String, CodingKey { case media, corners; case rotatedBounds = "rotated_bounds", coversCanvas = "covers_canvas" }
        }
        struct Case: Decodable {
            let name: String
            let canvas: Size
            let image: Size
            let crop: Crop?
            let fit: Bool
            let rotationDeg: Double
            let zoom: Double?
            let expected: Expected
            enum CodingKeys: String, CodingKey { case name, canvas, image, crop, fit, expected, zoom; case rotationDeg = "rotation_deg" }
        }
        struct Validation: Decodable {
            let name: String
            let tilt: JSONValue
            let valid: Bool
        }
        struct Normalization: Decodable { let name: String; let tilt: JSONValue; let stored: JSONValue? }
        let cases: [Case]
        let validation: [Validation]
        let normalization: [Normalization]
    }

    /// Any JSON value, kept as raw data so the real decoder is the one under test.
    private struct JSONValue: Decodable {
        let data: Data
        init(from decoder: Decoder) throws {
            let value = try decoder.singleValueContainer()
            let object = try value.decode(AnyDecodable.self)
            data = try JSONSerialization.data(withJSONObject: object.value, options: [.fragmentsAllowed])
        }
    }
    private struct AnyDecodable: Decodable {
        let value: Any
        init(from decoder: Decoder) throws {
            let c = try decoder.singleValueContainer()
            if c.decodeNil() { value = NSNull() }
            else if let v = try? c.decode(Bool.self) { value = v }
            else if let v = try? c.decode(Double.self) { value = v }
            else if let v = try? c.decode(String.self) { value = v }
            else if let v = try? c.decode([AnyDecodable].self) { value = v.map(\.value) }
            else { value = try c.decode([String: AnyDecodable].self).mapValues(\.value) }
        }
    }

    private func fixture(_ version: String = "v1") throws -> Fixture {
        var url = URL(fileURLWithPath: #filePath)
        for _ in 0..<6 { url.deleteLastPathComponent() }
        url.appendPathComponent("tests/fixtures/slide-tilt-parity/\(version).json")
        return try JSONDecoder().decode(Fixture.self, from: Data(contentsOf: url))
    }

    private func assertGeometry(_ file: Fixture) {
        for item in file.cases {
            let canvas = CGSize(width: item.canvas.w, height: item.canvas.h)
            let crop = item.crop.map { NormalizedSourceRect(x: $0.x, y: $0.y, width: $0.width, height: $0.height) }
            let image = CGSize(width: item.image.w, height: item.image.h)
            let media = SlidePostTiltGeometry.mediaSize(imageSize: image, crop: crop, canvas: canvas, fit: item.fit, zoom: item.zoom ?? 1)
            XCTAssertEqual(media.width, item.expected.media.w, accuracy: 1e-6, item.name)
            XCTAssertEqual(media.height, item.expected.media.h, accuracy: 1e-6, item.name)
            let bounds = SlidePostTiltGeometry.rotatedBounds(media, degrees: item.rotationDeg)
            XCTAssertEqual(bounds.width, item.expected.rotatedBounds.w, accuracy: 1e-6, item.name)
            XCTAssertEqual(bounds.height, item.expected.rotatedBounds.h, accuracy: 1e-6, item.name)
            let corners = SlidePostTiltGeometry.rotatedCorners(media, degrees: item.rotationDeg, canvas: canvas)
            for (got, want) in zip(corners, item.expected.corners) {
                XCTAssertEqual(got.x, want[0], accuracy: 1e-6, item.name)
                XCTAssertEqual(got.y, want[1], accuracy: 1e-6, item.name)
            }
            XCTAssertEqual(SlidePostTiltGeometry.coversCanvas(media, degrees: item.rotationDeg, canvas: canvas), item.expected.coversCanvas, item.name)
        }
    }

    func testGeometryMatchesTheSharedFixture() throws {
        let file = try fixture()
        XCTAssertGreaterThanOrEqual(file.cases.count, 8)
        assertGeometry(file)
    }

    /// KRI-1170: the zoomed media (cover or whole, times zoom) feeds the same bounds, corners and coverage as the cloud.
    func testZoomedGeometryMatchesTheSharedFixtureV2() throws {
        let file = try fixture("v2")
        XCTAssertGreaterThanOrEqual(file.cases.count, 8)
        XCTAssertTrue(file.cases.allSatisfy { $0.zoom != nil })
        assertGeometry(file)
    }

    func testZoomValidationAndNormalizationMatchTheSharedFixtureV2() throws {
        let file = try fixture("v2")
        for item in file.validation {
            let decoded = try? JSONDecoder().decode(SlidePostTilt.self, from: item.tilt.data)
            XCTAssertEqual(decoded != nil, item.valid, item.name)
        }
        for item in file.normalization {
            let decoded = try JSONDecoder().decode(SlidePostTilt.self, from: item.tilt.data)
            XCTAssertEqual(decoded.isNeutral, item.stored == nil, item.name)
            guard let stored = item.stored else { continue }
            let wire = try JSONSerialization.jsonObject(with: JSONEncoder().encode(decoded)) as? NSDictionary
            let want = try JSONSerialization.jsonObject(with: stored.data) as? NSDictionary
            XCTAssertEqual(wire, want, "\(item.name): the encoded tilt is the stored shape (zoom 1 left out)")
        }
    }

    func testValidationVerdictsMatchTheSharedFixture() throws {
        for item in try fixture().validation {
            let decoded = try? JSONDecoder().decode(SlidePostTilt.self, from: item.tilt.data)
            // The server also refuses a colour on a non-colour backdrop and unknown keys; the engine's strict decoder does too.
            XCTAssertEqual(decoded != nil, item.valid, item.name)
        }
    }

    func testAnUprightTiltIsNoTiltAndDrawsNothing() throws {
        for item in try fixture().normalization {
            let decoded = try JSONDecoder().decode(SlidePostTilt.self, from: item.tilt.data)
            XCTAssertTrue(decoded.isNeutral, item.name)
            let slide = SlidePostSlide(id: "s", assetID: "a", kind: "image", edits: .init(tilt: decoded))
            XCTAssertNil(SlidePostFraming.effectiveTilt(for: slide), item.name)
        }
    }

    // MARK: Save semantics

    private func editsJSON(_ edits: SlidePostEdits) throws -> [String: Any] {
        try XCTUnwrap(JSONSerialization.jsonObject(with: JSONEncoder().encode(edits)) as? [String: Any])
    }

    func testEncodingAlwaysWritesTheTiltKeySoAClearIsNeverMistakenForAnOlderApp() throws {
        let plain = try editsJSON(SlidePostEdits())
        XCTAssertTrue(plain.keys.contains("tilt"), "A missing key means keep what is stored")
        XCTAssertTrue(plain["tilt"] is NSNull)

        let turned = try editsJSON(.init(tilt: SlidePostTilt(rotationDeg: 12.5, backdrop: .blur)))
        let tilt = try XCTUnwrap(turned["tilt"] as? [String: Any])
        XCTAssertEqual(tilt["rotation_deg"] as? Double, 12.5)
        XCTAssertEqual((tilt["backdrop"] as? [String: Any])?["kind"] as? String, "blur")
        XCTAssertNil((tilt["backdrop"] as? [String: Any])?["color"])
    }

    func testTheWireShapeRoundTripsAndAnOmittedBackdropIsBlank() throws {
        let coloured = SlidePostEdits(tilt: SlidePostTilt(rotationDeg: -40, backdrop: .color("#336699")))
        let again = try JSONDecoder().decode(SlidePostEdits.self, from: JSONEncoder().encode(coloured))
        XCTAssertEqual(again, coloured)
        let bare = try JSONDecoder().decode(SlidePostTilt.self, from: Data(#"{"rotation_deg": 5}"#.utf8))
        XCTAssertEqual(bare.backdrop, .blank)
    }

    func testAUnitZoomIsLeftOutSoATiltStoredBeforeZoomRoundTripsByteForByte() throws {
        let old = Data(#"{"backdrop":{"kind":"blur"},"rotation_deg":12.5}"#.utf8)
        let tilt = try JSONDecoder().decode(SlidePostTilt.self, from: old)
        XCTAssertEqual(tilt.zoom, 1)
        let wire = try XCTUnwrap(JSONSerialization.jsonObject(with: JSONEncoder().encode(tilt)) as? [String: Any])
        XCTAssertEqual(Set(wire.keys), ["rotation_deg", "backdrop"], "no zoom key at 1x")
        let zoomed = try XCTUnwrap(JSONSerialization.jsonObject(with: JSONEncoder().encode(SlidePostTilt(rotationDeg: 0, zoom: 1.5))) as? [String: Any])
        XCTAssertEqual(zoomed["zoom"] as? Double, 1.5)
    }

    func testAZoomAloneIsATiltAndIsNeverReadAsUpright() throws {
        XCTAssertTrue(SlidePostTilt(rotationDeg: 0, backdrop: .blur).isNeutral)
        XCTAssertFalse(SlidePostTilt(rotationDeg: 0, zoom: 1.25).isNeutral)
        let slide = SlidePostSlide(id: "s", assetID: "a", kind: "image", edits: .init(tilt: SlidePostTilt(rotationDeg: 0, zoom: 0.6)))
        XCTAssertEqual(SlidePostFraming.effectiveTilt(for: slide)?.zoom, 0.6)
    }

    func testAZoomIsHeldToTheRangeAndRoundedToTheHundredth() {
        XCTAssertEqual(SlidePostTilt(rotationDeg: 5, zoom: 9).zoom, 3)
        XCTAssertEqual(SlidePostTilt(rotationDeg: 5, zoom: 0.1).zoom, 0.5)
        XCTAssertEqual(SlidePostTilt(rotationDeg: 5, zoom: 1.2999999).zoom, 1.3)
        XCTAssertEqual(SlidePostTilt(rotationDeg: 5, zoom: .nan).zoom, 1)
    }

    func testAZoomOutsideTheRangeFailsTheDecodeInsteadOfBecomingNoZoom() {
        for bad in [#"{"rotation_deg": 5, "zoom": 0.4}"#, #"{"rotation_deg": 5, "zoom": 3.5}"#, #"{"rotation_deg": 5, "zoom": "big"}"#] {
            XCTAssertThrowsError(try JSONDecoder().decode(SlidePostEdits.self, from: Data("{\"tilt\": \(bad)}".utf8)), bad)
        }
    }

    func testMissingAndNullTiltBothDecodeAsNoTilt() throws {
        let missing = try JSONDecoder().decode(SlidePostEdits.self, from: Data(#"{"look_preset":"none"}"#.utf8))
        let null = try JSONDecoder().decode(SlidePostEdits.self, from: Data(#"{"look_preset":"none","tilt":null}"#.utf8))
        XCTAssertNil(missing.tilt)
        XCTAssertNil(null.tilt)
    }

    func testATiltThisBuildCannotReadFailsTheDecodeInsteadOfBecomingNoTilt() {
        for bad in [#"{"rotation_deg": 200}"#, #"{"rotation_deg": 10, "backdrop": {"kind": "media"}}"#, #"{"backdrop": {"kind": "blur"}}"#] {
            XCTAssertThrowsError(try JSONDecoder().decode(SlidePostEdits.self, from: Data("{\"tilt\": \(bad)}".utf8)), bad)
        }
    }

    func testCapabilityIsOffUnlessTheServerSaysSo() throws {
        let off = try JSONDecoder().decode(CreationCapabilities.self, from: Data(#"{"formats":[]}"#.utf8))
        XCTAssertFalse(off.slidePostTiltEnabled)
        let on = try JSONDecoder().decode(CreationCapabilities.self, from: Data(#"{"formats":[],"slide_post_tilt":true}"#.utf8))
        XCTAssertTrue(on.slidePostTiltEnabled)
        XCTAssertFalse(on.slidePostTiltZoomEnabled, "zoom is its own capability")
        let zoom = try JSONDecoder().decode(CreationCapabilities.self, from: Data(#"{"formats":[],"slide_post_tilt":true,"slide_post_tilt_zoom":true}"#.utf8))
        XCTAssertTrue(zoom.slidePostTiltZoomEnabled)
    }

    func testThisBuildSpeaksTheProtocolThatCarriesZoom() {
        XCTAssertGreaterThanOrEqual(KriaClientProtocolContract.version, 33, "a build that ships zoom must declare protocol 33")
    }

    // MARK: The session

    private func session() -> SlidePostSession {
        let session = SlidePostTestSubmissionStorage.session()
        session.draft = SlidePostDraft(version: 1, platformProfile: "tiktok_photo", slides: [
            SlidePostSlide(id: "s1", assetID: "a1", kind: "image"), SlidePostSlide(id: "s2", assetID: "a2", kind: "video"),
        ])
        return session
    }

    func testPhotosAndVideosBothTurnAndUprightClears() {
        let session = session()
        session.setTilt(slideID: "s1", SlidePostTilt(rotationDeg: 10))
        session.setTilt(slideID: "s2", SlidePostTilt(rotationDeg: -20, backdrop: .blur))
        XCTAssertEqual(session.draft?.slides[0].edits?.tilt?.rotationDeg, 10)
        XCTAssertEqual(session.draft?.slides[1].edits?.tilt?.backdrop, .blur)
        session.setTilt(slideID: "s1", SlidePostTilt(rotationDeg: 0, backdrop: .blur))
        XCTAssertNil(session.draft?.slides[0].edits?.tilt, "an upright turn is stored as no tilt")
    }

    func testAZoomDragIsOneUndoStepAndKeepsTheAngleAndBackdrop() {
        let session = session()
        session.setTilt(slideID: "s1", SlidePostTilt(rotationDeg: 20, backdrop: .blur))
        for zoom in stride(from: 1.05, through: 2.0, by: 0.05) {
            session.setTilt(slideID: "s1", SlidePostTilt(rotationDeg: 20, backdrop: .blur, zoom: zoom), coalescing: "zoom-drag")
        }
        XCTAssertEqual(session.draft?.slides[0].edits?.tilt, SlidePostTilt(rotationDeg: 20, backdrop: .blur, zoom: 2))
        session.undoEdit()
        XCTAssertEqual(session.draft?.slides[0].edits?.tilt, SlidePostTilt(rotationDeg: 20, backdrop: .blur), "one undo walks the drag back to the turn")
        session.setTilt(slideID: "s2", SlidePostTilt(rotationDeg: 0, zoom: 1.5))
        XCTAssertEqual(session.draft?.slides[1].edits?.tilt?.zoom, 1.5, "a video zooms too, with no turn")
        session.setTilt(slideID: "s2", SlidePostTilt(rotationDeg: 0, zoom: 1))
        XCTAssertNil(session.draft?.slides[1].edits?.tilt, "zoom back to 1x at 0 degrees is no tilt")
    }

    func testADialDragIsOneUndoStepAndANoOpStagesNothing() {
        let session = session()
        for degrees in stride(from: 1.0, through: 30.0, by: 1.0) {
            session.setTilt(slideID: "s1", SlidePostTilt(rotationDeg: degrees), coalescing: "drag")
        }
        session.setTilt(slideID: "s1", SlidePostTilt(rotationDeg: 30))
        XCTAssertEqual(session.draft?.slides[0].edits?.tilt?.rotationDeg, 30)
        session.undoEdit()
        XCTAssertNil(session.draft?.slides[0].edits?.tilt, "one undo walks back the whole drag")
        XCTAssertFalse(session.canUndoEdit)
    }

    // MARK: Panel helpers

    func testTheDialSnapsToItsDetentsAndTheQuarterTurnWalksClockwise() {
        XCTAssertEqual(SlidePostTiltControls.snapped(2), 0)
        XCTAssertEqual(SlidePostTiltControls.snapped(-88), -90)
        XCTAssertEqual(SlidePostTiltControls.snapped(178), 180)
        XCTAssertEqual(SlidePostTiltControls.snapped(10), 10)
        var angle = 0.0
        var walked: [Double] = []
        for _ in 0..<5 { angle = SlidePostTiltControls.quarterTurn(from: angle); walked.append(angle) }
        XCTAssertEqual(walked, [90, 180, -90, 0, 90])
        XCTAssertEqual(SlidePostTiltControls.readout(0), "Upright")
        XCTAssertEqual(SlidePostTiltControls.readout(12), "12\u{00B0} right")
        XCTAssertEqual(SlidePostTiltControls.readout(-30), "30\u{00B0} left")
    }

    func testTheZoomSliderLandsOnOneXAndReadsLikeALabel() {
        XCTAssertEqual(SlidePostTiltControls.snappedZoom(1.03), 1)
        XCTAssertEqual(SlidePostTiltControls.snappedZoom(0.97), 1)
        XCTAssertEqual(SlidePostTiltControls.snappedZoom(1.05), 1.05)
        XCTAssertEqual(SlidePostTiltControls.snappedZoom(0.95), 0.95)
        XCTAssertEqual(SlidePostTiltControls.snappedZoom(3.4), 3)
        XCTAssertEqual(SlidePostTiltControls.zoomReadout(1.3), "1.3x")
        XCTAssertEqual(SlidePostTiltControls.zoomReadout(1), "1.0x")
        XCTAssertEqual(SlidePostTiltControls.zoomReadout(0.5), "0.5x")
    }

    // MARK: Real render against the shared geometry

    private var directory: URL!
    override func setUp() {
        super.setUp()
        directory = FileManager.default.temporaryDirectory.appending(path: "tilt-\(UUID())")
        try? FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
    }
    override func tearDown() {
        try? FileManager.default.removeItem(at: directory)
        super.tearDown()
    }

    /// Left half red, right half blue.
    private func halvesPhoto(width: Int = 400, height: Int = 300) throws -> URL {
        let format = UIGraphicsImageRendererFormat(); format.scale = 1; format.opaque = true
        let image = UIGraphicsImageRenderer(size: CGSize(width: width, height: height), format: format).image { _ in
            UIColor(red: 0.9, green: 0.08, blue: 0.08, alpha: 1).setFill(); UIRectFill(CGRect(x: 0, y: 0, width: width / 2, height: height))
            UIColor(red: 0.08, green: 0.08, blue: 0.9, alpha: 1).setFill(); UIRectFill(CGRect(x: width / 2, y: 0, width: width - width / 2, height: height))
        }
        let url = directory.appending(path: "halves.jpg")
        let destination = try XCTUnwrap(CGImageDestinationCreateWithURL(url as CFURL, UTType.jpeg.identifier as CFString, 1, nil))
        CGImageDestinationAddImage(destination, try XCTUnwrap(image.cgImage), [kCGImageDestinationLossyCompressionQuality: 1.0] as CFDictionary)
        XCTAssertTrue(CGImageDestinationFinalize(destination))
        return url
    }

    private func pixels(_ data: Data) throws -> (width: Int, height: Int, rgb: (Int, Int) -> (Int, Int, Int)) {
        let cg = try XCTUnwrap(UIImage(data: data)?.cgImage)
        let width = cg.width, height = cg.height
        var bytes = [UInt8](repeating: 0, count: width * height * 4)
        let context = CGContext(data: &bytes, width: width, height: height, bitsPerComponent: 8, bytesPerRow: width * 4,
                                space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue)!
        context.draw(cg, in: CGRect(x: 0, y: 0, width: width, height: height))
        let snapshot = bytes
        return (width, height, { x, y in
            let i = (min(max(y, 0), height - 1) * width + min(max(x, 0), width - 1)) * 4
            return (Int(snapshot[i]), Int(snapshot[i + 1]), Int(snapshot[i + 2]))
        })
    }

    private func near(_ a: (Int, Int, Int), _ b: (Int, Int, Int), _ tolerance: Int = 60) -> Bool {
        abs(a.0 - b.0) <= tolerance && abs(a.1 - b.1) <= tolerance && abs(a.2 - b.2) <= tolerance
    }

    private let red = (230, 20, 20), blue = (20, 20, 230)

    private func backdropName(_ backdrop: ClipBackdrop) -> String {
        switch backdrop { case .blank: "blank"; case .blur: "blur"; case .color: "colour"; case .media: "media" }
    }

    /// What a canvas pixel must show for the halves photo, from the shared geometry alone: a half colour, "backdrop", or nil
    /// near an edge or the seam where anti-aliasing makes the answer ambiguous.
    private func expected(_ point: CGPoint, canvas: CGSize, image: CGSize, fit: Bool, degrees: Double, zoom: Double = 1) -> String? {
        let media = SlidePostTiltGeometry.mediaSize(imageSize: image, crop: nil, canvas: canvas, fit: fit, zoom: zoom)
        let radians = degrees * .pi / 180
        let dx = Double(point.x - canvas.width / 2), dy = Double(point.y - canvas.height / 2)
        let mx = dx * cos(radians) + dy * sin(radians), my = -dx * sin(radians) + dy * cos(radians)
        let margin = 14.0
        if abs(abs(mx) - Double(media.width) / 2) < margin || abs(abs(my) - Double(media.height) / 2) < margin { return nil }
        if abs(mx) > Double(media.width) / 2 || abs(my) > Double(media.height) / 2 { return "backdrop" }
        if abs(mx) < margin { return nil }
        return mx < 0 ? "red" : "blue"
    }

    private func check(degrees: Double, fit: SlidePostFit?, backdrop: ClipBackdrop, backdropColour: (Int, Int, Int)?, zoom: Double = 1,
                       profile: String = "instagram_carousel", file: StaticString = #filePath, line: UInt = #line) async throws {
        let url = try halvesPhoto()
        let tilt = SlidePostTilt(rotationDeg: degrees, backdrop: backdrop, zoom: zoom)
        let slide = SlidePostSlide(id: "s", assetID: "a", kind: "image", edits: SlidePostEdits(fit: fit, tilt: tilt))
        let asset = SlidePostAsset(id: "a", kind: "image", status: "ready", sourceURL: url)
        let data = try await SlidePostOnDeviceRender.renderSlide(slide: slide, asset: asset, profile: profile, aspect: nil, scale: 1)
        // Optional artifact for side-by-side review with the cloud render (TEST_RUNNER_KRIA_TILT_ARTIFACTS=<dir>).
        if let dir = ProcessInfo.processInfo.environment["KRIA_TILT_ARTIFACTS"] {
            try data.write(to: URL(fileURLWithPath: dir).appending(path: "device-\(Int(degrees))-\(fit?.rawValue ?? "fill")-\(backdropName(backdrop))-z\(Int((zoom * 100).rounded())).jpg"))
        }
        let out = try pixels(data)
        let canvas = SlidePostOnDeviceRender.canvas(profile: profile, aspect: nil)
        XCTAssertEqual(CGSize(width: out.width, height: out.height), canvas, file: file, line: line)
        var judged = 0, wrong = 0, backdropSeen = 0
        for column in 0..<9 {
            for row in 0..<15 {
                let point = CGPoint(x: (Double(column) + 0.5) * canvas.width / 9, y: (Double(row) + 0.5) * canvas.height / 15)
                // The Kria watermark sits in the lower left: not part of the photo.
                if point.x < canvas.width * 0.35, point.y > canvas.height * 0.6 { continue }
                guard let want = expected(point, canvas: canvas, image: CGSize(width: 400, height: 300), fit: fit != nil, degrees: degrees, zoom: zoom) else { continue }
                let got = out.rgb(Int(point.x), Int(point.y))
                judged += 1
                switch want {
                case "backdrop":
                    backdropSeen += 1
                    if let backdropColour, !near(got, backdropColour) { wrong += 1 }
                    if backdropColour == nil, !near(got, (0, 0, 0), 70) { wrong += 1 }
                case "red": if !near(got, red) { wrong += 1 }
                default: if !near(got, blue) { wrong += 1 }
                }
            }
        }
        XCTAssertGreaterThanOrEqual(judged, 40, file: file, line: line)
        XCTAssertLessThanOrEqual(wrong, max(1, judged / 40), "wrong \(wrong) of \(judged), backdrop points \(backdropSeen)", file: file, line: line)
    }

    func testAClockwiseTurnMatchesTheSharedGeometry() async throws {
        try await check(degrees: 30, fit: nil, backdrop: .blank, backdropColour: nil)
    }

    func testACounterClockwiseTurnMatchesTheSharedGeometry() async throws {
        try await check(degrees: -25, fit: nil, backdrop: .blank, backdropColour: nil)
    }

    func testAQuarterTurnPutsTheLeftHalfOnTop() async throws {
        try await check(degrees: 90, fit: nil, backdrop: .blank, backdropColour: nil)
        let url = try halvesPhoto()
        let slide = SlidePostSlide(id: "s", assetID: "a", kind: "image", edits: .init(tilt: SlidePostTilt(rotationDeg: 90)))
        let asset = SlidePostAsset(id: "a", kind: "image", status: "ready", sourceURL: url)
        let out = try pixels(try await SlidePostOnDeviceRender.renderSlide(slide: slide, asset: asset, profile: "instagram_carousel", aspect: nil, scale: 1))
        XCTAssertTrue(near(out.rgb(out.width / 2, out.height / 5), red), "clockwise: the old left (red) edge ends on top")
        XCTAssertTrue(near(out.rgb(out.width / 2, out.height * 4 / 5), blue))
    }

    /// KRI-1170: the same point-by-point check as the cloud's `TestZoomImageRender`, on the Core Image path.
    func testZoomedPhotosMatchTheSharedGeometry() async throws {
        try await check(degrees: 0, fit: nil, backdrop: .blank, backdropColour: nil, zoom: 1.5)
        try await check(degrees: 0, fit: nil, backdrop: .blank, backdropColour: nil, zoom: 0.5)
        try await check(degrees: 20, fit: nil, backdrop: .blank, backdropColour: nil, zoom: 0.7)
        try await check(degrees: 20, fit: nil, backdrop: .blank, backdropColour: nil, zoom: 1)
        try await check(degrees: 20, fit: nil, backdrop: .blank, backdropColour: nil, zoom: 1.5)
        try await check(degrees: 30, fit: .black, backdrop: .blank, backdropColour: nil, zoom: 2)
        try await check(degrees: -35, fit: nil, backdrop: .blank, backdropColour: nil, zoom: 0.6)
        try await check(degrees: -35, fit: nil, backdrop: .blank, backdropColour: nil, zoom: 1.5)
        try await check(degrees: 60, fit: nil, backdrop: .blank, backdropColour: nil, zoom: 3)
        try await check(degrees: 30, fit: .black, backdrop: .blank, backdropColour: nil, zoom: 0.6)
        try await check(degrees: 0, fit: .white, backdrop: .blank, backdropColour: nil, zoom: 2.5)
        try await check(degrees: 20, fit: .black, backdrop: .color("#336699"), backdropColour: (0x33, 0x66, 0x99), zoom: 0.7)
    }

    func testAStickerKeepsItsSizeAndPlaceUnderAZoomedPhoto() async throws {
        let url = try halvesPhoto()
        let tile = directory.appending(path: "zoom-sticker.png")
        let sticker = UIGraphicsImageRenderer(size: CGSize(width: 50, height: 50)).image { _ in
            UIColor(red: 0, green: 0.9, blue: 0, alpha: 1).setFill(); UIRectFill(CGRect(x: 0, y: 0, width: 50, height: 50))
        }
        try XCTUnwrap(sticker.pngData()).write(to: tile)
        let model = SlidePostSticker(id: "st", assetID: "pic", xFrac: 0.5, yFrac: 0.5, widthFrac: 0.2, rotationDeg: 0)
        let prepared = SlidePostPreparedSticker(sticker: model, image: try XCTUnwrap(UIImage(contentsOfFile: tile.path)))
        let slide = SlidePostSlide(id: "s", assetID: "a", kind: "image", edits: .init(stickers: [model], tilt: SlidePostTilt(rotationDeg: 0, zoom: 2.5)))
        let asset = SlidePostAsset(id: "a", kind: "image", status: "ready", sourceURL: url)
        let out = try pixels(try await SlidePostOnDeviceRender.renderSlide(slide: slide, asset: asset, profile: "instagram_carousel", aspect: nil, stickers: [prepared], scale: 1))
        let half = Int(0.2 * 1080 / 2)
        let inside = out.rgb(540 + half - 8, 675), outside = out.rgb(540 + half + 14, 675)
        XCTAssertTrue(inside.1 > 160 && inside.0 < 80, "the sticker still covers its own 20 percent width: \(inside)")
        XCTAssertFalse(outside.1 > 160 && outside.0 < 80, "and did not grow with the photo: \(outside)")
    }

    func testAWholePhotoOverAColourBackdropFillsTheUncoveredCorners() async throws {
        try await check(degrees: 20, fit: .black, backdrop: .color("#336699"), backdropColour: (0x33, 0x66, 0x99))
    }

    func testBlurBackdropIsASoftCopyNotBlackAndNeverCoversTheMedia() async throws {
        let url = try halvesPhoto()
        let slide = SlidePostSlide(id: "s", assetID: "a", kind: "image", edits: .init(fit: .black, tilt: SlidePostTilt(rotationDeg: 20, backdrop: .blur)))
        let asset = SlidePostAsset(id: "a", kind: "image", status: "ready", sourceURL: url)
        let out = try pixels(try await SlidePostOnDeviceRender.renderSlide(slide: slide, asset: asset, profile: "instagram_carousel", aspect: nil, scale: 1))
        // The top-left corner is outside the turned, whole photo: a blurred red wash, not black.
        let corner = out.rgb(30, 30)
        XCTAssertGreaterThan(corner.0, 100)
        XCTAssertLessThan(corner.2, 120)
        // The photo centre keeps its colours (the seam sits at the centre, so look a little to each side).
        XCTAssertTrue(near(out.rgb(out.width / 2 - 80, out.height / 2), red))
        XCTAssertTrue(near(out.rgb(out.width / 2 + 80, out.height / 2), blue))
    }

    func testAStickerStaysUprightOnATurnedPhoto() async throws {
        let url = try halvesPhoto()
        let tile = directory.appending(path: "sticker.png")
        let sticker = UIGraphicsImageRenderer(size: CGSize(width: 50, height: 50)).image { _ in
            UIColor(red: 0, green: 0.9, blue: 0, alpha: 1).setFill(); UIRectFill(CGRect(x: 0, y: 0, width: 50, height: 50))
        }
        try XCTUnwrap(sticker.pngData()).write(to: tile)
        let model = SlidePostSticker(id: "st", assetID: "pic", xFrac: 0.5, yFrac: 0.5, widthFrac: 0.4, rotationDeg: 0)
        let prepared = SlidePostPreparedSticker(sticker: model, image: try XCTUnwrap(UIImage(contentsOfFile: tile.path)))
        let slide = SlidePostSlide(id: "s", assetID: "a", kind: "image", edits: .init(stickers: [model], tilt: SlidePostTilt(rotationDeg: 30)))
        let asset = SlidePostAsset(id: "a", kind: "image", status: "ready", sourceURL: url)
        let out = try pixels(try await SlidePostOnDeviceRender.renderSlide(slide: slide, asset: asset, profile: "instagram_carousel", aspect: nil, stickers: [prepared], scale: 1))
        let half = Int(0.4 * 1080 / 2)
        for (sx, sy) in [(-1, -1), (1, -1), (1, 1), (-1, 1)] {
            let pixel = out.rgb(540 + sx * (half - 8), 675 + sy * (half - 8))
            XCTAssertTrue(pixel.1 > 160 && pixel.0 < 80, "sticker corner (\(sx), \(sy)) is still green and axis-aligned: \(pixel)")
        }
    }
}
