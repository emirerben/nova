import SwiftUI

/// Font chooser for the editor. System menus ignore custom fonts, so this opens
/// a sheet whose rows are each drawn in their own typeface (KRI-171).
/// `selection == nil` means "Default" (captions only, `includeDefault`).
struct NativeFontPicker: View {
    let selection: String?
    var includeDefault = false
    var defaultLabel = "Default · TikTok Sans"
    var accessibilityLabelText = "Font"
    let accessibilityID: String
    let onSelect: (String?) -> Void

    @State private var presented = false
    @State private var query = ""

    private var catalog: NativeFontCatalog { .shared }
    private var triggerLabel: String { selection ?? (includeDefault ? defaultLabel : "Inter") }

    var body: some View {
        Button { presented = true } label: {
            HStack {
                Text(triggerLabel).font(catalog.previewFont(selection, size: 16)).lineLimit(1)
                Spacer(minLength: 2)
                Image(systemName: "chevron.down").font(.system(size: 10))
            }
            .foregroundStyle(KriaColor.ink)
            .padding(.horizontal, 12)
            .frame(maxWidth: .infinity, minHeight: 44)
            .background(KriaColor.softZinc, in: RoundedRectangle(cornerRadius: 10))
        }
        .buttonStyle(.plain)
        .accessibilityLabel(accessibilityLabelText)
        .accessibilityValue(triggerLabel)
        .accessibilityIdentifier(accessibilityID)
        .sheet(isPresented: $presented) { sheet }
    }

    /// The current font stays listed even when it isn't a picker font (e.g. the
    /// deprecated "Inter Regular" that new text layers start with).
    private var options: [String] {
        var fonts = catalog.pickerFonts
        if let selection, !fonts.contains(selection) { fonts.insert(selection, at: 0) }
        let needle = query.trimmingCharacters(in: .whitespaces)
        return needle.isEmpty ? fonts : fonts.filter { $0.localizedCaseInsensitiveContains(needle) }
    }

    private var sheet: some View {
        NavigationStack {
            ScrollView {
                LazyVStack(spacing: 8) {
                    if includeDefault && query.isEmpty {
                        row(title: defaultLabel, font: nil, isSelected: selection == nil, id: "default")
                    }
                    ForEach(options, id: \.self) { name in
                        row(title: name, font: name, isSelected: selection == name, id: Self.slug(name))
                    }
                }
                .padding(16)
            }
            .navigationTitle(accessibilityLabelText)
            .navigationBarTitleDisplayMode(.inline)
            .searchable(text: $query, placement: .navigationBarDrawer(displayMode: .always), prompt: "Search fonts")
            .toolbar {
                ToolbarItem(placement: .confirmationAction) { Button("Done") { presented = false } }
            }
        }
        .presentationDetents([.medium, .large])
    }

    private func row(title: String, font: String?, isSelected: Bool, id: String) -> some View {
        Button {
            onSelect(font)
            presented = false
        } label: {
            HStack {
                Text(title).font(catalog.previewFont(font, size: 20)).lineLimit(1)
                Spacer()
                if isSelected { Image(systemName: "checkmark.circle.fill") }
            }
            .foregroundStyle(KriaColor.ink)
            .padding(.horizontal, 15)
            .frame(maxWidth: .infinity, minHeight: 50, alignment: .leading)
            .background(isSelected ? KriaColor.sage : KriaColor.softZinc)
            .clipShape(RoundedRectangle(cornerRadius: 13, style: .continuous))
        }
        .buttonStyle(.plain)
        .accessibilityAddTraits(isSelected ? .isSelected : [])
        .accessibilityIdentifier("native-editor-font-option-\(id)")
    }

    static func slug(_ name: String) -> String {
        name.lowercased().split { !$0.isLetter && !$0.isNumber }.joined(separator: "-")
    }
}
