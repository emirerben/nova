import SwiftUI

/// Tappable options for a question whose answer is a decision between the creator's own instructions.
/// The message text above already lists the options, so a build/server without this card reads the same.
struct ChoiceQuestionCard: View {
    let question: ChoiceQuestion
    /// Open to answer, or answered (`answered` carries what the creator chose). A failed send reopens the card with its
    /// own failure line and "Try again".
    let phase: AskPhase<String>
    /// A send is in flight (this card's or anything else's): the options wait.
    var isBusy = false
    /// Receives the structured answer and its readable message.
    var submit: (ChoiceSelectionSubmission, String) -> Void = { _, _ in }
    /// KRI-802: in a reply that also asks for clips, the option tapped but not sent yet (it goes with the clips).
    var heldKey: String? = nil
    /// KRI-802: what happens next while the tapped option waits for the clips.
    var note: String? = nil
    /// KRI-802: undoes the held option so another can be picked.
    var changeHeld: (() -> Void)? = nil
    /// Hands the creator the composer to answer in their own words; nil hides the row.
    var typeMyOwn: (() -> Void)? = nil
    /// KRI-1560: puts an editable option's words in the composer for the creator to change; nil hides the pencils.
    var edit: ((String) -> Void)? = nil
    /// The creator is typing their own answer, so the blocks are disabled.
    var isTyping = false
    /// False for a reply that asks the clips too: Kria's decision then goes with the clips question.
    var allowsDecide = true
    /// KRI-1582: whose clips a version 2 preview is drawn over (the chat's project).
    var projectID: UUID? = nil

    var body: some View {
        if case .answered(let summary) = phase {
            Group {
                if summary.isEmpty {
                    KriaAnsweredReceipt(text: String(localized: "You answered"))
                } else if let option = question.previewedOption(answered: summary) {
                    DesignYouChose(option: option, projectID: projectID)
                } else {
                    KriaYouChose(text: summary)
                }
            }
            .accessibilityIdentifier("choice-card-answered")
        } else if question.hasPreviews && heldKey == nil {
            DesignPreviewStrip(question: question, projectID: projectID, isSending: isBusy, isLocked: isTyping,
                               typeMyOwn: question.allowFreeText ? typeMyOwn : nil) { option, message in
                submit(question.submission(for: option), message)
            }
            .padding(.top, 2)
            .accessibilityElement(children: .contain)
            .accessibilityIdentifier("choice-card")
        } else {
            VStack(alignment: .leading, spacing: 8) {
                KriaAnswerStack(
                    options: question.options.map {
                        KriaAnswerOption(id: $0.key, title: $0.label, detail: $0.detail, isRecommended: $0.recommended,
                                         identifier: "choice-option-\($0.key)", isEditable: $0.editable)
                    },
                    heldID: heldKey, isSending: isBusy, isLocked: isTyping,
                    typeMyOwn: question.allowFreeText ? typeMyOwn : nil,
                    edit: question.allowFreeText && heldKey == nil ? edit.map { edit in { edit($0.title) } } : nil,
                    decide: decideOption.map { option in { submit(question.submission(for: option), "Let Kria decide") } },
                    retriesFailedSend: true
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
