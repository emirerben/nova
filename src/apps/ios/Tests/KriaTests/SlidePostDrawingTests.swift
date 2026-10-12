import CoreImage
import ImageIO
import KriaMediaEngine
import UniformTypeIdentifiers
import XCTest
@testable import Kria

/// KRI-589 PR 2: freehand ink on slides. The wire, the caps and the identity-transform geometry are pinned by
/// `tests/fixtures/slide-drawing-parity/v1.json`, the same file the server's `SlideDrawing` validator and
/// skia painter are held to.
@MainActor final class SlidePostDrawingTests: XCTestCase {
    private let itemID = "33333333-3333-3333-3333-333333333333"
    private var defaults: UserDefaults!
    private var suite = ""
    private var directory: URL!

    override func setUp() {
        super.setUp()
        suite = "SlidePostDrawingTests.\(UUID())"; defaults = UserDefaults(suiteName: suite)
        directory = FileManager.default.temporaryDirectory.appending(path: "slidedrawing-\(UUID())")
        try? FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
    }
    override func tearDown() {
        NativeEditorURLProtocol.handler = nil
        defaults.removePersistentDomain(forName: suite)
        try? FileManager.default.removeItem(at: directory)
        super.tearDown()
    }

    // MARK: Fixture

    private struct Fixture: Decodable {
        struct Caps: Decodable { let max_strokes_per_slide: Int; let max_points_per_slide: Int; let max_points_per_post: Int }
        struct Named: Decodable { let name: String; let drawing: JSONValue }
        struct Geometry: Decodable {
            let name: String
            let canvas: [Double]
            let canvas_aspect: Double
            let points: [[Double]]
            let width_frac: Double
            let expected_fit_origin: [Double]
            let expected_fit_size: [Double]
            let expected_points_px: [[Double]]
            let expected_width_px: Double
        }
        let caps: Caps
        let drawings: [Named]
        let invalid: [Named]
        let geometry: [Geometry]
    }
    private func fixture() throws -> Fixture {
        var url = URL(fileURLWithPath: #filePath)
        for _ in 0..<6 { url.deleteLastPathComponent() }
        url.appendPathComponent("tests/fixtures/slide-drawing-parity/v1.json")
        return try JSONDecoder().decode(Fixture.self, from: Data(contentsOf: url))
    }
    private func data(_ value: JSONValue) throws -> Data { try JSONEncoder().encode(value) }

    func testEveryFixtureDrawingDecodesAndRoundTripsLosslessly() throws {
        let file = try fixture()
        XCTAssertGreaterThanOrEqual(file.drawings.count, 5)
        for item in file.drawings {
            let drawing = try JSONDecoder().decode(SlidePostDrawing.self, from: data(item.drawing))
            XCTAssertTrue(drawing.isValid, item.name)
            XCTAssertEqual(drawing.json, item.drawing, "\(item.name) must encode back to the wire it came from")
            // Through the real encoder + decoder too (what a save and a reload do).
            let again = try JSONDecoder().decode(SlidePostDrawing.self, from: JSONEncoder().encode(drawing))
            XCTAssertEqual(again, drawing, item.name)
        }
        let textures = Set(file.drawings.flatMap { $0.drawing.objectValue?["strokes"]?.arrayValue ?? [] }.compactMap { $0.objectValue?["texture"]?.stringValue })
        XCTAssertEqual(textures, Set(DrawingTexture.allCases.map(\.rawValue)), "the fixture must exercise every texture")
    }

    func testEveryInvalidFixtureEntryIsRejected() throws {
        let file = try fixture()
        XCTAssertGreaterThanOrEqual(file.invalid.count, 9)
        for item in file.invalid {
            XCTAssertThrowsError(try JSONDecoder().decode(SlidePostDrawing.self, from: data(item.drawing)), "\(item.name) must be rejected")
            XCTAssertNil(SlidePostDrawing(json: item.drawing), item.name)
        }
    }

    func testCapsAreTheFixturesAndASlideOverThemIsInvalid() throws {
        let caps = try fixture().caps
        XCTAssertEqual(SlidePostDrawing.maxStrokes, caps.max_strokes_per_slide)
        XCTAssertEqual(SlidePostDrawing.maxPointsPerSlide, caps.max_points_per_slide)
        XCTAssertEqual(SlidePostDrawing.maxPointsPerPost, caps.max_points_per_post)
        XCTAssertTrue(SlidePostDrawing(canvasAspect: 0.5625, strokes: strokes(60, points: 2)).isValid)
        XCTAssertFalse(SlidePostDrawing(canvasAspect: 0.5625, strokes: strokes(61, points: 2)).isValid)
        XCTAssertTrue(SlidePostDrawing(canvasAspect: 0.5625, strokes: strokes(2, points: 1500)).isValid)
        XCTAssertFalse(SlidePostDrawing(canvasAspect: 0.5625, strokes: strokes(3, points: 1500)).isValid, "4,500 points on one slide")
    }

    // MARK: Three-state save contract

    private func object(_ value: some Encodable) throws -> [String: Any] {
        try XCTUnwrap(JSONSerialization.jsonObject(with: JSONEncoder().encode(value)) as? [String: Any])
    }

    func testEncodingAlwaysSaysWhetherTheDrawingIsSetOrCleared() throws {
        XCTAssertTrue(try object(SlidePostEdits())["drawing"] is NSNull, "null clears; a missing key would mean an older app")
        let drawing = SlidePostDrawing(canvasAspect: 0.5625, strokes: strokes(1, points: 3))
        let sent = try XCTUnwrap(try object(SlidePostEdits(drawing: drawing))["drawing"] as? [String: Any])
        XCTAssertEqual(sent["canvas_aspect"] as? Double, 0.5625)
        XCTAssertEqual((sent["strokes"] as? [[String: Any]])?.first?["width_frac"] as? Double, 0.02, "snake_case on the slide wire")
        XCTAssertNil((sent["strokes"] as? [[String: Any]])?.first?["widthFrac"])
    }

    func testAnOlderPayloadWithoutTheKeyDecodesAsNoDrawing() throws {
        let missing = try JSONDecoder().decode(SlidePostEdits.self, from: Data(#"{"look_preset":"none"}"#.utf8))
        let null = try JSONDecoder().decode(SlidePostEdits.self, from: Data(#"{"look_preset":"none","drawing":null}"#.utf8))
        XCTAssertNil(missing.drawing); XCTAssertNil(null.drawing)
        let set = try JSONDecoder().decode(SlidePostEdits.self, from: Data(##"{"look_preset":"none","drawing":{"canvas_aspect":1,"strokes":[{"color":"#FF3B30","width_frac":0.02,"points":[[0.1,0.1],[0.9,0.9]]}]}}"##.utf8))
        XCTAssertEqual(set.drawing?.strokes.count, 1)
        XCTAssertEqual(set.drawing?.strokes.first?.texture, .pen, "the stroke defaults are the server's")
        // A drawing this build cannot read must fail loudly: dropping it would make the next save clear it.
        XCTAssertThrowsError(try JSONDecoder().decode(SlidePostEdits.self, from: Data(#"{"drawing":{"canvas_aspect":1,"scale":2,"strokes":[]}}"#.utf8)))
    }

    func testDrawingDirtiesTheEditorAndSurvivesTheDraftRoundTrip() throws {
        let plain = SlidePostDraft(version: 1, platformProfile: "tiktok_photo", slides: [.init(id: "s", assetID: "a", kind: "image")])
        var drawn = plain
        drawn.slides[0].edits = SlidePostEdits(drawing: SlidePostDrawing(canvasAspect: 0.5625, strokes: strokes(2, points: 4)))
        XCTAssertFalse(plain.hasSameContent(as: drawn))
        let back = try JSONDecoder().decode(SlidePostDraft.self, from: JSONEncoder().encode(drawn))
        XCTAssertEqual(back, drawn)
        XCTAssertEqual(drawn.drawingPointCount, 8)
    }

    func testTheCapabilityDecodesAndDefaultsOff() throws {
        let on = try JSONDecoder().decode(CreationCapabilities.self, from: Data(#"{"formats":[],"slide_post_drawing":true}"#.utf8))
        let old = try JSONDecoder().decode(CreationCapabilities.self, from: Data(#"{"formats":[]}"#.utf8))
        XCTAssertTrue(on.slidePostDrawingEnabled)
        XCTAssertFalse(old.slidePostDrawingEnabled)
    }

    // MARK: Session

    private func stroke(points count: Int, seed: Int = 1, color: String = "#FF3B30") -> DrawingStroke {
        var points: [[Double]] = []
        for index in 0..<count {
            let x = Double(index % 100) / 100
            let y = Double((index * 7) % 100) / 100
            points.append([x, y])
        }
        return DrawingStroke(color: color, opacity: 1, widthFrac: 0.02, texture: .pen, seed: seed, points: points)
    }
    private func strokes(_ count: Int, points: Int) -> [DrawingStroke] { (0..<count).map { stroke(points: points, seed: $0) } }

    private func session(slides: Int = 3, profile: String = "tiktok_photo") -> SlidePostSession {
        let session = SlidePostSession(defaults: defaults)
        session.draft = SlidePostDraft(version: 1, platformProfile: profile,
                                       slides: (1...slides).map { SlidePostSlide(id: "s\($0)", assetID: "a\($0)", kind: "image") })
        return session
    }
    private let portrait = CGSize(width: 1080, height: 1920)

    func testDoneIsOneUndoStepAndRedoBringsTheInkBack() {
        let session = session()
        XCTAssertTrue(session.addDrawingStrokes(slideID: "s1", strokes: strokes(2, points: 5), canvas: portrait))
        XCTAssertEqual(session.draft?.slides[0].edits?.drawing?.strokes.count, 2)
        XCTAssertEqual(session.draft?.slides[0].edits?.drawing?.canvasAspect, 0.5625)
        XCTAssertTrue(session.hasUnsavedChanges, "Done is an unsaved edit")
        session.undoEdit()
        XCTAssertNil(session.draft?.slides[0].edits?.drawing, "one Done, one undo")
        session.redoEdit()
        XCTAssertEqual(session.draft?.slides[0].edits?.drawing?.strokes.count, 2)
        session.clearDrawing(slideID: "s1")
        XCTAssertNil(session.draft?.slides[0].edits?.drawing)
        session.undoEdit()
        XCTAssertEqual(session.draft?.slides[0].edits?.drawing?.strokes.count, 2, "clearing is undoable too")
    }

    func testDrawMoreAppendsToTheExistingStrokesAndOtherSlidesAreUntouched() {
        let session = session()
        session.addDrawingStrokes(slideID: "s1", strokes: [stroke(points: 3, seed: 1)], canvas: portrait)
        session.addDrawingStrokes(slideID: "s1", strokes: [stroke(points: 4, seed: 2)], canvas: portrait)
        XCTAssertEqual(session.draft?.slides[0].edits?.drawing?.strokes.map(\.seed), [1, 2])
        XCTAssertNil(session.draft?.slides[1].edits?.drawing)
        session.undoEdit()
        XCTAssertEqual(session.draft?.slides[0].edits?.drawing?.strokes.map(\.seed), [1])
    }

    /// The rule for "draw more" after the canvas changed shape: new strokes are authored in the EXISTING drawing's aspect
    /// (Draw mode lays the sketch over that region), so they are appended unchanged and never re-projected.
    func testDrawMoreAfterTheCanvasChangedShapeKeepsTheDrawingsOwnAspect() {
        let session = session()
        session.addDrawingStrokes(slideID: "s1", strokes: [stroke(points: 3, seed: 1)], canvas: portrait)
        let square = CGSize(width: 1080, height: 1080)
        XCTAssertEqual(SlidePostDrawing.authoringAspect(existing: session.draft?.slides[0].edits?.drawing, canvas: square), 0.5625)
        session.addDrawingStrokes(slideID: "s1", strokes: [stroke(points: 4, seed: 2)], canvas: square)
        let drawing = session.draft?.slides[0].edits?.drawing
        XCTAssertEqual(drawing?.canvasAspect, 0.5625, "the aspect never changes under existing ink")
        XCTAssertEqual(drawing?.strokes.last, stroke(points: 4, seed: 2), "points are appended exactly as drawn")
        // A slide with no ink yet takes the canvas it is on.
        session.addDrawingStrokes(slideID: "s2", strokes: [stroke(points: 3)], canvas: square)
        XCTAssertEqual(session.draft?.slides[1].edits?.drawing?.canvasAspect, 1)
        // And the region Draw mode covers is the aspect-fit of the drawing inside the preview.
        let region = SlidePostDrawing.fitRect(aspect: 0.5625, in: CGSize(width: 400, height: 400))
        XCTAssertEqual(region.width, 225, accuracy: 0.01); XCTAssertEqual(region.minX, 87.5, accuracy: 0.01); XCTAssertEqual(region.height, 400)
    }

    func testCapsBlockNewStrokesWithAnInlineReason() {
        let session = session()
        session.setDrawing(slideID: "s1", strokes: strokes(60, points: 2), canvasAspect: 0.5625)
        XCTAssertNotNil(session.drawingCapMessage(slideID: "s1", extraPoints: 2, extraStrokes: 1), "61st stroke")
        XCTAssertNil(session.drawingCapMessage(slideID: "s2", extraPoints: 2, extraStrokes: 1), "another slide is fine")
        XCTAssertFalse(session.addDrawingStrokes(slideID: "s1", strokes: [stroke(points: 2)], canvas: portrait))
        XCTAssertEqual(session.draft?.slides[0].edits?.drawing?.strokes.count, 60)

        // Points: 3,000 per slide, 8,000 per post.
        session.setDrawing(slideID: "s1", strokes: strokes(2, points: 1500), canvasAspect: 0.5625)
        XCTAssertNotNil(session.drawingCapMessage(slideID: "s1", extraPoints: 2, extraStrokes: 1))
        XCTAssertTrue(session.setDrawing(slideID: "s2", strokes: strokes(2, points: 1500), canvasAspect: 0.5625))
        XCTAssertTrue(session.setDrawing(slideID: "s3", strokes: strokes(1, points: 1500) + strokes(1, points: 500), canvasAspect: 0.5625))
        XCTAssertEqual(session.draft?.drawingPointCount, 8000)
        XCTAssertNotNil(session.drawingCapMessage(slideID: "s3", extraPoints: 2, extraStrokes: 1), "the post is full")
        XCTAssertFalse(session.setDrawing(slideID: "s3", strokes: strokes(1, points: 1500) + strokes(1, points: 501), canvasAspect: 0.5625))
        XCTAssertNil(session.draft?.validationMessage)
    }

    func testTheDraftIsRestoredWithItsInkAfterReopening() async throws {
        let refs = [SlidePostSlide(id: "s1", assetID: "a1", kind: "image")]
        let saved = SlidePostState(itemID: itemID, title: "Post", draft: SlidePostDraft(version: 1, platformProfile: "tiktok_photo", slides: refs),
                                   assets: [.init(id: "a1", kind: "image", status: "ready")])
        NativeEditorURLProtocol.handler = { _ in (200, try JSONEncoder().encode(saved)) }
        let first = SlidePostSession(defaults: defaults)
        await first.refresh(api: NativeEditorTestSupport.api(), itemID: itemID)
        XCTAssertTrue(first.addDrawingStrokes(slideID: "s1", strokes: strokes(2, points: 6), canvas: portrait))
        XCTAssertTrue(first.hasUnsavedChanges)
        let reopened = SlidePostSession(defaults: defaults)
        await reopened.refresh(api: NativeEditorTestSupport.api(), itemID: itemID)
        XCTAssertEqual(reopened.draft?.slides[0].edits?.drawing, first.draft?.slides[0].edits?.drawing)
        XCTAssertTrue(reopened.hasUnsavedChanges, "unsaved ink is still unsaved after a reopen")
    }

    // MARK: Paint layer

    func testThePaintLayerIsCentredOnItsOwnBoundingBox() {
        let layer = SlidePostDrawing.paintLayer(strokes: [DrawingStroke(color: "#FF0000", widthFrac: 0.02, points: [[0.1, 0.2], [0.5, 0.6]])], canvasAspect: 0.5625)
        XCTAssertEqual(layer.centerX, 0.3, accuracy: 1e-9); XCTAssertEqual(layer.centerY, 0.4, accuracy: 1e-9)
        XCTAssertEqual(layer.scale, 1); XCTAssertEqual(layer.rotationDeg, 0); XCTAssertEqual(layer.entrance, .none)
        XCTAssertEqual(layer.start, 0); XCTAssertEqual(layer.end, 1)
    }

    // MARK: Renderers (geometry fixture)

    private func gray(_ name: String) throws -> URL {
        let format = UIGraphicsImageRendererFormat(); format.scale = 1; format.opaque = true
        let image = UIGraphicsImageRenderer(size: CGSize(width: 600, height: 600), format: format).image { _ in
            UIColor(white: 0.5, alpha: 1).setFill(); UIRectFill(CGRect(x: 0, y: 0, width: 600, height: 600))
        }
        let url = directory.appending(path: name)
        let destination = try XCTUnwrap(CGImageDestinationCreateWithURL(url as CFURL, UTType.jpeg.identifier as CFString, 1, nil))
        CGImageDestinationAddImage(destination, try XCTUnwrap(image.cgImage), [kCGImageDestinationLossyCompressionQuality: 1.0] as CFDictionary)
        XCTAssertTrue(CGImageDestinationFinalize(destination))
        return url
    }

    private struct Bitmap {
        let width: Int, height: Int, bytes: [UInt8]
        init(_ cg: CGImage) throws {
            width = cg.width; height = cg.height
            var buffer = [UInt8](repeating: 0, count: cg.width * cg.height * 4)
            let context = try XCTUnwrap(CGContext(data: &buffer, width: cg.width, height: cg.height, bitsPerComponent: 8, bytesPerRow: cg.width * 4,
                                                  space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue))
            context.draw(cg, in: CGRect(x: 0, y: 0, width: cg.width, height: cg.height))
            bytes = buffer
        }
        /// Bounding box of every inked pixel (top-left coordinates), nil when there is none.
        func inkBounds() -> CGRect? {
            var minX = width, minY = height, maxX = -1, maxY = -1
            for y in 0..<height {
                for x in 0..<width where isInk(Double(x), Double(y)) {
                    minX = min(minX, x); maxX = max(maxX, x); minY = min(minY, y); maxY = max(maxY, y)
                }
            }
            return maxX < 0 ? nil : CGRect(x: minX, y: minY, width: maxX - minX + 1, height: maxY - minY + 1)
        }
        /// Top-left pixel coordinates.
        func isWhite(_ x: Double, _ y: Double) -> Bool {
            let px = min(width - 1, max(0, Int(x.rounded()))), py = min(height - 1, max(0, Int(y.rounded())))
            let i = (py * width + px) * 4
            return bytes[i] > 220 && bytes[i + 1] > 220 && bytes[i + 2] > 220
        }
        func isInk(_ x: Double, _ y: Double) -> Bool {
            let px = min(width - 1, max(0, Int(x.rounded()))), py = min(height - 1, max(0, Int(y.rounded())))
            let i = (py * width + px) * 4
            return Int(bytes[i]) > 170 && Int(bytes[i + 1]) < 90 && Int(bytes[i + 2]) < 90
        }
    }

    private func place(_ g: Fixture.Geometry) -> (profile: String, aspect: String?, framing: SlidePostFraming?) {
        switch (Int(g.canvas[0]), Int(g.canvas[1])) {
        case (1080, 1920): ("tiktok_photo", nil, nil)
        case (1080, 1080): ("tiktok_photo", "1:1", nil)
        case (1920, 1080): ("tiktok_photo", nil, SlidePostFraming(orientation: .landscape))
        default: ("tiktok_photo", nil, nil)
        }
    }
    /// What the painted ink must satisfy against a `geometry` case. The pen smooths its corners, so interior vertices sit
    /// well off the painted curve (a renderer detail the server's skia port only approximates); what the fixture pins
    /// is WHERE the authoring canvas lands. The stroke runs corner to corner of that canvas, whose ends the painter does
    /// not smooth, so the ink's bounding box must be the fit rectangle (give or take half the stroke width), and the
    /// pixels at the stroke's first point must be inked.
    private func assertInk(_ ink: Bitmap, _ g: Fixture.Geometry, _ label: String, file: StaticString = #filePath, line: UInt = #line) {
        let canvas = CGSize(width: g.canvas[0], height: g.canvas[1])
        let half = g.expected_width_px / 2 + 4
        guard let box = ink.inkBounds() else { return XCTFail("\(label): no ink painted", file: file, line: line) }
        let origin = g.expected_fit_origin, size = g.expected_fit_size
        XCTAssertEqual(box.minX, max(0, origin[0]), accuracy: half, "\(label) left", file: file, line: line)
        XCTAssertEqual(box.minY, max(0, origin[1]), accuracy: half, "\(label) top", file: file, line: line)
        XCTAssertEqual(box.maxX, min(canvas.width, origin[0] + size[0]), accuracy: half, "\(label) right", file: file, line: line)
        XCTAssertEqual(box.maxY, min(canvas.height, origin[1] + size[1]), accuracy: half, "\(label) bottom", file: file, line: line)
        let first = g.expected_points_px[0]
        XCTAssertTrue(ink.isInk(min(canvas.width - 3, max(3, first[0])), min(canvas.height - 3, max(3, first[1]))), "\(label) first point", file: file, line: line)
    }
    private func inkedStroke(_ g: Fixture.Geometry) -> DrawingStroke {
        DrawingStroke(color: "#FF0000", opacity: 1, widthFrac: g.width_frac, texture: .pen, seed: 7, points: g.points)
    }

    func testPhotoRenderPutsInkWhereTheGeometryFixtureSaysAndLeavesTheMarginsClean() async throws {
        let file = try fixture()
        let source = try gray("gray.jpg")
        let asset = SlidePostAsset(id: "asset-s", kind: "image", status: "ready", sourceURL: source)
        for g in file.geometry {
            let (profile, aspect, framing) = place(g)
            let canvas = CGSize(width: g.canvas[0], height: g.canvas[1])
            let drawing = SlidePostDrawing(canvasAspect: g.canvas_aspect, strokes: [inkedStroke(g)])
            let drawn = SlidePostSlide(id: "s", assetID: "asset-s", kind: "image", edits: SlidePostEdits(framing: framing, drawing: drawing))
            let bare = SlidePostSlide(id: "s", assetID: "asset-s", kind: "image", edits: SlidePostEdits(framing: framing))
            XCTAssertTrue(SlidePostOnDeviceRender.supports(SlidePostDraft(version: 1, platformProfile: profile, slides: [drawn]), assets: [asset]),
                          "\(g.name): a drawing alone never forces a server render")

            let drawnData = try await SlidePostOnDeviceRender.renderSlide(slide: drawn, asset: asset, profile: profile, aspect: aspect, scale: 1)
            let bareData = try await SlidePostOnDeviceRender.renderSlide(slide: bare, asset: asset, profile: profile, aspect: aspect, scale: 1)
            let out = try Bitmap(try XCTUnwrap(XCTUnwrap(UIImage(data: drawnData)).cgImage))
            let untouched = try Bitmap(try XCTUnwrap(XCTUnwrap(UIImage(data: bareData)).cgImage))
            XCTAssertEqual(CGSize(width: out.width, height: out.height), canvas, g.name)

            assertInk(out, g, g.name)
            XCTAssertNil(untouched.inkBounds(), "\(g.name): an undrawn slide has no ink")

            // The letterbox: nothing outside the fitted authoring canvas.
            let origin = g.expected_fit_origin, size = g.expected_fit_size
            let margins: [(Double, Double)] = origin[0] > 1
                ? [(origin[0] / 2, canvas.height / 2), ((origin[0] + size[0] + canvas.width) / 2, canvas.height / 2)]
                : (origin[1] > 1 ? [(canvas.width / 2, origin[1] / 2), (canvas.width / 2, (origin[1] + size[1] + canvas.height) / 2)] : [])
            for (x, y) in margins { XCTAssertFalse(out.isInk(x, y), "\(g.name): the margin at (\(x), \(y)) stays clean") }
            XCTAssertEqual(margins.isEmpty, origin[0] <= 1 && origin[1] <= 1, g.name)
        }
    }

    /// Every export stacks photo, text, ink, watermark, and the editor preview matches: ink drawn across a caption
    /// covers it. A stroke thick enough to cover the glyph heights at the canvas middle must leave no caption pixel
    /// along its centre line (the same slide without ink proves the caption really is there).
    func testInkIsPaintedOverTextInThePhotoRender() async throws {
        let asset = SlidePostAsset(id: "asset-s", kind: "image", status: "ready", sourceURL: try gray("stack.jpg"))
        var caption = SlidePostTextElement(id: "t", text: "KRIA")
        caption.position = "center"; caption.sizePx = 200; caption.color = "#FFFFFF"; caption.shadowEnabled = false
        var edits = SlidePostEdits()
        edits.setTexts([caption])
        var inked = edits
        inked.drawing = SlidePostDrawing(canvasAspect: 0.5625, strokes: [
            DrawingStroke(color: "#FF0000", opacity: 1, widthFrac: 0.08, texture: .pen, seed: 1, points: [[0.0, 0.5], [1.0, 0.5]]),
        ])
        let plain = SlidePostSlide(id: "s", assetID: "asset-s", kind: "image", edits: edits)
        let drawn = SlidePostSlide(id: "s", assetID: "asset-s", kind: "image", edits: inked)

        let plainData = try await SlidePostOnDeviceRender.renderSlide(slide: plain, asset: asset, profile: "tiktok_photo", aspect: nil, scale: 1)
        let drawnData = try await SlidePostOnDeviceRender.renderSlide(slide: drawn, asset: asset, profile: "tiktok_photo", aspect: nil, scale: 1)
        let textOnly = try Bitmap(try XCTUnwrap(XCTUnwrap(UIImage(data: plainData)).cgImage))
        let both = try Bitmap(try XCTUnwrap(XCTUnwrap(UIImage(data: drawnData)).cgImage))
        let y = Double(textOnly.height) / 2
        let xs = stride(from: Double(textOnly.width) * 0.3, through: Double(textOnly.width) * 0.7, by: 4).map { $0 }
        XCTAssertTrue(xs.contains { textOnly.isWhite($0, y) }, "precondition: the caption has pixels on the stroke's centre line")
        for x in xs { XCTAssertTrue(both.isInk(x, y), "ink covers the caption at x=\(x)") }
    }

    /// The PR 1 bug: the painter moves the strokes' bounding-box centre onto the layer centre, so a stroke away from the
    /// middle of the canvas lands in the middle unless the layer is centred on its own box. The geometry fixture's
    /// corner-to-corner strokes cannot see that (their box is already centred), so this one sits in the top-left.
    func testInkAwayFromTheMiddleStaysWhereItWasDrawn() throws {
        let canvas = CGSize(width: 1080, height: 1920)
        let drawing = SlidePostDrawing(canvasAspect: 0.5625, strokes: [
            DrawingStroke(color: "#FF0000", opacity: 1, widthFrac: 0.02, texture: .pen, seed: 3, points: [[0.1, 0.1], [0.2, 0.15], [0.3, 0.2]]),
        ])
        let format = UIGraphicsImageRendererFormat(); format.scale = 1; format.opaque = true
        let image = UIGraphicsImageRenderer(size: canvas, format: format).image { context in
            UIColor(white: 0.5, alpha: 1).setFill(); UIRectFill(CGRect(origin: .zero, size: canvas))
            drawing.paint(in: context.cgContext, canvas: canvas)
        }
        let box = try XCTUnwrap(Bitmap(try XCTUnwrap(image.cgImage)).inkBounds())
        XCTAssertEqual(box.minX, 108, accuracy: 16); XCTAssertEqual(box.maxX, 324, accuracy: 16)
        XCTAssertEqual(box.minY, 192, accuracy: 16); XCTAssertEqual(box.maxY, 384, accuracy: 16)
    }

    func testVideoSlideOverlayRasterizesTheSameInk() throws {
        let g = try XCTUnwrap(fixture().geometry.first)
        let pixels = CGSize(width: g.canvas[0], height: g.canvas[1])
        let drawing = SlidePostDrawing(canvasAspect: g.canvas_aspect, strokes: [inkedStroke(g)])
        let overlay = try SlidePostVideoRender.staticOverlay(texts: [], drawing: drawing, pixels: pixels)
        let none = try SlidePostVideoRender.staticOverlay(texts: [], drawing: nil, pixels: pixels)
        let context = CIContext()
        func bitmap(_ image: CIImage) throws -> Bitmap {
            try Bitmap(try XCTUnwrap(context.createCGImage(image, from: CGRect(origin: .zero, size: pixels), format: .RGBA8, colorSpace: CGColorSpace(name: CGColorSpace.sRGB))))
        }
        let drawn = try bitmap(overlay), plain = try bitmap(none)
        assertInk(drawn, g, "video overlay")
        XCTAssertNil(plain.inkBounds(), "no drawing, no ink")
    }
}
