import XCTest

/// KRI-1501 two-language captions on the phone editor's live preview: the spoken caption plus a smaller translated
/// line, which is its own editor track (docs/pipelines/dual-captions.md).
///
/// Ways this breaks, written before the code; each is checked below:
///  1. a pair's translated line is not drawn (wrong language table, gate read wrong, font missing a letter);
///  2. the translated line wraps, is cut at the frame edge, or overlaps the spoken line (the long German case);
///  3. the track (lane, Captions row, preview object) shows on a build the server did not offer it to;
///  4. a tap or drag on the translated line selects or moves the spoken caption (or the other way round);
///  5. "Apply to all" after a translated-line drag moves the spoken captions instead of the translated lines;
///  6. the switch hides the line in the panel but the preview still draws it (or Save would lose the texts);
///  7. picking another language leaves the old language's lines on screen or shows no progress / result.
/// The 12 pair shots (`pair-<spoken>-<translated>.png` in `$KRIA_SHOT_DIR`) are the review evidence for 1 and 2.
@MainActor
final class NativeDualCaptionUITests: XCTestCase {
    private static let languages = ["tr", "de", "en", "pt"]

    private func launch(_ pair: String = "tr-en", extra: [String] = []) -> XCUIApplication {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor", "-ui-testing-editor-dual-captions", "-ui-testing-editor-dual-pair-" + pair,
                               "-ui-testing-editor-live-preview", "-ui-testing-editor-caption-style-scope"] + extra
        // English screen text: the checks read the preview objects' "position x%, y%" values.
        app.launchArguments += ["-AppleLanguages", "(en)", "-AppleLocale", "en_US"]
        app.launchEnvironment["UI_TEST_REDUCE_MOTION"] = "1"
        app.launch()
        XCTAssertTrue(app.buttons["native-editor-tool-captions"].waitForExistence(timeout: 20))
        return app
    }

    /// Simulator evidence for review: kept on the result bundle, and written to `KRIA_SHOT_DIR` when set.
    private func shot(_ app: XCUIApplication, _ name: String, element: XCUIElement? = nil) {
        let screenshot = element?.screenshot() ?? app.screenshot()
        let attachment = XCTAttachment(screenshot: screenshot)
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
        if let directory = ProcessInfo.processInfo.environment["KRIA_SHOT_DIR"] {
            try? FileManager.default.createDirectory(atPath: directory, withIntermediateDirectories: true)
            try? screenshot.pngRepresentation.write(to: URL(fileURLWithPath: directory).appendingPathComponent(name + ".png"))
        }
    }

    private func preview(_ app: XCUIApplication) -> XCUIElement { app.descendants(matching: .any)["native-editor-preview"].firstMatch }
    private func translated(_ app: XCUIApplication, _ cue: Int = 0) -> XCUIElement {
        app.descendants(matching: .any)["native-editor-preview-caption_translation-native-caption-\(cue)"].firstMatch
    }
    private func spoken(_ app: XCUIApplication, _ cue: Int = 0) -> XCUIElement {
        app.descendants(matching: .any)["native-editor-preview-caption_cue-native-caption-\(cue)"].firstMatch
    }

    /// "…, position 50%, 86%" from a preview object's value (its accessibility frame is the canvas-sized hit shape).
    private func position(_ element: XCUIElement) -> CGPoint? {
        guard let value = element.value as? String, let range = value.range(of: "position ") else { return nil }
        let numbers = value[range.upperBound...].split(separator: ",").prefix(2)
            .map { Double($0.trimmingCharacters(in: CharacterSet(charactersIn: " %"))) }
        guard numbers.count == 2, let x = numbers[0], let y = numbers[1] else { return nil }
        return CGPoint(x: x, y: y)
    }

    private func wait(_ label: String, timeout: TimeInterval = 10, _ condition: @escaping () -> Bool) {
        let predicate = NSPredicate { _, _ in condition() }
        XCTAssertEqual(XCTWaiter.wait(for: [XCTNSPredicateExpectation(predicate: predicate, object: nil)], timeout: timeout), .completed, label)
    }

    /// Selects the first translated line from its own timeline lane: the playhead parks inside the cue and the
    /// Translation panel opens. Done closes it again so the preview is full size.
    private func parkOnFirstLine(_ app: XCUIApplication) {
        let bar = app.descendants(matching: .any)["native-editor-timeline-caption_translation-native-caption-0"].firstMatch
        XCTAssertTrue(bar.waitForExistence(timeout: 15), "the Translation lane lists the translated lines")
        bar.tap()
        XCTAssertTrue(app.buttons["native-editor-translation-done"].waitForExistence(timeout: 5), "a translated line opens its own panel")
        app.buttons["native-editor-translation-done"].tap()
        XCTAssertTrue(translated(app).waitForExistence(timeout: 15))
        XCTAssertTrue(spoken(app).waitForExistence(timeout: 5))
    }

    func testTurkishCaptionTranslationPairs() { checkTranslationPairs(spokenLanguage: "tr") }
    func testGermanCaptionTranslationPairs() { checkTranslationPairs(spokenLanguage: "de") }
    func testEnglishCaptionTranslationPairs() { checkTranslationPairs(spokenLanguage: "en") }
    func testPortugueseCaptionTranslationPairs() { checkTranslationPairs(spokenLanguage: "pt") }

    private func checkTranslationPairs(spokenLanguage: String) {
        for translatedLanguage in Self.languages where translatedLanguage != spokenLanguage {
            let pair = "\(spokenLanguage)-\(translatedLanguage)"
            let app = launch(pair)
            parkOnFirstLine(app)
            wait("\(pair): the translated line is drawn on the preview") { self.position(self.translated(app)) != nil }
            guard let small = position(translated(app)), let big = position(spoken(app)) else {
                XCTFail("\(pair): positions unreadable"); app.terminate(); continue
            }
            XCTAssertGreaterThan(small.y, big.y, "\(pair): attached under the spoken caption")
            XCTAssertEqual(small.x, 50, accuracy: 3, "\(pair): centred like the spoken caption")
            XCTAssertLessThan(small.y, 90, "\(pair): inside the frame budget")
            // The shot is the fullscreen preview (a tap on empty sky opens it): the drawn frame alone, without the
            // editor's selection outline and handle over the selected line.
            preview(app).coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.08)).tap()
            let fullscreen = app.descendants(matching: .any)["native-editor-preview-fullscreen"].firstMatch
            XCTAssertTrue(fullscreen.waitForExistence(timeout: 5), "\(pair): fullscreen preview")
            Thread.sleep(forTimeInterval: 1.0)
            shot(app, "pair-\(pair)", element: fullscreen.exists ? fullscreen : preview(app))
            app.terminate()
        }
    }

    /// 3: no capability, no track: no lane, no Captions row, nothing drawn, even with saved translated lines.
    func testTrackShowsOnlyWithTheCapability() {
        let closed = launch(extra: ["-ui-testing-editor-dual-captions-no-capability"])
        XCTAssertFalse(closed.descendants(matching: .any)["native-editor-timeline-caption_translation-native-caption-0"].waitForExistence(timeout: 3))
        XCTAssertFalse(closed.descendants(matching: .any)["native-editor-lane-label-TRANSLATION-0"].exists)
        closed.buttons["native-editor-tool-captions"].tap()
        XCTAssertTrue(closed.descendants(matching: .any)["native-editor-caption-visible"].waitForExistence(timeout: 5))
        XCTAssertFalse(closed.buttons["native-editor-caption-translation"].exists, "no Translated line row without the capability")
        XCTAssertFalse(translated(closed).exists)
        shot(closed, "capability-off")
        closed.terminate()

        let open = launch()
        XCTAssertTrue(open.descendants(matching: .any)["native-editor-timeline-caption_translation-native-caption-0"].waitForExistence(timeout: 15))
        open.buttons["native-editor-tool-captions"].tap()
        let row = open.buttons["native-editor-caption-translation"]
        XCTAssertTrue(row.waitForExistence(timeout: 5))
        row.tap()
        XCTAssertTrue(open.buttons["native-editor-translation-done"].waitForExistence(timeout: 5), "Captions opens the Translation panel")
        shot(open, "capability-on-panel")
    }

    /// 4, 5: a tap selects only the line under the finger; a drag of the translated line moves only it, and its
    /// "Apply to all" carries to the translated lines, never the spoken captions.
    func testTranslatedLineMovesApartFromTheSpokenCaption() {
        let app = launch(extra: ["-ui-testing-editor-document-snapshot"])
        parkOnFirstLine(app)
        let snapshot = app.descendants(matching: .any)["native-editor-test-document"].firstMatch
        func document() -> [String: Any] {
            guard let text = snapshot.value as? String, let data = text.data(using: .utf8),
                  let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return [:] }
            return object
        }
        func captionMeta() -> [String: Any] {
            ((document()["editor_payload"] as? [String: Any])?["sections"] as? [String: Any])?["caption_meta"] as? [String: Any] ?? [:]
        }
        func translationOverride() -> [String: Any]? {
            ((captionMeta()["translation"] as? [String: Any])?["line_overrides"] as? [String: Any])?["native-caption-0"] as? [String: Any]
        }
        func spokenOverrides() -> Any? { (captionMeta()["appearance"] as? [String: Any])?["line_overrides"] }
        /// The spoken line's centre as a share of the canvas, whatever size the preview has with a panel open.
        func spokenCentre() -> CGPoint {
            let canvas = preview(app).frame, box = spoken(app).frame
            return CGPoint(x: (box.midX - canvas.minX) / canvas.width, y: (box.midY - canvas.minY) / canvas.height)
        }

        // A preview object's frame is the line's drawn box; each tap selects only the line under the finger.
        spoken(app).tap()
        XCTAssertTrue(app.buttons["native-editor-captions-done"].waitForExistence(timeout: 5), "a tap on the spoken line opens Captions")
        XCTAssertTrue(spoken(app).isSelected); XCTAssertFalse(translated(app).isSelected)
        app.buttons["native-editor-captions-done"].tap()
        translated(app).tap()
        XCTAssertTrue(app.buttons["native-editor-translation-done"].waitForExistence(timeout: 5), "a tap on the translated line opens its own panel")
        XCTAssertTrue(translated(app).isSelected); XCTAssertFalse(spoken(app).isSelected)
        app.buttons["native-editor-translation-done"].tap()
        XCTAssertTrue(translated(app).waitForExistence(timeout: 5))

        let before = spokenCentre()
        let stillReady = NSPredicate { _, _ in (self.preview(app).value as? String ?? "").contains("captionStillReady:true") }
        _ = XCTWaiter.wait(for: [XCTNSPredicateExpectation(predicate: stillReady, object: nil)], timeout: 10)
        let start = translated(app).coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.5))
        start.press(forDuration: 0.1, thenDragTo: start.withOffset(CGVector(dx: 0, dy: -120)), withVelocity: 200, thenHoldForDuration: 0.1)
        wait("the drag writes a translated-line position") { (translationOverride()?["position_y_frac"] as? Double) != nil }
        let movedY = translationOverride()?["position_y_frac"] as? Double ?? 1
        XCTAssertLessThan(movedY, 0.8, "the translated line moved up")
        XCTAssertNil(spokenOverrides(), "no spoken caption override was written")
        let applyAll = app.buttons["Apply to all translated lines"]
        XCTAssertTrue(applyAll.waitForExistence(timeout: 5), "the offer names the translated lines")
        shot(app, "drag-translated-line", element: preview(app))
        applyAll.tap()
        wait("Apply to all moves the translated track") {
            ((captionMeta()["translation"] as? [String: Any])?["position_y_frac"] as? Double).map { abs($0 - movedY) < 0.001 } ?? false
        }
        XCTAssertNil(spokenOverrides(), "Apply to all never moves the spoken captions")
        XCTAssertNil((captionMeta()["appearance"] as? [String: Any])?["position_y_frac"])
        if app.buttons["native-editor-translation-done"].exists { app.buttons["native-editor-translation-done"].tap() }
        wait("the spoken line is drawn where it was") {
            let now = spokenCentre()
            return abs(now.x - before.x) < 0.02 && abs(now.y - before.y) < 0.02
        }
    }

    /// 6, 7: the switch removes the line from the preview; a new language replaces every line.
    func testSwitchOffRemovesTheLineAndANewLanguageReplacesIt() {
        let app = launch()
        parkOnFirstLine(app)
        app.buttons["native-editor-tool-captions"].tap()
        app.buttons["native-editor-caption-translation"].tap()
        let visible = app.switches["native-editor-translation-visible"]
        XCTAssertTrue(visible.waitForExistence(timeout: 5))
        visible.tap()
        wait("the translated line leaves the preview") { !self.translated(app).exists }
        XCTAssertTrue(spoken(app).exists, "the spoken caption stays")
        shot(app, "switch-off", element: preview(app))
        visible.tap()
        XCTAssertTrue(translated(app).waitForExistence(timeout: 10))

        app.buttons["native-editor-translation-language-pt"].tap()
        let line = app.textFields["native-editor-translation-line-native-caption-0"].exists
            ? app.textFields["native-editor-translation-line-native-caption-0"] : app.textViews["native-editor-translation-line-native-caption-0"]
        wait("Portuguese replaces English") { (line.value as? String) == "Eles combinam isso em três estratégias." }
        XCTAssertTrue(app.buttons["native-editor-translation-language-pt"].isSelected)
        wait("the preview shows the Portuguese line") { (self.translated(app).label).contains("estratégias") }
        shot(app, "language-pt")
    }
}
