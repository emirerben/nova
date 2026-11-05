import SwiftUI

/// The live buttons a problem card can carry. A card shows only the ones its outcome uses, and leaves a
/// control out entirely when the screen has no handler for it.
struct OverlayProblemHandlers {
    var tryAgain: (() -> Void)?
    var editPrompt: (() -> Void)?
    var checkAgain: (() -> Void)?
    var saveAgain: (() -> Void)?
    var startAgain: (() -> Void)?
    var close: (() -> Void)?
}

/// What the generated thing is called in a notice. Overlays, a single edited image and the AI voice share
/// the six outcomes and differ only in these words.
struct OverlayProblemNoun {
    let singular: String
    let plural: String
    var hasPrompt = true

    static let overlay = OverlayProblemNoun(singular: "overlay", plural: "overlays")
    static let voice = OverlayProblemNoun(singular: "voice", plural: "voices", hasPrompt: false)
}

/// One `KriaNoticeCard` per outcome, in one first-person voice (Paper S1.09). Red cards carry an ink button;
/// butter stays on the sheet's own primary.
struct OverlayProblemCard: View {
    let problem: OverlayProblem
    var noun: OverlayProblemNoun = .overlay
    var handlers = OverlayProblemHandlers()
    @Environment(\.openURL) private var openURL

    var body: some View {
        KriaNoticeCard(tone: tone, title: title, message: message, action: action, link: link)
            .accessibilityIdentifier("generated-media-problem")
    }

    private var tone: KriaNoticeCard.Tone {
        switch problem {
        case .unsure, .offline: .info
        case .changedAfterStart, .sourceGone: .warning
        default: .failure
        }
    }

    private var title: String {
        switch problem {
        case .couldNotMake(let failed, let total):
            if total <= 1 { return "I couldn’t make this \(noun.singular)" }
            return failed == total ? "I couldn’t make your \(noun.plural)" : "I couldn’t make \(failed) of your \(total) \(noun.plural)"
        case .unsure: return "I’m not sure it finished"
        case .didNotSave(let count):
            return count > 1 ? "\(count) \(noun.plural) are made, but didn’t save" : "Your \(noun.singular) is made, but it didn’t save"
        case .changedAfterStart: return "This \(noun.singular) changed after you started"
        case .sourceGone: return "This \(noun.singular) is no longer in your video"
        case .sessionEnded: return "Your session ended"
        case .updateRequired: return "Update Kria to make \(noun.plural)"
        case .offline: return "You’re offline"
        case .couldNotAdd: return "I couldn’t add your \(noun.plural)"
        }
    }

    private var message: String {
        switch problem {
        case .couldNotMake(let failed, let total):
            if !noun.hasPrompt { return "Your script is still here. Try again." }
            if total <= 1 || failed == total { return total <= 1 ? "Your prompt is still here. Try again or change it." : "Your prompts are still here. Try again or change a prompt." }
            let ready = total - failed
            return "The other \(ready) \(ready == 1 ? "is" : "are") ready. Each prompt is still here. Try \(failed == 1 ? "the missing one" : "the missing ones") again or change \(failed == 1 ? "its" : "their") prompt."
        case .unsure: return "I won’t start another one on my own. Check again in a minute."
        case .didNotSave: return "Saving again won’t make a new \(noun.singular)."
        case .changedAfterStart: return "Discard this version and start again from the newest one."
        case .sourceGone: return "Close this and choose another image."
        case .sessionEnded: return "Sign in again and I’ll pick up where we left off."
        case .updateRequired: return "This version can’t make new images."
        case .offline: return "Check again when you’re connected."
        case .couldNotAdd: return "Your \(noun.plural) are saved. Try again in a moment."
        }
    }

    private var action: KriaNoticeAction? {
        switch problem {
        case .couldNotMake: handlers.tryAgain.map { KriaNoticeAction(title: "Try again", handler: $0) }
        case .unsure, .offline: handlers.checkAgain.map { KriaNoticeAction(title: "Check again", handler: $0) }
        case .didNotSave: handlers.saveAgain.map { KriaNoticeAction(title: "Save again", handler: $0) }
        case .changedAfterStart: handlers.startAgain.map { KriaNoticeAction(title: "Start again", handler: $0) }
        case .sourceGone: handlers.close.map { KriaNoticeAction(title: "Close", handler: $0) }
        case .sessionEnded: KriaNoticeAction(title: "Sign in") { NotificationCenter.default.post(name: .kriaSessionExpired, object: nil) }
        case .updateRequired: KriaNoticeAction(title: "Update Kria") { openURL(KriaUpdateLink.destination) }
        case .couldNotAdd: handlers.tryAgain.map { KriaNoticeAction(title: "Try again", handler: $0) }
        }
    }

    private var link: KriaNoticeAction? {
        guard case .couldNotMake = problem, noun.hasPrompt else { return nil }
        return handlers.editPrompt.map { KriaNoticeAction(title: "Edit prompt", handler: $0) }
    }
}
