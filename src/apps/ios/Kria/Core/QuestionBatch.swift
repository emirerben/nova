import Foundation

/// KRI-802: one reply that asks a choice card and a clip picker together (servers send it to protocol 14 builds).
///
/// Neither answer is sent on its own: each waits here until both are in, then ONE turn carries both
/// (`choice_selection` + `clip_selection`). Each turn re-plans the whole edit, so two separate answers would make
/// the creator wait twice.
struct QuestionBatch: Equatable {
    private(set) var choice: ChoiceSelectionSubmission?
    private(set) var choiceText: String?
    private(set) var clips: ClipSelectionSubmission?
    private(set) var clipsText: String?

    mutating func hold(choice: ChoiceSelectionSubmission, text: String) {
        self.choice = choice
        choiceText = text
    }

    mutating func hold(clips: ClipSelectionSubmission, text: String) {
        self.clips = clips
        clipsText = text
    }

    /// Both answers as the one message to send, or nil while one is still missing.
    var ready: (message: String, choice: ChoiceSelectionSubmission, clips: ClipSelectionSubmission)? {
        guard let choice, let choiceText, let clips, let clipsText else { return nil }
        return ("\(choiceText) · \(clipsText)", choice, clips)
    }

    /// Under the choice card once it is answered and the clips are not.
    var choiceNote: String? {
        choice != nil && clips == nil ? "Now choose the clips below. I'll send both answers together." : nil
    }

    /// Under the clip card once the clips are chosen and the choice is not.
    var clipsNote: String? {
        clips != nil && choice == nil ? "Clips saved. Answer the question above and I'll send both." : nil
    }
}
