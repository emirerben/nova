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
}
