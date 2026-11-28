import XCTest

/// KRI-1633: "Try intro styles" in the editor, from the entry to a Save that carries editable title rows.
///
/// The fixture transport (`IntroStylesUITestFixture`) answers the route with the offer the backend builds
/// (`Tests/Fixtures/KRI1633IntroStyles.json`) and appends every editor-commit body to a file this test reads. Each tile
/// is drawn on this iPhone by the media engine.
///
/// Failure modes pinned here: the entry is missing, or shows without the capability; fewer than three looks plus
/// "No intro style"; a tap does not put that look's rows in the next Save, or drops `source_params` on the way; her
/// colour edit after the pick is not what Save sends; "No intro style" does not restore the server's plain title rows;
/// Turkish shows English labels.
@MainActor
final class IntroStylesUITests: XCTestCase {
    private func element(_ app: XCUIApplication, _ id: String) -> XCUIElement {
        app.descendants(matching: .any)[id].firstMatch
    }

    private func attach(_ app: XCUIApplication, _ name: String) {
        let shot = XCTAttachment(screenshot: app.screenshot())
        shot.name = name
        shot.lifetime = .keepAlways
        add(shot)
    }

    private func waitFor(_ element: XCUIElement, _ format: String, _ args: CVarArg..., timeout: TimeInterval = 15) -> Bool {
        let expectation = XCTNSPredicateExpectation(predicate: NSPredicate(format: format, argumentArray: args), object: element)
        return XCTWaiter.wait(for: [expectation], timeout: timeout) == .completed
    }

    private static let fixturePath = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent()
        .appendingPathComponent("Fixtures/KRI1633IntroStyles.json").path

    private func fixture() throws -> [String: Any] {
        try XCTUnwrap(JSONSerialization.jsonObject(with: Data(contentsOf: URL(fileURLWithPath: Self.fixturePath))) as? [String: Any])
    }

    private func options(_ block: String) throws -> [[String: Any]] {
        try XCTUnwrap((try XCTUnwrap(fixture()[block] as? [String: Any]))["options"] as? [[String: Any]])
    }

    private func launch(_ extra: [String] = [], capture: URL) -> XCUIApplication {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor", "-ui-testing-editor-intro-styles"] + extra
        app.launchEnvironment = [
            "KRIA_EDITOR_INTRO_STYLES": "1", "KRIA_EDITOR_INTRO_STYLES_FIXTURE": Self.fixturePath,
            "KRIA_EDITOR_INTRO_STYLES_CAPTURE": capture.path, "UI_TEST_REDUCE_MOTION": "1",
        ]
        app.launch()
        return app
    }

    /// The editor-commit bodies the app has sent, oldest first.
    private func commits(_ capture: URL) -> [[String: Any]] {
        guard let text = try? String(contentsOf: capture, encoding: .utf8) else { return [] }
        return text.split(separator: "\n").compactMap { try? JSONSerialization.jsonObject(with: Data($0.utf8)) as? [String: Any] }
    }

    private func introKey(_ row: Any) -> String? {
        let params = (row as? [String: Any])?["source_params"] as? [String: Any]
        let look = params?["title_look"] as? [String: Any]
        return (look?["intro_style"] as? [String: Any])?["key"] as? String
    }

    private func save(_ app: XCUIApplication, file: StaticString = #filePath, line: UInt = #line) {
        let save = app.buttons["native-editor-save"]
        XCTAssertTrue(save.waitForExistence(timeout: 5), file: file, line: line)
        XCTAssertEqual(save.label, "Save changes", "the edit is unsaved", file: file, line: line)
        save.tap()
        XCTAssertTrue(waitFor(save, "label == %@", "Saved", timeout: 20), "Save reaches the saved state; reads \(save.label)", file: file, line: line)
    }

    private func openPanel(_ app: XCUIApplication, entryLabel: String = "Try intro styles") {
        let textTool = app.buttons["native-editor-tool-text"]
        XCTAssertTrue(textTool.waitForExistence(timeout: 20))
        textTool.tap()
        let entry = app.buttons["native-editor-intro-styles-entry"]
        XCTAssertTrue(entry.waitForExistence(timeout: 10), "the Text tool offers intro styles")
        XCTAssertEqual(entry.label, entryLabel)
        entry.tap()
        XCTAssertTrue(element(app, "native-editor-intro-styles-panel").waitForExistence(timeout: 10))
    }

    func testOffersFittedLooksPickSavesAsEditableTitleRowsInEnglishAndTurkish() throws {
        executionTimeAllowance = 300
        let capture = FileManager.default.temporaryDirectory.appendingPathComponent("kri1633-commits-\(UUID().uuidString).jsonl")
        defer { try? FileManager.default.removeItem(at: capture) }
        let app = launch(capture: capture)
        XCTAssertTrue(element(app, "native-editor-preview").waitForExistence(timeout: 30))

        // 1-2: the entry, then at least three looks plus "No intro style", each drawn on this phone.
        openPanel(app)
        let none = app.buttons["intro-style-option-none"]
        XCTAssertTrue(none.waitForExistence(timeout: 15), "the offer loads")
        let tiles = app.buttons.matching(NSPredicate(format: "identifier BEGINSWITH 'intro-style-option-'"))
        XCTAssertGreaterThanOrEqual(tiles.count, 4, "three or more looks plus none")
        let looks = try options("offer_talking_en")
        let recommended = try XCTUnwrap(looks[0]["key"] as? String)
        XCTAssertTrue(waitFor(app.buttons["intro-style-option-\(recommended)"], "value == %@", "Preview drawn over your clip", timeout: 20),
                      "a tile is drawn over her clip; reads \(app.buttons["intro-style-option-\(recommended)"].value ?? "nil")")
        XCTAssertTrue(app.buttons["intro-style-option-\(recommended)"].label.hasSuffix("recommended"),
                      "the first look is the recommended one; reads \(app.buttons["intro-style-option-\(recommended)"].label)")
        attach(app, "Intro styles panel (English)")

        // 3: the second look, then Save: the body carries its rows.
        let second = try XCTUnwrap(looks[1]["key"] as? String)
        app.buttons["intro-style-option-\(second)"].tap()
        XCTAssertTrue(waitFor(app.buttons["intro-style-option-\(second)"], "selected == true", timeout: 10), "the tapped look is marked")
        app.buttons["native-editor-intro-styles-done"].tap()
        save(app)
        var sent = try XCTUnwrap(commits(capture).last?["text_elements"] as? [Any])
        XCTAssertEqual(sent.count, (looks[1]["rows"] as? [Any])?.count)
        XCTAssertTrue(sent.allSatisfy { introKey($0) == second }, "every saved line carries title_look.intro_style.key \(second)")
        XCTAssertTrue(NSArray(array: sent).isEqual(to: looks[1]["rows"] as? [Any] ?? []), "the saved rows are the option's rows, raw")

        // 4: her colour on line 1 wins and is what the next Save sends.
        app.buttons["native-editor-tool-text"].tap()
        let row = element(app, "native-editor-text-row-opening-title")
        XCTAssertTrue(row.waitForExistence(timeout: 10))
        row.tap()
        let style = app.buttons.matching(identifier: "native-editor-text-tabs").matching(NSPredicate(format: "label == %@", "Style")).firstMatch
        XCTAssertTrue(style.waitForExistence(timeout: 5))
        style.tap()
        let blue = app.buttons["Text color #9BCAFF"]
        XCTAssertTrue(blue.waitForExistence(timeout: 5))
        blue.tap()
        XCTAssertTrue(waitFor(blue, "selected == true", timeout: 5))
        app.buttons["native-editor-text-inspector-done"].tap()
        let closeText = app.buttons["native-editor-text-done"]
        if closeText.waitForExistence(timeout: 5) { closeText.tap() }
        save(app)
        sent = try XCTUnwrap(commits(capture).last?["text_elements"] as? [Any])
        let first = try XCTUnwrap(sent.first as? [String: Any])
        XCTAssertEqual(first["color"] as? String, "#9BCAFF", "her colour, not the look's")
        XCTAssertEqual(introKey(first), second, "the look's other fields stay")

        // 5: "No intro style" restores the server's plain title rows.
        openPanel(app)
        XCTAssertTrue(app.buttons["intro-style-option-none"].waitForExistence(timeout: 15))
        app.buttons["intro-style-option-none"].tap()
        app.buttons["native-editor-intro-styles-done"].tap()
        save(app)
        sent = try XCTUnwrap(commits(capture).last?["text_elements"] as? [Any])
        let plain = try XCTUnwrap(options("offer_talking_en").last?["rows"] as? [Any])
        XCTAssertTrue(NSArray(array: sent).isEqual(to: plain), "none restores the plain title rows exactly")
        app.terminate()

        // 6: Turkish, on the Turkish montage offer.
        let turkish = launch(["-ui-testing-editor-intro-styles-tr", "-AppleLanguages", "(tr)", "-AppleLocale", "tr_TR"], capture: capture)
        XCTAssertTrue(element(turkish, "native-editor-preview").waitForExistence(timeout: 30))
        openPanel(turkish, entryLabel: "Giriş stillerini dene")
        XCTAssertTrue(turkish.buttons["intro-style-option-none"].waitForExistence(timeout: 15))
        XCTAssertTrue(turkish.buttons["intro-style-option-grotesk_hand"].label.hasPrefix("Kalın + el yazısı"),
                      "reads \(turkish.buttons["intro-style-option-grotesk_hand"].label)")
        XCTAssertEqual(turkish.buttons["intro-style-option-none"].label, "Giriş stili yok")
        // The state line is localized too, so it is checked as "no longer drawing" rather than by its English words.
        XCTAssertTrue(waitFor(turkish.buttons["intro-style-option-grotesk_hand"], "value != nil AND value != ''", timeout: 20))
        attach(turkish, "Intro styles panel (Turkish)")
        turkish.terminate()
    }

    /// KRI-1615: a card look on a Talking edit that already has a card of hers. The pick's Save carries the look's title
    /// rows and its cards after hers in the contract's shape; she deletes one card with the editor's own delete and the
    /// next Save keeps the rest; "No intro style" then takes the look's cards away and leaves hers.
    func testACardLookSavesHerPhotosAfterHerCardAndADeletedCardStaysGone() throws {
        executionTimeAllowance = 300
        let capture = FileManager.default.temporaryDirectory.appendingPathComponent("kri1615-commits-\(UUID().uuidString).jsonl")
        defer { try? FileManager.default.removeItem(at: capture) }
        let expected = try XCTUnwrap(fixture()["save_body_cards_expected"] as? [String: Any])
        let key = try XCTUnwrap(expected["picked_key"] as? String)
        let cards = try XCTUnwrap(expected["media_overlays"] as? [[String: Any]]).compactMap { $0["id"] as? String }
        let app = launch(["-ui-testing-editor-intro-styles-cards"], capture: capture)
        XCTAssertTrue(element(app, "native-editor-preview").waitForExistence(timeout: 30))

        openPanel(app)
        let look = app.buttons["intro-style-option-\(key)"]
        XCTAssertTrue(look.waitForExistence(timeout: 15), "the offer shows the card look")
        XCTAssertTrue(look.label.hasSuffix("photo card look"), "VoiceOver says it lays out her photos; reads \(look.label)")
        XCTAssertTrue(waitFor(look, "value == %@", "Preview drawn over your clip", timeout: 20),
                      "the tile is drawn over her clip with her photos; reads \(look.value ?? "nil")")
        for _ in 0..<4 where !look.isHittable { element(app, "intro-styles-strip").swipeLeft() }
        attach(app, "Intro styles panel with a card look")
        look.tap()
        XCTAssertTrue(waitFor(look, "selected == true", timeout: 10), "the tapped card look is marked")
        app.buttons["native-editor-intro-styles-done"].tap()
        save(app)
        var sent = try XCTUnwrap(commits(capture).last)
        let rows = try XCTUnwrap(expected["text_elements"] as? [Any])
        XCTAssertTrue(NSArray(array: sent["text_elements"] as? [Any] ?? []).isEqual(to: rows), "the look's title rows, raw")
        XCTAssertTrue(NSArray(array: sent["media_overlays"] as? [Any] ?? []).isEqual(to: expected["media_overlays"] as? [Any] ?? []),
                      "her card first, then each card in the contract's shape: \(sent["media_overlays"] ?? "nil")")
        attach(app, "Editor after saving a card look")

        let gone = cards[1]
        let card = element(app, "native-editor-timeline-media_overlay-\(gone)")
        XCTAssertTrue(card.waitForExistence(timeout: 10), "the card is on the timeline")
        card.tap()
        let remove = app.buttons["native-editor-remove-visual"]
        XCTAssertTrue(remove.waitForExistence(timeout: 5))
        remove.tap()
        let closeVisuals = app.buttons["native-editor-visuals-done"]
        if closeVisuals.waitForExistence(timeout: 5) { closeVisuals.tap() }
        save(app)
        sent = try XCTUnwrap(commits(capture).last)
        let kept = (sent["media_overlays"] as? [[String: Any]])?.compactMap { $0["id"] as? String }
        XCTAssertEqual(kept, cards.filter { $0 != gone }, "Save keeps her card and the other intro card")

        openPanel(app)
        let none = app.buttons["intro-style-option-none"]
        XCTAssertTrue(none.waitForExistence(timeout: 15))
        none.tap()
        app.buttons["native-editor-intro-styles-done"].tap()
        save(app)
        sent = try XCTUnwrap(commits(capture).last)
        XCTAssertEqual((sent["media_overlays"] as? [[String: Any]])?.compactMap { $0["id"] as? String }, [cards[0]],
                       "No intro style takes the look's cards away and keeps hers")
        app.terminate()
    }

    /// KRI-1616: a sticker look (two of her transparent pictures hanging past the sides of the frame) is named and drawn as
    /// one, and its Save carries the stickers exactly where the server placed them, past the frame included.
    func testAStickerLookHangingPastTheFrameSavesWhereTheServerPlacedIt() throws {
        executionTimeAllowance = 300
        let capture = FileManager.default.temporaryDirectory.appendingPathComponent("kri1616-commits-\(UUID().uuidString).jsonl")
        defer { try? FileManager.default.removeItem(at: capture) }
        let option = try XCTUnwrap(try options("offer_talking_stickers").first { ($0["key"] as? String)?.hasSuffix("+stickers_edge") == true })
        let key = try XCTUnwrap(option["key"] as? String)
        let stickers = try XCTUnwrap(option["cards"] as? [[String: Any]])
        let app = launch(["-ui-testing-editor-intro-styles-stickers"], capture: capture)
        XCTAssertTrue(element(app, "native-editor-preview").waitForExistence(timeout: 30))

        openPanel(app)
        let look = app.buttons["intro-style-option-\(key)"]
        XCTAssertTrue(look.waitForExistence(timeout: 15), "the offer shows the sticker look")
        XCTAssertTrue(look.label.hasSuffix("sticker look"), "VoiceOver says it adds her stickers; reads \(look.label)")
        XCTAssertTrue(waitFor(look, "value == %@", "Preview drawn over your clip", timeout: 20),
                      "the tile is drawn over her clip, the stickers cut by its edge; reads \(look.value ?? "nil")")
        for _ in 0..<4 where !look.isHittable { element(app, "intro-styles-strip").swipeLeft() }
        attach(app, "Intro styles panel with a sticker look")
        look.tap()
        XCTAssertTrue(waitFor(look, "selected == true", timeout: 10), "the tapped sticker look is marked")
        app.buttons["native-editor-intro-styles-done"].tap()
        save(app)
        let sent = try XCTUnwrap(commits(capture).last?["media_overlays"] as? [[String: Any]])
        XCTAssertEqual(sent.compactMap { $0["id"] as? String }, stickers.compactMap { $0["id"] as? String })
        for (row, sticker) in zip(sent, stickers) {
            for field in ["x_frac", "y_frac", "scale", "z", "start_s", "end_s"] {
                XCTAssertEqual(row[field] as? Double, sticker[field] as? Double, "\(row["id"] ?? ""): \(field) as the server placed it")
            }
            XCTAssertEqual(row["src_gcs_path"] as? String, sticker["gcs_path"] as? String)
            XCTAssertNil(row["caption_clear"], "the server decides the caption mark again")
        }
        attach(app, "Editor after saving a sticker look")
        app.terminate()
    }
}
