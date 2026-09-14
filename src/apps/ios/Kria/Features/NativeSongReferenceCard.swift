import SwiftUI
import UIKit

struct NativeSongReferenceCard: View {
    let presentation: NativeEditorSongReferencePresentation
    @State private var copied = false

    init(reference: NativeSongReference) { presentation = .reference(reference) }
    init(presentation: NativeEditorSongReferencePresentation) { self.presentation = presentation }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Label("Add this song when you post", systemImage: "music.note")
                .font(KriaFont.body(13).weight(.semibold))
            switch presentation {
            case let .reference(reference):
                Text(reference.title).font(KriaFont.body(15).weight(.semibold))
                if let artist = reference.artist { Text(artist).font(KriaFont.body(13)).foregroundStyle(KriaColor.zinc) }
                Text(reference.timeRange).font(.system(.body, design: .monospaced).weight(.medium))
                Text("Add this song in TikTok or Instagram. This video exports without the song.")
                    .font(KriaFont.body(13)).foregroundStyle(KriaColor.zinc).fixedSize(horizontal: false, vertical: true)
                Button(copied ? "Song details copied" : "Copy song details") {
                    UIPasteboard.general.string = reference.copyText
                    copied = true
                }
                .font(KriaFont.body(13).weight(.semibold)).frame(minHeight: 44)
                .accessibilityIdentifier("native-song-reference-copy")
            case .saveToUpdateTiming:
                Text("Save to update song timing")
                    .font(KriaFont.body(15).weight(.semibold))
                Text("The edited video is longer than this saved song section. Save before copying song details.")
                    .font(KriaFont.body(13)).foregroundStyle(KriaColor.zinc).fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(14).frame(maxWidth: .infinity, alignment: .leading)
        .background(KriaColor.selectionSoft, in: RoundedRectangle(cornerRadius: 12))
        .overlay(RoundedRectangle(cornerRadius: 12).stroke(KriaColor.border, lineWidth: 1))
        .accessibilityIdentifier("native-song-reference")
    }
}
