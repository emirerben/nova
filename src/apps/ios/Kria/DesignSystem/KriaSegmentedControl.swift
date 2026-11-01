import SwiftUI

/// A short row of capsule choices. The selected one is butter and the rest are fill, like the role chips, so
/// a setting with two or three states reads as the same control family as the rest of the app. Give the
/// container an `accessibilityIdentifier`; each segment is a button named by its label.
struct KriaSegmentedControl<Value: Hashable>: View {
    let options: [(label: String, value: Value)]
    @Binding var selection: Value

    var body: some View {
        HStack(spacing: 6) {
            ForEach(options.indices, id: \.self) { index in
                let selected = options[index].value == selection
                Button { selection = options[index].value } label: {
                    Text(options[index].label)
                        .font(KriaFont.body(14).weight(.semibold))
                        .foregroundStyle(KriaColor.ink)
                        .lineLimit(1).fixedSize()
                        .padding(.horizontal, 14)
                        .frame(minWidth: 56, minHeight: 44)
                        .background(selected ? KriaColor.butter : KriaColor.fill, in: Capsule())
                        .contentShape(Capsule())
                }
                .buttonStyle(.plain)
                .accessibilityAddTraits(selected ? .isSelected : [])
            }
        }
        // Three 44pt segments must fit one row at every text size.
        .dynamicTypeSize(...DynamicTypeSize.xxxLarge)
        .accessibilityElement(children: .contain)
    }
}
