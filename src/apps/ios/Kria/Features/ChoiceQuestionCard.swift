import SwiftUI

/// How a conflict-choice question is shown under its message (KRI-282).
enum ChoiceQuestionCardMode {
    /// The latest unanswered question: tappable. `submit` receives the structured answer and its readable message.
    /// While the answer's message is still pending the card stays here with `isSending` true, so a refused send
    /// reopens it with a failure line instead of silently flipping back.
    case active(isSending: Bool, submit: (ChoiceSelectionSubmission, String) -> Void)
    /// Already answered: read-only, collapsed. `summary` is what the creator chose.
    case answered(summary: String?)
    /// A newer question replaced this one before it was answered.
    case superseded
}

/// Tappable options for a question whose answer is a decision between the creator's own instructions.
/// The message text above already lists the options, so a build/server without this card reads the same.
struct ChoiceQuestionCard: View {
    let question: ChoiceQuestion
    let mode: ChoiceQuestionCardMode
    /// KRI-802: in a reply that also asks for clips, the option tapped but not sent yet (it goes with the clips).
    var heldKey: String? = nil
    /// KRI-802: what happens next while the tapped option waits for the clips.
    var note: String? = nil
    /// KRI-802: undoes the held option so another can be picked.
    var changeHeld: (() -> Void)? = nil
    /// Hands the creator the composer to answer in their own words; nil hides the row.
    var typeMyOwn: (() -> Void)? = nil
    /// The creator is typing their own answer, so the blocks are disabled.
    var isTyping = false
    /// False for a reply that asks the clips too: Kria's decision then goes with the clips question.
    var allowsDecide = true

    var body: some View {
        switch mode {
        case .answered(let summary):
            Group {
                if let summary { KriaYouChose(text: summary) } else { KriaAnsweredReceipt(text: "You answered") }
            }
            .accessibilityIdentifier("choice-card-answered")
        case .superseded:
            KriaSupersededLine().accessibilityIdentifier("choice-card-superseded")
        case .active(let isSending, let submit):
            VStack(alignment: .leading, spacing: 8) {
                KriaAnswerStack(
                    options: question.options.map {
                        KriaAnswerOption(id: $0.key, title: $0.label, detail: $0.detail, isRecommended: $0.recommended,
                                         identifier: "choice-option-\($0.key)")
                    },
                    heldID: heldKey, isSending: isSending, isLocked: isTyping,
                    typeMyOwn: question.allowFreeText ? typeMyOwn : nil,
                    decide: decideOption.map { option in { submit(question.submission(for: option), "Let Kria decide") } }
                ) { option in
                    if let chosen = question.option(key: option.id) { submit(question.submission(for: chosen), question.message(for: chosen)) }
                }
                if let note {
                    // The note, then "Change" under it on the left (Paper S2.07).
                    VStack(alignment: .leading, spacing: 0) {
                        Text(note).kriaType(.label).foregroundStyle(KriaColor.ink)
                            .fixedSize(horizontal: false, vertical: true)
                            .accessibilityIdentifier("choice-batch-note")
                        if let changeHeld {
                            Button("Change", action: changeHeld)
                                .buttonStyle(KriaButtonStyle(role: .tertiary, size: .medium))
                                .padding(.leading, -12)
                                .accessibilityIdentifier("choice-batch-change")
                        }
                    }
                }
            }
            .padding(.top, 2)
            .accessibilityElement(children: .contain)
            .accessibilityIdentifier("choice-card")
        }
    }

    /// "Let Kria decide" takes the option Kria recommends, so the server still receives a structured answer.
    /// Hidden when nothing is recommended and in a reply that asks the clips too.
    private var decideOption: ChoiceQuestion.Option? {
        allowsDecide && heldKey == nil ? question.options.first(where: \.recommended) : nil
    }
}
