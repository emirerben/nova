#if DEBUG
import CryptoKit
import Foundation

/// CA-6 (`KRIA_CHAT_RESEARCH_SELECTION`): a server that offers the pick of sourced facts, shaped like
/// `app.kria.research_selection` and `research_preview.preview_payload(with_ids=True)`: a 64-hex `selection_id`, a 16-hex
/// `fact_id` per row, and on a narrowing the server's own event order (`user_message`, `approval_cancelled`,
/// `draft_applied` with `research_selection`, `approval_requested`).
///
/// Modes: "1" = every answer is taken; "stale" = the first answer meets a newer card (409 `research_selection_stale`);
/// "fail-once" = the first answer is lost on the network, and its retry must be the same body; "long-tr" = long Turkish facts.
/// `KRIA_CHAT_SEND_DELAY` holds the answer.
struct ResearchSelectionUITestFixture {
    static var mode: String? { ProcessInfo.processInfo.environment["KRIA_CHAT_RESEARCH_SELECTION"] }

    struct Fact {
        let id: String
        let text: String
        let start: Double
        let end: Double
        let title: String
        let url: String
    }

    static var facts: [Fact] {
        if mode == "long-tr" {
            return [
                Fact(id: "9c1e4a7b2d5f8036", text: "Galata Kulesi, 1348 yılında Cenevizliler tarafından Hristiyanlık Kulesi adıyla, surların en yüksek noktasına, şehrin Haliç kıyısındaki ticaret kolonisini gözetlemek ve savunmak için inşa edilmiştir.", start: 3, end: 7.5, title: "Galata Kulesi", url: "https://www.britannica.com/topic/Galata-Tower"),
                Fact(id: "e4b7a1c93d08f562", text: "Kule, yüzyıllar boyunca İstanbul’un en yüksek yapılarından biri olarak kaldı ve gemilerin Boğaz’a girerken yönünü bulmasına yardım eden bir gözetleme noktası olarak da kullanıldı.", start: 12, end: 16.5, title: "Galata Tower history", url: "https://www.history.com/topics/galata-tower"),
                Fact(id: "07d3f9a5c2e1b684", text: "Konik çatı, 1875 yılındaki büyük onarımdan sonra eklendi; bugünkü siluetini veren bu çatı kulenin en tanınmış özelliklerinden biridir ve her yıl milyonlarca ziyaretçi çeker.", start: 21, end: 25.5, title: "Galata Tower", url: "https://en.wikipedia.org/wiki/Galata_Tower"),
            ]
        }
        return [
            Fact(id: "9c1e4a7b2d5f8036", text: "Galata Tower was built in 1348 by the Genoese.", start: 3, end: 7.5, title: "Galata Tower | History", url: "https://www.britannica.com/topic/Galata-Tower"),
            Fact(id: "e4b7a1c93d08f562", text: "It stayed among the tallest structures in Istanbul for centuries.", start: 12, end: 16.5, title: "Galata Tower history", url: "https://www.history.com/topics/galata-tower"),
            Fact(id: "07d3f9a5c2e1b684", text: "Its conical cap was added after a restoration in 1875.", start: 21, end: 25.5, title: "Galata Tower", url: "https://en.wikipedia.org/wiki/Galata_Tower"),
        ]
    }

    static func selectionID(_ ids: [String]) -> String {
        SHA256.hash(data: Data((["fixture"] + ids).joined(separator: "|").utf8)).map { String(format: "%02x", $0) }.joined()
    }

    /// `research_preview.preview_payload(prepared, with_ids=True)` for the facts `ids` names.
    static func preview(_ ids: [String]) -> [String: Any] {
        [
            "version": 1, "retrieved_at": "2026-11-26T09:00:00Z", "selection_id": selectionID(ids),
            "explanations": facts.filter { ids.contains($0.id) }.map { fact -> [String: Any] in
                ["fact_id": fact.id, "text": fact.text, "start_s": fact.start, "end_s": fact.end,
                 "sources": [["title": fact.title, "url": fact.url]]]
            },
        ]
    }

    enum Outcome {
        case lost
        case problem(status: Int, code: String, message: String)
        case replayed(turnID: String)
        case narrowed(Narrowed)
    }

    struct Narrowed {
        let receipt: [String: Any]
        let preview: [String: Any]?
        let reply: String
        let turnID: String
        let approvalID: String
        let cancelledApprovalID: String
        let cancelledTurnID: String
        /// A refused answer that left a newer draft behind (the card the creator answered was no longer the draft's card).
        let refused: (code: String, message: String)?
    }

    private var current = facts.map(\.id)
    private var turnID = ""
    private var approvalID = ""
    private var seen: [String: String] = [:]
    private var lostKey: String?
    private var lostDigest: String?
    private var staleServed = false
    private var counter = 0

    mutating func begin(turnID: String, approvalID: String) {
        guard self.turnID.isEmpty else { return }
        self.turnID = turnID
        self.approvalID = approvalID
        current = Self.facts.map(\.id)
    }

    private mutating func nextIDs() -> (turn: String, approval: String) {
        counter += 1
        let suffix = String(format: "%012d", counter)
        return ("00000000-0000-4000-8000-\(suffix)", "00000000-0000-4000-9000-\(suffix)")
    }

    var latestApprovalID: String { approvalID }

    mutating func accept(_ selection: [String: Any], clientEventID: String, body: [String: Any]) -> Outcome {
        let digest = (try? JSONSerialization.data(withJSONObject: body, options: [.sortedKeys])).flatMap { String(data: $0, encoding: .utf8) } ?? ""
        if let earlier = seen[clientEventID] {
            return earlier == digest ? .replayed(turnID: turnID)
                : .problem(status: 409, code: "idempotency_key_reused", message: "That request id was used with a different body.")
        }
        if Self.mode == "fail-once", lostKey == nil {
            lostKey = clientEventID
            lostDigest = digest
            return .lost
        }
        if clientEventID == lostKey, digest != lostDigest {
            return .problem(status: 409, code: "idempotency_key_reused", message: "That request id was used with a different body.")
        }
        let asked = selection["selection_id"] as? String ?? ""
        let kept = (selection["kept_fact_ids"] as? [String] ?? []).filter(current.contains)
        let wanted = selection["kept_fact_ids"] as? [String] ?? []
        if Self.mode == "stale", !staleServed {
            staleServed = true
            let ids = nextIDs()
            let older = (turn: turnID, approval: approvalID)
            current = Array(current.prefix(2))
            turnID = ids.turn
            approvalID = ids.approval
            return .narrowed(Narrowed(receipt: [:], preview: Self.preview(current), reply: "", turnID: ids.turn, approvalID: ids.approval,
                                      cancelledApprovalID: older.approval, cancelledTurnID: older.turn,
                                      refused: ("research_selection_stale", "These facts are from a card this draft no longer shows.")))
        }
        guard asked == Self.selectionID(current) else {
            return .problem(status: 409, code: "research_selection_stale", message: "These facts are from a card this draft no longer shows.")
        }
        if !wanted.isEmpty, kept.isEmpty {
            return .problem(status: 409, code: "research_selection_stale", message: "None of these facts are in this draft any more.")
        }
        seen[clientEventID] = digest
        if kept == current && !wanted.isEmpty { return .replayed(turnID: turnID) }
        let ids = nextIDs()
        let older = (turn: turnID, approval: approvalID)
        let waived = kept.isEmpty
        let receipt: [String: Any] = ["selection_id": asked, "kept_fact_ids": kept, "waived": waived]
        let total = current.count
        current = kept
        turnID = ids.turn
        approvalID = ids.approval
        let reply = waived ? "Continuing without the sourced facts." : "Kept \(kept.count) of \(total) sourced facts."
        return .narrowed(Narrowed(receipt: receipt, preview: waived ? nil : Self.preview(kept), reply: reply, turnID: ids.turn, approvalID: ids.approval,
                                  cancelledApprovalID: older.approval, cancelledTurnID: older.turn, refused: nil))
    }
}
#endif
