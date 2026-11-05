import Foundation

/// Steer now (Green 5): a message the creator sends while Kria is still working waits behind that work (the server's
/// one queue slot). Like Codex, it waits in a bar attached to the top of the input box, not in the chat: "Steer"
/// stops the earlier turn so Kria works on it now, the trash removes it, and "⋯" offers Edit / Steer instead / Cancel.
/// The thread projection carries no turn status, so every decision here is read from the events, the way the web reads
/// its queued message. The server re-checks every Steer and refuses one that cannot happen, so a wrong guess here only
/// costs a hidden button. docs/pipelines/steer-now.md.
enum SteerNowPolicy {
    struct Waiting: Equatable, Sendable {
        /// The waiting message's own turn.
        let turnID: String
        /// The turn it waits behind (`queued_behind_turn_id`); nil from a server without Steer now.
        let behindTurnID: String?
        /// The `user_message` event (hidden from the chat while it waits).
        let messageEventID: String
        /// The creator's words, shown in the bar and put back in the box by Edit.
        let text: String
    }

    /// Once the earlier turn reached one of these, its video is being made or it is over: no Steer.
    private static let earlierTurnSettledTypes: Set<String> = [
        "approval_approved", "render_queued", "render_started", "rendering", "plan_block", "generation_started",
        "turn_cancelled", "approval_denied", "approval_expired", "assistant_error", "agent_assistant_error",
        "assistant_render_failed", "agent_assistant_render_failed",
    ]

    /// Turn ids compare without case (the server and older fixtures differ); the original spelling is what is sent.
    private static func turnID(_ event: ThreadEvent, _ key: String = "turn_id") -> String? {
        event.payload?[key]?.stringValue
    }

    private static func same(_ lhs: String?, _ rhs: String?) -> Bool {
        guard let lhs, let rhs else { return false }
        return lhs.caseInsensitiveCompare(rhs) == .orderedSame
    }

    private static func isQueuedMessage(_ event: ThreadEvent) -> Bool {
        event.role == "user" && event.eventType == "user_message" && event.payload?["turn_status"]?.stringValue == "queued"
    }

    /// True once the creator took the message back (the trash or Edit): the server marks the cancel.
    private static func wasRemoved(_ turn: String, in events: [ThreadEvent]) -> Bool {
        events.contains {
            $0.eventType == "turn_cancelled" && same(turnID($0), turn) && $0.payload?["removed_while_waiting"] == .bool(true)
        }
    }

    /// The message waiting behind a running turn, or nil. It stops waiting as soon as its own turn says anything, a
    /// Steer handed over to it, or the creator removed it.
    static func waiting(in events: [ThreadEvent]) -> Waiting? {
        guard let queued = events.last(where: isQueuedMessage), let turn = turnID(queued) else { return nil }
        let later = events.filter { $0.sequence > queued.sequence }
        if later.contains(where: { same(turnID($0), turn) || same(turnID($0, "steered_to_turn_id"), turn) }) { return nil }
        return Waiting(turnID: turn, behindTurnID: turnID(queued, "queued_behind_turn_id"), messageEventID: queued.id,
                       text: (queued.content ?? "").trimmingCharacters(in: .whitespacesAndNewlines))
    }

    /// The chat leaves out the message that waits in the bar (it would show twice) and any message the creator took
    /// back. A message that was steered, or that Kria picked up on its own, joins the chat.
    static func hiddenMessageEventIDs(in events: [ThreadEvent]) -> Set<String> {
        var hidden = Set<String>()
        if let waiting = waiting(in: events) { hidden.insert(waiting.messageEventID) }
        for event in events where isQueuedMessage(event) {
            if let turn = turnID(event), wasRemoved(turn, in: events) { hidden.insert(event.id) }
        }
        return hidden
    }

    /// What the trash / Edit does after a stale-revision answer and a refresh. Kria may have picked the message up in
    /// between (its own turn spoke, a Steer handed over to it, or a newer message now waits), or it may already be
    /// gone. Only a message that still waits is tried again, and the server re-checks that (`only_if_queued`).
    enum RemovalRetry: Equatable { case retry, alreadyStarted, alreadyRemoved }

    static func removalRetry(for removing: Waiting, in events: [ThreadEvent]) -> RemovalRetry {
        if same(waiting(in: events)?.turnID, removing.turnID) { return .retry }
        return wasRemoved(removing.turnID, in: events) ? .alreadyRemoved : .alreadyStarted
    }

    /// Whether to offer Steer: the earlier turn is still planning or its card is still up.
    static func canSteer(_ waiting: Waiting, events: [ThreadEvent]) -> Bool {
        guard let behind = waiting.behindTurnID else { return false }
        let own = events.filter { same(turnID($0), behind) }
        if own.contains(where: { earlierTurnSettledTypes.contains($0.eventType) }) { return false }
        // Answered with words and no card (a question, a reply): that turn is over.
        let answered = own.contains { ChatTranscriptMessage.from(event: $0)?.role == .assistant }
        let carded = own.contains { $0.eventType == "approval_requested" }
        return !answered || carded
    }
}

/// The words of the waiting-message bar, in the chat's language (never the phone's).
struct SteerStrings {
    let language: ClipAnswerLanguage
    init(_ language: ClipAnswerLanguage) { self.language = language }

    private func say(_ en: String, _ tr: String) -> String { language == .turkish ? tr : en }

    var steer: String { say("Steer", "Yönlendir") }
    var steerHint: String {
        say("Stops what Kria is doing and works on this message instead.",
            "Kria'nın şu an yaptığını durdurur ve bunun yerine bu mesaj üzerinde çalışır.")
    }
    var steerInstead: String { say("Steer instead", "Yönlendir") }
    var edit: String { say("Edit message", "Mesajı düzenle") }
    var cancel: String { say("Cancel message", "Mesajı iptal et") }
    var delete: String { say("Delete waiting message", "Bekleyen mesajı sil") }
    var more: String { say("More options", "Diğer seçenekler") }
    func waitingLabel(_ text: String) -> String { say("Waiting message: \(text)", "Bekleyen mesaj: \(text)") }
    var waitingHint: String {
        say("Kria reads this after the current step.", "Kria bunu şu anki adımdan sonra okur.")
    }
    var oneAlreadyWaiting: String {
        say("One message is already waiting. Steer it, or send this after Kria answers.",
            "Zaten bekleyen bir mesaj var. Onu yönlendir ya da bunu Kria yanıtladıktan sonra gönder.")
    }
    var videoAlreadyStarted: String {
        say("Your video is already being made. Kria reads your message next.",
            "Videon zaten hazırlanıyor. Kria mesajını sonra okuyacak.")
    }
    var couldNotSwitch: String {
        say("Kria couldn’t switch to your newer message. It is still waiting.",
            "Kria yeni mesajına geçemedi. Mesajın hâlâ bekliyor.")
    }
    var alreadyStarted: String {
        say("Kria already started on this message.", "Kria bu mesaja çoktan başladı.")
    }
    var couldNotRemove: String {
        say("Kria couldn’t remove that message. Try again.", "Kria bu mesajı kaldıramadı. Tekrar dene.")
    }
}
