import SwiftUI

/// How a conflict-choice question is shown under its message (KRI-282).
enum ChoiceQuestionCardMode {
    /// The latest unanswered question: tappable. `submit` receives the structured answer and its readable message.
    case active(isSending: Bool, submit: (ChoiceSelectionSubmission, String) -> Void)
    /// Already answered (or superseded): read-only, collapsed. `summary` is what the creator chose.
    case answered(summary: String?)
}

/// Tappable options for a question whose answer is a decision between the creator's own instructions.
/// The message text above already lists the options, so a build/server without this card reads the same.
struct ChoiceQuestionCard: View {
    let question: ChoiceQuestion
    let mode: ChoiceQuestionCardMode

    var body: some View {
        switch mode {
        case .answered(let summary):
            HStack(spacing: 8) {
                Image(systemName: "checkmark.circle.fill").foregroundStyle(KriaColor.success)
                Text(summary.map { "You chose: \($0)" } ?? "You answered")
                    .font(KriaFont.body(13).weight(.medium)).foregroundStyle(KriaColor.mutedInk)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .frame(minHeight: 44, alignment: .leading)
            .accessibilityElement(children: .combine)
            .accessibilityIdentifier("choice-card-answered")
        case .active(let isSending, let submit):
            VStack(alignment: .leading, spacing: 8) {
                ForEach(question.options) { option in
                    QuestionOptionButton(
                        text: option.label,
                        isRecommended: option.recommended,
                        detail: option.detail,
                        isDisabled: isSending,
                        action: { submit(question.submission(for: option), question.message(for: option)) }
                    )
                    .accessibilityIdentifier("choice-option-\(option.key)")
                }
            }
            .padding(.top, 2)
            .accessibilityElement(children: .contain)
            .accessibilityIdentifier("choice-card")
        }
    }
}
