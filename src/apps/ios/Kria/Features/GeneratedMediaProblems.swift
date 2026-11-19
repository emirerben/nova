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

    static let overlay = OverlayProblemNoun(singular: String(localized: "overlay"), plural: String(localized: "overlays"))
    static let voice = OverlayProblemNoun(singular: String(localized: "voice"), plural: String(localized: "voices"), hasPrompt: false)
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
            if total <= 1 { return String(localized: "I couldn’t make this \(noun.singular)") }
            return failed == total ? String(localized: "I couldn’t make your \(noun.plural)") : String(localized: "I couldn’t make \(failed) of your \(total) \(noun.plural)")
        case .unsure: return String(localized: "I’m not sure it finished")
        case .didNotSave(let count):
            return count > 1 ? String(localized: "\(count) \(noun.plural) are made, but didn’t save") : String(localized: "Your \(noun.singular) is made, but it didn’t save")
        case .changedAfterStart: return String(localized: "This \(noun.singular) changed after you started")
        case .sourceGone: return String(localized: "This \(noun.singular) is no longer in your video")
        case .sessionEnded: return String(localized: "Your session ended")
        case .updateRequired: return String(localized: "Update Kria to make \(noun.plural)")
        case .offline: return String(localized: "You’re offline")
        case .couldNotAdd: return String(localized: "I couldn’t add your \(noun.plural)")
        }
    }

    private var message: String {
        switch problem {
        case .couldNotMake(let failed, let total):
            if !noun.hasPrompt { return String(localized: "Your script is still here. Try again.") }
            if total <= 1 || failed == total { return total <= 1 ? String(localized: "Your prompt is still here. Try again or change it.") : String(localized: "Your prompts are still here. Try again or change a prompt.") }
            let ready = total - failed
            let readyPart = ready == 1 ? String(localized: "The other one is ready.") : String(localized: "The other \(ready) are ready.")
            let retryPart = failed == 1 ? String(localized: "Each prompt is still here. Try the missing one again or change its prompt.") : String(localized: "Each prompt is still here. Try the missing ones again or change their prompt.")
            return readyPart + " " + retryPart
        case .unsure: return String(localized: "I won’t start another one on my own. Check again in a minute.")
        case .didNotSave: return String(localized: "Saving again won’t make a new \(noun.singular).")
        case .changedAfterStart: return String(localized: "Discard this version and start again from the newest one.")
        case .sourceGone: return String(localized: "Close this and choose another image.")
        case .sessionEnded: return String(localized: "Sign in again and I’ll pick up where we left off.")
        case .updateRequired: return String(localized: "This version can’t make new images.")
        case .offline: return String(localized: "Check again when you’re connected.")
        case .couldNotAdd: return String(localized: "Your \(noun.plural) are saved. Try again in a moment.")
        }
    }

    private var action: KriaNoticeAction? {
        switch problem {
        case .couldNotMake: handlers.tryAgain.map { KriaNoticeAction(title: String(localized: "Try again"), handler: $0) }
        case .unsure, .offline: handlers.checkAgain.map { KriaNoticeAction(title: String(localized: "Check again"), handler: $0) }
        case .didNotSave: handlers.saveAgain.map { KriaNoticeAction(title: String(localized: "Save again"), handler: $0) }
        case .changedAfterStart: handlers.startAgain.map { KriaNoticeAction(title: String(localized: "Start again"), handler: $0) }
        case .sourceGone: handlers.close.map { KriaNoticeAction(title: String(localized: "Close"), handler: $0) }
        case .sessionEnded: KriaNoticeAction(title: String(localized: "Sign in")) { NotificationCenter.default.post(name: .kriaSessionExpired, object: nil) }
        case .updateRequired: KriaNoticeAction(title: String(localized: "Update Kria")) { openURL(KriaUpdateLink.destination) }
        case .couldNotAdd: handlers.tryAgain.map { KriaNoticeAction(title: String(localized: "Try again"), handler: $0) }
        }
    }

    private var link: KriaNoticeAction? {
        guard case .couldNotMake = problem, noun.hasPrompt else { return nil }
        return handlers.editPrompt.map { KriaNoticeAction(title: String(localized: "Edit prompt"), handler: $0) }
    }
}
