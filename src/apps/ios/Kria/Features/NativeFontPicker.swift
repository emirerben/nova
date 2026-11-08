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
    @State private var tab: Tab? = .all

    private var catalog: NativeFontCatalog { .shared }
    private var triggerLabel: String { selection ?? (includeDefault ? defaultLabel : "Inter") }

    var body: some View {
        Button {
            tab = .all
            query = ""
            presented = true
        } label: {
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

    /// One sideways tab: everything, or one type group (KRI-1312).
    private enum Tab: Hashable {
        case all
        case group(NativeFontGroup)

        var label: String {
            switch self {
            case .all: "All"
            case .group(let group): group.label
            }
        }

        var slug: String {
            switch self {
            case .all: "all"
            case .group(let group): group.rawValue
            }
        }
    }

    private var tabs: [Tab] { [.all] + catalog.populatedGroups.map(Tab.group) }
    private var isSearching: Bool { !query.trimmingCharacters(in: .whitespaces).isEmpty }

    /// The current font stays listed even when it isn't a picker font (e.g. the
    /// deprecated "Inter Regular" that new text layers start with).
    private var allFonts: [String] {
        var fonts = catalog.pickerFonts
        if let selection, !fonts.contains(selection) { fonts.insert(selection, at: 0) }
        return fonts
    }

    private func fonts(in tab: Tab) -> [String] {
        switch tab {
        case .all: allFonts
        case .group(let group): catalog.fonts(in: group)
        }
    }

    private var searchResults: [String] {
        let needle = query.trimmingCharacters(in: .whitespaces)
        return allFonts.filter { $0.localizedCaseInsensitiveContains(needle) }
    }

    private var sheet: some View {
        NavigationStack {
            Group {
                if isSearching {
                    page(fonts: searchResults, tab: .all, includeDefault: false)
                } else if tabs.count > 1 {
                    VStack(spacing: 0) {
                        tabStrip
                        pager
                    }
                } else {
                    page(fonts: allFonts, tab: .all, includeDefault: includeDefault)
                }
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

    /// Chips that scroll sideways; the selected one follows a swipe of the pages below.
    private var tabStrip: some View {
        ScrollViewReader { proxy in
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 8) {
                    ForEach(tabs, id: \.self) { item in
                        let isSelected = tab == item
                        Button {
                            withAnimation(.easeInOut(duration: 0.25)) { tab = item }
                        } label: {
                            Text(item.label)
                                .font(KriaFont.body(14))
                                .foregroundStyle(KriaColor.ink)
                                .padding(.horizontal, 16)
                                .frame(minHeight: 36)
                                .background(isSelected ? KriaColor.sage : KriaColor.softZinc, in: Capsule())
                        }
                        .buttonStyle(.plain)
                        .accessibilityAddTraits(isSelected ? .isSelected : [])
                        .accessibilityIdentifier("native-editor-font-tab-\(item.slug)")
                        .id(item)
                    }
                }
                .padding(.horizontal, 16)
                .padding(.vertical, 8)
            }
            .onChange(of: tab) { _, selected in
                guard let selected else { return }
                withAnimation(.easeInOut(duration: 0.25)) { proxy.scrollTo(selected, anchor: .center) }
            }
        }
        .accessibilityIdentifier("native-editor-font-tabs")
    }

    /// One full-width page per tab; swiping sideways moves between them.
    private var pager: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            LazyHStack(spacing: 0) {
                ForEach(tabs, id: \.self) { item in
                    page(fonts: fonts(in: item), tab: item, includeDefault: includeDefault && item == .all)
                        .containerRelativeFrame(.horizontal)
                        .id(item)
                }
            }
            .scrollTargetLayout()
        }
        .scrollTargetBehavior(.paging)
        .scrollPosition(id: $tab)
    }

    private func page(fonts: [String], tab: Tab, includeDefault: Bool) -> some View {
        ScrollView {
            LazyVStack(spacing: 8) {
                if includeDefault {
                    row(title: defaultLabel, font: nil, isSelected: selection == nil, id: "default")
                }
                ForEach(fonts, id: \.self) { name in
                    // Only the All page keeps the bare id; a font sits on two pages, so tab pages are prefixed.
                    let slug = Self.slug(name)
                    row(title: name, font: name, isSelected: selection == name, id: tab == .all ? slug : "\(tab.slug)-\(slug)")
                }
                if fonts.isEmpty && !includeDefault {
                    Text("No fonts match")
                        .font(KriaFont.body(15))
                        .foregroundStyle(KriaColor.zinc)
                        .padding(.top, 24)
                }
            }
            .padding(16)
        }
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
