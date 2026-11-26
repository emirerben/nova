import XCTest
@testable import Kria

/// KRI-1591: the clip-order card (the decided clip order as a strip, a sheet to change it, one `clip_order_ops` turn to
/// confirm it). The failure modes, each pinned below or in `ClipOrderUITests` (the flows):
///
///  1. No card, or `shown: false`, draws nothing and changes no thread that never had one.        testNoCardOrNotShownDrawsNothing
///  2. 19 and 40 clips parse, reorder and stay editable; more than the wire allows is read-only.   testNineteenAndFortyClipsReorderAndAHugeCardIsReadOnly (+ UI)
///  3. Dragging in the sheet and closing it sends nothing (the draft is local state only).         testDraggingWithoutConfirmingSendsNothing (+ UI)
///  4. Confirm sends ONE `clip_order_ops` with the whole order and `confirm: true`; a retry of a
///     failed send resends the same client event id and the same body.                            testConfirmSendsOneWholeOrderTurn, testRetryResendsTheSameBody (+ UI)
///  5. Two quick confirms send only the last full order, once.                                    testQuickConfirmsSendOnlyTheLastOrderOnce
///  6. A rebased or stale answer is just the server's newer card: nothing to show as an error.     testTheServersNewerCardWinsOverTheOptimisticOrder (+ UI)
///  7. `editable: false` (a video exists) is read-only.                                           testReadOnlyCardAllowsNoEditing (+ UI)
///  8. Tapping a clip larger / a missing original: UI only (needs the simulator)                  ClipOrderUITests
///  9. The drag's user event and its card event never become chat bubbles.                        testTheDragTurnIsNeverAChatMessage (+ UI)
/// 10. `assistant_error` `clip_order_changed` carries the card the strip draws.                   testAnErrorMessageThatCarriesACardAnchorsTheStrip (+ UI)
/// 11. A label never shows a media id, UUID or file name; the phone invents none.                 testLabelsNeverShowAnIdentifier
/// 12. EN/TR/DE strings: `scripts/ios/localization.py check`, `scan` and `built`.
/// 13. Older cards collapse to one line, the newest card sits under the newest carrier.           testOnlyTheNewestCarrierShowsTheStrip (+ UI)
/// 14. Unknown `source`, missing optional keys and extra keys never break the card.               testDecodingIsTolerant
final class ClipOrderTests: XCTestCase {
    // MARK: fixtures

    private func json(_ text: String) -> [String: JSONValue] {
        (try? JSONDecoder().decode([String: JSONValue].self, from: Data(text.utf8))) ?? [:]
    }

    private func cardObject(_ ids: [String], proposal: String = "9f2c41d0a7b3e655", shown: Bool = true, editable: Bool = true,
                            confirmed: Bool = false, moved: Set<String> = [], extra: String = "") -> String {
        let cards = ids.enumerated().map { index, id in
            #"{"media_id":"\#(id)","position":\#(index + 1),"label":"clip \#(id) words","kind":"video","group":null,"source":"server_default","moved":\#(moved.contains(id))}"#
        }.joined(separator: ",")
        return #"{"version":1,"proposal_id":"\#(proposal)","shown":\#(shown),"confirmed":\#(confirmed),"editable":\#(editable),"total":\#(ids.count),"cards":[\#(cards)]\#(extra)}"#
    }

    private func card(_ ids: [String], proposal: String = "9f2c41d0a7b3e655", shown: Bool = true, editable: Bool = true,
                      confirmed: Bool = false, moved: Set<String> = [], extra: String = "") -> String {
        #"{"clip_order_card":\#(cardObject(ids, proposal: proposal, shown: shown, editable: editable, confirmed: confirmed, moved: moved, extra: extra))}"#
    }

    private func event(_ id: String, _ sequence: Int, type: String = "draft_applied", role: String = "assistant", content: String? = "Here is your draft.",
                       payload: String? = nil) -> ThreadEvent {
        ThreadEvent(id: id, sequence: sequence, revision: sequence, role: role, eventType: type, content: content,
                    payload: payload.map(json), createdAt: Date(timeIntervalSince1970: 0))
    }

    private func parsed(_ ids: [String] = ["a", "b", "c"], extra: String = "") throws -> ClipOrderCard {
        try XCTUnwrap(ClipOrderCard.parse(payload: json(card(ids, extra: extra)), eventID: "e", sequence: 1))
    }

    // MARK: 1, 14: reading the card

    func testNoCardOrNotShownDrawsNothing() {
        XCTAssertEqual(ClipOrderThread.fold([]), .empty)
        XCTAssertEqual(ClipOrderThread.fold([event("e1", 1, payload: "{}")]), .empty)
        XCTAssertEqual(ClipOrderThread.fold([event("e1", 1, payload: card(["a", "b"], shown: false))]), .empty, "shown:false renders nothing")
        // A hidden newest card hides the older one too: nothing says the creator should still see a strip.
        let hiddenLater = [event("e1", 1, payload: card(["a", "b"])), event("e2", 2, type: "clip_order_card", content: nil, payload: card(["a", "b"], shown: false))]
        XCTAssertEqual(ClipOrderThread.fold(hiddenLater), .empty)
    }

    func testDecodingIsTolerant() throws {
        let tolerant = #"{"clip_order_card":{"proposal_id":"p1","cards":[{"media_id":"a","source":"a_future_source","label":"the map video"},{"media_id":"b","position":2,"extra":{"x":1}}],"future_key":true}}"#
        let card = try XCTUnwrap(ClipOrderCard.parse(payload: json(tolerant), eventID: "e", sequence: 1))
        XCTAssertEqual(card.orderedMediaIDs, ["a", "b"])
        XCTAssertEqual(card.items[0].source, .unknown("a_future_source"))
        XCTAssertNil(card.items[0].source.origin, "a source this build cannot name wears no badge")
        XCTAssertFalse(card.shown, "a card that does not say it is shown is not shown")
        XCTAssertFalse(card.confirmed)
        XCTAssertFalse(card.rebased)
        XCTAssertEqual(card.total, 2)
        XCTAssertNil(card.items[1].label)
        XCTAssertEqual(ClipOrderLabel.text(card.items[1]), "Clip 2")
        let sources: [(String, ClipOrderCard.Origin?)] = [("server_default", .upload), ("creator_text", .yours), ("creator_drag", .yours),
                                                           ("creator_editor", .yours), ("ai_story", .ai)]
        for (wire, origin) in sources { XCTAssertEqual(ClipOrderCard.Source(wire: wire).origin, origin, wire) }
        // Unusable cards are no card, never a crash.
        for bad in [#"{"clip_order_card":"x"}"#, #"{"clip_order_card":{"cards":[]}}"#, #"{"clip_order_card":{"proposal_id":"p","cards":[]}}"#,
                    #"{"clip_order_card":{"proposal_id":"p","cards":[{"label":"no id"},{"media_id":""}]}}"#, "{}"] {
            XCTAssertNil(ClipOrderCard.parse(payload: json(bad), eventID: "e", sequence: 1), bad)
        }
        // A repeated clip is listed once; positions order the clips, a missing position keeps the listed order.
        let repeated = #"{"clip_order_card":{"proposal_id":"p","shown":true,"cards":[{"media_id":"b","position":2},{"media_id":"a","position":1},{"media_id":"b","position":3}]}}"#
        XCTAssertEqual(ClipOrderCard.parse(payload: json(repeated), eventID: "e", sequence: 1)?.orderedMediaIDs, ["a", "b"])
    }

    func testCapabilityIsDecodedTolerantly() throws {
        func caps(_ extra: String) throws -> CreationCapabilities { try JSONDecoder().decode(CreationCapabilities.self, from: Data(#"{"formats":[]\#(extra)}"#.utf8)) }
        XCTAssertEqual(try caps(#","clip_order_cards":true"#).clipOrderCards, true)
        XCTAssertEqual(try caps(#","clip_order_cards":false"#).clipOrderCards, false)
        XCTAssertNil(try caps("").clipOrderCards, "an older server does not say")
        XCTAssertNil(try caps(#","clip_order_cards":"yes""#).clipOrderCards, "a garbled value never fails the capabilities load")
    }

    // MARK: 11: labels

    func testLabelsNeverShowAnIdentifier() throws {
        func item(_ label: String?, id: String = "m-1", position: Int = 3) -> ClipOrderCard.Item {
            ClipOrderCard.Item(mediaID: id, position: position, label: label, isPhoto: false, group: nil, source: .serverDefault, moved: false)
        }
        XCTAssertEqual(ClipOrderLabel.text(item("the map video")), "the map video")
        XCTAssertEqual(ClipOrderLabel.text(item(nil)), "Clip 3", "no label: a neutral name by position")
        XCTAssertEqual(ClipOrderLabel.text(item("  ")), "Clip 3")
        let identifiers = ["F7B6D371-1487-4E2A-9B0C-3D1E2F4A5B6C", "f7b6d3711487a2b09c3d1e2f4a5b6c7d", "IMG_1042.mov", "clip.mp4", "photo.HEIC", "m-1"]
        for label in identifiers {
            let shown = ClipOrderLabel.text(item(label))
            XCTAssertEqual(shown, "Clip 3", "\(label) must not reach the screen")
        }
        // The thread's own media names (`<UUID>-<original>`) are never consulted: the label is the server's words or "Clip N".
        let withID = ClipOrderCard.Item(mediaID: "F7B6D371-1487-4E2A-9B0C-3D1E2F4A5B6C-T2_01.mp4", position: 1, label: "the beach", isPhoto: false, group: nil, source: .serverDefault, moved: false)
        XCTAssertEqual(ClipOrderLabel.text(withID), "the beach")
        XCTAssertFalse(ClipOrderLabel.text(item(nil, id: "F7B6D371-1487-4E2A-9B0C-3D1E2F4A5B6C")).contains("F7B6"))
    }

    // MARK: 9, 10, 13: where the strip goes

    func testTheDragTurnIsNeverAChatMessage() {
        let ops = #"{"clip_order_ops":{"proposal_id":"p","ordered_media_ids":["b","a"],"confirm":true},"turn_id":"t"}"#
        // The real server stores no content; a placeholder must not show either, so the payload is what decides.
        for content in [nil, "Order updated", ""] {
            let drag = event("u1", 5, type: "user_message", role: "user", content: content, payload: ops)
            XCTAssertTrue(drag.isClipOrderDrag)
            XCTAssertNil(ChatTranscriptMessage.from(event: drag), "content \(content ?? "nil")")
        }
        let result = event("c1", 6, type: "clip_order_card", content: nil, payload: card(["b", "a"]))
        XCTAssertNil(ChatTranscriptMessage.from(event: result), "the drag's card event adds no message")
        XCTAssertNotNil(ChatTranscriptMessage.from(event: event("u2", 7, type: "user_message", role: "user", content: "make it shorter", payload: #"{"turn_id":"t2"}"#)))
        XCTAssertFalse(event("u3", 8, type: "user_message", role: "user", content: "hi", payload: nil).isClipOrderDrag)
        // The drag is not a turn the next message can be the continuation of.
        let events = [event("u0", 1, type: "user_message", role: "user", content: "make a vlog", payload: #"{"turn_id":"11111111-1111-1111-1111-111111111111"}"#),
                      event("u1", 5, type: "user_message", role: "user", content: nil, payload: #"{"clip_order_ops":{"proposal_id":"p","ordered_media_ids":[],"confirm":true},"turn_id":"22222222-2222-2222-2222-222222222222"}"#)]
        XCTAssertEqual(RequestOutcomeContext.forSubmission(events: events, continuation: false).parentTurnID?.uuidString, "11111111-1111-1111-1111-111111111111")
    }

    func testOnlyTheNewestCarrierShowsTheStrip() {
        let events = [event("d1", 1, payload: card(["a", "b", "c"])),
                      event("d2", 3, payload: card(["b", "a", "c"], proposal: "p2", moved: ["b"])),
                      event("x", 4, type: "clip_order_card", content: nil, payload: card(["c", "b", "a"], proposal: "p3"))]
        let thread = ClipOrderThread.fold(events.shuffled())
        XCTAssertEqual(thread.newest?.proposalID, "p3", "the newest card of any event is what the creator last saw")
        XCTAssertEqual(thread.newest?.orderedMediaIDs, ["c", "b", "a"])
        XCTAssertEqual(thread.anchorMessageID, "d2", "the strip sits under the newest message that carries a card")
    }

    func testAnErrorMessageThatCarriesACardAnchorsTheStrip() throws {
        let error = event("err", 4, type: "assistant_error", content: "You changed the clip order, so I cancelled the plan.",
                          payload: #"{"code":"clip_order_changed","clip_order_card":\#(cardObject(["b", "a"]))}"#)
        let thread = ClipOrderThread.fold([event("d1", 1, payload: card(["a", "b"])), error])
        XCTAssertEqual(thread.anchorMessageID, "err")
        XCTAssertEqual(thread.newest?.orderedMediaIDs, ["b", "a"])
        let message = try XCTUnwrap(ChatTranscriptMessage.from(event: error))
        XCTAssertEqual(message.role, .assistant)
        XCTAssertEqual(message.content, "You changed the clip order, so I cancelled the plan.")
    }

    // MARK: 2, 7: size and read-only

    func testNineteenAndFortyClipsReorderAndAHugeCardIsReadOnly() throws {
        for count in [19, 40] {
            let ids = (1...count).map { "clip-\($0)" }
            let parsed = try XCTUnwrap(ClipOrderCard.parse(payload: json(card(ids)), eventID: "e", sequence: 1))
            XCTAssertEqual(parsed.items.count, count)
            XCTAssertTrue(parsed.allowsEditing)
            var draft = ClipOrderDraft(order: parsed.orderedMediaIDs)
            draft.move(from: IndexSet(integer: count - 1), to: 0)
            XCTAssertEqual(draft.order.first, "clip-\(count)")
            XCTAssertEqual(Set(draft.order), Set(ids), "a move never drops or repeats a clip")
            XCTAssertEqual(draft.order.count, count)
        }
        let huge = try XCTUnwrap(ClipOrderCard.parse(payload: json(card((1...201).map { "c\($0)" })), eventID: "e", sequence: 1))
        XCTAssertFalse(huge.allowsEditing, "the wire takes 200 ids; a longer card is shown, never sent short")
        let ops = ClipOrderOps(proposalID: String(repeating: "p", count: 80), orderedMediaIDs: huge.orderedMediaIDs, confirm: true)
        XCTAssertEqual(ops.proposalID.count, 64)
        XCTAssertEqual(ops.orderedMediaIDs.count, 200)
    }

    func testReadOnlyCardAllowsNoEditing() throws {
        let locked = try XCTUnwrap(ClipOrderCard.parse(payload: json(card(["a", "b"], editable: false)), eventID: "e", sequence: 1))
        XCTAssertFalse(locked.allowsEditing)
        XCTAssertFalse(try XCTUnwrap(ClipOrderCard.parse(payload: json(card(["a", "b"], shown: false)), eventID: "e", sequence: 1)).allowsEditing)
    }

    func testDraftMovesFollowListOnMoveAndNameWhatTheCreatorMoved() {
        var draft = ClipOrderDraft(order: ["a", "b", "c", "d"])
        XCTAssertFalse(draft.isChanged)
        draft.move(from: IndexSet(integer: 0), to: 3)
        XCTAssertEqual(draft.order, ["b", "c", "a", "d"], "List.onMove's destination is an index into the old array")
        draft.moveUp("d")
        XCTAssertEqual(draft.order, ["b", "c", "d", "a"])
        draft.moveDown("b")
        XCTAssertEqual(draft.order, ["c", "b", "d", "a"])
        XCTAssertEqual(draft.moved, ["a", "d", "b"])
        XCTAssertTrue(draft.isChanged)
        XCTAssertFalse(draft.canMoveUp("c"))
        XCTAssertFalse(draft.canMoveDown("a"))
        draft.move(from: IndexSet(integer: 9), to: 0)
        XCTAssertEqual(draft.order, ["c", "b", "d", "a"], "an out-of-range move changes nothing")
    }

    // MARK: 3, 4, 5, 6: sending

    /// A transport whose answers the test controls: how many turns went out, what they carried, and when each returns.
    @MainActor private final class Wire {
        var requests: [SubmitTurnRequest] = []
        var revision = 7
        var failures: [Bool] = []
        var gates: [CheckedContinuation<Void, Never>] = []
        var holdFirst = false
        var accepted = 0

        var transport: ClipOrderCoordinator.Transport {
            .init(revision: { [self] in revision },
                  submit: { [self] request in
                      requests.append(request)
                      if holdFirst, requests.count == 1 { await withCheckedContinuation { gates.append($0) } }
                      if !failures.isEmpty, failures.removeFirst() { throw APIError.unsupported }
                      return TurnAccepted(turnID: request.clientEventID, threadRevision: 8, status: "completed")
                  },
                  accepted: { [self] _ in accepted += 1 })
        }

        func body(_ index: Int) throws -> [String: Any] {
            let data = try JSONEncoder().encode(requests[index])
            return try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
        }
    }

    @MainActor private func settle(_ coordinator: ClipOrderCoordinator, until status: ClipOrderCoordinator.Status = .idle) async {
        for _ in 0..<200 where coordinator.status != status { await Task.yield() }
    }

    @MainActor func testConfirmSendsOneWholeOrderTurn() async throws {
        let wire = Wire(), coordinator = ClipOrderCoordinator()
        let card = try parsed(["a", "b", "c"])
        coordinator.confirm(order: ["c", "a", "b"], moved: ["c"], card: card, transport: wire.transport)
        XCTAssertEqual(coordinator.display(for: card).order, ["c", "a", "b"], "the dragged order shows at once")
        XCTAssertEqual(coordinator.display(for: card).moved, ["c"])
        await settle(coordinator)
        XCTAssertEqual(wire.requests.count, 1, "exactly one turn")
        XCTAssertEqual(wire.accepted, 1)
        let body = try wire.body(0)
        XCTAssertEqual(body["message"] as? String, ClipOrderOps.placeholderMessage)
        XCTAssertEqual(body["expected_thread_revision"] as? Int, 7)
        let ops = try XCTUnwrap(body["clip_order_ops"] as? [String: Any])
        XCTAssertEqual(ops["proposal_id"] as? String, "9f2c41d0a7b3e655")
        XCTAssertEqual(ops["ordered_media_ids"] as? [String], ["c", "a", "b"], "the whole order, not a diff")
        XCTAssertEqual(ops["confirm"] as? Bool, true)
        XCTAssertNil(body["editor_state"], "a drag carries no editor state")
        XCTAssertNil(body["outcome_context"])
        XCTAssertEqual(Set(body.keys), ["message", "client_event_id", "expected_thread_revision", "clip_order_ops"])
        XCTAssertEqual(coordinator.status, .idle)
    }

    @MainActor func testDraggingWithoutConfirmingSendsNothing() async throws {
        let wire = Wire(), coordinator = ClipOrderCoordinator()
        let card = try parsed()
        coordinator.sheet = ClipOrderSheetSeed(card: card, order: card.orderedMediaIDs)
        var draft = ClipOrderDraft(order: card.orderedMediaIDs)
        draft.moveDown("a"); draft.moveDown("a")
        coordinator.sheet = nil
        await settle(coordinator)
        XCTAssertTrue(draft.isChanged)
        XCTAssertTrue(wire.requests.isEmpty, "closing the sheet without confirming sends nothing")
        XCTAssertEqual(coordinator.display(for: card).order, ["a", "b", "c"], "and the strip keeps the order the server holds")
        XCTAssertEqual(coordinator.status, .idle)
    }

    @MainActor func testRetryResendsTheSameBody() async throws {
        let wire = Wire(), coordinator = ClipOrderCoordinator()
        wire.failures = [true]
        let card = try parsed()
        coordinator.confirm(order: ["b", "a", "c"], moved: ["b"], card: card, transport: wire.transport)
        await settle(coordinator, until: .failed)
        XCTAssertEqual(coordinator.status, .failed)
        XCTAssertEqual(coordinator.display(for: card).order, ["b", "a", "c"], "the local order is kept on failure")
        wire.revision = 12
        coordinator.confirm(order: ["b", "a", "c"], moved: ["b"], card: card, transport: wire.transport)
        await settle(coordinator)
        XCTAssertEqual(wire.requests.count, 2)
        let first = try wire.body(0), retry = try wire.body(1)
        XCTAssertEqual(first["client_event_id"] as? String, retry["client_event_id"] as? String, "the same client event id")
        XCTAssertEqual(retry["expected_thread_revision"] as? Int, 7, "the revision is frozen with the body")
        XCTAssertEqual(NSDictionary(dictionary: first), NSDictionary(dictionary: retry), "the same body")
        // A different order is a new request with a new id (the same id with another body is a 409).
        wire.failures = [true]
        coordinator.confirm(order: ["c", "b", "a"], moved: ["c"], card: card, transport: wire.transport)
        await settle(coordinator, until: .failed)
        coordinator.confirm(order: ["a", "b", "c"], moved: [], card: card, transport: wire.transport)
        await settle(coordinator)
        let third = try wire.body(2), fourth = try wire.body(3)
        XCTAssertNotEqual(third["client_event_id"] as? String, first["client_event_id"] as? String)
        XCTAssertNotEqual(fourth["client_event_id"] as? String, third["client_event_id"] as? String)
    }

    @MainActor func testQuickConfirmsSendOnlyTheLastOrderOnce() async throws {
        let wire = Wire(), coordinator = ClipOrderCoordinator()
        wire.holdFirst = true
        let card = try parsed(["a", "b", "c", "d"])
        coordinator.confirm(order: ["b", "a", "c", "d"], moved: ["b"], card: card, transport: wire.transport)
        coordinator.confirm(order: ["c", "b", "a", "d"], moved: ["c"], card: card, transport: wire.transport)
        coordinator.confirm(order: ["d", "c", "b", "a"], moved: ["d"], card: card, transport: wire.transport)
        coordinator.confirm(order: ["d", "c", "b", "a"], moved: ["d"], card: card, transport: wire.transport)
        for _ in 0..<50 where wire.gates.isEmpty { await Task.yield() }
        XCTAssertEqual(wire.requests.count, 1, "one turn in flight, the rest wait")
        XCTAssertEqual(coordinator.display(for: card).order, ["d", "c", "b", "a"], "the creator sees the last order at once")
        wire.gates.forEach { $0.resume() }
        await settle(coordinator)
        XCTAssertEqual(wire.requests.count, 2, "the first in-flight turn, then only the LAST full order")
        let ops = try XCTUnwrap(try wire.body(1)["clip_order_ops"] as? [String: Any])
        XCTAssertEqual(ops["ordered_media_ids"] as? [String], ["d", "c", "b", "a"])
        XCTAssertNotEqual(try wire.body(0)["client_event_id"] as? String, try wire.body(1)["client_event_id"] as? String)
    }

    @MainActor func testTheServersNewerCardWinsOverTheOptimisticOrder() async throws {
        let wire = Wire(), coordinator = ClipOrderCoordinator()
        let old = try parsed(["a", "b", "c"])
        coordinator.confirm(order: ["c", "b", "a"], moved: ["c"], card: old, transport: wire.transport)
        await settle(coordinator)
        // The server did not apply the list as sent (rebased): its card is what shows, with its own marks, and no error.
        let rebased = try XCTUnwrap(ClipOrderCard.parse(payload: json(card(["b", "a", "c"], proposal: "p2", moved: ["b"], extra: #","rebased":true"#)), eventID: "newer", sequence: 9))
        XCTAssertTrue(rebased.rebased)
        XCTAssertEqual(coordinator.display(for: rebased).order, ["b", "a", "c"])
        XCTAssertEqual(coordinator.display(for: rebased).moved, ["b"])
        XCTAssertEqual(coordinator.status, .idle, "a rebase is not a failure")
        // A failed confirm is moot once the server's order moved on.
        wire.failures = [true]
        coordinator.confirm(order: ["a", "c", "b"], moved: ["a"], card: rebased, transport: wire.transport)
        await settle(coordinator, until: .failed)
        coordinator.serverCardChanged(to: "even-newer")
        XCTAssertEqual(coordinator.status, .idle)
        XCTAssertNil(coordinator.pendingOrder)
    }

    func testTheOutboxNeverResendsAnAcceptedOrder() {
        var outbox = ClipOrderOutbox()
        let ops = ClipOrderOps(proposalID: "p", orderedMediaIDs: ["a", "b"], confirm: true)
        let first = outbox.confirm(ops)
        XCTAssertNotNil(first)
        XCTAssertNil(outbox.confirm(ops), "the same order in flight is not sent twice")
        XCTAssertNil(outbox.succeeded())
        XCTAssertTrue(outbox.isIdle)
        XCTAssertNotEqual(outbox.confirm(ops)?.clientEventID, first?.clientEventID, "an accepted order confirmed again is a new turn")
    }

    func testTheRequestBodyCarriesTheOrderUnderTheWireNames() throws {
        let request = SubmitTurnRequest(message: "Order updated", clientEventID: "c1", expectedThreadRevision: 3,
                                        clipOrderOps: ClipOrderOps(proposalID: "p", orderedMediaIDs: ["x", "y"], confirm: false))
        let object = try XCTUnwrap(JSONSerialization.jsonObject(with: JSONEncoder().encode(request)) as? [String: Any])
        XCTAssertEqual((object["clip_order_ops"] as? [String: Any])?["confirm"] as? Bool, false)
        XCTAssertEqual((object["clip_order_ops"] as? [String: Any])?["ordered_media_ids"] as? [String], ["x", "y"])
        let plain = try XCTUnwrap(JSONSerialization.jsonObject(with: JSONEncoder().encode(SubmitTurnRequest(message: "hi", clientEventID: "c", expectedThreadRevision: 1))) as? [String: Any])
        XCTAssertNil(plain["clip_order_ops"], "every other turn is byte-identical to before")
    }
}
