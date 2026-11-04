import Foundation

/// Steer now (Green 5): a message the creator sends while Kria is still working waits as "Up next" behind that work
/// (the server's one queue slot), with a "Steer now" button that stops the earlier turn so Kria works on the waiting
/// message instead. The thread projection carries no turn status, so both decisions are read from the events, the way
/// the web reads its queued message. The server re-checks every Steer and refuses one that cannot happen, so a wrong
/// guess here only costs a hidden button. docs/pipelines/steer-now.md.
enum SteerNowPolicy {
    struct Waiting: Equatable, Sendable {
        /// The waiting message's own turn.
        let turnID: String
        /// The turn it waits behind (`queued_behind_turn_id`); nil from a server without Steer now.
        let behindTurnID: String?
        /// The `user_message` event the "Up next" row sits under.
        let messageEventID: String
    }

    /// Once the earlier turn reached one of these, its video is being made or it is over: no Steer now.
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

    /// The message waiting behind a running turn, or nil. It stops waiting as soon as its own turn says anything, or a
    /// Steer handed over to it.
    static func waiting(in events: [ThreadEvent]) -> Waiting? {
        guard let queued = events.last(where: {
            $0.role == "user" && $0.eventType == "user_message" && $0.payload?["turn_status"]?.stringValue == "queued"
        }), let turn = turnID(queued) else { return nil }
        let later = events.filter { $0.sequence > queued.sequence }
        if later.contains(where: { same(turnID($0), turn) || same(turnID($0, "steered_to_turn_id"), turn) }) { return nil }
        return Waiting(turnID: turn, behindTurnID: turnID(queued, "queued_behind_turn_id"), messageEventID: queued.id)
    }

    /// Whether to offer Steer now: the earlier turn is still planning or its card is still up.
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

/// The words of the Up next row, in the chat's language (never the phone's).
struct SteerStrings {
    let language: ClipAnswerLanguage
    init(_ language: ClipAnswerLanguage) { self.language = language }

    private func say(_ en: String, _ tr: String) -> String { language == .turkish ? tr : en }

    var upNext: String { say("Up next", "Sırada") }
    var upNextHint: String {
        say("Kria reads this message after the current step.", "Kria bu mesajı şu anki adımdan sonra okur.")
    }
    var steerNow: String { say("Steer now", "Şimdi buna geç") }
    var steerNowHint: String {
        say("Stops what Kria is doing and works on this message instead.",
            "Kria'nın şu an yaptığını durdurur ve bunun yerine bu mesaj üzerinde çalışır.")
    }
    var steering: String { say("Switching…", "Geçiliyor…") }
    var oneAlreadyWaiting: String {
        say("One message is already up next. Tap Steer now, or send this after Kria answers.",
            "Sırada zaten bir mesaj var. Şimdi buna geç'e dokun ya da bunu Kria yanıtladıktan sonra gönder.")
    }
    var videoAlreadyStarted: String {
        say("Your video is already being made. Kria reads your message next.",
            "Videon zaten hazırlanıyor. Kria mesajını sonra okuyacak.")
    }
    var couldNotSwitch: String {
        say("Kria couldn’t switch to your newer message. It is still up next.",
            "Kria yeni mesajına geçemedi. Mesajın hâlâ sırada.")
    }
}
