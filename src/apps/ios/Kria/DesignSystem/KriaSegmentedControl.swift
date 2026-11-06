import SwiftUI

/// A short row of choices in a fill track. The selected segment is ink with a paper label, like every other
/// selected state; the rest are mutedInk on the track. Give the container an `accessibilityIdentifier`; each
/// segment is a button named by its label.
///
/// `fillsWidth` makes the segments equal and stretches the track (a full-width picker such as video shape).
/// Otherwise the track hugs its labels, for a control that sits beside a setting's name.
struct KriaSegmentedControl<Value: Hashable>: View {
    let options: [(label: String, value: Value)]
    @Binding var selection: Value
    var fillsWidth = false

    var body: some View {
        HStack(spacing: 0) {
            ForEach(options.indices, id: \.self) { index in
                let selected = options[index].value == selection
                Button { selection = options[index].value } label: {
                    Text(options[index].label)
                        .kriaType(.button)
                        .foregroundStyle(selected ? KriaColor.paper : KriaColor.mutedInk)
                        .lineLimit(1).fixedSize()
                        .padding(.horizontal, 14)
                        .frame(maxWidth: fillsWidth ? .infinity : nil, minHeight: 44)
                        .frame(minWidth: 56)
                        .background(selected ? KriaColor.ink : Color.clear, in: Capsule())
                        .contentShape(Capsule())
                }
                .buttonStyle(.plain)
                .accessibilityAddTraits(selected ? .isSelected : [])
            }
        }
        .padding(4)
        .frame(maxWidth: fillsWidth ? .infinity : nil)
        .background(KriaColor.fill, in: Capsule())
        // Three 44pt segments must fit one row at every text size.
        .dynamicTypeSize(...DynamicTypeSize.xxxLarge)
        .accessibilityElement(children: .contain)
    }
}
