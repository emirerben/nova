import CoreGraphics
import KriaMediaEngine
import XCTest
@testable import Kria

/// KRI-1633 intro styles on tap, the iPhone half. The offer is the server's (`Tests/Fixtures/KRI1633IntroStyles.json`);
/// this build must read it, draw it, apply it as ordinary editable rows and leave her words and edits alone.
///
/// Failure modes this file pins (numbers are the spec's F-list; F2-F8, F11-F16 are the backend's):
/// - F1 an old build, a flag-off account or an unadmitted account sees the entry: the entry needs the
///   `intro_styles` capability key, which the server sends only when on.
/// - F9 her words are changed or her unsaved title edit is lost: the request carries her CURRENT rows raw, and an
///   offer built for other words is dropped, never shown or applied.
/// - F10 Turkish breaks: the live-preview compile of a TR look draws the same capitals as the server's preview layers
///   ("trendi" -> "TRENDİ").
/// - F17 previews do not match what gets applied, or a tile falls back to text: every option, "none" included, has a
///   parsed preview; one that does not parse turns all of them into label tiles; an answer this build cannot apply
///   (newer version, a row outside the title family, no "none" last) reads as unavailable, never half shown.
/// - F18 request cost: one request per open of the panel, none while the offer still fits her title, an answer for a
///   title that changed meanwhile discarded (checked here by context; the closed-panel cancel by the UI test).
/// - F19 ink unreadable on her footage is an accepted v1 limit: the tile is drawn over her own frame so she can judge.
/// - F20 a route that cannot save a title: the capability is absent (no entry), or the answer is unavailable with its
///   own line; a refusal (404/426/409/422/429) maps to its own outcome.
/// - F21 the model is called from the phone: the client only ever calls the one route.
/// - F22 the replace transaction deletes rows outside the title family, removes a protected row, leaves a half-applied
///   state on undo, or leaves a line of the old stack behind (a pick, Done, reopen, pick): the whole title-family stack
///   on screen is replaced, nothing else, in one undo step. A pending deletion of a line the look puts back is cleared,
///   or Save would drop that line (F9).
/// - F23 `source_params` (`title_look.intro_style`, `text_case`, `line_spacing`, `text_language`) is dropped on the
///   round trip so the server saves plain rows: the Save lane equals `option.rows` raw.
/// - F24 the entry shows in the cloud or web editor, a slide post, or a variant without a title row.
/// - F25 chrome breaks the design rules (Fraunces, borders, helper copy): labels are the closed set below, Inter only.
///
/// KRI-1615 stage C, her photos as cards (`offer_talking_cards`, the stage B wire):
/// - C1 an offer without cards (every build below 62, the switch off) changes: its options parse exactly as before.
/// - C2 a malformed card (bad slot, no media id, scale outside (0, 1], more than four, a repeated id, an id without the
///   `intro-card-` prefix, an empty window) is shown, or its rows are applied as a plain look (the title placed between
///   cards with no cards): that one look is dropped, the plain looks and "none" stay.
/// - C3 a look's cards and rows land in two undo steps, or undo restores one lane only: one transaction over both.
/// - C4 a second pick stacks cards, or a plain look or "none" after a card look leaves its cards: every `intro-card-*` is
///   replaced, her own cards are kept in place and first.
/// - C5 Save sends a card in another shape, or with `caption_clear` (the server decides it): exactly the contract's keys.
/// - C6 a card she deleted comes back, or a pending deletion drops a card a later pick puts back.
/// - C7 a card look applied where the overlay lane is locked (she could not move or delete its cards): refused whole.
/// - C8 a tile claims a look whose card she moved or deleted.
@MainActor
final class IntroStylesOfferTests: XCTestCase {
    private func fixture() throws -> [String: JSONValue] {
        let name = "KRI1633IntroStyles"
        let url = Bundle(for: Self.self).url(forResource: name, withExtension: "json")
            ?? URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent()
                .appendingPathComponent("Fixtures/\(name).json")
        return try XCTUnwrap(JSONDecoder().decode(JSONValue.self, from: Data(contentsOf: url)).objectValue)
    }

    /// `editor_talking_en` is the backend's own editor payload; the other routes open on the plain title their offer's
    /// "none" option carries (the rows the title shows now), with the same capability map.
    private func editorVariant(_ block: String) throws -> [String: JSONValue] {
        let key = String(block.dropFirst("editor_".count))
        let variant = IntroStylesUITestFixture.editorVariant(block: key, in: try fixture())
        XCTAssertFalse(variant.isEmpty, block)
        return variant
    }

    private func offer(_ block: String) throws -> IntroStylesOffer {
        guard case .offer(let offer) = IntroStylesOffer.parse(try XCTUnwrap(try fixture()[block])) else {
            XCTFail("\(block) is not an offer"); throw XCTSkip()
        }
        return offer
    }

    /// A session on the fixture's plain-title edit with the offer shown for it.
    private func session(editor: String, offer block: String, capability: Bool = true) throws -> (NativeEditorSession, IntroStylesOffer) {
        var variant = try editorVariant(editor)
        if !capability { variant["editor_capabilities"] = .object(["text_elements": .object(["editable": .bool(true)])]) }
        let session = NativeEditorSession(draft: NativeEditorUITestFixtures.introStyles(variant: variant), operations: LocalEditorOperations())
        let offer = try offer(block)
        session.introStylesState = .ready(offer)
        session.introStylesOfferContext = session.introStylesContext()
        return (session, offer)
    }

    private func compiler() throws -> NativeEditorRenderCompiler {
        try NativeEditorRenderCompiler(fontDirectory: XCTUnwrap(Bundle.main.url(forResource: "fonts", withExtension: nil)))
    }

    private func titleLane(_ session: NativeEditorSession) -> [JSONValue] {
        let sections = session.document.encodeSnapshot()["editor_payload"]?.objectValue?["sections"]?.objectValue
        return sections?["text_elements"]?.arrayValue ?? []
    }

    // MARK: Wire (F17, F18)

    func testEveryBlockOfTheFixtureReadsAndTheUnavailableOneSaysWhy() throws {
        let blocks = try fixture()
        for key in ["offer_talking_en", "offer_montage_tr", "offer_narrated_en"] {
            let offer = try offer(key)
            XCTAssertGreaterThanOrEqual(offer.options.count, 4, "\(key): at least 3 looks plus none")
            XCTAssertEqual(offer.options.last?.key, "none", "\(key): none is last")
            XCTAssertEqual(offer.options.first?.recommended, true)
            XCTAssertEqual(offer.options.filter(\.recommended).count, 1)
            XCTAssertTrue(IntroStyleFamily.ids.contains(offer.titleFamily))
        }
        XCTAssertEqual(IntroStylesOffer.parse(try XCTUnwrap(blocks["unavailable_nothing_fits"])), .unavailable(reason: "nothing_fits"))
        for key in ["editor_talking_en", "editor_montage_tr", "editor_narrated_en"] {
            let variant = try editorVariant(key)
            XCTAssertEqual(variant["editor_capabilities"]?.objectValue?["intro_styles"], .bool(true), key)
            XCTAssertFalse(variant["text_elements"]?.arrayValue?.isEmpty ?? true, "\(key): a plain title row to restyle")
        }
        XCTAssertNotNil(blocks["save_body_expected"])
    }

    func testEveryOptionIncludingNoneHasAParsedPreviewAndOnePreviewThatFailsDropsAll() throws {
        for key in ["offer_talking_en", "offer_montage_tr", "offer_narrated_en"] {
            let offer = try offer(key)
            XCTAssertTrue(offer.hasPreviews, key)
            XCTAssertTrue(offer.options.allSatisfy { $0.preview != nil }, key)
        }
        var broken = try XCTUnwrap(try fixture()["offer_talking_en"]?.objectValue)
        var options = try XCTUnwrap(broken["options"]?.arrayValue)
        var second = try XCTUnwrap(options[1].objectValue)
        second["preview"] = .object(["media_id": .string("fixture-clip"), "time_s": .number(1), "text_layers": .array([])])
        options[1] = .object(second)
        broken["options"] = .array(options)
        guard case .offer(let parsed) = IntroStylesOffer.parse(.object(broken)) else { return XCTFail("still an offer") }
        XCTAssertFalse(parsed.hasPreviews)
        XCTAssertTrue(parsed.options.allSatisfy { $0.preview == nil }, "previews are all or nothing")
        XCTAssertEqual(parsed.options.map(\.rows), try offer("offer_talking_en").options.map(\.rows), "the rows are untouched")
    }

    func testAnAnswerThisBuildCannotApplyIsUnavailableNotHalfShown() throws {
        let good = try XCTUnwrap(try fixture()["offer_talking_en"]?.objectValue)
        func parse(_ edit: (inout [String: JSONValue]) -> Void) -> IntroStylesOffer.Parsed {
            var copy = good; edit(&copy); return IntroStylesOffer.parse(.object(copy))
        }
        XCTAssertEqual(parse { $0["version"] = .number(2) }, .unavailable(reason: nil))
        XCTAssertEqual(parse { $0["title_family"] = .string("closing-title") }, .unavailable(reason: nil))
        XCTAssertEqual(parse { $0["replace_ids"] = .array([.string("guided-title::sequence-1")]) }, .unavailable(reason: nil))
        XCTAssertEqual(parse { $0["options"] = .array(Array(good["options"]?.arrayValue?.dropLast() ?? [])) }, .unavailable(reason: nil), "no none")
        XCTAssertEqual(parse {
            var options = good["options"]?.arrayValue ?? []
            options[0] = options[1]
            $0["options"] = .array(options)
        }, .unavailable(reason: nil), "a duplicate key")
        XCTAssertEqual(parse {
            var options = good["options"]?.arrayValue ?? []
            var first = options[0].objectValue ?? [:]
            var rows = first["rows"]?.arrayValue ?? []
            var row = rows[0].objectValue ?? [:]
            row["id"] = .string("clip-label-1")
            rows[0] = .object(row)
            first["rows"] = .array(rows)
            options[0] = .object(first)
            $0["options"] = .array(options)
        }, .unavailable(reason: nil), "a row outside the title family")
        XCTAssertEqual(parse { $0["available"] = .bool(false); $0["reason"] = .string("title_too_short") }, .unavailable(reason: "title_too_short"))
    }

    func testRefusalsMapToTheirOwnOutcomeAndLabelsAreTheClosedSet() {
        func refusal(_ status: Int, _ code: String? = nil) -> IntroStylesFailure {
            IntroStylesFailure.from(APIError.requestFailed(status: status, detail: RequestFailureDetail(nil, object: code.map { ["code": .string($0)] })))
        }
        XCTAssertEqual(refusal(404), .hidden)
        XCTAssertEqual(refusal(426, "client_update_required"), .unavailable)
        XCTAssertEqual(refusal(422, "intro_styles_title_rows_invalid"), .unavailable)
        XCTAssertEqual(refusal(429), .retry)
        XCTAssertEqual(refusal(500), .retry)
        XCTAssertEqual(IntroStylesFailure.from(APIError.offline), .retry)
        XCTAssertEqual(IntroStylesFailure.from(APIError.baselineConflict), .newerVersion)
        XCTAssertEqual(IntroStylesFailure.from(APIError.nativeUpdateRequired), .unavailable)
        XCTAssertEqual(IntroStyleLabels.name(forKey: "serif_italic", position: 2), "Serif + italic")
        XCTAssertEqual(IntroStyleLabels.name(forKey: "something_new", position: 3), "Style 3")
        XCTAssertEqual(IntroStyleLabels.unavailableLine(reason: "nothing_fits"), "No intro style fits this video yet.")
        XCTAssertEqual(IntroStyleLabels.unavailableLine(reason: "no_title"), "Intro styles aren't available for this video.")
    }

    // MARK: Entry (F1, F24)

    func testNoEntryWithoutTheCapabilityKeyOrATitleRowOrAnEditableTextLane() throws {
        let (open, _) = try session(editor: "editor_talking_en", offer: "offer_talking_en")
        XCTAssertTrue(open.canOfferIntroStyles)
        XCTAssertTrue(open.isIntroStyleTitleRow("opening-title"))
        XCTAssertFalse(open.isIntroStyleTitleRow("clip-label-1"))

        let (old, _) = try session(editor: "editor_talking_en", offer: "offer_talking_en", capability: false)
        XCTAssertFalse(old.canOfferIntroStyles, "no capability key, no entry (old build, flag off, unadmitted account)")

        var wordBars = try editorVariant("editor_montage_tr")
        wordBars["text_elements"] = .array([.object(["id": .string("guided-title::sequence-1"), "text": .string("Bu"),
                                                     "start_s": .number(0), "end_s": .number(1), "role": .string("generative_intro")]),
                                            .object(["id": .string("clip-label-1"), "text": .string("Kadıköy"),
                                                     "start_s": .number(0), "end_s": .number(1), "role": .string("generative_intro")])])
        let bars = NativeEditorSession(draft: NativeEditorUITestFixtures.introStyles(variant: wordBars), operations: LocalEditorOperations())
        XCTAssertFalse(bars.canOfferIntroStyles, "word bars and clip labels are not a title")

        var locked = try editorVariant("editor_talking_en")
        locked["editor_capabilities"] = .object(["intro_styles": .bool(true), "text_elements": .object(["editable": .bool(false)])])
        let closed = NativeEditorSession(draft: NativeEditorUITestFixtures.introStyles(variant: locked), operations: LocalEditorOperations())
        XCTAssertFalse(closed.canOfferIntroStyles, "a locked text lane has no entry")

        var readOnly = try editorVariant("editor_talking_en")
        var title = try XCTUnwrap(readOnly["text_elements"]?.arrayValue?.first?.objectValue)
        title["source_params"] = .object(["read_only": .bool(true)])
        readOnly["text_elements"] = .array([.object(title)])
        let protected = NativeEditorSession(draft: NativeEditorUITestFixtures.introStyles(variant: readOnly), operations: LocalEditorOperations())
        XCTAssertFalse(protected.canOfferIntroStyles, "a read-only title row is not restyled")
    }

    func testTitleFamilyMatchIsStrict() {
        XCTAssertEqual(IntroStyleFamily.family(of: "opening-title"), "opening-title")
        XCTAssertEqual(IntroStyleFamily.family(of: "narrated-title-line-3"), "narrated-title")
        XCTAssertEqual(IntroStyleFamily.family(of: "guided-title-line-12"), "guided-title")
        for id in ["guided-title::sequence-1", "guided-title-line-", "guided-title-line-x", "opening-title-lines-2", "closing-title", "clip-label-2"] {
            XCTAssertNil(IntroStyleFamily.family(of: id), id)
        }
        XCTAssertEqual(IntroStyleFamily.line(of: "opening-title"), 1)
        XCTAssertEqual(IntroStyleFamily.line(of: "opening-title-line-4"), 4)
    }

    // MARK: Request (F9)

    func testTheRequestCarriesHerCurrentRowsRawIncludingAnUnsavedEdit() throws {
        let (session, _) = try session(editor: "editor_talking_en", offer: "offer_talking_en")
        session.updateTextContent(id: "opening-title", content: "Why I wake up early")
        let rows = session.introStyleTitleRows.map { session.document.textRowJSON($0) }
        XCTAssertEqual(rows.first?.objectValue?["text"], .string("Why I wake up early"), "her unsaved rewrite, not the saved title")
        let body = try JSONDecoder().decode(JSONValue.self, from: JSONEncoder().encode(
            IntroStylesRequest(baseGeneration: session.document.revision.baseGeneration, titleRows: rows))).objectValue
        XCTAssertEqual(body?["version"], .number(1))
        XCTAssertEqual(body?["base_generation"], .string(session.document.revision.baseGeneration))
        XCTAssertEqual(body?["title_rows"], .array(rows))
        XCTAssertNil(session.introStylesCurrentOffer, "an offer built for her old words is dropped, never shown")
    }

    // MARK: Apply (F22, F23)

    func testApplyReapplyAndUndoRestoreTheDocumentExactly() throws {
        let (session, offer) = try session(editor: "editor_talking_en", offer: "offer_talking_en")
        let original = session.document
        XCTAssertEqual(session.introStyleSelectedKey(in: offer), "none", "an unstyled title is the none option")

        XCTAssertTrue(session.applyIntroStyle(offer.options[0]))
        let first = session.document
        XCTAssertEqual(session.introStyleTitleRows.count, offer.options[0].rows.count)
        XCTAssertEqual(session.introStyleSelectedKey(in: offer), offer.options[0].key)

        XCTAssertTrue(session.applyIntroStyle(offer.options[2]))
        XCTAssertEqual(session.introStyleTitleRows.count, offer.options[2].rows.count, "a look replaces the whole stack on screen")
        XCTAssertEqual(session.document.textElements.map(\.id), offer.options[2].rows.compactMap { $0.objectValue?["id"]?.stringValue })

        XCTAssertTrue(session.applyIntroStyle(offer.options[0]), "re-apply")
        XCTAssertEqual(session.document, first, "the same look twice is the same document")

        session.undo(); session.undo()
        XCTAssertEqual(session.document.textElements, first.textElements, "one undo step per tap")
        session.undo()
        XCTAssertEqual(session.document, original, "undo restores the document exactly")
        XCTAssertEqual(session.introStyleSelectedKey(in: offer), "none")
    }

    func testTheSaveLaneEqualsTheOptionRowsRaw() throws {
        for (editor, block, pick) in [("editor_talking_en", "offer_talking_en", 1), ("editor_montage_tr", "offer_montage_tr", 0), ("editor_narrated_en", "offer_narrated_en", 2)] {
            let (session, offer) = try session(editor: editor, offer: block)
            let option = offer.options[pick]
            XCTAssertTrue(session.applyIntroStyle(option), block)
            XCTAssertEqual(titleLane(session), option.rows, "\(block): source_params, text_case, line_spacing and text_language survive")
            XCTAssertTrue(session.hasUnsavedChanges, "the text lane is dirty, so Save sends it")
            // The lane goes out again after her next edit, not just the pick.
            XCTAssertTrue(session.applyIntroStyle(try XCTUnwrap(offer.options.last)))
            XCTAssertEqual(titleLane(session), offer.options.last?.rows, "none restores the server's plain title rows raw")
        }
    }

    /// A session whose title is the stack of `look` (as after a pick and a Save, or a pick, Done and reopen), with the
    /// offer still the one fetched for the plain title: its `replace_ids` name only the old plain row.
    private func stackedSession(look: IntroStylesOffer.Option, editor: String, offer block: String) throws -> NativeEditorSession {
        var variant = try editorVariant(editor)
        variant["text_elements"] = .array(look.rows)
        let session = NativeEditorSession(draft: NativeEditorUITestFixtures.introStyles(variant: variant), operations: LocalEditorOperations())
        session.introStylesState = .ready(try offer(block))
        session.introStylesOfferContext = session.introStylesContext()
        return session
    }

    func testAPickOverAnEarlierPickLeavesNoLineOfTheOldStackBehind() throws {
        // Narrated has 3-line and 2-line looks: the stale `replace_ids` (["narrated-title"]) never names the old extra lines.
        let offer = try offer("offer_narrated_en")
        let three = try XCTUnwrap(offer.options.first { $0.rows.count == 3 })
        let two = try XCTUnwrap(offer.options.first { $0.rows.count == 2 })
        let session = try stackedSession(look: three, editor: "editor_narrated_en", offer: "offer_narrated_en")
        XCTAssertEqual(session.introStyleTitleRows.count, 3)

        XCTAssertTrue(session.applyIntroStyle(two))
        XCTAssertEqual(session.document.textElements.map(\.id), two.rows.compactMap { $0.objectValue?["id"]?.stringValue },
                       "the third line of the earlier look is gone")
        XCTAssertEqual(titleLane(session), two.rows)

        let again = try stackedSession(look: three, editor: "editor_narrated_en", offer: "offer_narrated_en")
        let none = try XCTUnwrap(offer.options.last)
        XCTAssertTrue(again.applyIntroStyle(none))
        XCTAssertEqual(titleLane(again), none.rows, "No intro style restores today's plain title, her words once")
        again.undo()
        XCTAssertEqual(again.introStyleTitleRows.count, 3, "undo brings the whole stack back")
    }

    func testAPendingDeletionOfALineTheLookPutsBackIsClearedSoSaveKeepsIt() throws {
        let offer = try offer("offer_talking_en")
        let look = try XCTUnwrap(offer.options.first { $0.rows.count == 3 })
        let session = try stackedSession(look: look, editor: "editor_talking_en", offer: "offer_talking_en")
        // She deleted the middle line of the saved stack (what `deleteSelection` records for a persisted row).
        XCTAssertTrue(session.deleteText(id: "opening-title-line-2"))
        XCTAssertEqual(session.document.deletions, [EditorDeletion(kind: "text", id: "opening-title-line-2")])
        session.introStylesOfferContext = session.introStylesContext()
        let before = session.document

        XCTAssertTrue(session.applyIntroStyle(offer.options[1]))
        XCTAssertEqual(titleLane(session), offer.options[1].rows)
        XCTAssertTrue(session.document.deletions.isEmpty, "the text deletion would make Save drop the re-inserted line")

        session.undo()
        XCTAssertEqual(session.document, before, "undo restores the lane and the deletion record together")

        // The first line deleted, then No intro style: the plain title must not be filtered out on the server.
        let other = try stackedSession(look: look, editor: "editor_talking_en", offer: "offer_talking_en")
        XCTAssertTrue(other.deleteText(id: "opening-title"))
        XCTAssertEqual(other.document.deletions, [EditorDeletion(kind: "text", id: "opening-title")])
        other.introStylesOfferContext = other.introStylesContext()
        XCTAssertTrue(other.applyIntroStyle(try XCTUnwrap(offer.options.last)))
        XCTAssertEqual(titleLane(other), offer.options.last?.rows)
        XCTAssertTrue(other.document.deletions.isEmpty)
    }

    func testNoneOnAnUnstyledTitleChangesNothing() throws {
        let (session, offer) = try session(editor: "editor_talking_en", offer: "offer_talking_en")
        let before = session.document
        XCTAssertTrue(session.applyIntroStyle(try XCTUnwrap(offer.options.last)))
        XCTAssertEqual(session.document, before)
        XCTAssertFalse(session.hasUnsavedChanges, "tapping none on a title that was never styled is not an edit")
    }

    func testApplyTouchesOnlyTheTitleFamilyAndNeverAProtectedRow() throws {
        var variant = try editorVariant("editor_talking_en")
        let others: [JSONValue] = [
            .object(["id": .string("clip-label-1"), "text": .string("Kadıköy"), "start_s": .number(0), "end_s": .number(2), "role": .string("generative_intro")]),
            .object(["id": .string("closing-title"), "text": .string("See you"), "start_s": .number(4), "end_s": .number(6), "role": .string("generative_intro")]),
            .object(["id": .string("guided-title::sequence-1"), "text": .string("word"), "start_s": .number(1), "end_s": .number(2), "role": .string("generative_intro")]),
            .object(["id": .string("opening-title-line-2"), "text": .string("my mornings"), "start_s": .number(0), "end_s": .number(3), "role": .string("generative_intro")]),
        ]
        let title = try XCTUnwrap(variant["text_elements"]?.arrayValue)
        variant["text_elements"] = .array([others[0]] + title + [others[3], others[1], others[2]])
        let session = NativeEditorSession(draft: NativeEditorUITestFixtures.introStyles(variant: variant), operations: LocalEditorOperations())
        let offer = try offer("offer_talking_en")
        var sent = offer
        sent = IntroStylesOffer(offerID: offer.offerID, route: offer.route, titleFamily: offer.titleFamily, language: nil,
                                replaceIDs: ["opening-title", "opening-title-line-2"], options: offer.options)
        session.introStylesState = .ready(sent)
        session.introStylesOfferContext = session.introStylesContext()
        let keptBefore = session.document.textElements.filter { IntroStyleFamily.family(of: $0.id) == nil }
        XCTAssertTrue(session.applyIntroStyle(offer.options[0]))
        XCTAssertEqual(session.document.textElements.filter { IntroStyleFamily.family(of: $0.id) == nil }, keptBefore,
                       "labels, closing text and word bars are untouched and in order")
        XCTAssertEqual(session.document.textElements.first?.id, "clip-label-1", "the stack lands where the old title was")
        XCTAssertTrue(session.document.deletions.isEmpty, "no deletion record: Save sends the lane")

        // A protected (read-only) title row blocks the whole swap.
        var protectedVariant = variant
        var protectedRow = try XCTUnwrap(title.first?.objectValue)
        protectedRow["source_params"] = .object(["read_only": .bool(true)])
        protectedVariant["text_elements"] = .array([.object(protectedRow)])
        let locked = NativeEditorSession(draft: NativeEditorUITestFixtures.introStyles(variant: protectedVariant), operations: LocalEditorOperations())
        locked.introStylesState = .ready(offer)
        locked.introStylesOfferContext = locked.introStylesContext()
        let before = locked.document
        XCTAssertFalse(locked.applyIntroStyle(offer.options[0]))
        XCTAssertEqual(locked.document, before)
    }

    func testHerEditAfterThePickWinsAndIsWhatSaveSends() throws {
        let (session, offer) = try session(editor: "editor_talking_en", offer: "offer_talking_en")
        XCTAssertTrue(session.applyIntroStyle(offer.options[1]))
        session.setTextColor(id: "opening-title", color: "#9BCAFF")
        let lane = titleLane(session)
        XCTAssertEqual(lane.first?.objectValue?["color"], .string("#9BCAFF"), "her colour is what Save sends")
        XCTAssertEqual(lane.first?.objectValue?["source_params"], offer.options[1].rows.first?.objectValue?["source_params"],
                       "the look's other fields stay")
        XCTAssertNil(session.introStyleSelectedKey(in: offer), "an edited look is hers: no tile claims it")
        XCTAssertNotNil(session.introStylesCurrentOffer, "a colour edit keeps her words, so the offer still fits them")
        // A later pick is a new action: it replaces the look and takes her colour with the rest of the row.
        XCTAssertTrue(session.applyIntroStyle(offer.options[2]))
        XCTAssertEqual(titleLane(session), offer.options[2].rows)
    }

    // MARK: Turkish (F10)

    func testTurkishLooksDrawTheSameCapitalsAsTheServersPreview() throws {
        let (session, offer) = try session(editor: "editor_montage_tr", offer: "offer_montage_tr")
        let source = ResolvedEditorSource(clipIndex: 0, mediaID: "original",
            asset: MediaAsset(id: "local", relativePath: "original.mp4", fingerprint: AssetFingerprint(hex: String(repeating: "a", count: 64), byteCount: 100), duration: 6),
            url: URL(fileURLWithPath: "/fixture/original.mp4"))
        func squeezed(_ text: String) -> String { text.filter { !$0.isWhitespace } }
        func drawnText() throws -> [String] {
            let clips = session.timelineClips.map { clip -> EditorClip in var clip = clip; clip.sourceClipIndex = 0; return clip }
            let live = try compiler().compile(document: session.document, clips: clips, items: session.timelineItems,
                                              sources: [0: source], deviceCaptions: true).recipe.textLayers
            return live.map { squeezed($0.runs.map(\.text).joined()) }.sorted()
        }
        var sawDotlessCapital = false
        for option in offer.options {
            XCTAssertTrue(session.applyIntroStyle(option), option.key)
            let drawn = try drawnText()
            let served = try XCTUnwrap(option.preview).layers.map { squeezed($0.runs.map(\.text).joined()) }.sorted()
            XCTAssertEqual(drawn, served, "\(option.key): the live preview draws the server's preview text")
            sawDotlessCapital = sawDotlessCapital || drawn.contains { $0.contains("YILIN") }
        }
        XCTAssertTrue(sawDotlessCapital, "'yılın' in a caps pair is 'YILIN' (dotless ı), as the server draws it")

        // The fixture's title has no dotted i in a caps line, so the rule is checked on her own edit of a caps look:
        // 'trendi' is 'TRENDİ', not 'TRENDI'.
        let caps = try XCTUnwrap(offer.options.first { option in
            option.rows.first?.objectValue?["text_case"] == .string("upper")
        })
        XCTAssertTrue(session.applyIntroStyle(caps))
        let first = try XCTUnwrap(session.introStyleTitleRows.first)
        session.updateTextContent(id: first.id, content: "trendi")
        XCTAssertTrue(try drawnText().contains { $0.contains("TRENDİ") }, "a Turkish caps line draws the dotted capital")
    }

    // MARK: Card looks (KRI-1615, C1-C2)

    /// The server's card looks, raw, in wire order.
    private func rawCardLooks() throws -> [[String: JSONValue]] {
        let options = try XCTUnwrap(try fixture()["offer_talking_cards"]?.objectValue?["options"]?.arrayValue)
        return options.compactMap(\.objectValue).filter { $0["cards"] != nil }
    }

    func testCardLooksParseWithHerPhotosAfterThePlainLooksAndNoneLast() throws {
        let offer = try offer("offer_talking_cards")
        let raw = try rawCardLooks()
        XCTAssertGreaterThanOrEqual(raw.count, 1)
        XCTAssertEqual(offer.options.filter(\.isCardLook).map(\.key), raw.compactMap { $0["key"]?.stringValue }, "every card look the server sent")
        let kinds = offer.options.dropLast().map(\.isCardLook)
        XCTAssertEqual(kinds, kinds.sorted { !$0 && $1 }, "plain looks first, then card looks")
        XCTAssertEqual(offer.options.last?.key, "none")
        XCTAssertTrue(offer.hasPreviews)
        for (option, wire) in zip(offer.options.filter(\.isCardLook), raw) {
            let cards = try XCTUnwrap(wire["cards"]?.arrayValue).compactMap(\.objectValue)
            XCTAssertEqual(option.cards.map(\.id), cards.compactMap { $0["id"]?.stringValue })
            XCTAssertEqual(option.cards.map(\.gcsPath), cards.compactMap { $0["gcs_path"]?.stringValue })
            XCTAssertEqual(option.cards.map(\.x), cards.compactMap { $0["x_frac"]?.numberValue })
            XCTAssertEqual(option.cards.map(\.y), cards.compactMap { $0["y_frac"]?.numberValue })
            XCTAssertEqual(option.cards.map(\.scale), cards.compactMap { $0["scale"]?.numberValue })
            XCTAssertEqual(option.cards.map(\.captionClear), cards.compactMap { $0["caption_clear"]?.boolValue })
            XCTAssertEqual(option.previewCards.map(\.mediaID), cards.compactMap { $0["media_id"]?.stringValue },
                           "the preview draws her photos in slot order")
            let box = try XCTUnwrap(wire["preview"]?.objectValue?["cards"]?.arrayValue?.first?.objectValue?["box"]?.objectValue)
            let drawn = try XCTUnwrap(option.previewCards.first?.box)
            XCTAssertEqual(Double(drawn.minX), try XCTUnwrap(box["left"]?.numberValue), accuracy: 1e-9)
            XCTAssertEqual(Double(drawn.maxY), try XCTUnwrap(box["bottom"]?.numberValue), accuracy: 1e-9)
        }
        XCTAssertTrue(offer.options.filter { !$0.isCardLook }.allSatisfy { $0.cards.isEmpty && $0.previewCards.isEmpty })
    }

    func testAnOfferWithoutCardsParsesExactlyAsBefore() throws {
        for key in ["offer_talking_en", "offer_montage_tr", "offer_narrated_en"] {
            let offer = try offer(key)
            XCTAssertTrue(offer.options.allSatisfy { $0.cards.isEmpty && $0.previewCards.isEmpty }, key)
        }
        // The card offer minus its card looks is a plain offer: the same plain options, rows and previews.
        var plain = try XCTUnwrap(try fixture()["offer_talking_cards"]?.objectValue)
        plain["options"] = .array(try XCTUnwrap(plain["options"]?.arrayValue).filter { $0.objectValue?["cards"] == nil })
        guard case .offer(let parsed) = IntroStylesOffer.parse(.object(plain)) else { return XCTFail("still an offer") }
        XCTAssertEqual(parsed.options, try offer("offer_talking_cards").options.filter { !$0.isCardLook })
    }

    func testAMalformedCardDropsOnlyItsLookAndThePlainLooksStay() throws {
        let good = try XCTUnwrap(try fixture()["offer_talking_cards"]?.objectValue)
        let expected = try offer("offer_talking_cards")
        let target = try XCTUnwrap(try rawCardLooks().first?["key"]?.stringValue)
        func parse(_ edit: (inout [[String: JSONValue]]) -> Void) -> IntroStylesOffer.Parsed {
            var copy = good
            copy["options"] = .array((good["options"]?.arrayValue ?? []).map { entry in
                guard var option = entry.objectValue, option["key"] == .string(target),
                      var cards = option["cards"]?.arrayValue?.compactMap(\.objectValue) else { return entry }
                edit(&cards)
                option["cards"] = .array(cards.map(JSONValue.object))
                return .object(option)
            })
            return IntroStylesOffer.parse(.object(copy))
        }
        let malformed: [(String, (inout [[String: JSONValue]]) -> Void)] = [
            ("a slot that is not the id's", { $0[0]["slot"] = .string("prop_7") }),
            ("no media id", { $0[0].removeValue(forKey: "media_id") }),
            ("an empty storage path", { $0[0]["gcs_path"] = .string("") }),
            ("no generation", { $0[0].removeValue(forKey: "generation") }),
            ("scale 0", { $0[0]["scale"] = .number(0) }),
            ("scale over 1", { $0[0]["scale"] = .number(1.2) }),
            ("a centre off the canvas", { $0[0]["x_frac"] = .number(-0.1) }),
            ("more than four cards", { cards in
                cards = (1...5).map { n in var card = cards[0]; card["id"] = .string("intro-card-\(n)"); card["slot"] = .string("prop_\(n)"); return card }
            }),
            ("a repeated id", { $0[1]["id"] = $0[0]["id"]; $0[1]["slot"] = $0[0]["slot"] }),
            ("an id outside the intro-card family", { $0[0]["id"] = .string("her-card"); $0[0]["slot"] = .string("prop_1") }),
            ("an empty window", { $0[0]["end_s"] = $0[0]["start_s"] }),
            ("a fractional z", { $0[0]["z"] = .number(0.5) }),
            ("no caption mark", { $0[0].removeValue(forKey: "caption_clear") }),
            ("no cards at all", { $0 = [] }),
        ]
        for (name, edit) in malformed {
            guard case .offer(let parsed) = parse(edit) else { XCTFail("\(name): the plain looks are still offered"); continue }
            XCTAssertNil(parsed.options.first { $0.key == target }, "\(name): the card look is dropped, never shown as a plain look")
            XCTAssertEqual(parsed.options, expected.options.filter { $0.key != target }, "\(name): every other option stays as it was")
        }
    }

    func testACardLookWhosePreviewCannotDrawItsCardsTurnsEveryTileIntoALabelTile() throws {
        var broken = try XCTUnwrap(try fixture()["offer_talking_cards"]?.objectValue)
        let target = try XCTUnwrap(try rawCardLooks().first?["key"]?.stringValue)
        broken["options"] = .array(try XCTUnwrap(broken["options"]?.arrayValue).map { entry in
            guard var option = entry.objectValue, option["key"] == .string(target),
                  var preview = option["preview"]?.objectValue else { return entry }
            preview["cards"] = .array(Array(preview["cards"]?.arrayValue?.dropLast() ?? []))
            option["preview"] = .object(preview)
            return .object(option)
        })
        guard case .offer(let parsed) = IntroStylesOffer.parse(.object(broken)) else { return XCTFail("still an offer") }
        XCTAssertFalse(parsed.hasPreviews, "a card look drawn without one of its photos would not be what she gets")
        XCTAssertTrue(parsed.options.allSatisfy { $0.previewCards.isEmpty })
        XCTAssertEqual(parsed.options.first { $0.key == target }?.cards, try offer("offer_talking_cards").options.first { $0.key == target }?.cards,
                       "the look itself is still offered")
    }

    // MARK: Card looks applied (C3-C8)

    private func cardSession(overlaysEditable: Bool = true) throws -> (NativeEditorSession, IntroStylesOffer) {
        var variant = try editorVariant("editor_talking_cards")
        if !overlaysEditable, var caps = variant["editor_capabilities"]?.objectValue {
            caps["overlays"] = .bool(false)
            variant["editor_capabilities"] = .object(caps)
        }
        let session = NativeEditorSession(draft: NativeEditorUITestFixtures.introStyles(variant: variant), operations: LocalEditorOperations())
        let offer = try offer("offer_talking_cards")
        session.introStylesState = .ready(offer)
        session.introStylesOfferContext = session.introStylesContext()
        return (session, offer)
    }

    private func cardLooks(_ offer: IntroStylesOffer) throws -> (around: IntroStylesOffer.Option, grid: IntroStylesOffer.Option) {
        let looks = offer.options.filter(\.isCardLook)
        return (try XCTUnwrap(looks.first { $0.cards.count == 2 }), try XCTUnwrap(looks.first { $0.cards.count == 4 }))
    }

    private func overlayIDs(_ session: NativeEditorSession) -> [String] { session.document.mediaOverlays.map(\.id) }

    func testACardLookAddsHerPhotosAfterHerOwnCardInOneUndoStep() throws {
        let (session, offer) = try cardSession()
        let original = session.document
        let look = try cardLooks(offer).around
        XCTAssertEqual(overlayIDs(session), ["her-card"])

        XCTAssertTrue(session.applyIntroStyle(look))
        XCTAssertEqual(overlayIDs(session), ["her-card"] + look.cards.map(\.id), "her card stays first, in place")
        XCTAssertEqual(session.document.mediaOverlays.first, original.mediaOverlays.first)
        XCTAssertEqual(titleLane(session), look.rows)
        XCTAssertEqual(session.introStyleSelectedKey(in: offer), look.key)
        XCTAssertTrue(session.hasUnsavedChanges)

        session.undo()
        XCTAssertEqual(session.document, original, "one undo restores the title and the cards together")
        session.redo()
        XCTAssertEqual(overlayIDs(session), ["her-card"] + look.cards.map(\.id))
        XCTAssertEqual(titleLane(session), look.rows)
    }

    func testPickingTheSameCardLookTwiceOrAnotherOneNeverStacksCards() throws {
        let (session, offer) = try cardSession()
        let (around, grid) = try cardLooks(offer)
        XCTAssertTrue(session.applyIntroStyle(around))
        let first = session.document
        XCTAssertTrue(session.applyIntroStyle(grid))
        XCTAssertEqual(overlayIDs(session), ["her-card"] + grid.cards.map(\.id))
        XCTAssertTrue(session.applyIntroStyle(around))
        XCTAssertEqual(session.document, first, "the same look twice is the same document")
    }

    func testAPlainLookOrNoneAfterACardLookTakesItsCardsAwayAndKeepsHers() throws {
        for plain in [try offer("offer_talking_cards").options.first { !$0.isCardLook && !$0.isNone },
                      try offer("offer_talking_cards").options.last] {
            let (session, offer) = try cardSession()
            let option = try XCTUnwrap(plain)
            XCTAssertTrue(session.applyIntroStyle(try cardLooks(offer).grid))
            XCTAssertTrue(session.applyIntroStyle(option))
            XCTAssertEqual(overlayIDs(session), ["her-card"], "\(option.key): the look's title was placed without cards")
            XCTAssertEqual(titleLane(session), option.rows)
            XCTAssertEqual(session.introStyleSelectedKey(in: offer), option.key)
        }
    }

    func testSaveSendsTheContractCardRowsAfterHersWithoutTheCaptionMark() throws {
        let (session, offer) = try cardSession()
        let expected = try XCTUnwrap(try fixture()["save_body_cards_expected"]?.objectValue)
        let look = try XCTUnwrap(offer.options.first { $0.key == expected["picked_key"]?.stringValue })
        XCTAssertTrue(session.applyIntroStyle(look))
        let lanes = try XCTUnwrap(session.exportEditorState()).lanes
        XCTAssertEqual(lanes.mediaOverlays, expected["media_overlays"]?.arrayValue,
                       "her card, then each card in the contract's shape: the server pins the photo and decides caption_clear")
        XCTAssertEqual(lanes.textElements, expected["text_elements"]?.arrayValue)
        XCTAssertEqual(session.document.mediaOverlays.dropFirst().map { $0.raw["caption_clear"] }, look.cards.map { _ in .bool(true) },
                       "the editor keeps the server's mark so the preview draws each card where it exports")
    }

    func testTheLivePreviewDrawsACaptionClearCardWhereTheExportWill() throws {
        let (session, offer) = try cardSession()
        let look = try cardLooks(offer).around
        XCTAssertTrue(session.applyIntroStyle(look))
        let bottom = try XCTUnwrap(look.cards.max { $0.y < $1.y })
        XCTAssertGreaterThan(bottom.y, NativeEditorRenderCompiler.deviceCardMaxYFraction, "the fixture's second card sits under her captions")
        let fingerprint = AssetFingerprint(hex: String(repeating: "b", count: 64), byteCount: 10)
        func source(_ id: String) -> ResolvedEditorSource {
            ResolvedEditorSource(clipIndex: 0, mediaID: id, asset: MediaAsset(id: id, relativePath: "\(id).jpg", fingerprint: fingerprint,
                                                                           naturalSize: MediaSize(width: 300, height: 400)),
                                 url: URL(fileURLWithPath: "/fixture/\(id).jpg"))
        }
        let footage = ResolvedEditorSource(clipIndex: 0, mediaID: "original",
            asset: MediaAsset(id: "local", relativePath: "original.mp4", fingerprint: AssetFingerprint(hex: String(repeating: "a", count: 64), byteCount: 100), duration: 6),
            url: URL(fileURLWithPath: "/fixture/original.mp4"))
        let clips = session.timelineClips.map { clip -> EditorClip in var clip = clip; clip.sourceClipIndex = 0; return clip }
        let media = Dictionary(uniqueKeysWithValues: session.document.mediaOverlays.map { ("overlay:" + $0.id, source($0.id)) })
        let recipe = try compiler().compile(document: session.document, clips: clips, items: session.timelineItems,
                                            sources: [0: footage], mediaSources: media, deviceCaptions: true).recipe
        let drawn = try XCTUnwrap(recipe.tracks.first { $0.kind == .overlay }?.clips.first { $0.id == "overlay:" + bottom.id })
        XCTAssertEqual(drawn.transform.positionY, (bottom.y * 1920).rounded(.toNearestOrEven) - 960, accuracy: 0.5,
                       "drawn at its own y, not lifted onto her face at 0.62")
    }

    func testDeletingOneCardKeepsTheRestAndNoTileClaimsTheLook() throws {
        let (session, offer) = try cardSession()
        let look = try cardLooks(offer).grid
        XCTAssertTrue(session.applyIntroStyle(look))
        let deleted = look.cards[0].id
        session.removeMediaOverlay(id: deleted)
        XCTAssertEqual(overlayIDs(session), ["her-card"] + look.cards.dropFirst().map(\.id))
        XCTAssertNil(session.introStyleSelectedKey(in: offer), "the look without one of its cards is hers now")
        let sent = try XCTUnwrap(session.exportEditorState()).lanes.mediaOverlays?.compactMap { $0.objectValue?["id"]?.stringValue }
        XCTAssertEqual(sent, ["her-card"] + look.cards.dropFirst().map(\.id), "Save keeps every other card")
        XCTAssertTrue(session.applyIntroStyle(look), "tapping the look again brings the card back")
        XCTAssertEqual(overlayIDs(session), ["her-card"] + look.cards.map(\.id))
    }

    func testARePickedCardClearsItsPendingDeletion() throws {
        // As after a Save: the look's cards are her saved cards, the offer is the one fetched for that edit.
        let (fresh, offer) = try cardSession()
        let look = try cardLooks(offer).around
        XCTAssertTrue(fresh.applyIntroStyle(look))
        var variant = try editorVariant("editor_talking_cards")
        variant["text_elements"] = .array(look.rows)
        variant["media_overlays"] = .array(fresh.document.mediaOverlays.map { .object($0.raw) })
        let session = NativeEditorSession(draft: NativeEditorUITestFixtures.introStyles(variant: variant), operations: LocalEditorOperations())
        session.introStylesState = .ready(offer)
        session.introStylesOfferContext = session.introStylesContext()
        XCTAssertEqual(session.introStyleSelectedKey(in: offer), look.key, "a saved card look reads as that look")

        session.removeMediaOverlay(id: look.cards[0].id)
        XCTAssertEqual(session.document.deletions, [EditorDeletion(kind: "media_overlay", id: look.cards[0].id)])
        XCTAssertTrue(session.applyIntroStyle(look))
        XCTAssertTrue(session.document.deletions.isEmpty, "the deletion would make Save drop the card the look puts back")
        XCTAssertEqual(overlayIDs(session), ["her-card"] + look.cards.map(\.id))
    }

    func testACardTileDrawsHerCachedPhotoInItsBoxAndAPlateWhereThisIPhoneHasNone() async throws {
        let look = try cardLooks(try offer("offer_talking_cards")).grid
        let cached = look.previewCards[0], missing = look.previewCards[1]
        let thumbnail = CreationMediaPreview.url(mediaID: cached.mediaID)
        try FileManager.default.createDirectory(at: thumbnail.deletingLastPathComponent(), withIntermediateDirectories: true)
        try XCTUnwrap(UIGraphicsImageRenderer(size: CGSize(width: 30, height: 40)).image { context in
            UIColor.red.setFill(); context.fill(CGRect(x: 0, y: 0, width: 30, height: 40))
        }.jpegData(compressionQuality: 1)).write(to: thumbnail)
        defer { try? FileManager.default.removeItem(at: thumbnail) }
        try? FileManager.default.removeItem(at: CreationMediaPreview.url(mediaID: missing.mediaID))

        let drawn = await DesignPreviewRenderer.withCards(look.previewCards, over: nil, projectID: nil)
        let image = try XCTUnwrap(drawn)
        func pixel(at fraction: CGPoint) -> [UInt8] {
            var rgba = [UInt8](repeating: 0, count: 4)
            let context = CGContext(data: &rgba, width: 1, height: 1, bitsPerComponent: 8, bytesPerRow: 4,
                                    space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)!
            context.draw(image, in: CGRect(x: -fraction.x * CGFloat(image.width), y: -(1 - fraction.y) * CGFloat(image.height),
                                           width: CGFloat(image.width), height: CGFloat(image.height)))
            return Array(rgba.prefix(3))
        }
        let photo = pixel(at: CGPoint(x: cached.box.midX, y: cached.box.midY))
        XCTAssertGreaterThan(photo[0], 200, "her photo fills its box: \(photo)")
        XCTAssertLessThan(photo[1], 60)
        let plate = pixel(at: CGPoint(x: missing.box.midX, y: missing.box.midY))
        XCTAssertTrue(plate.allSatisfy { $0 > 220 }, "a neutral plate where the photo is not on this iPhone: \(plate)")
        XCTAssertEqual(pixel(at: CGPoint(x: 0.5, y: 0.5)), [0, 0, 0], "no clip: the canvas stays as the text pass draws it")
    }

    func testCardLookNamesSayTheirFontsAndPhotos() {
        XCTAssertEqual(IntroStyleLabels.name(forKey: "young_serif+around_speaker", position: 5), "Soft serif + photos")
        XCTAssertEqual(IntroStyleLabels.name(forKey: "grotesk_pen+grid_2x2", position: 6), "Bold + marker + photo grid")
        XCTAssertEqual(IntroStyleLabels.name(forKey: "grotesk_pen+ring", position: 7), "Style 7", "an arrangement this build has no name for")
        XCTAssertEqual(IntroStyleLabels.photoCardLook, "photo card look")
    }

    func testAPlainPickOnAnEditWithoutCardsSendsOnlyItsTitle() throws {
        for (editor, block) in [("editor_talking_en", "offer_talking_en"), ("editor_montage_tr", "offer_montage_tr"),
                                ("editor_narrated_en", "offer_narrated_en")] {
            let (session, offer) = try session(editor: editor, offer: block)
            XCTAssertTrue(session.applyIntroStyle(offer.options[0]), block)
            XCTAssertEqual(session.dirtySections, [.text], "\(block): only the title lane changed")
            let lanes = try XCTUnwrap(session.exportEditorState()).lanes
            XCTAssertNil(lanes.mediaOverlays, "\(block): no card lane goes out")
            XCTAssertEqual(lanes.textElements, offer.options[0].rows)
        }
    }

    func testACardLookIsRefusedWhereTheOverlayLaneIsLocked() throws {
        let (session, offer) = try cardSession(overlaysEditable: false)
        let before = session.document
        XCTAssertFalse(session.applyIntroStyle(try cardLooks(offer).around), "she could neither move nor delete its cards")
        XCTAssertEqual(session.document, before)
        let plain = try XCTUnwrap(offer.options.first { !$0.isCardLook && !$0.isNone })
        XCTAssertTrue(session.applyIntroStyle(plain), "a plain look touches no card")
        XCTAssertEqual(overlayIDs(session), ["her-card"])
    }
}
