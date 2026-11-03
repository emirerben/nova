import SwiftUI

/// A text field (Paper S4.05): fill, radius 16, `KriaType.body`. Focus turns the fill to selectionSoft with a
/// sky caret; an error turns it failureSoft and shows its message in failureText. Text grows with its content.
///
/// Single-line fields are 48pt and submit on return. Multi-line fields start at `minHeight` and grow.
struct KriaField: View {
    let label: String
    @Binding var text: String
    var placeholder: String?
    /// A helper line under the field, e.g. "Helps Kria know where the image fits."
    var helper: String?
    var errorMessage: String?
    /// Shows "68 / 1500" under the trailing edge.
    var maxCount: Int?
    var isMultiline = false
    var minHeight: CGFloat = 132
    var submitLabel: SubmitLabel = .done
    var onSubmit: () -> Void = {}

    @Environment(\.isEnabled) private var isEnabled
    @FocusState private var focused: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            field
                .kriaType(.body)
                .foregroundStyle(KriaColor.ink)
                .tint(KriaColor.sky)
                .focused($focused)
                .padding(.horizontal, 16).padding(.vertical, isMultiline ? 16 : 12)
                .frame(maxWidth: .infinity, minHeight: isMultiline ? minHeight : 48, alignment: isMultiline ? .topLeading : .leading)
                .background(background, in: RoundedRectangle(cornerRadius: 16, style: .continuous))
                .contentShape(RoundedRectangle(cornerRadius: 16, style: .continuous))
                .onTapGesture { focused = true }
                .opacity(isEnabled ? 1 : 0.5)
            if errorMessage != nil || helper != nil || maxCount != nil {
                HStack(alignment: .top, spacing: 8) {
                    if let errorMessage {
                        Text(errorMessage).foregroundStyle(KriaColor.failureText)
                            .accessibilityLabel("Error: \(errorMessage)")
                    } else if let helper {
                        Text(helper).foregroundStyle(KriaColor.mutedInk)
                    }
                    Spacer(minLength: 0)
                    if let maxCount {
                        Text("\(text.count) / \(maxCount)").foregroundStyle(KriaColor.mutedInk).monospacedDigit()
                    }
                }
                .kriaType(.caption)
            }
        }
    }

    @ViewBuilder private var field: some View {
        let prompt = Text(placeholder ?? label).foregroundStyle(KriaColor.mutedInk)
        if isMultiline {
            TextField(label, text: $text, prompt: prompt, axis: .vertical)
                .lineLimit(3...)
        } else {
            TextField(label, text: $text, prompt: prompt)
                .submitLabel(submitLabel)
                .onSubmit(onSubmit)
        }
    }

    private var background: Color {
        if errorMessage != nil { return KriaColor.failureSoft }
        return focused ? KriaColor.selectionSoft : KriaColor.fill
    }
}
