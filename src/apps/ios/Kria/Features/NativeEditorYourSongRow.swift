import SwiftUI

/// The creator's own song in the Sounds tab (KRI-374). Read-only: the song is
/// a project asset the server pinned to this edit, not a catalog track, so
/// there is no track ID, volume, offset or remove control to offer.
struct NativeEditorYourSongRow: View {
    let song: NativeEditorYourSong

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 12) {
                Image(systemName: "music.note").frame(width: 44, height: 44)
                    .background(KriaColor.sage, in: RoundedRectangle(cornerRadius: 12))
                    .accessibilityHidden(true)
                VStack(alignment: .leading, spacing: 4) {
                    Text(song.title).font(KriaFont.body(15).weight(.semibold))
                    Text(song.window).font(KriaFont.body(13)).foregroundStyle(KriaColor.zinc)
                    if let mode = song.mode {
                        Text(mode).font(KriaFont.body(13)).foregroundStyle(KriaColor.zinc)
                    }
                }
                Spacer(minLength: 0)
            }
            .frame(minHeight: 44)
            .accessibilityElement(children: .ignore)
            .accessibilityLabel(song.accessibilitySummary)
            .accessibilityIdentifier("native-editor-your-song")
            Label(NativeEditorYourSong.helperCopy, systemImage: "speaker.slash")
                .font(KriaFont.body(13)).foregroundStyle(KriaColor.zinc)
                .fixedSize(horizontal: false, vertical: true)
                .accessibilityIdentifier("native-editor-your-song-note")
        }
    }
}
