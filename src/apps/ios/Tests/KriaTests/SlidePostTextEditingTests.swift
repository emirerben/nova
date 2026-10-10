import XCTest
@testable import Kria

/// The slide Text tab reuses the real native Text panel through `NativeTextEditing`. These pin the
/// slide adapter's mapping (typed fields + `extra`), its clamping, undo grouping, Apply-to-all, and that
/// the video session satisfies the same protocol unchanged.
@MainActor final class SlidePostTextEditingTests: XCTestCase {
    private let slideID = "slide-1"
    private var defaults: UserDefaults!
    private var suite: String!
    override func setUp() {
        super.setUp(); suite = "SlidePostTextEditingTests.\(UUID())"; defaults = UserDefaults(suiteName: suite)
    }
    override func tearDown() {
        defaults.removePersistentDomain(forName: suite); defaults = nil
        super.tearDown()
    }

    private func makeSession(texts: [[SlidePostTextElement]] = [[SlidePostTextElement(id: "t1", text: "Athens")], [SlidePostTextElement(id: "u1", text: "Rome")]]) -> SlidePostSession {
        let slides = texts.enumerated().map { index, texts -> SlidePostSlide in
            var slide = SlidePostSlide(id: "slide-\(index + 1)", assetID: "asset-\(index + 1)", kind: "image")
            var edits = SlidePostEdits(); edits.setTexts(texts); slide.edits = edits
            return slide
        }
        let session = SlidePostSession(defaults: defaults)
        session.draft = SlidePostDraft(version: 1, platformProfile: "instagram_carousel", slides: slides)
        return session
    }
    private func texts(_ session: SlidePostSession, slide: String = "slide-1") -> [SlidePostTextElement] {
        session.draft?.slides.first { $0.id == slide }?.edits?.effectiveTexts ?? []
    }

    // MARK: Mapping

    func testEveryStyleKeyRoundTripsThroughTheJSONIncludingExtraKeysAtTopLevel() throws {
        var element = SlidePostTextElement(id: "t1", text: "Hi")
        element.setEditorValue(.number(30), forKey: "rotation_deg")
        element.setEditorValue(.string("#112233"), forKey: "stroke_color")
        element.setEditorValue(.string("#445566"), forKey: "shadow_color")
        element.setEditorValue(.number(0.4), forKey: "shadow_opacity")
        element.setEditorValue(.string("#FFF0A6"), forKey: "background_color")
        element.setEditorValue(.string("Highlight"), forKey: "editor_preset")
        element.setEditorValue(.string("upper"), forKey: "text_case")
        element.setEditorValue(.number(0.2), forKey: "letter_spacing")
        element.setEditorValue(.number(1.5), forKey: "line_spacing")
        element.extra["future_server_key"] = .string("kept")
        let data = try JSONEncoder().encode(element)
        let top = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
        XCTAssertEqual(top["rotation_deg"] as? Double, 30)
        XCTAssertEqual(top["stroke_color"] as? String, "#112233")
        XCTAssertEqual(top["shadow_opacity"] as? Double, 0.4)
        XCTAssertEqual(top["background_color"] as? String, "#FFF0A6")
        XCTAssertEqual(top["editor_preset"] as? String, "Highlight")
        XCTAssertEqual(top["text_case"] as? String, "upper")
        XCTAssertEqual(top["future_server_key"] as? String, "kept")
        XCTAssertNil(top["extra"], "extra keys are flattened, never nested")
        let decoded = try JSONDecoder().decode(SlidePostTextElement.self, from: data)
        XCTAssertEqual(decoded, element)
        // And the panel sees every one of them in its own raw vocabulary.
        let raw = decoded.editorElement.raw
        XCTAssertEqual(raw["rotation_deg"]?.numberValue, 30)
        XCTAssertEqual(raw["stroke_color"]?.stringValue, "#112233")
        XCTAssertEqual(raw["shadow_color"]?.stringValue, "#445566")
        XCTAssertEqual(raw["shadow_opacity"]?.numberValue, 0.4)
        XCTAssertEqual(raw["background_color"]?.stringValue, "#FFF0A6")
        XCTAssertEqual(raw["editor_preset"]?.stringValue, "Highlight")
    }

    func testTypedFieldsMapBothWays() {
        var element = SlidePostTextElement(id: "t1", text: "Hi")
        XCTAssertEqual(element.editorElement.raw["font_family"]?.stringValue, "Inter", "the default Inter-Bold shows as the registry's Inter")
        element.setEditorValue(.string("Inter"), forKey: "font_family")
        XCTAssertEqual(element.fontFamily, SlidePostTextElement.defaultFont, "choosing the shown font is not a change")
        element.setEditorValue(.string("Playfair Display"), forKey: "font_family")
        XCTAssertEqual(element.fontFamily, "Playfair Display")
        element.setEditorValue(.string("#a63224"), forKey: "color"); XCTAssertEqual(element.color, "#A63224")
        element.setEditorValue(.string("right"), forKey: "alignment"); XCTAssertEqual(element.alignment, "right")
        element.setEditorValue(.number(0.25), forKey: "x_frac"); element.setEditorValue(.number(0.7), forKey: "y_frac")
        XCTAssertEqual(element.editorElement.raw["x_frac"]?.numberValue, 0.25)
        element.setEditorValue(.null, forKey: "x_frac"); XCTAssertNil(element.xFrac)
        element.setEditorValue(.bool(false), forKey: "shadow_enabled")
        XCTAssertEqual(element.editorElement.raw["shadow_opacity"]?.numberValue, 0, "no shadow shows 0%")
    }

    /// KRI-564: small text looked different in the saved image than in the preview because the preview floored the
    /// font at 8pt while the export scales exactly. The text must scale in proportion to the canvas.
    func testSmallTextScalesWithTheCanvasSoPreviewMatchesTheExport() throws {
        var element = SlidePostTextElement(id: "t1", text: "Small text")
        element.sizePx = 12
        element.extra[SlidePostTextElement.wrapLinesKey] = .bool(false)
        func inkWidth(_ pixels: CGSize) throws -> Int {
            let image = try XCTUnwrap(SlidePostOnDeviceRender.textLayer([element], pixels: pixels)?.cgImage)
            let width = image.width, height = image.height
            var bytes = [UInt8](repeating: 0, count: width * height * 4)
            let context = try XCTUnwrap(CGContext(data: &bytes, width: width, height: height, bitsPerComponent: 8, bytesPerRow: width * 4,
                                                  space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue))
            context.draw(image, in: CGRect(x: 0, y: 0, width: width, height: height))
            var minX = width, maxX = -1
            for y in 0..<height { for x in 0..<width where bytes[(y * width + x) * 4 + 3] > 32 { minX = min(minX, x); maxX = max(maxX, x) } }
            return maxX < minX ? 0 : maxX - minX + 1
        }
        let preview = try inkWidth(CGSize(width: 360, height: 640))
        let export = try inkWidth(CGSize(width: 1080, height: 1920))
        XCTAssertGreaterThan(preview, 0)
        XCTAssertEqual(Double(export) / Double(preview), 3, accuracy: 0.45, "export ink is 3x the preview's, like the canvas")
    }

    func testInvalidValuesAreIgnoredAndRangesClamp() {
        var element = SlidePostTextElement(id: "t1", text: "Hi")
        let before = element
        element.setEditorValue(.string("not-a-color"), forKey: "color")
        element.setEditorValue(.string("diagonal"), forKey: "alignment")
        element.setEditorValue(.string("sideways"), forKey: "position")
        element.setEditorValue(.string("#12"), forKey: "stroke_color")
        element.setEditorValue(.string("Loud"), forKey: "editor_preset")
        element.setEditorValue(.string("shout"), forKey: "text_case")
        element.setEditorValue(.string("x"), forKey: "background")
        element.setEditorValue(.object(["entrance": .string("fade")]), forKey: "animation_phases")
        XCTAssertEqual(element, before, "bad input and video-only keys change nothing")

        // KRI-564: a slide keeps the video editor's `wrap_lines` contract. Absent = legacy auto-wrap.
        XCTAssertTrue(element.wrapsLines)
        element.setEditorValue(.string("nope"), forKey: "wrap_lines"); XCTAssertEqual(element, before, "non-bool is ignored")
        element.setEditorValue(.bool(false), forKey: "wrap_lines"); XCTAssertFalse(element.wrapsLines)
        element.setEditorValue(.null, forKey: "wrap_lines"); XCTAssertTrue(element.wrapsLines)
        XCTAssertEqual(element, before, "clearing the flag restores the legacy element")

        element.setEditorValue(.number(9_999), forKey: "size_px"); XCTAssertEqual(element.sizePx, 200)
        element.setEditorValue(.number(1), forKey: "size_px"); XCTAssertEqual(element.sizePx, 8)
        element.setEditorValue(.number(99), forKey: "stroke_width"); XCTAssertEqual(element.strokeWidth, 20)
        element.setEditorValue(.number(-4), forKey: "stroke_width"); XCTAssertEqual(element.strokeWidth, 0)
        element.setEditorValue(.number(720), forKey: "rotation_deg"); XCTAssertEqual(element.extra["rotation_deg"]?.numberValue, 360)
        element.setEditorValue(.number(-720), forKey: "rotation_deg"); XCTAssertEqual(element.extra["rotation_deg"]?.numberValue, -360)
        element.setEditorValue(.number(0), forKey: "rotation_deg"); XCTAssertNil(element.extra["rotation_deg"], "0 degrees is the default, not a stored key")
        element.setEditorValue(.number(7), forKey: "shadow_opacity"); XCTAssertEqual(element.extra["shadow_opacity"]?.numberValue, 1)
        element.setEditorValue(.number(5), forKey: "x_frac"); XCTAssertEqual(element.xFrac, 1)
        element.setEditorValue(.number(0), forKey: "max_width_frac"); XCTAssertEqual(element.maxWidthFrac, 0.2)
        element.setEditorValue(.number(9), forKey: "letter_spacing"); XCTAssertEqual(element.extra["letter_spacing"]?.numberValue, 0.5)
        element.setEditorValue(.number(0), forKey: "line_spacing"); XCTAssertEqual(element.extra["line_spacing"]?.numberValue, 0.5)
        element.setEditorValue(.number(.infinity), forKey: "x_frac"); XCTAssertNil(element.xFrac)
        XCTAssertFalse(element.isInvalid, "everything the panel can produce passes the save validation")
    }

    func testResizingScalesAStoredWrapWidthLikeTheVideoEditor() {
        var element = SlidePostTextElement(id: "t1", text: "Hi")
        element.maxWidthFrac = 0.5
        element.setSize(172)
        XCTAssertEqual(element.sizePx, 172)
        XCTAssertEqual(try XCTUnwrap(element.maxWidthFrac), 1.0, accuracy: 0.0001)
        element.setSize(86)
        XCTAssertEqual(try XCTUnwrap(element.maxWidthFrac), 0.5, accuracy: 0.0001)
    }

    func testPresetsWriteTheSameRawKeysAsTheVideoEditor() throws {
        for preset in ["Simple", "Bold", "Highlight"] {
            let video = NativeEditorSession(draft: EditorDraft(projectID: UUID(), clips: [], text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0))
            video.addText(content: "Hi")
            let videoID = try XCTUnwrap(video.document.textElements.first?.id)
            video.applyTextPreset(id: videoID, preset: preset)
            let videoRaw = try XCTUnwrap(video.textElement(id: videoID)).raw

            var slide = SlidePostTextElement(id: "t1", text: "Hi")
            slide.applyEditorPreset(preset)
            let slideRaw = slide.editorElement.raw
            for key in ["editor_preset", "font_family", "color"] {
                XCTAssertEqual(slideRaw[key], videoRaw[key], "\(preset) \(key)")
            }
            if preset == "Highlight" { XCTAssertEqual(slideRaw["background_color"], .string("#FFF0A6")) }
            else { XCTAssertNil(slideRaw["background_color"], "\(preset) clears the box colour") }
        }
        var element = SlidePostTextElement(id: "t1", text: "Hi")
        element.applyEditorPreset("Highlight"); element.applyEditorPreset("Bold")
        XCTAssertNil(element.extra["background_color"], "switching away from Highlight removes its box")
        element.applyEditorPreset("Nonsense")
        XCTAssertEqual(element.extra["editor_preset"]?.stringValue, "Bold")
    }

    // MARK: Protocol conformance

    private func exercise<S: NativeTextEditing>(_ editor: S, id: String) {
        editor.setTextSize(id: id, sizePX: 60)
        editor.setTextAlignment(id: id, alignment: "left")
        editor.setTextColor(id: id, color: "#9BCAFF")
        editor.updateTextRaw(id: id, key: "stroke_width", value: .number(5))
        editor.updateTextRaw(id: id, key: "stroke_color", value: .string("#112233"))
        editor.updateTextRaw(id: id, key: "rotation_deg", value: .number(15))
        editor.setTextPosition(id: id, x: 0.3, y: 0.6)
        editor.applyTextPreset(id: id, preset: "Highlight")
    }

    func testNativeSessionConformanceIsUnchangedAndMatchesTheSlideAdapterOnSharedKeys() throws {
        let video = NativeEditorSession(draft: EditorDraft(projectID: UUID(), clips: [], text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0))
        video.addText(content: "Hi")
        let videoID = try XCTUnwrap(video.document.textElements.first?.id)
        exercise(video, id: videoID)
        let videoRaw = try XCTUnwrap(video.textElement(id: videoID)).raw

        let session = makeSession()
        let editor = SlidePostTextEditor(session: session, slideID: slideID)
        exercise(editor, id: "t1")
        let slideRaw = try XCTUnwrap(editor.textElement(id: "t1")).raw
        XCTAssertEqual(try XCTUnwrap(slideRaw["size_px"]?.numberValue), try XCTUnwrap(videoRaw["size_px"]?.numberValue), accuracy: 0.001)
        for key in ["alignment", "color", "stroke_width", "stroke_color", "rotation_deg", "position", "x_frac", "y_frac", "editor_preset", "font_family", "background_color"] {
            XCTAssertEqual(slideRaw[key], videoRaw[key], key)
        }
        XCTAssertEqual(slideRaw["color"], .string("#30352C"), "Highlight's text colour wins, applied last")
    }

    func testAdapterGatesOnBusyAndMissingText() {
        let session = makeSession()
        let editor = SlidePostTextEditor(session: session, slideID: slideID)
        XCTAssertTrue(editor.canEdit(.text))
        XCTAssertEqual(editor.textDeletion(id: "nope"), .blocked("This text no longer exists."))
        XCTAssertFalse(editor.deleteText(id: "nope"))
        editor.updateTextRaw(id: "nope", key: "color", value: .string("#000000"))
        XCTAssertEqual(texts(session).map(\.id), ["t1"])
        XCTAssertNil(editor.textElement(id: "nope"))
    }

    /// The shared Text panel's position steppers read this: a slide text sits
    /// where the slide draws it (its own presets and edge x), not at the video's.
    func testPanelAnchorIsWhereTheSlideDrawsTheText() {
        let session = makeSession()
        let editor = SlidePostTextEditor(session: session, slideID: slideID)
        XCTAssertEqual(editor.textAnchor(id: "t1"), CGPoint(x: 0.5, y: 0.82))
        editor.setTextAlignment(id: "t1", alignment: "left")
        XCTAssertEqual(editor.textAnchor(id: "t1"), CGPoint(x: 0.08, y: 0.82))
        XCTAssertNil(editor.textAnchor(id: "nope"))
    }

    func testMovingOneAxisKeepsTheOtherWhereTheSlideDrawsIt() throws {
        let session = makeSession()
        let editor = SlidePostTextEditor(session: session, slideID: slideID)
        editor.setTextAlignment(id: "t1", alignment: "left")

        editor.moveText(id: "t1", y: 0.5)

        let text = try XCTUnwrap(texts(session).first)
        XCTAssertEqual(text.position, "custom")
        XCTAssertEqual(text.xFrac, 0.08)
        XCTAssertEqual(text.yFrac, 0.5)
    }

    func testTextContentCapsAtOneHundredTwentyUnicodeScalars() {
        let session = makeSession()
        let editor = SlidePostTextEditor(session: session, slideID: slideID)
        editor.updateTextContent(id: "t1", content: String(repeating: "a", count: 300))
        XCTAssertEqual(texts(session)[0].text.unicodeScalars.count, 120)
        editor.updateTextContent(id: "t1", content: String(repeating: "👩‍👩‍👧", count: 60))
        XCTAssertLessThanOrEqual(texts(session)[0].text.unicodeScalars.count, 120)
        XCTAssertFalse(texts(session)[0].isInvalid)
    }

    // MARK: Undo grouping

    func testEachPanelWriteIsOneUndoStepAndATransactionCollapsesAGesture() {
        let session = makeSession()
        let editor = SlidePostTextEditor(session: session, slideID: slideID)
        editor.setTextAlignment(id: "t1", alignment: "left")
        editor.setTextColor(id: "t1", color: "#9BCAFF")
        session.undoEdit()
        XCTAssertEqual(texts(session)[0].alignment, "left", "two separate writes are two undo steps")
        XCTAssertEqual(texts(session)[0].color, "#FFFFFF")
        session.undoEdit()

        editor.beginTransaction()
        for size in [70.0, 80, 90, 100] { editor.setTextSize(id: "t1", sizePX: size) }
        editor.updateTextRaw(id: "t1", key: "rotation_deg", value: .number(10))
        editor.endTransaction()
        XCTAssertEqual(texts(session)[0].sizePx, 100)
        session.undoEdit()
        XCTAssertEqual(texts(session)[0].sizePx, 86, "the whole drag undoes at once")
        XCTAssertNil(texts(session)[0].extra["rotation_deg"])
        XCTAssertFalse(session.canUndoEdit)
        session.redoEdit()
        XCTAssertEqual(texts(session)[0].sizePx, 100)
    }

    func testTwoConsecutiveTransactionsAreTwoUndoSteps() {
        let session = makeSession()
        let editor = SlidePostTextEditor(session: session, slideID: slideID)
        editor.beginTransaction(); editor.setTextSize(id: "t1", sizePX: 70); editor.endTransaction()
        editor.beginTransaction(); editor.setTextSize(id: "t1", sizePX: 90); editor.endTransaction()
        session.undoEdit()
        XCTAssertEqual(texts(session)[0].sizePx, 70)
    }

    func testDeleteIsOneUndoableStep() {
        let session = makeSession()
        let editor = SlidePostTextEditor(session: session, slideID: slideID)
        XCTAssertTrue(editor.deleteText(id: "t1"))
        XCTAssertTrue(texts(session).isEmpty)
        session.undoEdit()
        XCTAssertEqual(texts(session).map(\.id), ["t1"])
    }

    // MARK: Apply to all slides

    func testApplyToAllCopiesTheWholeLookIncludingRotationAndPositionButNeverWords() {
        let session = makeSession()
        let editor = SlidePostTextEditor(session: session, slideID: slideID)
        exercise(editor, id: "t1")
        editor.updateTextRaw(id: "t1", key: "shadow_color", value: .string("#445566"))
        session.applyStyleToAllSlides(slideID: slideID, textID: "t1")
        let target = texts(session, slide: "slide-2")[0]
        XCTAssertEqual(target.text, "Rome", "words are never copied")
        XCTAssertEqual(target.id, "u1")
        XCTAssertEqual(target.extra["rotation_deg"]?.numberValue, 15)
        XCTAssertEqual(target.extra["stroke_color"]?.stringValue, "#112233")
        XCTAssertEqual(target.extra["shadow_color"]?.stringValue, "#445566")
        XCTAssertEqual(target.extra["background_color"]?.stringValue, "#FFF0A6")
        XCTAssertEqual(target.extra["editor_preset"]?.stringValue, "Highlight")
        XCTAssertEqual(target.position, "custom"); XCTAssertEqual(target.xFrac, 0.3); XCTAssertEqual(target.yFrac, 0.6)
        XCTAssertEqual(target.sizePx, 60); XCTAssertEqual(target.strokeWidth, 5)
        session.undoEdit()
        XCTAssertNil(texts(session, slide: "slide-2")[0].extra["rotation_deg"], "one undo reverts every slide")
    }

    func testApplyToAllClearsKeysTheSourceResetToDefault() {
        var other = SlidePostTextElement(id: "u1", text: "Rome")
        other.setEditorValue(.number(40), forKey: "rotation_deg")
        other.setEditorValue(.string("#FFF0A6"), forKey: "background_color")
        other.extra["future_server_key"] = .string("stay")
        let session = makeSession(texts: [[SlidePostTextElement(id: "t1", text: "Athens")], [other]])
        session.applyStyleToAllSlides(slideID: slideID, textID: "t1")
        let target = texts(session, slide: "slide-2")[0]
        XCTAssertNil(target.extra["rotation_deg"]); XCTAssertNil(target.extra["background_color"])
        XCTAssertEqual(target.extra["future_server_key"]?.stringValue, "stay", "non-style extras survive")
    }

    func testApplyToAllOnlyTouchesTheMatchingIndexText() {
        let first = SlidePostTextElement(id: "t1", text: "Athens")
        var second = SlidePostTextElement(id: "t2", text: "Greece"); second.setEditorValue(.number(20), forKey: "rotation_deg")
        let session = makeSession(texts: [[first, second], [SlidePostTextElement(id: "u1", text: "Rome"), SlidePostTextElement(id: "u2", text: "Italy")]])
        let editor = SlidePostTextEditor(session: session, slideID: slideID)
        editor.updateTextRaw(id: "t1", key: "rotation_deg", value: .number(45))
        session.applyStyleToAllSlides(slideID: slideID, textID: "t1")
        let target = texts(session, slide: "slide-2")
        XCTAssertEqual(target[0].extra["rotation_deg"]?.numberValue, 45)
        XCTAssertNil(target[1].extra["rotation_deg"], "siblings keep their own look")
    }

    func testStrokeWidthAboveTwelveIsValidOnTheNewContract() {
        var element = SlidePostTextElement(id: "t1", text: "Hi")
        element.setEditorValue(.number(20), forKey: "stroke_width")
        XCTAssertFalse(element.isInvalid)
        element.strokeWidth = 21
        XCTAssertTrue(element.isInvalid)
    }

    // MARK: Canvas <-> panel agreement (KRI-298 integration)

    func testCanvasTransformAndPanelEditsAgreeOnSizeAndRotationAndSaveTopLevel() throws {
        let session = makeSession()
        let editor = SlidePostTextEditor(session: session, slideID: slideID)
        // A canvas pinch+twist goes through session.updateText(applyTransform) ...
        session.updateText(slideID: slideID, textID: "t1", coalescing: "canvas") { element in
            let base = element.transformBaseline
            var live = NativeTextLiveTransform()
            live.begin(baseline: base, bounds: nil)
            live.resize(current: base, scale: 1.5, rotation: 30, snapRotation: false)
            element.applyTransform(from: base, live: live)
        }
        // ... and the panel reads the very same numbers.
        let canvas = texts(session)[0]
        let raw = try XCTUnwrap(editor.textElement(id: "t1")).raw
        XCTAssertEqual(raw["size_px"]?.numberValue, Double(canvas.sizePx))
        XCTAssertEqual(raw["rotation_deg"]?.numberValue ?? 0, canvas.rotationDeg, accuracy: 1e-9)
        XCTAssertEqual(canvas.rotationDeg, 30, accuracy: 1e-9)
        XCTAssertGreaterThan(canvas.sizePx, 86)
        // A panel edit is what the canvas draws next.
        editor.updateTextRaw(id: "t1", key: "rotation_deg", value: .number(-20))
        editor.setTextSize(id: "t1", sizePX: 100)
        XCTAssertEqual(texts(session)[0].rotationDeg, -20, accuracy: 1e-9)
        XCTAssertEqual(texts(session)[0].sizePx, 100)
        editor.updateTextRaw(id: "t1", key: "stroke_color", value: .string("#112233"))
        editor.updateTextRaw(id: "t1", key: "shadow_opacity", value: .number(0.4))
        // Save payload: every style key sits at the top level of the element, never nested.
        let data = try JSONEncoder().encode(try XCTUnwrap(session.draft))
        let root = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
        let slides = try XCTUnwrap(root["slides"] as? [[String: Any]])
        let edits = try XCTUnwrap(slides[0]["edits"] as? [String: Any])
        let element = try XCTUnwrap((edits["texts"] as? [[String: Any]])?.first)
        XCTAssertEqual(element["rotation_deg"] as? Double, -20)
        XCTAssertEqual(element["size_px"] as? Int, 100)
        XCTAssertEqual(element["stroke_color"] as? String, "#112233")
        XCTAssertEqual(element["shadow_opacity"] as? Double, 0.4)
        XCTAssertNil(element["extra"])
    }
}
