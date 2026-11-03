import SwiftUI

extension CreationFormat {
    /// The SF Symbol on a video-type row.
    var rowSymbol: String {
        switch self {
        case .talkingToCamera: "video.fill"
        case .montage: "square.grid.2x2.fill"
        case .narrated: "mic.fill"
        case .lipsync: "music.mic"
        case .slides: "photo.stack"
        }
    }

    /// One line under the title when the creator is choosing between types.
    var rowDetail: String {
        switch self {
        case .talkingToCamera: "One clear take, your voice at the center"
        case .montage: "Many clips cut together to a beat"
        case .narrated: "Your voice over footage you already have"
        case .lipsync: "Film yourself to a song"
        case .slides: "Photos and videos as slides"
        }
    }
}

/// The video type the chat is working on, first in the transcript so the timeline reads in order (Paper S3.02).
struct VideoTypeRow: View {
    let format: CreationFormat
    let change: () -> Void

    var body: some View {
        HStack(spacing: 12) {
            Image(systemName: format.rowSymbol).font(.system(size: 17)).foregroundStyle(KriaColor.ink)
                .frame(width: 40, height: 40).background(KriaColor.paper, in: Circle())
            VStack(alignment: .leading, spacing: 2) {
                Text(format.title).kriaType(.button).foregroundStyle(KriaColor.ink)
                Text("Your video type").kriaType(.caption).foregroundStyle(KriaColor.mutedInk)
            }
            Spacer(minLength: 8)
            Button("Change", action: change)
                .buttonStyle(KriaButtonStyle(role: .secondary, size: .medium, onFill: true))
                .accessibilityLabel("Change video type").accessibilityIdentifier("change-video-type")
        }
        .padding(.leading, 12).padding(.trailing, 8).padding(.vertical, 8)
        .background(KriaColor.fill, in: RoundedRectangle(cornerRadius: 20, style: .continuous))
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("video-type-row")
    }
}

/// Picking another video type after one is chosen (Paper S3.02b): a plain list with the current one in ink.
struct FormatReselectStage: View {
    let formats: [CreationFormat]
    let selected: CreationFormat
    let isBusy: Bool
    let select: (CreationFormat) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Choose your video type").kriaType(.heading).foregroundStyle(KriaColor.ink).accessibilityAddTraits(.isHeader)
            Text("You can switch any time. Clips you already added stay.").kriaType(.body).foregroundStyle(KriaColor.mutedInk)
            ForEach(formats) { format in
                let isSelected = format == selected
                Button { select(format) } label: {
                    HStack(spacing: 12) {
                        Image(systemName: format.rowSymbol).font(.system(size: 17)).foregroundStyle(KriaColor.ink)
                            .frame(width: 40, height: 40).background(KriaColor.paper, in: Circle())
                        VStack(alignment: .leading, spacing: 2) {
                            Text(format.title).kriaType(.button).foregroundStyle(isSelected ? KriaColor.paper : KriaColor.ink)
                            Text(format.rowDetail).kriaType(.label).foregroundStyle(isSelected ? KriaColor.fill : KriaColor.mutedInk)
                                .multilineTextAlignment(.leading)
                        }
                        Spacer(minLength: 0)
                        if isSelected {
                            Image(systemName: "checkmark").font(.system(size: 12, weight: .bold)).foregroundStyle(KriaColor.ink)
                                .frame(width: 28, height: 28).background(KriaColor.paper, in: Circle())
                        }
                    }
                    .padding(12).frame(maxWidth: .infinity, minHeight: 68, alignment: .leading)
                    .background(isSelected ? KriaColor.ink : KriaColor.fill, in: RoundedRectangle(cornerRadius: 20, style: .continuous))
                    .contentShape(RoundedRectangle(cornerRadius: 20, style: .continuous))
                }
                .buttonStyle(.plain).disabled(isBusy)
                .accessibilityAddTraits(isSelected ? .isSelected : [])
                .accessibilityIdentifier("reselect-format-\(format.rawValue)")
            }
        }
    }
}

/// What the composer's + offers. A nil closure hides its row; `disabledReason` dims everything and says why.
struct ChatAttachMenu {
    var disabledReason: String?
    var offersSaved = false
    var record: (() -> Void)?
    var upload: (FootageSourceKind) -> Void
    var addScript: (() -> Void)?
    var addVoiceover: (() -> Void)?
    /// Only where the server offers them for this format.
    var addSong: (() -> Void)?
    var addOverlays: (() -> Void)?
}

/// The floating tray over the composer (Paper S3.05): Record, Upload, then optional script and voiceover. Upload
/// swaps the list for "Upload from" so Photos, Saved uploads and Files only appear once Upload is chosen.
struct ChatAttachTray: View {
    let menu: ChatAttachMenu
    let close: () -> Void
    @State private var choosingSource = false
    @Environment(\.accessibilityReduceTransparency) private var reduceTransparency

    private var enabled: Bool { menu.disabledReason == nil }

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            if choosingSource {
                HStack(spacing: 12) {
                    Button { choosingSource = false } label: { Image(systemName: "chevron.left").font(.system(size: 15, weight: .semibold)) }
                        .buttonStyle(.kriaIcon).accessibilityLabel("Back").accessibilityIdentifier("attach-tray-back")
                    Text("Upload from").kriaType(.button).foregroundStyle(KriaColor.ink).accessibilityAddTraits(.isHeader)
                    Spacer()
                }
                UploadSourceRows(noun: "videos", offersSaved: menu.offersSaved) { kind in close(); menu.upload(kind) }
            } else {
                if let record = menu.record {
                    row("video.fill", "Record", "Film now", id: "attach-record") { close(); record() }
                }
                row("photo.on.rectangle", "Upload", "Use existing video", id: "attach-upload") { choosingSource = true }
                if let addScript = menu.addScript {
                    row("text.alignleft", "Add script", "Shows while you record", id: "attach-script") { close(); addScript() }
                }
                if let addVoiceover = menu.addVoiceover {
                    row("mic.fill", "Add voiceover", "Record, upload or AI voice", id: "attach-voiceover") { close(); addVoiceover() }
                }
                if let addSong = menu.addSong {
                    row("music.note", "Add song", "Cut to a song or lip-sync", id: "attach-song") { close(); addSong() }
                }
                if let addOverlays = menu.addOverlays {
                    row("square.on.square", "Add overlays", "Photos and clips for context", id: "attach-overlays") { close(); addOverlays() }
                }
                if let reason = menu.disabledReason {
                    Text(reason).kriaType(.caption).foregroundStyle(KriaColor.mutedInk)
                        .frame(maxWidth: .infinity).padding(.top, 4).accessibilityIdentifier("attach-tray-reason")
                }
            }
        }
        .padding(8)
        .frame(maxWidth: 340, alignment: .leading)
        .background {
            let shape = RoundedRectangle(cornerRadius: 28, style: .continuous)
            ZStack {
                if !KriaTransparency.isReduced(reduceTransparency) { shape.fill(.regularMaterial) }
                shape.fill(KriaColor.paper.opacity(KriaTransparency.isReduced(reduceTransparency) ? 1 : 0.86))
            }
            .shadow(color: KriaColor.ink.opacity(0.14), radius: 16, y: 4)
        }
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("attach-tray")
    }

    private func row(_ symbol: String, _ title: String, _ detail: String, id: String, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            HStack(spacing: 12) {
                Image(systemName: symbol).font(.system(size: 18)).foregroundStyle(KriaColor.ink)
                    .frame(width: 40, height: 40).background(KriaColor.fill, in: Circle())
                VStack(alignment: .leading, spacing: 2) {
                    Text(title).kriaType(.button).foregroundStyle(KriaColor.ink)
                    Text(detail).kriaType(.caption).foregroundStyle(KriaColor.mutedInk)
                }
                Spacer(minLength: 0)
            }
            .padding(.horizontal, 4).frame(minHeight: 56).contentShape(Rectangle())
        }
        .buttonStyle(.plain).disabled(!enabled).opacity(enabled ? 1 : 0.4)
        .accessibilityLabel("\(title), \(detail)").accessibilityIdentifier(id)
    }
}

/// The video has no audible speech (Paper S3.15): say so once, then offer both ways forward side by side.
struct NoSpeechRecoveryCard: View {
    var recordAgain: (() -> Void)?
    var chooseAnother: (() -> Void)?

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            KriaNoticeCard(tone: .failure, title: "We couldn’t hear any speech in this video",
                           message: "Record it again or choose a different video.")
                .accessibilityIdentifier("speech-cleanup-no-speech")
            if recordAgain != nil || chooseAnother != nil {
                KriaSourceRow(options: [
                    recordAgain.map { action in
                        KriaSourceOption(id: "record-again", symbol: KriaSourceOption.recordSymbol, title: "Record again", identifier: "no-speech-record", action: action)
                    },
                    chooseAnother.map { action in
                        KriaSourceOption(id: "upload-another", symbol: KriaSourceOption.uploadSymbol, title: "Upload another", identifier: "no-speech-upload", action: action)
                    }
                ].compactMap { $0 }, layout: .compact)
            }
        }
    }
}
