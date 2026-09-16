import SwiftUI
import UIKit

/// A thin bar that expands on tap, matching the web `SongReferenceNotice`.
/// Collapsed it is one 44 pt row, so it never crowds the editor's timeline
/// and tools.
struct NativeSongReferenceCard: View {
    let presentation: NativeEditorSongReferencePresentation
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var expanded = false
    @State private var copied = false

    init(reference: NativeSongReference) { presentation = .reference(reference) }
    init(presentation: NativeEditorSongReferencePresentation) { self.presentation = presentation }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Button { expanded.toggle() } label: {
                HStack(spacing: 8) {
                    summary
                        .lineLimit(1)
                        .frame(maxWidth: .infinity, alignment: .leading)
                    Image(systemName: "chevron.down")
                        .font(.system(size: 12, weight: .semibold))
                        .foregroundStyle(KriaColor.zinc)
                        .rotationEffect(.degrees(expanded ? 180 : 0))
                        .animation(reduceMotion ? nil : .easeOut(duration: 0.15), value: expanded)
                        .accessibilityHidden(true)
                }
                .padding(.horizontal, 12)
                .frame(maxWidth: .infinity, minHeight: 44)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityValue(expanded ? "Expanded" : "Collapsed")
            .accessibilityHint(expanded ? "Hides the song details" : "Shows the song details")
            .accessibilityIdentifier("native-song-reference-toggle")

            if expanded {
                details
                    .padding(.horizontal, 12)
                    .padding(.vertical, 8)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .overlay(alignment: .top) { Rectangle().fill(KriaColor.line).frame(height: 1) }
            }
        }
        .font(KriaFont.body(13))
        .foregroundStyle(KriaColor.ink)
        .background(KriaColor.paper, in: RoundedRectangle(cornerRadius: 12))
        .overlay(RoundedRectangle(cornerRadius: 12).stroke(KriaColor.line, lineWidth: 1))
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("native-song-reference")
    }

    private var summary: Text {
        switch presentation {
        case let .reference(reference):
            Text("\(Text("Add the song when posting").fontWeight(.semibold))\(Text(" · " + reference.songLine).foregroundStyle(KriaColor.zinc))")
        case .saveToUpdateTiming:
            Text("Save to update song timing").fontWeight(.semibold)
        }
    }

    @ViewBuilder private var details: some View {
        switch presentation {
        case let .reference(reference):
            VStack(alignment: .leading, spacing: 4) {
                Text(reference.songLine).fontWeight(.semibold).fixedSize(horizontal: false, vertical: true)
                Text("Use \(reference.timeRange) in TikTok or Instagram.").monospacedDigit()
                Text("Song audio is not included in this video.")
                    .font(KriaFont.body(12)).foregroundStyle(KriaColor.zinc)
                Button(copied ? "Song details copied" : "Copy song details") {
                    UIPasteboard.general.string = reference.copyText
                    copied = true
                }
                .font(KriaFont.body(13).weight(.semibold)).frame(minHeight: 44)
                .accessibilityIdentifier("native-song-reference-copy")
            }
        case .saveToUpdateTiming:
            Text("The edited video is longer than this saved song section. Save before copying song details.")
                .foregroundStyle(KriaColor.zinc).fixedSize(horizontal: false, vertical: true)
        }
    }
}
