import XCTest
@testable import Kria

/// AI suggestion blocks. The inputs are the mock sets the offline server also serves (`SuggestionCardsMocks`), so
/// one fixture feeds the unit tests, the XCUITest and previews. The first test pins those fixtures to the generated
/// OpenAPI contract: a mock that names a key the server does not send would make every other test here meaningless.
@MainActor
final class SuggestionCardsTests: XCTestCase {
    private func decode(_ object: [String: Any]) throws -> SuggestionSet {
        try JSONDecoder().decode(SuggestionSet.self, from: JSONSerialization.data(withJSONObject: object))
    }

    private func card(_ kind: String = "captions", id: String = "ss_1", title: String = "Add captions", prompt: String = "Add captions",
                      copy: String? = nil) -> SuggestionCard {
        SuggestionCard(id: id, kind: kind, title: title, prompt: prompt, copyText: copy)
    }

    // MARK: contract

    func testMockSetsOnlyUseKeysTheGeneratedContractNames() throws {
        let url = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent()
            .deletingLastPathComponent().appendingPathComponent("Kria/Generated/openapi.yaml")
        let spec = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(contentsOf: url)) as? [String: Any])
        let schemas = try XCTUnwrap((spec["components"] as? [String: Any])?["schemas"] as? [String: Any])
        func properties(_ name: String) throws -> Set<String> {
            Set(try XCTUnwrap(((schemas[name] as? [String: Any])?["properties"] as? [String: Any])).keys)
        }
        let setKeys = try properties("SuggestionSetOut"), cardKeys = try properties("SuggestionOut"), digestKeys = try properties("SuggestionDigest")
        for mock in [SuggestionCardsMocks.ready(), SuggestionCardsMocks.stale(), SuggestionCardsMocks.refreshed(), SuggestionCardsMocks.empty()] {
            XCTAssertTrue(Set(mock.keys).isSubset(of: setKeys), "set keys not in contract: \(Set(mock.keys).subtracting(setKeys))")
            XCTAssertTrue(Set((mock["digest"] as? [String: Any] ?? [:]).keys).isSubset(of: digestKeys))
        }
        for card in SuggestionCardsMocks.everyKind {
            XCTAssertTrue(Set(card.keys).isSubset(of: cardKeys), "card keys not in contract: \(Set(card.keys).subtracting(cardKeys))")
        }
        let tap = try XCTUnwrap(schemas["SuggestionTapIn"] as? [String: Any])
        XCTAssertEqual(Set(try XCTUnwrap(tap["required"] as? [String])), ["set_id", "chip_id"], "the pin keeps its wire shape")
    }

    // MARK: decoding

    func testEveryKindDecodesWithItsOwnTitleAndFields() throws {
        let kinds = SuggestionCardsMocks.everyKind
        XCTAssertEqual(kinds.count, 13)
        let cards = try kinds.map { try JSONDecoder().decode(SuggestionCard.self, from: JSONSerialization.data(withJSONObject: $0)) }
        XCTAssertEqual(cards.map(\.kind), kinds.map { $0["kind"] as! String })
        XCTAssertGreaterThan(Set(cards.map(\.symbolName)).count, 8, "kinds do not all collapse onto the generic symbol")
        let mute = try XCTUnwrap(cards.first { $0.kind == "mute_clip" })
        XCTAssertEqual(mute.evidence, ["Wind in clip 3"])
        XCTAssertEqual(mute.clipOrdinal, 3)
        XCTAssertEqual(mute.mediaID, "m3")
        XCTAssertEqual(try XCTUnwrap(cards.first { $0.kind == "generated_overlay" }).costLabel, "$0.04")
        XCTAssertNil(mute.costLabel)
    }

    func testAnUnknownKindAndActionStillRenderAndOnlyTheBrokenCardIsDropped() throws {
        let set = try decode(SuggestionCardsMocks.unknown())
        XCTAssertEqual(set.cards.map(\.kind), ["color_grade_v9"], "the card with no title is dropped alone")
        let future = try XCTUnwrap(set.cards.first)
        XCTAssertEqual(future.title, "Warm up the grade")
        XCTAssertEqual(future.actionType, "grade_recipe")
        XCTAssertEqual(future.symbolName, "sparkles")
        XCTAssertEqual(SuggestionReadyContent.resolve(set: set), .cards([future]))
    }

    func testAHookWithoutItsExactTextIsNeverShown() throws {
        var hook = SuggestionCardsMocks.card("hook", "Add a hook", prompt: "Add this hook", copy: nil, action: "copy_decision")
        var set = SuggestionCardsMocks.set("sg_x", cards: [hook])
        XCTAssertTrue(try decode(set).cards.isEmpty, "a tap must never approve words the creator cannot read")
        hook["copy_text"] = String(repeating: "x", count: SuggestionCard.maxCopyLength + 1)
        set = SuggestionCardsMocks.set("sg_x", cards: [hook])
        XCTAssertTrue(try decode(set).cards.isEmpty, "text the app could not show whole is not approved either")
        hook["copy_text"] = "Almost missed this"
        XCTAssertEqual(try decode(SuggestionCardsMocks.set("sg_x", cards: [hook])).cards.first?.copyText, "Almost missed this")
    }

    func testTurkishTextSurvivesDecodingByteForByte() throws {
        let set = try decode(SuggestionCardsMocks.turkish())
        let hook = try XCTUnwrap(set.cards.first)
        XCTAssertEqual(hook.copyText, "Şu manzarayı kaçırıyordum neredeyse, ışıklar yanınca ıslak taşlar parladı")
        XCTAssertEqual(hook.title, "İlk saniyeye bir kanca ekle")
        XCTAssertEqual(hook.why, "Açılış sakin, izleyici ilk üç saniyede kayabilir")
    }

    func testCardsAreRankedDedupedAndCappedAtFive() throws {
        let many = (1...8).map { SuggestionCardsMocks.card("captions", "Card \($0)", prompt: "p\($0)", id: "ss_\($0 % 6)") }
        let set = try decode(SuggestionCardsMocks.set("sg_many", cards: many))
        XCTAssertEqual(set.cards.count, 5)
        XCTAssertEqual(Set(set.cards.map(\.id)).count, 5)
        XCTAssertEqual(set.cards.map(\.rank), set.cards.map(\.rank).sorted())
    }

    func testABadWhyOrCostDropsOnlyThatField() throws {
        var raw = SuggestionCardsMocks.card("mute_clip", "Mute clip 3", why: String(repeating: "w", count: 200), prompt: "Mute clip 3", cost: -1)
        raw["evidence"] = ["", String(repeating: "e", count: 80), "Wind in clip 3"]
        let parsed = try XCTUnwrap(try decode(SuggestionCardsMocks.set("sg_f", cards: [raw])).cards.first)
        XCTAssertNil(parsed.why)
        XCTAssertNil(parsed.costUSD)
        XCTAssertEqual(parsed.evidence, ["Wind in clip 3"])
    }

    // MARK: what the ready stage draws

    func testV2CardsReplaceChipsAndAnOlderServerKeepsItsChips() throws {
        let chip = SuggestionChip(id: "c", kind: "captions", label: "Add captions", message: "Add captions", rank: 1)
        let legacy = SuggestionSet(setID: "sg_l", state: "ready", status: "ok", chips: [chip])
        XCTAssertEqual(SuggestionReadyContent.resolve(set: legacy), .chips([chip]))
        let v2 = SuggestionSet(setID: "sg_2", state: "ready", status: "ok", chips: [chip], cards: [card()])
        XCTAssertEqual(SuggestionReadyContent.resolve(set: v2), .cards([card()]))
        XCTAssertEqual(SuggestionReadyContent.resolve(set: try decode(SuggestionCardsMocks.empty())), .none)
        XCTAssertEqual(SuggestionReadyContent.resolve(set: nil), .none)
    }

    func testCardsAboveThisAppsProtocolAreDroppedAndTheChipsTakeOver() {
        let chip = SuggestionChip(id: "c", kind: "captions", label: "Add captions", message: "Add captions", rank: 1)
        let gated = SuggestionCard(id: "g", kind: "mute_clip", title: "Mute clip 3", prompt: "Mute clip 3", requiresProtocol: 99)
        let set = SuggestionSet(setID: "sg", state: "ready", status: "ok", chips: [chip], cards: [gated, card()])
        XCTAssertEqual(SuggestionReadyContent.resolve(set: set, appProtocol: 5), .cards([card()]))
        let onlyGated = SuggestionSet(setID: "sg", state: "ready", status: "ok", chips: [chip], cards: [gated])
        XCTAssertEqual(SuggestionReadyContent.resolve(set: onlyGated, appProtocol: 5), .chips([chip]))
    }

    func testCardsOnlyAppearInTheStateAndStatusTheyWereServedFor() {
        let pending = SuggestionSet(setID: "sg", state: "ready", status: "pending", chips: [], cards: [card()])
        XCTAssertEqual(SuggestionReadyContent.resolve(set: pending), .none)
        let firstPrompt = SuggestionSet(setID: "sg", state: "first_prompt", status: "ok", chips: [], cards: [card()])
        XCTAssertEqual(SuggestionReadyContent.resolve(set: firstPrompt), .none, "ready cards never leak onto the first prompt")
    }

    func testAHeldV2SetIsFrozenLikeChipsSoCardsNeverReorderUnderTheThumb() {
        let held = SuggestionSet(setID: "sg_1", state: "ready", status: "ok", chips: [], cards: [card(id: "a"), card(id: "b")])
        let reordered = SuggestionSet(setID: "sg_1", state: "ready", status: "ok", chips: [], cards: [card(id: "b")])
        let next = SuggestionSet(setID: "sg_2", state: "ready", status: "ok", chips: [], cards: [card(id: "b")])
        XCTAssertEqual(SuggestionChipsPolicy.merge(current: held, incoming: reordered), held)
        XCTAssertEqual(SuggestionChipsPolicy.merge(current: held, incoming: next), next)
    }

    // MARK: ledger

    func testOneTapAtATimeAndADoubleTapIsOneTurn() {
        let ledger = SuggestionCardLedger()
        let a = card(id: "a"), b = card(id: "b")
        XCTAssertTrue(ledger.begin(a))
        XCTAssertFalse(ledger.begin(a), "double tap")
        XCTAssertFalse(ledger.begin(b), "a second card while the first is on its way")
        XCTAssertEqual(ledger.visible([a, b]).map(\.id), ["a", "b"], "the sending card stays so the creator sees it settle")
    }

    func testAnAcceptedTapHidesTheCardAndAFailedOneLeavesItTappable() {
        let ledger = SuggestionCardLedger()
        let a = card(id: "a")
        XCTAssertTrue(ledger.begin(a))
        ledger.finish(a, accepted: false)
        XCTAssertEqual(ledger.phase(of: "a"), .idle, "offline or refused: nothing reached the server")
        XCTAssertTrue(ledger.begin(a))
        ledger.finish(a, accepted: true)
        XCTAssertEqual(ledger.phase(of: "a"), .applied)
        XCTAssertTrue(ledger.visible([a]).isEmpty)
        ledger.finish(a, accepted: false)
        XCTAssertEqual(ledger.phase(of: "a"), .applied, "the trailing no-op finish cannot undo an accepted tap")
        XCTAssertFalse(ledger.begin(a), "an applied card cannot be sent twice")
    }

    func testAStaleTapShowsTheNoticeAndAsksForAFreshSet() {
        let ledger = SuggestionCardLedger()
        let a = card(id: "a"), b = card(id: "b")
        let tokenBefore = ledger.refreshToken
        XCTAssertTrue(ledger.begin(a))
        ledger.markStale(a, setID: "sg_1")
        XCTAssertEqual(ledger.staleNotice, SuggestionStaleNotice(cardID: "a", setID: "sg_1"))
        XCTAssertEqual(ledger.visible([a, b]).map(\.id), ["b"])
        XCTAssertGreaterThan(ledger.refreshToken, tokenBefore)
        ledger.reconcile(with: SuggestionSet(setID: "sg_1", state: "ready", status: "ok", chips: [], cards: [b]))
        XCTAssertNotNil(ledger.staleNotice, "the same set is not the refresh yet")
        ledger.reconcile(with: SuggestionSet(setID: "sg_2", state: "ready", status: "ok", chips: [], cards: [b]))
        XCTAssertNil(ledger.staleNotice, "the refreshed set clears the notice")
    }

    func testANewSetBringsBackHandledCardsExceptDismissedOnes() {
        let ledger = SuggestionCardLedger()
        let a = card(id: "a"), b = card(id: "b")
        ledger.reconcile(with: SuggestionSet(setID: "sg_1", state: "ready", status: "ok", chips: [], cards: [a, b]))
        XCTAssertTrue(ledger.begin(a)); ledger.finish(a, accepted: true)
        ledger.dismiss(b)
        ledger.reconcile(with: SuggestionSet(setID: "sg_1", state: "ready", status: "ok", chips: [], cards: [a, b]))
        XCTAssertEqual(ledger.visible([a, b]), [], "the same set: both stay gone")
        ledger.reconcile(with: SuggestionSet(setID: "sg_2", state: "ready", status: "ok", chips: [], cards: [a, b]))
        XCTAssertEqual(ledger.visible([a, b]).map(\.id), ["a"], "a new set may offer an applied card again; a dismissal is for good")
    }

    func testASetChangingMidTapKeepsTheTapSending() {
        let ledger = SuggestionCardLedger()
        let a = card(id: "a")
        ledger.reconcile(with: SuggestionSet(setID: "sg_1", state: "ready", status: "ok", chips: [], cards: [a]))
        XCTAssertTrue(ledger.begin(a))
        ledger.reconcile(with: SuggestionSet(setID: "sg_2", state: "ready", status: "ok", chips: [], cards: [a]))
        XCTAssertEqual(ledger.phase(of: "a"), .sending, "the poll must not forget a tap that is still in flight")
        XCTAssertFalse(ledger.begin(a))
    }

    // MARK: wire

    func testDismissPostsTheSetAndCardAndNeverThrows() async throws {
        var seen: (method: String?, path: String?, body: [String: String])?
        NativeEditorURLProtocol.handler = { request in
            let body = try XCTUnwrap(try JSONSerialization.jsonObject(with: NativeEditorTestSupport.bodyData(request)) as? [String: String])
            seen = (request.httpMethod, request.url?.path, body)
            return (200, Data("{}".utf8))
        }
        let api = NativeEditorTestSupport.api()
        await api.dismissSuggestion(threadID: PreviewFixtures.projectID, setID: "sg_1", suggestionID: "ss_2")
        XCTAssertEqual(seen?.method, "POST")
        XCTAssertEqual(seen?.path, "/creation-threads/\(PreviewFixtures.projectID.uuidString)/suggestions/dismiss")
        XCTAssertEqual(seen?.body, ["set_id": "sg_1", "chip_id": "ss_2"])

        for status in [404, 405, 500] {
            NativeEditorURLProtocol.handler = { _ in (status, Data("{}".utf8)) }
            await api.dismissSuggestion(threadID: PreviewFixtures.projectID, setID: "sg_1", suggestionID: "ss_2")
        }
    }

    func testTheCardTapReusesThePinnedWireShapeAndTheStaleCodeIsTheServersName() throws {
        let tap = SuggestionTap(setID: "sg_1", chipID: "ss_9")
        let request = SubmitTurnRequest(message: "Mute clip 3, it has wind noise", clientEventID: "c", expectedThreadRevision: 4, suggestion: tap)
        let body = try XCTUnwrap(JSONSerialization.jsonObject(with: JSONEncoder().encode(request)) as? [String: Any])
        XCTAssertEqual(body["suggestion"] as? [String: String], ["set_id": "sg_1", "chip_id": "ss_9"])
        XCTAssertEqual(body["message"] as? String, "Mute clip 3, it has wind noise")
        XCTAssertEqual(SuggestionTap.staleConflictCode, "suggestion_stale")
    }
}
