import Foundation

/// Where one ask card is in its life. A card that is queued behind another or was replaced draws nothing, so
/// there is no case for it.
enum AskPhase<Answer> {
    case open
    case submitting(Answer)
    case answered(Answer)
    case failed(String)
    case stale
}

extension AskPhase: Equatable where Answer: Equatable {}

/// What the chat hands one ask card: where it is in its life, whether a send is in flight, and what the card calls
/// with the creator's answer.
struct AskCardState<Answer, Submit> {
    let phase: AskPhase<Answer>
    let isBusy: Bool
    let submit: Submit
}

enum AskKind: String, CaseIterable, Hashable {
    case clip, songOrder, songRange, region, moment, choice
}

/// Folds the ordered transcript into which ask the creator sees. Asks come one after the other: the oldest
/// unanswered ask is open, every later one waits until it is answered, and a newer ask of the same kind replaces
/// an unanswered one in its place (the server keeps only the newest question of a kind open, and mints a new
/// `question_id` each time it asks again).
struct AskFold: Equatable {
    enum Standing: Equatable {
        case open, answered, queued, replaced, stale
    }

    enum Closer: Equatable {
        /// Any later plain reply from the creator, even one still sending. A message that answers another ask is not one.
        case anyReply
        /// Any later reply the server has confirmed; a send still in flight keeps the ask open.
        case confirmedReply
        /// Only a reply carrying a structured answer with this value.
        case answer(String)
    }

    struct Ask: Equatable {
        let id: String
        let kind: AskKind
        let closer: Closer
    }

    struct AnswerKey: Hashable {
        let kind: AskKind
        let value: String
    }

    struct Entry: Equatable {
        let messageID: String
        var asks: [Ask] = []
        var isUser = false
        var isPending = false
        var answers: Set<AnswerKey> = []
    }

    private struct Key: Hashable {
        let messageID: String
        let askID: String
    }

    private var standings: [Key: Standing] = [:]
    private var answeredByMessage: [Key: String] = [:]
    private var heldMessages: Set<String> = []
    private(set) var openMessageID: String?

    func standing(_ messageID: String, _ askID: String) -> Standing? { standings[Key(messageID: messageID, askID: askID)] }

    func answeredBy(_ messageID: String, _ askID: String) -> String? { answeredByMessage[Key(messageID: messageID, askID: askID)] }

    func holdsRow(_ messageID: String) -> Bool { heldMessages.contains(messageID) }

    /// `closed` holds question ids the server said are no longer open; an unanswered one leaves the queue.
    static func fold(_ entries: [Entry], closed: Set<String> = []) -> AskFold {
        var fold = AskFold()
        var queue: [(key: Key, ask: Ask)] = []
        var askKeys: [String: [Key]] = [:]
        for entry in entries {
            if entry.isUser {
                let head = queue.first?.key.messageID
                let isReply = entry.answers.isEmpty
                queue.removeAll { slot in
                    let closes: Bool
                    switch slot.ask.closer {
                    case .anyReply: closes = isReply && slot.key.messageID == head
                    case .confirmedReply: closes = isReply && slot.key.messageID == head && !entry.isPending
                    case .answer(let value): closes = entry.answers.contains(AnswerKey(kind: slot.ask.kind, value: value))
                    }
                    guard closes else { return false }
                    fold.standings[slot.key] = .answered
                    fold.answeredByMessage[slot.key] = entry.messageID
                    return true
                }
            } else {
                for ask in entry.asks {
                    let key = Key(messageID: entry.messageID, askID: ask.id)
                    askKeys[entry.messageID, default: []].append(key)
                    if let index = queue.firstIndex(where: { $0.ask.kind == ask.kind }) {
                        fold.standings[queue[index].key] = .replaced
                        queue[index] = (key, ask)
                    } else {
                        queue.append((key, ask))
                    }
                }
            }
        }
        queue.removeAll { slot in
            guard closed.contains(slot.ask.id) else { return false }
            fold.standings[slot.key] = .stale
            return true
        }
        fold.openMessageID = queue.first?.key.messageID
        for slot in queue {
            fold.standings[slot.key] = slot.key.messageID == fold.openMessageID ? .open : .queued
        }
        fold.heldMessages = Set(askKeys.compactMap { message, keys in
            keys.allSatisfy { fold.standings[$0] == .queued } ? message : nil
        })
        return fold
    }
}

extension AskFold {
    /// What the card for `askID` on `messageID` draws, or nil when it draws nothing: it is queued behind another ask,
    /// was replaced or closed, or is answered by something the card cannot name. `failure` is the server's reason for
    /// refusing the last answer; it wins over `isStale`, which only says the list under the card was refreshed.
    func phase<Answer>(_ messageID: String, _ askID: String, answer: () -> Answer?, failure: String? = nil,
                       isStale: Bool = false) -> AskPhase<Answer>? {
        switch standing(messageID, askID) {
        case .open:
            if let failure { return .failed(failure) }
            return isStale ? .stale : .open
        case .answered:
            return answer().map(AskPhase.answered)
        default:
            return nil
        }
    }
}
