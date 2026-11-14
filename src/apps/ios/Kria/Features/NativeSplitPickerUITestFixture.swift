#if DEBUG
import SwiftUI

/// `-ui-testing-editor-split-picker`: the Split picker with a creator who has a media library. The simulator has no
/// account and no photos, so the fixture answers the library check after a delay, draws a grid of solid tiles, and
/// makes a pick take a few seconds to read. That is slow enough to watch the layout while the answer is pending and
/// while a tile is mid-add.
enum NativeSplitPickerFixture {
    static var isActive: Bool { ProcessInfo.processInfo.arguments.contains("-ui-testing-editor-split-picker") }
    /// `KRIA_SPLIT_PICKER_LIBRARY_DELAY_S` sets how long the library check stays unanswered.
    static var libraryAnswerDelay: Duration {
        .seconds(Double(ProcessInfo.processInfo.environment["KRIA_SPLIT_PICKER_LIBRARY_DELAY_S"] ?? "") ?? 3)
    }
    static let readDelay: Duration = .seconds(3)
    static let tileCount = 8

    static func tileID(_ index: Int) -> String { "fixture-tile-\(index)" }

    static func read() async throws -> URL {
        try await Task.sleep(for: readDelay)
        guard let url = Bundle.main.url(forResource: "istanbul", withExtension: "mp4") else { throw AddClipSourceUnreadable() }
        return url
    }
}

struct NativeSplitPickerFixtureGrid: View {
    let disabled: Bool
    let adding: String?
    let onTap: (String) -> Void

    var body: some View {
        LazyVGrid(columns: Array(repeating: GridItem(.flexible(), spacing: 4), count: 4), spacing: 4) {
            ForEach(0..<NativeSplitPickerFixture.tileCount, id: \.self) { index in
                let id = NativeSplitPickerFixture.tileID(index)
                Button { onTap(id) } label: {
                    Color.clear
                        .aspectRatio(1, contentMode: .fit)
                        .overlay { KriaColor.fill }
                        .clipped()
                }
                .buttonStyle(.plain)
                .disabled(disabled)
                .accessibilityLabel("Photo")
                .accessibilityIdentifier("native-editor-split-gallery-tile-\(index)")
                .nativeSplitAdding(adding == id, identifier: "native-editor-split-adding-\(index)")
                .clipShape(RoundedRectangle(cornerRadius: 10))
            }
        }
    }
}
#endif
